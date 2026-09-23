"""Live market-data ingestion: one process per venue.

    python -m uxtrader.data.ingest --venue binanceusdm --symbols BTC/USDT:USDT \\
        --bus nats://nats:4222 --health-port 8080

WebSocket-first via ccxt.pro, normalised into ``uxtrader.types`` models and published
on the bus. Three responsibilities beyond "read the socket":

* **Closed bars only.** A candle is published once, when a newer candle proves it is
  closed. Publishing the forming candle is look-ahead with extra steps.
* **Staleness.** Every feed is registered with ``uxcore.ws.FeedMonitor``; a feed
  silent for longer than ``stale_after`` publishes ``feed.stale``, which makes the risk
  engine block entries on that symbol and the OMS cancel resting orders.
* **Liveness for the orchestrator.** ``/healthz`` returns 503 when any feed is stale,
  so Kubernetes/compose restart a wedged process. This is the single highest-value
  piece of automation in the data layer.

On order-book continuity: ccxt.pro already validates the venue's update-id chain
and re-snapshots on a gap for the venues that publish one (Binance's ``U``/``u``). This
service therefore checks staleness, not sequence, at its level; ``FeedMonitor`` sequence
checks are used by plugins that consume raw venue streams directly.
"""
from __future__ import annotations

import argparse
import asyncio
import logging
from datetime import datetime, timezone
from decimal import Decimal
from typing import Any

from uxcore.ws import FeedMonitor

from ..bus import Bus
from ..events import FEED_STALE, StaleFeed, md_subject
from ..types import Bar, BookLevel, BookSnapshot, Funding, TradeTick

log = logging.getLogger(__name__)


def _ts(ms: int | float | None) -> datetime:
    if ms is None:
        return datetime.now(timezone.utc)
    return datetime.fromtimestamp(ms / 1000, tz=timezone.utc)


def _d(x: Any) -> Decimal:
    return Decimal(str(x)) if x is not None else Decimal('0')


class IngestService:
    def __init__(self, exchange: Any, bus: Bus, symbols: list[str], *,
                 channels: tuple[str, ...] = ('trades', 'book', 'bars', 'funding'),
                 timeframe: str = '1m', book_depth: int = 20,
                 stale_after: float = 5.0, funding_poll_s: float = 60.0,
                 max_backoff_s: float = 30.0) -> None:
        self.ex = exchange
        self.bus = bus
        self.symbols = symbols
        self.channels = channels
        self.timeframe = timeframe
        self.book_depth = book_depth
        self.funding_poll_s = funding_poll_s
        self.max_backoff_s = max_backoff_s
        self.venue = getattr(exchange, 'id', 'unknown')
        self.monitor = FeedMonitor(soft_stale=stale_after, on_stale=self._on_stale)
        self._last_bar_ts: dict[str, int] = {}
        self._last_funding_ts: dict[str, int] = {}
        self._stale_published: set[str] = set()
        self._tasks: list[asyncio.Task[None]] = []
        self.errors = 0

    # -- lifecycle -------------------------------------------------------------

    async def start(self) -> None:
        await self.monitor.start(interval=1.0)
        for symbol in self.symbols:
            for channel in self.channels:
                loop = getattr(self, f'_{channel}')
                self._tasks.append(asyncio.create_task(
                    self._supervise(channel, symbol, loop), name=f'{channel}:{symbol}'))

    async def stop(self) -> None:
        for t in self._tasks:
            t.cancel()
        await asyncio.gather(*self._tasks, return_exceptions=True)
        self._tasks.clear()
        await self.monitor.stop()

    def healthy(self) -> bool:
        return all(h['age'] < self.monitor.soft_stale  # type: ignore[operator]
                   for h in self.monitor.snapshot().values())

    # -- supervision -----------------------------------------------------------

    async def _supervise(self, channel: str, symbol: str, loop) -> None:
        """Run one feed forever. A crash in one feed never takes down the others."""
        key = self.ex.feed_key(symbol, channel) if hasattr(self.ex, 'feed_key') \
            else f'{self.venue}|{symbol}|{channel}'
        self.monitor.register(key)
        backoff = 0.5
        while True:
            try:
                await loop(symbol, key)
                backoff = 0.5
            except asyncio.CancelledError:
                raise
            except Exception as exc:                    # noqa: BLE001
                self.errors += 1
                log.warning('ingest_error venue=%s channel=%s symbol=%s err=%s backoff=%.1fs',
                            self.venue, channel, symbol, exc, backoff)
                await asyncio.sleep(backoff)
                backoff = min(backoff * 2, self.max_backoff_s)

    async def _seen(self, key: str) -> None:
        await self.monitor.observe(key)
        self._stale_published.discard(key)

    async def _on_stale(self, key: str, age: float) -> None:
        if key in self._stale_published:
            return
        self._stale_published.add(key)
        _, symbol, _ = key.split('|', 2)
        await self.bus.publish(FEED_STALE, StaleFeed(key=key, venue=self.venue,
                                                     symbol=symbol, age_s=age))

    # -- feeds -----------------------------------------------------------------

    async def _trades(self, symbol: str, key: str) -> None:
        trades = await self.ex.watch_trades(symbol)
        for t in trades:
            info = t.get('info') or {}
            await self.bus.publish(md_subject(self.venue, 'trade', symbol), TradeTick(
                venue=self.venue, symbol=symbol, ts=_ts(t.get('timestamp')),
                price=_d(t['price']), amount=_d(t['amount']),
                side='buy' if t.get('side') == 'buy' else 'sell',
                is_liquidation=bool(info.get('liquidation') or info.get('isLiquidation'))))
        await self._seen(key)

    async def _book(self, symbol: str, key: str) -> None:
        ob = await self.ex.watch_order_book(symbol, self.book_depth)
        bids = tuple(BookLevel(price=_d(p), size=_d(s)) for p, s, *_ in ob['bids'][:self.book_depth])
        asks = tuple(BookLevel(price=_d(p), size=_d(s)) for p, s, *_ in ob['asks'][:self.book_depth])
        if bids and asks:
            await self.bus.publish(md_subject(self.venue, 'book', symbol), BookSnapshot(
                venue=self.venue, symbol=symbol, ts=_ts(ob.get('timestamp')),
                bids=bids, asks=asks, sequence=ob.get('nonce')))
        await self._seen(key)

    async def _bars(self, symbol: str, key: str) -> None:
        candles = await self.ex.watch_ohlcv(symbol, self.timeframe)
        await self._seen(key)
        if not candles:
            return
        newest = int(candles[-1][0])
        last = self._last_bar_ts.get(symbol, -1)
        # Every candle older than the newest one is closed. Publish each exactly once.
        for c in candles:
            ts = int(c[0])
            if last < ts < newest:
                await self.bus.publish(md_subject(self.venue, 'bar', symbol), Bar(
                    venue=self.venue, symbol=symbol, timeframe=self.timeframe, ts=_ts(ts),
                    open=_d(c[1]), high=_d(c[2]), low=_d(c[3]), close=_d(c[4]),
                    volume=_d(c[5]), closed=True))
                self._last_bar_ts[symbol] = ts

    async def _funding(self, symbol: str, key: str) -> None:
        """Polled: funding settles every 1–8 h, a socket is overkill. Publishes each
        realised print once, which is what S1's EWMA consumes."""
        hist = await self.ex.fetch_funding_rate_history(symbol, None, 3)
        await self._seen(key)
        for h in hist or []:
            ts = int(h['timestamp'])
            if ts > self._last_funding_ts.get(symbol, -1):
                self._last_funding_ts[symbol] = ts
                await self.bus.publish(md_subject(self.venue, 'funding', symbol), Funding(
                    venue=self.venue, symbol=symbol, ts=_ts(ts),
                    rate=_d(h['fundingRate'])))
        await asyncio.sleep(self.funding_poll_s)


async def serve_health(service: IngestService, port: int) -> asyncio.base_events.Server:
    """Minimal stdlib HTTP health endpoint — no web framework in the data path."""
    async def handle(reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        await reader.readline()
        ok = service.healthy()
        body = b'ok' if ok else b'stale'
        status = b'200 OK' if ok else b'503 Service Unavailable'
        writer.write(b'HTTP/1.1 ' + status + b'\r\nContent-Length: ' +
                     str(len(body)).encode() + b'\r\nConnection: close\r\n\r\n' + body)
        await writer.drain()
        writer.close()
    return await asyncio.start_server(handle, '0.0.0.0', port)


async def _main() -> int:
    parser = argparse.ArgumentParser(description='Live market-data ingestion for one venue.')
    parser.add_argument('--venue', required=True)
    parser.add_argument('--symbols', default='BTC/USDT:USDT,ETH/USDT:USDT')
    parser.add_argument('--timeframe', default='1m')
    parser.add_argument('--bus', default='nats://nats:4222')
    parser.add_argument('--health-port', type=int, default=8080)
    args = parser.parse_args()
    logging.basicConfig(level=logging.INFO)

    from uxcore import make_exchange

    from ..bus import connect
    ex = make_exchange(args.venue)
    await ex.load_markets()
    bus = await connect(args.bus)
    service = IngestService(ex, bus, args.symbols.split(','), timeframe=args.timeframe)
    await service.start()
    server = await serve_health(service, args.health_port)
    try:
        await asyncio.Event().wait()
    finally:
        server.close()
        await service.stop()
        await ex.close()
        await bus.close()
    return 0


if __name__ == '__main__':
    raise SystemExit(asyncio.run(_main()))

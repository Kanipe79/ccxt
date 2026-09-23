"""Ingestion against a scripted ccxt.pro-shaped exchange."""
from __future__ import annotations

import asyncio

import pytest

from uxtrader.bus import InMemoryBus
from uxtrader.data.ingest import IngestService, serve_health
from uxtrader.events import StaleFeed
from uxtrader.types import Bar, BookSnapshot, Funding, TradeTick

SYM = 'BTC/USDT:USDT'
T0 = 1_704_067_200_000
M1 = 60_000


class ScriptedExchange:
    """Each watch_* call returns the next scripted item, then blocks forever (a quiet
    socket). An Exception instance in the script is raised (a dropped socket)."""

    id = 'fakevenue'

    def __init__(self, script: dict[str, list]) -> None:
        self.script = {k: list(v) for k, v in script.items()}

    async def _next(self, channel: str):
        q = self.script.get(channel, [])
        if not q:
            await asyncio.Event().wait()
        item = q.pop(0)
        if isinstance(item, BaseException):
            raise item
        return item

    async def watch_trades(self, symbol):
        return await self._next('trades')

    async def watch_order_book(self, symbol, limit=None):
        return await self._next('book')

    async def watch_ohlcv(self, symbol, timeframe='1m'):
        return await self._next('bars')

    async def fetch_funding_rate_history(self, symbol, since=None, limit=None):
        return await self._next('funding')


def candle(i: int, close: float = 100.0) -> list:
    return [T0 + i * M1, close, close + 1, close - 1, close, 3.0]


async def run_for(service: IngestService, seconds: float = 0.15) -> None:
    await service.start()
    await asyncio.sleep(seconds)
    await service.stop()


async def test_publishes_normalised_market_data():
    ex = ScriptedExchange({
        'trades': [[{'timestamp': T0, 'price': 50000, 'amount': 0.5, 'side': 'sell',
                     'info': {}}]],
        'book': [{'timestamp': T0, 'nonce': 7, 'bids': [[49999, 2], [49998, 1]],
                  'asks': [[50001, 1], [50002, 3]]}],
        'funding': [[{'timestamp': T0, 'fundingRate': 0.0001}]],
    })
    bus = InMemoryBus()
    await run_for(IngestService(ex, bus, [SYM], channels=('trades', 'book', 'funding'),
                                funding_poll_s=10))
    trade = bus.of_type(TradeTick)[0]
    assert trade.side == 'sell' and str(trade.price) == '50000'
    book = bus.of_type(BookSnapshot)[0]
    assert book.sequence == 7 and book.spread_bps == pytest.approx(0.4, abs=0.01)
    assert bus.of_type(Funding)[0].apr == pytest.approx(0.1095)


async def test_only_closed_bars_published_exactly_once():
    ex = ScriptedExchange({'bars': [
        [candle(0), candle(1)],                 # bar 1 forming → only bar 0 closed
        [candle(0), candle(1), candle(2)],      # bar 1 now closed
        [candle(1), candle(2, 101)],            # bar 2 updating, still forming
    ]})
    bus = InMemoryBus()
    await run_for(IngestService(ex, bus, [SYM], channels=('bars',)))
    bars = bus.of_type(Bar)
    assert [b.ts.timestamp() * 1000 for b in bars] == [T0, T0 + M1]


async def test_socket_error_is_retried_not_fatal():
    ex = ScriptedExchange({'trades': [
        ConnectionResetError('socket dropped'),
        [{'timestamp': T0, 'price': 1, 'amount': 1, 'side': 'buy', 'info': {}}],
    ]})
    bus = InMemoryBus()
    service = IngestService(ex, bus, [SYM], channels=('trades',))
    await run_for(service, 0.8)
    assert service.errors == 1
    assert len(bus.of_type(TradeTick)) == 1


async def test_silent_feed_publishes_stale_once_and_fails_health():
    ex = ScriptedExchange({})                   # nothing ever arrives
    bus = InMemoryBus()
    service = IngestService(ex, bus, [SYM], channels=('trades',), stale_after=0.2)
    await service.start()
    server = await serve_health(service, 0)
    port = server.sockets[0].getsockname()[1]
    await asyncio.sleep(1.5)
    reader, writer = await asyncio.open_connection('127.0.0.1', port)
    writer.write(b'GET /healthz HTTP/1.1\r\n\r\n')
    status = (await reader.readline()).decode()
    writer.close()
    server.close()
    await service.stop()

    stale = bus.of_type(StaleFeed)
    assert len(stale) == 1 and stale[0].symbol == SYM
    assert '503' in status

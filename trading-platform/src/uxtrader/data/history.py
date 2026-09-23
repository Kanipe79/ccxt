"""Historical backfill: OHLCV and funding, paginated, gap-checked, point-in-time.

    python -m uxtrader.data.history --venue binanceusdm \\
        --symbols BTC/USDT:USDT,ETH/USDT:USDT --timeframe 4h \\
        --since 2021-01-01 --out ./data --funding

Runs through uxcore, so it inherits the weight-aware limiter and the retry policy —
a four-year backfill of 40 symbols is exactly the workload that gets an IP banned
when run through a naive loop.
"""
from __future__ import annotations

import argparse
import asyncio
import logging
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Any

import pandas as pd

from .store import FUNDING_COLUMNS, OHLCV_COLUMNS, ParquetStore

log = logging.getLogger(__name__)

TF_MS = {'1m': 60_000, '5m': 300_000, '15m': 900_000, '1h': 3_600_000,
         '4h': 14_400_000, '1d': 86_400_000}

# A backfilled bar was knowable at its close. Add a small ingestion delay so research
# never assumes you acted on a bar the instant it closed.
AVAILABILITY_DELAY = timedelta(seconds=2)


@dataclass
class Gap:
    start: datetime
    end: datetime
    missing_bars: int


def detect_gaps(ts_ms: list[int], tf_ms: int) -> list[Gap]:
    """Missing bars inside the fetched range. Venues have outages; a gap that is not
    reported becomes a silent jump in every indicator that spans it."""
    gaps: list[Gap] = []
    for a, b in zip(ts_ms, ts_ms[1:]):
        if b - a > tf_ms:
            gaps.append(Gap(start=_dt(a + tf_ms), end=_dt(b - tf_ms),
                            missing_bars=(b - a) // tf_ms - 1))
    return gaps


def _dt(ms: int) -> datetime:
    return datetime.fromtimestamp(ms / 1000, tz=timezone.utc)


class HistoryLoader:
    def __init__(self, exchange: Any, *, now_ms: int | None = None) -> None:
        self.ex = exchange
        self._now_ms = now_ms

    def now_ms(self) -> int:
        return self._now_ms if self._now_ms is not None else int(
            datetime.now(timezone.utc).timestamp() * 1000)

    async def ohlcv(self, symbol: str, timeframe: str, since_ms: int,
                    until_ms: int | None = None, page_limit: int = 1000) -> pd.DataFrame:
        tf = TF_MS[timeframe]
        until = min(until_ms or self.now_ms(), self.now_ms())
        rows: dict[int, list[float]] = {}
        cursor = since_ms
        fetch = getattr(self.ex, 'ux_fetch_ohlcv', None) or self.ex.fetch_ohlcv

        while cursor < until:
            page = await fetch(symbol, timeframe, cursor, page_limit)
            if not page:
                break
            for r in page:
                rows[int(r[0])] = r
            last = int(page[-1][0])
            if last < cursor:                  # venue ignored `since`; stop rather than loop
                log.warning('ohlcv_since_ignored symbol=%s cursor=%s', symbol, cursor)
                break
            cursor = last + tf
            if len(page) < page_limit and cursor >= until:
                break

        # Drop the still-forming bar: its values will change. Keeping it is a
        # look-ahead bug at the right edge of every dataset.
        closed = [rows[k] for k in sorted(rows) if k + tf <= until and k < until]
        df = pd.DataFrame(closed, columns=['ts', 'open', 'high', 'low', 'close', 'volume'])
        if df.empty:
            return pd.DataFrame(columns=OHLCV_COLUMNS)
        df['ts'] = pd.to_datetime(df['ts'], unit='ms', utc=True)
        df['available_ts'] = df['ts'] + pd.Timedelta(milliseconds=tf) + AVAILABILITY_DELAY
        return df[OHLCV_COLUMNS]

    async def funding(self, symbol: str, since_ms: int, until_ms: int | None = None,
                      page_limit: int = 1000) -> pd.DataFrame:
        until = until_ms or self.now_ms()
        rows: dict[int, dict[str, Any]] = {}
        cursor = since_ms
        while cursor < until:
            page = await self.ex.fetch_funding_rate_history(symbol, cursor, page_limit)
            if not page:
                break
            for r in page:
                rows[int(r['timestamp'])] = r
            last = int(page[-1]['timestamp'])
            if last < cursor:
                break
            cursor = last + 1
            if len(page) < page_limit:
                break
        recs = [rows[k] for k in sorted(rows) if k <= until]
        if not recs:
            return pd.DataFrame(columns=FUNDING_COLUMNS)
        ts = [r['timestamp'] for r in recs]
        intervals = [((b - a) / 3_600_000) for a, b in zip(ts, ts[1:])]
        intervals = [intervals[0] if intervals else 8.0] + intervals
        df = pd.DataFrame({
            'ts': pd.to_datetime(ts, unit='ms', utc=True),
            'rate': [float(r['fundingRate']) for r in recs],
            'mark_price': [float((r.get('info') or {}).get('markPrice') or 'nan') for r in recs],
            'interval_hours': [round(i, 3) for i in intervals],
        })
        # Funding is published at the settlement timestamp.
        df['available_ts'] = df['ts'] + AVAILABILITY_DELAY
        return df[FUNDING_COLUMNS]


def bars_from_frame(df: pd.DataFrame, venue: str, symbol: str, timeframe: str):
    """Parquet rows → ``Bar`` events for the event-driven backtester."""
    from decimal import Decimal

    from ..types import Bar
    for row in df.itertuples(index=False):
        yield Bar(venue=venue, symbol=symbol, timeframe=timeframe,
                  ts=row.ts.to_pydatetime(),
                  open=Decimal(str(row.open)), high=Decimal(str(row.high)),
                  low=Decimal(str(row.low)), close=Decimal(str(row.close)),
                  volume=Decimal(str(row.volume)), closed=True)


def funding_from_frame(df: pd.DataFrame, venue: str, symbol: str):
    from decimal import Decimal

    from ..types import Funding
    for row in df.itertuples(index=False):
        yield Funding(venue=venue, symbol=symbol, ts=row.ts.to_pydatetime(),
                      rate=Decimal(str(row.rate)), interval_hours=float(row.interval_hours))


async def _main() -> int:
    parser = argparse.ArgumentParser(description='Backfill OHLCV and funding to Parquet.')
    parser.add_argument('--venue', required=True)
    parser.add_argument('--symbols', required=True)
    parser.add_argument('--timeframe', default='4h', choices=sorted(TF_MS))
    parser.add_argument('--since', required=True, help='YYYY-MM-DD')
    parser.add_argument('--out', default='./data')
    parser.add_argument('--funding', action='store_true')
    args = parser.parse_args()

    from uxcore import make_exchange
    ex = make_exchange(args.venue)
    store = ParquetStore(args.out)
    build = datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%SZ')
    since = int(datetime.fromisoformat(args.since).replace(tzinfo=timezone.utc).timestamp() * 1000)
    loader = HistoryLoader(ex)
    try:
        await ex.load_markets()
        for symbol in args.symbols.split(','):
            df = await loader.ohlcv(symbol, args.timeframe, since)
            gaps = detect_gaps([int(t.value // 1_000_000) for t in df['ts']], TF_MS[args.timeframe])
            path = store.write('ohlcv', args.venue, symbol, df, build=build, extra=args.timeframe,
                               meta={'gaps': [g.__dict__ for g in gaps]})
            print(f'{symbol} {args.timeframe}: {len(df)} bars, {len(gaps)} gaps → {path}')
            if args.funding:
                fdf = await loader.funding(symbol, since)
                fpath = store.write('funding', args.venue, symbol, fdf, build=build)
                print(f'{symbol} funding: {len(fdf)} prints → {fpath}')
    finally:
        await ex.close()
    return 0


if __name__ == '__main__':
    raise SystemExit(asyncio.run(_main()))

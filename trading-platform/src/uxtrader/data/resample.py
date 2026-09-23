"""Bar aggregation: 1m bars from ingestion → the timeframe each strategy trades.

Aggregating in the strategy engine (rather than subscribing to venue 4h candles) means
every timeframe is built from the same stored 1m stream, so live bars and backtest
bars come from identical data and identical code.

A higher-timeframe bar is emitted exactly once, when the 1m bar that completes it
arrives. It is never emitted early. A missing 1m bar inside the window does not
delay emission — the next 1m bar past the boundary closes the window — but it is
counted, because a 4h bar built from 200 of its 240 minutes is a data-quality event.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone

from ..types import Bar, _timeframe_delta

EPOCH = datetime(1970, 1, 1, tzinfo=timezone.utc)


def bucket_start(ts: datetime, tf: timedelta) -> datetime:
    """Floor to the timeframe grid, anchored at the Unix epoch (venue convention)."""
    return EPOCH + ((ts - EPOCH) // tf) * tf


@dataclass
class _Partial:
    start: datetime
    bars: list[Bar] = field(default_factory=list)


class Resampler:
    def __init__(self, timeframe: str) -> None:
        self.timeframe = timeframe
        self.tf = _timeframe_delta(timeframe)
        self._partials: dict[tuple[str, str], _Partial] = {}
        self.incomplete_bars = 0

    def push(self, bar: Bar) -> list[Bar]:
        """Feed one closed lower-timeframe bar; returns 0 or 1 completed bars
        (or 2 if a gap skipped a whole window boundary)."""
        if not bar.closed:
            return []
        key = (bar.venue, bar.symbol)
        start = bucket_start(bar.ts, self.tf)
        out: list[Bar] = []
        partial = self._partials.get(key)

        if partial is not None and start != partial.start:
            # A bar from a later window arrived: the previous window is over, even if
            # its final minute never came.
            out.append(self._emit(partial, complete=False))
            partial = None
        if partial is None:
            partial = self._partials[key] = _Partial(start=start)
        partial.bars.append(bar)

        if bar.close_ts >= partial.start + self.tf:
            out.append(self._emit(partial, complete=True))
            del self._partials[key]
        return out

    def _emit(self, partial: _Partial, *, complete: bool) -> Bar:
        bars = partial.bars
        expected = self.tf // (bars[0].close_ts - bars[0].ts)
        if not complete or len(bars) < expected:
            self.incomplete_bars += 1
        return Bar(venue=bars[0].venue, symbol=bars[0].symbol, timeframe=self.timeframe,
                   ts=partial.start, open=bars[0].open,
                   high=max(b.high for b in bars), low=min(b.low for b in bars),
                   close=bars[-1].close, volume=sum((b.volume for b in bars), bars[0].volume * 0),
                   closed=True)

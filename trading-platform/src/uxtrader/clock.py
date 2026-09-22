"""The only source of ``now`` in the entire codebase.

This exists to make look-ahead bias a *runtime error* rather than a code-review
discipline. In backtest, ``SimClock`` advances only as the engine replays events, and the
data adapter refuses to serve any row with ``ts > clock.now()``. A strategy that reaches
for future data raises instead of quietly producing a beautiful equity curve.

Rule: no module outside this one may call ``datetime.now()`` or ``time.time()`` for any
purpose that affects a trading decision. There is a CI grep for it.
"""
from __future__ import annotations

import asyncio
from datetime import datetime, timezone
from typing import Protocol


class Clock(Protocol):
    def now(self) -> datetime: ...
    async def sleep(self, seconds: float) -> None: ...


class LiveClock:
    def now(self) -> datetime:
        return datetime.now(timezone.utc)

    async def sleep(self, seconds: float) -> None:
        await asyncio.sleep(seconds)


class SimClock:
    """Backtest clock. Time moves only when the engine says so, and never backwards."""

    def __init__(self, start: datetime) -> None:
        if start.tzinfo is None:
            raise ValueError('SimClock requires a timezone-aware start')
        self._now = start

    def now(self) -> datetime:
        return self._now

    def advance_to(self, ts: datetime) -> None:
        if ts < self._now:
            raise LookAheadError(f'clock cannot move backwards: {ts} < {self._now}')
        self._now = ts

    async def sleep(self, seconds: float) -> None:
        from datetime import timedelta
        self._now += timedelta(seconds=seconds)


class LookAheadError(RuntimeError):
    """Raised whenever code reaches for data it could not have had at decision time."""


def guard(ts: datetime, clock: Clock, what: str = 'data') -> None:
    """Assert that `ts` was observable at `clock.now()`. Call this in every data adapter."""
    if ts > clock.now():
        raise LookAheadError(f'{what} at {ts} is in the future relative to {clock.now()}')

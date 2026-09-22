"""WebSocket resilience: sequence-gap detection, snapshot resync, staleness watchdog.

CCXT Pro reconnects. It does not tell you that you missed messages, and a silently-wrong
order book is worse than no order book at all — you will quote and trade against numbers
that never existed. This module is the difference between "the feed reconnected" and
"the feed is correct".
"""
from __future__ import annotations

import asyncio
import logging
import time
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field

log = logging.getLogger(__name__)

StaleCallback = Callable[[str, float], Awaitable[None]]
ResyncCallback = Callable[[str], Awaitable[None]]


@dataclass
class FeedHealth:
    """Per-(venue, symbol, channel) freshness and continuity state."""

    key: str
    last_msg_ts: float = field(default_factory=time.monotonic)
    last_seq: int | None = None
    gaps: int = 0
    resyncs: int = 0
    stale_since: float | None = None

    @property
    def age(self) -> float:
        return time.monotonic() - self.last_msg_ts


class FeedMonitor:
    """Watches every subscribed feed and escalates on gap or staleness.

    Escalation is deliberate and ordered:
      1. sequence gap        → resync from a REST snapshot, keep trading
      2. stale > soft (5 s)  → cancel resting orders for that symbol, block new entries
      3. stale > hard (30 s) → treat the venue as down; the risk engine vetoes everything
    """

    def __init__(self, *, soft_stale: float = 5.0, hard_stale: float = 30.0,
                 on_stale: StaleCallback | None = None,
                 on_resync: ResyncCallback | None = None) -> None:
        self.soft_stale = soft_stale
        self.hard_stale = hard_stale
        self._on_stale = on_stale
        self._on_resync = on_resync
        self._feeds: dict[str, FeedHealth] = {}
        self._task: asyncio.Task[None] | None = None

    def register(self, key: str) -> FeedHealth:
        return self._feeds.setdefault(key, FeedHealth(key=key))

    async def observe(self, key: str, *, seq: int | None = None,
                      prev_seq: int | None = None) -> bool:
        """Record a message. Returns False if a gap was detected (caller must resync).

        ``prev_seq`` supports venues that publish (U, u) style ranges: pass the message's
        first-update-id so we can verify it chains onto the last one we applied.
        """
        health = self.register(key)
        health.last_msg_ts = time.monotonic()
        health.stale_since = None

        if seq is None:
            return True

        expected = health.last_seq
        contiguous = (
            expected is None
            or (prev_seq is not None and prev_seq <= expected + 1 <= seq)
            or (prev_seq is None and seq == expected + 1)
        )
        health.last_seq = seq
        if contiguous:
            return True

        health.gaps += 1
        log.warning('ws_sequence_gap key=%s expected=%s got=%s gaps=%d',
                    key, expected, seq, health.gaps)
        if self._on_resync is not None:
            health.resyncs += 1
            await self._on_resync(key)
        return False

    def mark_resynced(self, key: str, seq: int | None) -> None:
        health = self.register(key)
        health.last_seq = seq
        health.last_msg_ts = time.monotonic()

    async def start(self, interval: float = 1.0) -> None:
        self._task = asyncio.create_task(self._loop(interval))

    async def stop(self) -> None:
        if self._task is not None:
            self._task.cancel()
            try:
                await self._task
            except asyncio.CancelledError:
                pass
            self._task = None

    async def _loop(self, interval: float) -> None:
        while True:
            await asyncio.sleep(interval)
            now = time.monotonic()
            for health in list(self._feeds.values()):
                age = now - health.last_msg_ts
                if age < self.soft_stale:
                    continue
                if health.stale_since is None:
                    health.stale_since = now
                    log.warning('ws_stale key=%s age=%.1fs', health.key, age)
                if self._on_stale is not None:
                    await self._on_stale(health.key, age)

    def snapshot(self) -> dict[str, dict[str, float | int | None]]:
        """For the Prometheus exporter and the dashboard."""
        return {
            h.key: {'age': round(h.age, 3), 'gaps': h.gaps,
                    'resyncs': h.resyncs, 'last_seq': h.last_seq}
            for h in self._feeds.values()
        }

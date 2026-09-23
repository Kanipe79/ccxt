"""Weight-aware adaptive rate limiting.

Upstream CCXT sleeps a fixed ``rateLimit`` between calls. That is fine for polling and
dangerous for trading: venues price requests by *weight*, not by count, so a fixed sleep
either wastes throughput or walks you into a 429 at the worst possible moment.

The design goal is not maximum throughput. It is: **a cancel or reduce-only order must
never be blocked by a rate limit.** Hence the reserved headroom.
"""
from __future__ import annotations

import asyncio
import time
from dataclasses import dataclass, field

# Weight budgets per venue per window. Verify against the venue's current docs on
# every upstream merge; venues change these without much warning.
VENUE_BUDGETS: dict[str, tuple[int, float]] = {
    # venue: (weight_budget, window_seconds)
    'binanceusdm': (2400, 60.0),
    'binance':     (6000, 60.0),
    'bybit':       (600, 5.0),
    'okx':         (60, 2.0),
    'hyperliquid': (1200, 60.0),
}

# Fraction of the budget reserved exclusively for risk-reducing operations.
# This is not tunable downward. During a cascade it is the only thing standing between
# you and an inability to cancel.
RISK_RESERVE = 0.30

# Header names the venue uses to report consumed weight, best-effort per venue.
WEIGHT_HEADERS = (
    'x-mbx-used-weight-1m',
    'x-mbx-order-count-1m',
    'x-ratelimit-used',
    'ratelimit-remaining',
)


@dataclass
class _Bucket:
    capacity: float
    window: float
    tokens: float = field(init=False)
    updated: float = field(default_factory=time.monotonic)

    def __post_init__(self) -> None:
        self.tokens = self.capacity

    def refill(self) -> None:
        now = time.monotonic()
        elapsed = now - self.updated
        self.tokens = min(self.capacity, self.tokens + elapsed * self.capacity / self.window)
        self.updated = now


class AdaptiveRateLimiter:
    """Token bucket that trusts the venue's own accounting over its own.

    Usage::

        async with limiter.acquire(weight=5, risk_reducing=False):
            resp = await exchange.fetch_ohlcv(...)
        limiter.observe_headers(exchange.last_response_headers)
    """

    def __init__(self, venue: str, *, budget: int | None = None,
                 window: float | None = None, safety: float = 0.85) -> None:
        default_budget, default_window = VENUE_BUDGETS.get(venue, (1200, 60.0))
        self.venue = venue
        self.safety = safety
        self._bucket = _Bucket(capacity=(budget or default_budget) * safety,
                               window=window or default_window)
        self._lock = asyncio.Lock()
        self._backoff_until = 0.0
        self.consumed_reported = 0.0   # exported to Prometheus

    # -- public ---------------------------------------------------------------

    def acquire(self, weight: int = 1, *, risk_reducing: bool = False):
        return _Acquisition(self, weight, risk_reducing)

    def observe_headers(self, headers: dict[str, str] | None) -> None:
        """Trust the venue's number over our own estimate — it is authoritative."""
        if not headers:
            return
        lowered = {k.lower(): v for k, v in headers.items()}
        for name in WEIGHT_HEADERS:
            if name in lowered:
                try:
                    used = float(lowered[name])
                except (TypeError, ValueError):
                    continue
                self.consumed_reported = used
                remaining = max(0.0, self._bucket.capacity - used)
                # Only ever correct downward. If the venue says we have used more than we
                # thought, believe it immediately; if less, do not inflate our own budget.
                self._bucket.refill()
                self._bucket.tokens = min(self._bucket.tokens, remaining)
                return

    def penalise(self, seconds: float) -> None:
        """Called on a 429. Hard stop for `seconds` — for EVERY call, cancels included,
        because venues escalate repeated 429s into IP bans (Binance: 418) that would
        block cancels for minutes.

        The bucket is drained to the risk reserve, not to zero: once the backoff ends,
        risk-reducing calls must be able to go immediately. Draining to zero would make
        them queue behind a full refill (~18 s on Binance futures) at exactly the
        moment they matter.
        """
        self._backoff_until = max(self._backoff_until, time.monotonic() + seconds)
        self._bucket.refill()
        self._bucket.tokens = min(self._bucket.tokens,
                                  self._bucket.capacity * RISK_RESERVE)

    @property
    def utilisation(self) -> float:
        self._bucket.refill()
        return 1.0 - (self._bucket.tokens / self._bucket.capacity)

    # -- internal -------------------------------------------------------------

    async def _wait_for(self, weight: int, risk_reducing: bool) -> None:
        floor = 0.0 if risk_reducing else self._bucket.capacity * RISK_RESERVE
        while True:
            async with self._lock:
                now = time.monotonic()
                if now >= self._backoff_until:
                    self._bucket.refill()
                    if self._bucket.tokens - weight >= floor:
                        self._bucket.tokens -= weight
                        return
                    deficit = (weight + floor) - self._bucket.tokens
                    wait = deficit * self._bucket.window / self._bucket.capacity
                else:
                    wait = self._backoff_until - now
            await asyncio.sleep(min(max(wait, 0.01), 5.0))


class _Acquisition:
    def __init__(self, limiter: AdaptiveRateLimiter, weight: int, risk_reducing: bool) -> None:
        self._limiter = limiter
        self._weight = weight
        self._risk_reducing = risk_reducing

    async def __aenter__(self) -> None:
        await self._limiter._wait_for(self._weight, self._risk_reducing)

    async def __aexit__(self, *exc_info: object) -> None:
        return None

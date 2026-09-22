"""Smart order routing across venues.

Note what this does *not* optimize: latency. You are not in that race, and pretending
otherwise leads to architecture decisions that cost you elsewhere. It routes on cost,
depth, recent fill quality, and — most importantly — remaining venue risk budget.
"""
from __future__ import annotations

import logging
from collections import defaultdict, deque
from dataclasses import dataclass, field
from decimal import Decimal

log = logging.getLogger(__name__)


@dataclass
class VenueQuote:
    venue: str
    bid: Decimal
    ask: Decimal
    bid_size: Decimal
    ask_size: Decimal
    taker_fee_bps: float
    maker_fee_bps: float

    @property
    def mid(self) -> Decimal:
        return (self.bid + self.ask) / 2

    @property
    def spread_bps(self) -> float:
        return float((self.ask - self.bid) / self.mid) * 1e4


@dataclass
class SmartRouter:
    """Chooses a venue, and splits when no single venue has the depth."""

    max_venue_fraction: float = 0.25
    depth_multiple: Decimal = Decimal('3')     # require 3x the clip resting
    _fill_quality: dict[str, deque[float]] = field(
        default_factory=lambda: defaultdict(lambda: deque(maxlen=100)))

    def record_fill_quality(self, venue: str, slippage_bps: float) -> None:
        self._fill_quality[venue].append(slippage_bps)

    def _recent_slippage(self, venue: str) -> float:
        samples = self._fill_quality.get(venue)
        if not samples:
            return 0.0
        return sum(samples) / len(samples)

    def cost_bps(self, quote: VenueQuote, side: str, amount: Decimal,
                 passive: bool) -> float:
        """Expected all-in cost. Half-spread only applies when crossing."""
        fee = quote.maker_fee_bps if passive else quote.taker_fee_bps
        spread_cost = 0.0 if passive else quote.spread_bps / 2
        resting = quote.ask_size if side == 'buy' else quote.bid_size
        depth_penalty = 0.0
        if resting > 0 and amount > resting:
            # crude square-root impact for the portion that walks the book
            depth_penalty = 40.0 * float((amount / resting) ** Decimal('0.5') - 1)
        return fee + spread_cost + depth_penalty + max(0.0, self._recent_slippage(quote.venue))

    def route(self, quotes: list[VenueQuote], side: str, amount: Decimal, *,
              passive: bool, venue_exposure: dict[str, float],
              equity: Decimal) -> list[tuple[str, Decimal]]:
        """Return [(venue, amount), ...]. Splits only when depth forces it."""
        eligible = [
            q for q in quotes
            if venue_exposure.get(q.venue, 0.0) < self.max_venue_fraction
        ]
        if not eligible:
            log.warning('router_no_eligible_venue side=%s amount=%s', side, amount)
            return []

        eligible.sort(key=lambda q: self.cost_bps(q, side, amount, passive))
        best = eligible[0]
        resting = best.ask_size if side == 'buy' else best.bid_size

        if amount <= resting * self.depth_multiple or len(eligible) == 1:
            return [(best.venue, amount)]

        # Split proportionally to available depth across the cheapest venues.
        allocations: list[tuple[str, Decimal]] = []
        remaining = amount
        for q in eligible:
            if remaining <= 0:
                break
            avail = (q.ask_size if side == 'buy' else q.bid_size) * self.depth_multiple
            take = min(remaining, avail)
            if take > 0:
                allocations.append((q.venue, take))
                remaining -= take
        if remaining > 0 and allocations:
            venue, qty = allocations[0]
            allocations[0] = (venue, qty + remaining)
        return allocations

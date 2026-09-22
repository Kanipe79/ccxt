"""Realistic fill simulation — shared by the backtester AND the paper broker.

Sharing this module is deliberate. If paper and backtest use the same fill model, then a
divergence between paper results and backtest results tells you something real about your
*signal*; and a divergence between paper and live tells you something real about your
*fill model*. Two independent measurements instead of one confounded one.

The most common way a backtest lies to you is assuming a limit order fills whenever price
touches it. In reality you are at the back of a queue, and on the touch that matters —
the one where price is about to run away from you — you do not get filled. ``QueueModel``
exists to stop that lie.
"""
from __future__ import annotations

import random
from dataclasses import dataclass
from decimal import Decimal

from ..types import BookLevel, BookSnapshot, Order, Side


@dataclass
class CostModel:
    """Defaults are the docs/01 §0.1 numbers. Calibrate `impact_k` from your own fills."""

    taker_fee_bps: float = 5.5
    maker_fee_bps: float = 2.0
    impact_k: float = 0.4          # square-root law coefficient
    latency_mean_ms: float = 250.0
    latency_std_ms: float = 80.0
    reject_rate: float = 0.001     # venues do reject; model it
    stress_multiplier: float = 1.0 # widen everything in stressed regimes

    def latency_ms(self, rng: random.Random) -> float:
        return max(20.0, rng.gauss(self.latency_mean_ms, self.latency_std_ms))

    def impact_bps(self, clip_notional: float, adv_notional: float) -> float:
        if adv_notional <= 0:
            return 0.0
        return (self.impact_k * (clip_notional / adv_notional) ** 0.5 * 1e4
                * self.stress_multiplier)


@dataclass
class QueueModel:
    """Estimates whether a resting limit order actually filled.

    Simplification that is honest about being one: when the order is submitted we record
    the size already resting at that price (our queue position). A trade print at or
    through our price consumes the queue first; we fill only from what is left. We also
    apply an adverse-selection haircut, because the fills you *do* get are
    disproportionately the ones you wish you had not.
    """

    adverse_selection_factor: float = 0.85    # fraction of touches that actually fill

    def queue_ahead(self, book: BookSnapshot, side: Side, price: Decimal) -> Decimal:
        levels = book.bids if side == 'buy' else book.asks
        for level in levels:
            if level.price == price:
                return level.size
        return Decimal('0')

    def fillable(self, queue_ahead: Decimal, traded_at_or_through: Decimal,
                 remaining: Decimal, rng: random.Random) -> Decimal:
        consumed = traded_at_or_through - queue_ahead
        if consumed <= 0:
            return Decimal('0')
        if rng.random() > self.adverse_selection_factor:
            return Decimal('0')
        return min(remaining, consumed)


class FillSimulator:
    def __init__(self, costs: CostModel | None = None,
                 queue: QueueModel | None = None, seed: int = 42) -> None:
        self.costs = costs or CostModel()
        self.queue = queue or QueueModel()
        self.rng = random.Random(seed)

    # -- market orders ---------------------------------------------------------

    def fill_market(self, order: Order, book: BookSnapshot,
                    adv_notional: float = 0.0) -> tuple[Decimal, Decimal, Decimal]:
        """Walk the book level by level. Returns (filled, avg_price, fee).

        The latency delay is applied by the *caller*, which must hand us the book as it
        was `latency_ms` after the decision — not the book the strategy saw.
        """
        if self.rng.random() < self.costs.reject_rate:
            return Decimal('0'), Decimal('0'), Decimal('0')

        levels = list(book.asks if order.side == 'buy' else book.bids)
        remaining, cost = order.amount, Decimal('0')

        for level in levels:
            if remaining <= 0:
                break
            take = min(remaining, level.size)
            cost += take * level.price
            remaining -= take

        filled = order.amount - remaining
        if filled <= 0:
            return Decimal('0'), Decimal('0'), Decimal('0')

        avg = cost / filled
        # Additional impact beyond the visible book — the book you see is not the book
        # you get, especially in size.
        impact = Decimal(str(self.costs.impact_bps(float(avg * filled), adv_notional) / 1e4))
        avg = avg * (1 + impact) if order.side == 'buy' else avg * (1 - impact)
        fee = avg * filled * Decimal(str(self.costs.taker_fee_bps / 1e4))
        return filled, avg, fee

    # -- limit orders ----------------------------------------------------------

    def fill_limit(self, order: Order, book_at_submit: BookSnapshot,
                   traded_volume_at_price: Decimal) -> tuple[Decimal, Decimal, Decimal]:
        """Returns (filled, price, fee). Fills only from volume that traded THROUGH
        our queue position — never simply because price touched our level."""
        if order.price is None:
            return Decimal('0'), Decimal('0'), Decimal('0')
        ahead = self.queue.queue_ahead(book_at_submit, order.side, order.price)
        filled = self.queue.fillable(ahead, traded_volume_at_price, order.remaining, self.rng)
        if filled <= 0:
            return Decimal('0'), Decimal('0'), Decimal('0')
        fee = order.price * filled * Decimal(str(self.costs.maker_fee_bps / 1e4))
        return filled, order.price, fee

    def would_cross(self, order: Order, book: BookSnapshot) -> bool:
        """Post-only rejection check — venues reject, so the simulator must too."""
        if order.price is None:
            return True
        return ((order.side == 'buy' and order.price >= book.asks[0].price)
                or (order.side == 'sell' and order.price <= book.bids[0].price))


def synthetic_book(mid: Decimal, spread_bps: float = 2.0, depth: int = 10,
                   size_per_level: Decimal = Decimal('5'),
                   venue: str = 'sim', symbol: str = 'BTC/USDT:USDT',
                   ts=None) -> BookSnapshot:
    """A book for tests and for bar-only backtests where L2 is unavailable.

    Using this in a backtest means your slippage numbers are assumptions, not
    measurements. Say so in the report.
    """
    from ..types import utcnow
    half = mid * Decimal(str(spread_bps / 2 / 1e4))
    bids = tuple(BookLevel(price=mid - half - mid * Decimal(str(i * 0.0001)),
                           size=size_per_level) for i in range(depth))
    asks = tuple(BookLevel(price=mid + half + mid * Decimal(str(i * 0.0001)),
                           size=size_per_level) for i in range(depth))
    return BookSnapshot(venue=venue, symbol=symbol, ts=ts or utcnow(),
                        bids=bids, asks=asks)

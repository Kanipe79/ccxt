"""Execution algorithms.

The strategy names an algo in its intent; the algo is what actually talks to the venue.
Keeping this separate means you can improve execution without touching — or
re-validating — a single strategy.

``PostOnlyPeg`` is the one that earns its keep: S5 is only viable as a maker, and the
difference between 2 bps and 5.5 bps per side is roughly half that strategy's Sharpe.
"""
from __future__ import annotations

import logging
from abc import ABC, abstractmethod
from decimal import Decimal

from ..clock import Clock
from ..types import Order, OrderState
from .oms import Broker

log = logging.getLogger(__name__)


class ExecutionAlgo(ABC):
    def __init__(self, broker: Broker, clock: Clock) -> None:
        self.broker = broker
        self.clock = clock

    @abstractmethod
    async def run(self, order: Order) -> Order: ...


class MarketAlgo(ExecutionAlgo):
    """Immediate, full size. Use for stops, kill-switch flattening, and S7 entries."""

    async def run(self, order: Order) -> Order:
        return await self.broker.submit(order)


class PostOnlyPegAlgo(ExecutionAlgo):
    """Rest at the touch, re-peg as the book moves, cross only as a last resort.

    Cost of being wrong: an unfilled entry (opportunity cost). Cost of crossing
    immediately: a guaranteed spread + taker fee on every trade. For any strategy with a
    sub-50-bps average win, the first cost is much cheaper than the second.
    """

    def __init__(self, broker: Broker, clock: Clock, *,
                 max_improvements: int = 3, repeg_interval_s: float = 5.0,
                 cross_after_s: float | None = 240.0) -> None:
        super().__init__(broker, clock)
        self.max_improvements = max_improvements
        self.repeg_interval_s = repeg_interval_s
        self.cross_after_s = cross_after_s

    async def run(self, order: Order) -> Order:
        started = self.clock.now()
        improvements = 0
        working = await self.broker.submit(order)

        while not working.state.terminal:
            await self.clock.sleep(self.repeg_interval_s)
            elapsed = (self.clock.now() - started).total_seconds()

            if self.cross_after_s is not None and elapsed >= self.cross_after_s:
                log.info('postonly_crossing coid=%s after=%.0fs', working.client_order_id, elapsed)
                await self.broker.cancel(working)
                taker = working.model_copy(update={
                    'type': 'market', 'price': None, 'post_only': False,
                    'amount': working.remaining, 'state': OrderState.PENDING_NEW})
                return await self.broker.submit(taker)

            mid = await self.broker.mid_price(working.venue, working.symbol)
            drifted = working.price is not None and abs(mid - working.price) / mid > Decimal('0.0005')
            if drifted and improvements < self.max_improvements:
                improvements += 1
                await self.broker.cancel(working)
                working = await self.broker.submit(working.model_copy(update={
                    'price': mid, 'amount': working.remaining,
                    'state': OrderState.PENDING_NEW}))
        return working


class TwapAlgo(ExecutionAlgo):
    """Equal slices over a window. Used by S2's weekly rebalance.

    Deliberately not adaptive: a TWAP whose slice size reacts to price is a small
    momentum strategy you did not backtest.
    """

    def __init__(self, broker: Broker, clock: Clock, *,
                 duration_s: int = 1800, slices: int = 10) -> None:
        super().__init__(broker, clock)
        self.duration_s = duration_s
        self.slices = max(1, slices)

    async def run(self, order: Order) -> Order:
        slice_amount = order.amount / self.slices
        interval = self.duration_s / self.slices
        filled = Decimal('0')
        last = order

        for i in range(self.slices):
            child = order.model_copy(update={
                'client_order_id': f'{order.client_order_id[:28]}s{i:02d}',
                'amount': slice_amount, 'state': OrderState.PENDING_NEW})
            last = await self.broker.submit(child)
            filled += last.filled
            if i < self.slices - 1:
                await self.clock.sleep(interval)

        return order.model_copy(update={'filled': filled,
                                        'state': OrderState.FILLED if filled >= order.amount
                                        else OrderState.PARTIALLY_FILLED})


class PovAlgo(ExecutionAlgo):
    """Participate at a fraction of traded volume. For clips above ~2% of ADV.

    Requires a live volume feed; the ``volume_fn`` callback returns the venue's traded
    volume over the last interval for this symbol.
    """

    def __init__(self, broker: Broker, clock: Clock, *, participation: float = 0.10,
                 interval_s: float = 30.0, max_duration_s: float = 3600.0,
                 volume_fn=None) -> None:
        super().__init__(broker, clock)
        self.participation = participation
        self.interval_s = interval_s
        self.max_duration_s = max_duration_s
        self.volume_fn = volume_fn

    async def run(self, order: Order) -> Order:
        if self.volume_fn is None:
            raise ValueError('PovAlgo requires a volume_fn')
        started = self.clock.now()
        filled = Decimal('0')
        i = 0
        while filled < order.amount:
            if (self.clock.now() - started).total_seconds() > self.max_duration_s:
                log.warning('pov_timeout coid=%s filled=%s/%s',
                            order.client_order_id, filled, order.amount)
                break
            recent = Decimal(str(await self.volume_fn(order.venue, order.symbol,
                                                      self.interval_s)))
            clip = min(order.amount - filled, recent * Decimal(str(self.participation)))
            if clip > 0:
                child = order.model_copy(update={
                    'client_order_id': f'{order.client_order_id[:28]}p{i:02d}',
                    'amount': clip, 'state': OrderState.PENDING_NEW})
                done = await self.broker.submit(child)
                filled += done.filled
                i += 1
            await self.clock.sleep(self.interval_s)
        return order.model_copy(update={'filled': filled})

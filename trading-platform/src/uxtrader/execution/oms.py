"""Order management: idempotency, the order state machine, and startup reconciliation.

The two rules that matter:

1. **Every order carries a deterministic client order id.** The same logical intent
   always produces the same id, so a duplicate submission after a restart or a message
   replay is rejected by the venue instead of doubling your position.
2. **An AMBIGUOUS order blocks its strategy until resolved.** A timeout after send means
   you do not know whether the venue has your order. Guessing is how accounts die.
"""
from __future__ import annotations

import hashlib
import logging
from collections.abc import Awaitable, Callable
from decimal import Decimal
from typing import Protocol

from ..clock import Clock
from ..types import (
    AlgoKind, Fill, Intent, Order, OrderState, Position, RiskDecision, Side,
)

log = logging.getLogger(__name__)


def client_order_id(strategy: str, symbol: str, intent_id: str, seq: int = 0) -> str:
    """Deterministic, venue-safe, ≤ 36 chars. This is the idempotency key."""
    digest = hashlib.sha256(f'{strategy}|{symbol}|{intent_id}|{seq}'.encode()).hexdigest()
    return f'ux{digest[:30]}'


class Broker(Protocol):
    """The seam between the platform and the outside world.

    ``PaperBroker`` and the live broker implement exactly this, which is what makes
    paper trading indistinguishable from live everywhere above this line.
    """

    async def submit(self, order: Order) -> Order: ...
    async def cancel(self, order: Order) -> Order: ...
    async def fetch_open_orders(self, venue: str, symbol: str | None = None) -> list[Order]: ...
    async def fetch_positions(self, venue: str) -> dict[str, Decimal]: ...
    async def mid_price(self, venue: str, symbol: str) -> Decimal: ...


class OrderManager:
    def __init__(self, broker: Broker, clock: Clock, *,
                 on_fill: Callable[[Fill], Awaitable[None]] | None = None) -> None:
        self.broker = broker
        self.clock = clock
        self._orders: dict[str, Order] = {}
        self._ambiguous: set[str] = set()
        self._on_fill = on_fill

    # -- the main path ---------------------------------------------------------

    async def execute(self, intent: Intent, decision: RiskDecision,
                      current: Position | None, venue: str) -> list[Order]:
        """Translate an approved intent into venue orders.

        Target-position semantics: the delta is computed here against portfolio truth,
        not by the strategy. Replaying the same intent is therefore a no-op.
        """
        if not decision.approved or decision.adjusted_position is None:
            return []
        if intent.strategy in self._blocked_strategies():
            log.error('execute_blocked_ambiguous strategy=%s', intent.strategy)
            return []

        have = current.quantity if current else Decimal('0')
        want = decision.adjusted_position
        delta = want - have

        if abs(delta) <= intent.target.tolerance:
            return []

        side: Side = 'buy' if delta > 0 else 'sell'
        amount = abs(delta)
        reduce_only = abs(want) < abs(have) and (want == 0 or (want > 0) == (have > 0))

        arrival = await self.broker.mid_price(venue, intent.symbol)
        coid = client_order_id(intent.strategy, intent.symbol, intent.intent_id)

        if coid in self._orders and not self._orders[coid].state.terminal:
            log.info('execute_duplicate_ignored coid=%s', coid)
            return [self._orders[coid]]

        order = Order(
            client_order_id=coid, intent_id=intent.intent_id, strategy=intent.strategy,
            venue=venue, symbol=intent.symbol, side=side,
            type='market' if intent.algo.kind is AlgoKind.MARKET else 'limit',
            amount=amount,
            price=None if intent.algo.kind is AlgoKind.MARKET else arrival,
            arrival_price=arrival, reduce_only=reduce_only,
            post_only=intent.algo.kind is AlgoKind.POST_ONLY_PEG,
            created_at=self.clock.now(), updated_at=self.clock.now(),
        )
        self._orders[coid] = order

        try:
            acked = await self.broker.submit(order)
        except Exception as exc:                       # noqa: BLE001
            klass = getattr(exc, 'klass', None)
            if klass is not None and getattr(klass, 'value', '') == 'ambiguous':
                order.state = OrderState.AMBIGUOUS
                self._ambiguous.add(coid)
                log.critical('order_ambiguous coid=%s strategy=%s — strategy halted',
                             coid, intent.strategy)
                return [order]
            order.state = OrderState.REJECTED
            log.error('order_rejected coid=%s err=%s', coid, exc)
            return [order]

        self._orders[coid] = acked
        return [acked]

    async def cancel_all_for(self, venue: str, symbol: str) -> int:
        """Used on feed staleness and by the kill switch. Risk-reducing, so it bypasses
        the rate-limiter reserve inside uxcore."""
        n = 0
        for order in list(self._orders.values()):
            if order.venue == venue and order.symbol == symbol and not order.state.terminal:
                await self.broker.cancel(order)
                n += 1
        return n

    # -- fills -----------------------------------------------------------------

    async def on_fill(self, fill: Fill) -> None:
        order = self._orders.get(fill.client_order_id)
        if order is None:
            log.warning('fill_for_unknown_order coid=%s', fill.client_order_id)
        else:
            order.filled += fill.amount
            prev = order.avg_price or Decimal('0')
            prev_qty = order.filled - fill.amount
            order.avg_price = ((prev * prev_qty + fill.price * fill.amount) / order.filled
                               if order.filled else fill.price)
            order.state = (OrderState.FILLED if order.remaining <= 0
                           else OrderState.PARTIALLY_FILLED)
            order.updated_at = fill.ts
            self._ambiguous.discard(order.client_order_id)
            if order.slippage_bps is not None:
                log.info('fill coid=%s slippage_bps=%.2f maker=%s',
                         order.client_order_id, order.slippage_bps, fill.is_maker)
        if self._on_fill is not None:
            await self._on_fill(fill)

    # -- reconciliation --------------------------------------------------------

    async def reconcile_on_start(self, venues: list[str],
                                 local_positions: dict[str, Decimal]) -> list[str]:
        """Blocking startup check. Refuse to trade if local and venue state disagree.

        This is mandatory, not best-effort. Starting a trading system that is unsure
        what it owns is the highest-variance thing you can do all day.
        """
        problems: list[str] = []
        for venue in venues:
            remote = await self.broker.fetch_positions(venue)
            symbols = set(remote) | set(local_positions)
            for symbol in symbols:
                a = local_positions.get(symbol, Decimal('0'))
                b = remote.get(symbol, Decimal('0'))
                denom = max(abs(a), abs(b), Decimal('1'))
                if abs(a - b) / denom > Decimal('0.001'):
                    problems.append(f'{venue}:{symbol} local={a} venue={b}')
            for order in await self.broker.fetch_open_orders(venue):
                if order.client_order_id not in self._orders:
                    problems.append(f'{venue}: orphan order {order.client_order_id}')
        if problems:
            log.critical('startup_reconciliation_failed n=%d', len(problems))
        return problems

    def _blocked_strategies(self) -> set[str]:
        return {self._orders[c].strategy for c in self._ambiguous if c in self._orders}

    def resolve_ambiguous(self, coid: str, state: OrderState) -> None:
        """Called by the operator or the reconciler after RB-02."""
        self._ambiguous.discard(coid)
        if coid in self._orders:
            self._orders[coid].state = state

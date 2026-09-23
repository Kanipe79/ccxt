"""Paper broker — implements the same ``Broker`` protocol as the live path.

Paper mode differs from live in *exactly this file*. Everything above it — strategies,
risk, OMS, portfolio, dashboards, database writes — is byte-identical. Grep for
``is_paper`` outside this directory should return nothing; if it does not, someone has
started building two systems and only one of them is tested.
"""
from __future__ import annotations

import logging
from collections.abc import Awaitable, Callable
from decimal import Decimal

from ..clock import Clock
from ..lab.fills import FillSimulator
from ..types import BookSnapshot, Fill, Order, OrderState

log = logging.getLogger(__name__)


class PaperBroker:
    """Matches against the *live* order book using the backtester's fill model."""

    def __init__(self, clock: Clock, books: Callable[[str, str], BookSnapshot | None], *,
                 simulator: FillSimulator | None = None,
                 on_fill: Callable[[Fill], Awaitable[None]] | None = None,
                 adv_lookup: Callable[[str], float] | None = None) -> None:
        self.clock = clock
        self._books = books
        self.sim = simulator or FillSimulator()
        self._on_fill = on_fill
        self._adv = adv_lookup or (lambda _s: 0.0)
        self._open: dict[str, Order] = {}
        self._positions: dict[tuple[str, str], Decimal] = {}

    def set_fill_handler(self, handler: Callable[[Fill], Awaitable[None]]) -> None:
        """Late binding: the OMS needs the broker, and the broker reports to the OMS."""
        self._on_fill = handler

    async def submit(self, order: Order) -> Order:
        book = self._books(order.venue, order.symbol)
        if book is None:
            log.warning('paper_no_book venue=%s symbol=%s', order.venue, order.symbol)
            return order.model_copy(update={'state': OrderState.REJECTED})

        if order.post_only and self.sim.would_cross(order, book):
            # Exactly what the venue does. A paper broker that silently accepts a
            # crossing post-only order will make S5 look profitable when it is not.
            return order.model_copy(update={'state': OrderState.REJECTED})

        if order.type == 'market':
            filled, avg, fee = self.sim.fill_market(order, book, self._adv(order.symbol))
            if filled <= 0:
                return order.model_copy(update={'state': OrderState.REJECTED})
            done = order.model_copy(update={
                'state': OrderState.FILLED, 'filled': filled, 'avg_price': avg,
                'updated_at': self.clock.now()})
            await self._emit_fill(done, avg, filled, fee, is_maker=False)
            return done

        self._open[order.client_order_id] = order.model_copy(
            update={'state': OrderState.NEW, 'updated_at': self.clock.now()})
        return self._open[order.client_order_id]

    async def on_trade_print(self, venue: str, symbol: str, price: Decimal,
                             volume: Decimal) -> None:
        """Drive resting limit orders from the live trade feed."""
        for coid, order in list(self._open.items()):
            if order.venue != venue or order.symbol != symbol or order.price is None:
                continue
            crosses = ((order.side == 'buy' and price <= order.price)
                       or (order.side == 'sell' and price >= order.price))
            if not crosses:
                continue
            book = self._books(venue, symbol)
            if book is None:
                continue
            filled, px, fee = self.sim.fill_limit(order, book, volume)
            if filled <= 0:
                continue
            total = order.filled + filled
            updated = order.model_copy(update={
                'filled': total, 'avg_price': px, 'updated_at': self.clock.now(),
                'state': OrderState.FILLED if total >= order.amount
                else OrderState.PARTIALLY_FILLED})
            self._open[coid] = updated
            if updated.state is OrderState.FILLED:
                self._open.pop(coid, None)
            await self._emit_fill(updated, px, filled, fee, is_maker=True)

    async def cancel(self, order: Order) -> Order:
        self._open.pop(order.client_order_id, None)
        return order.model_copy(update={'state': OrderState.CANCELED,
                                        'updated_at': self.clock.now()})

    async def fetch_open_orders(self, venue: str, symbol: str | None = None) -> list[Order]:
        return [o for o in self._open.values()
                if o.venue == venue and (symbol is None or o.symbol == symbol)]

    async def fetch_positions(self, venue: str) -> dict[str, Decimal]:
        return {sym: qty for (v, sym), qty in self._positions.items()
                if v == venue and qty != 0}

    async def mid_price(self, venue: str, symbol: str) -> Decimal:
        book = self._books(venue, symbol)
        return book.mid if book else Decimal('0')

    async def _emit_fill(self, order: Order, price: Decimal, amount: Decimal,
                         fee: Decimal, *, is_maker: bool) -> None:
        signed = amount if order.side == 'buy' else -amount
        key = (order.venue, order.symbol)
        self._positions[key] = self._positions.get(key, Decimal('0')) + signed
        fill = Fill(client_order_id=order.client_order_id, venue_order_id=None,
                    strategy=order.strategy, venue=order.venue, symbol=order.symbol,
                    side=order.side, price=price, amount=amount, fee=fee,
                    fee_currency='USDT', ts=self.clock.now(), is_maker=is_maker)
        if self._on_fill is not None:
            await self._on_fill(fill)

"""Kill-switch layer 3: venue-native stop orders resting on every protected book.

If the whole platform dies — process, host, cloud region — these still close the
position, because they live at the exchange. They are placed *wider* than the
strategy's own software stop (``buffer``), so in normal operation the strategy exits
first and the venue stop is only ever the backstop.

Reconciled on every portfolio snapshot:
  * each book with a known stop price gets exactly one resting stop, sized to the book;
  * a changed size or price → cancel and replace;
  * a flat book → cancel.

Reduce-only is used only when it is safe. Books are per strategy but the venue sees
the NET position: if S1 is short 0.5 and S4 long 0.3, a reduce-only *sell* for S4
would be rejected (the net is short). In that case the stop is placed as a plain order
sized to S4's book, which the venue can always accept.
"""
from __future__ import annotations

import hashlib
import logging
from dataclasses import dataclass
from decimal import Decimal

from ..events import PortfolioSnapshot
from ..types import Intent, Order, OrderState

log = logging.getLogger(__name__)

Key = tuple[str, str, str]          # (venue, symbol, strategy)


@dataclass
class _Live:
    order: Order
    price: Decimal
    amount: Decimal
    side: str


class StopManager:
    def __init__(self, broker, clock, *, buffer: Decimal = Decimal('0.005')) -> None:
        self.broker = broker
        self.clock = clock
        self.buffer = buffer
        self.desired: dict[Key, Decimal] = {}      # software stop price per book
        self.live: dict[Key, _Live] = {}
        self._version = 0
        self.placed = 0
        self.cancelled = 0

    def note_intent(self, intent: Intent, venue: str) -> None:
        key = (venue, intent.symbol, intent.strategy)
        if intent.target.position == 0:
            self.desired.pop(key, None)
        elif intent.stop is not None and intent.stop.venue_native:
            self.desired[key] = intent.stop.price

    def owns(self, client_order_id: str) -> Key | None:
        for key, live in self.live.items():
            if live.order.client_order_id == client_order_id:
                return key
        return None

    def on_stop_filled(self, client_order_id: str) -> None:
        key = self.owns(client_order_id)
        if key is not None:
            log.critical('venue_stop_triggered venue=%s symbol=%s strategy=%s', *key)
            self.live.pop(key, None)
            self.desired.pop(key, None)

    async def sync(self, snap: PortfolioSnapshot) -> None:
        books = {(p.venue, p.symbol, p.strategy): p.quantity for p in snap.positions}
        net: dict[tuple[str, str], Decimal] = {}
        for (v, s, _), q in books.items():
            net[(v, s)] = net.get((v, s), Decimal('0')) + q

        for key in set(self.desired) | set(self.live):
            qty = books.get(key, Decimal('0'))
            price = self.desired.get(key)
            live = self.live.get(key)
            if qty == 0 or price is None:
                if live is not None:
                    await self._cancel(key, live)
                if qty == 0:
                    self.desired.pop(key, None)
                continue
            side = 'sell' if qty > 0 else 'buy'
            trigger = price * (1 - self.buffer) if qty > 0 else price * (1 + self.buffer)
            amount = abs(qty)
            n = net.get(key[:2], Decimal('0'))
            reduce_only = (n != 0 and (n > 0) == (qty > 0) and amount <= abs(n))
            # reduce_only is part of the identity: another strategy flipping the net
            # makes a resting reduce-only stop unexecutable, so it must be replaced.
            if live is not None and live.amount == amount and live.price == trigger \
                    and live.side == side and live.order.reduce_only == reduce_only:
                continue
            if live is not None:
                await self._cancel(key, live)
            await self._place(key, side, amount, trigger, reduce_only)

    async def _place(self, key: Key, side: str, amount: Decimal, trigger: Decimal,
                     reduce_only: bool) -> None:
        venue, symbol, strategy = key
        self._version += 1
        coid = 'uxs' + hashlib.sha256(
            f'{venue}|{symbol}|{strategy}|{self._version}|{self.clock.now()}'.encode()
        ).hexdigest()[:29]
        order = Order(client_order_id=coid, intent_id='stop', strategy=strategy, venue=venue,
                      symbol=symbol, side=side, type='stop_market', amount=amount,
                      stop_price=trigger, reduce_only=reduce_only,
                      created_at=self.clock.now(), updated_at=self.clock.now())
        try:
            placed = await self.broker.submit(order)
        except Exception as exc:                          # noqa: BLE001
            log.critical('venue_stop_place_failed key=%s err=%s — book UNPROTECTED', key, exc)
            return
        if placed.state is OrderState.REJECTED:
            log.critical('venue_stop_rejected key=%s — book UNPROTECTED at venue', key)
            return
        self.live[key] = _Live(order=placed, price=trigger, amount=amount, side=side)
        self.placed += 1

    async def _cancel(self, key: Key, live: _Live) -> None:
        try:
            await self.broker.cancel(live.order)
        except Exception as exc:                          # noqa: BLE001
            # Leave it tracked: a stop we could not cancel may still fire, and the
            # next sync must retry rather than place a second one beside it.
            log.error('venue_stop_cancel_failed key=%s err=%s', key, exc)
            return
        self.live.pop(key, None)
        self.cancelled += 1

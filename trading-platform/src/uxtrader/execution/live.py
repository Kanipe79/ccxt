"""Live broker — the same ``Broker`` protocol as ``PaperBroker``, backed by uxcore.

Fills are taken ONLY from the venue's user-trade stream (``watch_my_trades``), never
from the REST create-order response. Taking them from both double-counts; taking them
only from REST misses fills on resting orders. Each venue trade id is applied once.
"""
from __future__ import annotations

import asyncio
import logging
from collections import deque
from collections.abc import Awaitable, Callable
from datetime import datetime, timezone
from decimal import Decimal
from typing import Any

from uxcore import ErrorClass, UXError

from ..clock import Clock, LiveClock
from ..types import Fill, Order, OrderState

log = logging.getLogger(__name__)

_STATUS = {'closed': OrderState.FILLED, 'canceled': OrderState.CANCELED,
           'cancelled': OrderState.CANCELED, 'expired': OrderState.CANCELED,
           'rejected': OrderState.REJECTED}


def _dec(x: Any) -> Decimal | None:
    return None if x is None else Decimal(str(x))


class LiveBroker:
    def __init__(self, exchanges: dict[str, Any], *, clock: Clock | None = None) -> None:
        self.ex = exchanges
        self.clock = clock or LiveClock()
        self._on_fill: Callable[[Fill], Awaitable[None]] | None = None
        self._orders_by_venue_id: dict[tuple[str, str], Order] = {}
        self._seen_trades: deque[str] = deque(maxlen=50_000)
        self._seen_set: set[str] = set()

    def set_fill_handler(self, handler: Callable[[Fill], Awaitable[None]]) -> None:
        self._on_fill = handler

    # -- Broker protocol -------------------------------------------------------

    async def submit(self, order: Order) -> Order:
        ex = self.ex[order.venue]
        amount, price = ex.round_to_market(order.symbol, order.amount, order.price)
        if amount <= 0:
            log.warning('order_below_venue_minimum coid=%s amount=%s',
                        order.client_order_id, order.amount)
            return order.model_copy(update={'state': OrderState.REJECTED})
        params: dict[str, Any] = {}
        if order.reduce_only:
            params['reduceOnly'] = True
        if order.post_only:
            params['postOnly'] = True
        try:
            raw = await ex.ux_create_order(
                order.symbol, order.type, order.side, float(amount),
                float(price) if price is not None else None,
                client_order_id=order.client_order_id, params=params)
        except UXError as err:
            if err.klass is ErrorClass.REJECTED:
                log.error('order_rejected coid=%s venue=%s err=%s',
                          order.client_order_id, order.venue, err)
                return order.model_copy(update={'state': OrderState.REJECTED})
            raise                       # AMBIGUOUS/FATAL/etc: the OMS must see these
        merged = self._merge(order.model_copy(update={'amount': amount, 'price': price}), raw)
        if merged.venue_order_id:
            self._orders_by_venue_id[(order.venue, merged.venue_order_id)] = merged
        return merged

    async def cancel(self, order: Order) -> Order:
        if not order.venue_order_id:
            return order.model_copy(update={'state': OrderState.CANCELED})
        try:
            raw = await self.ex[order.venue].ux_cancel_order(order.venue_order_id, order.symbol)
        except UXError as err:
            if err.klass is ErrorClass.REJECTED:     # OrderNotFound: already gone
                return order.model_copy(update={'state': OrderState.CANCELED})
            raise
        return self._merge(order, raw)

    async def fetch_open_orders(self, venue: str, symbol: str | None = None) -> list[Order]:
        raws = await self.ex[venue].fetch_open_orders(symbol)
        out = []
        for r in raws:
            coid = r.get('clientOrderId') or ''
            known = self._orders_by_venue_id.get((venue, str(r.get('id'))))
            out.append(Order(
                client_order_id=coid, venue_order_id=str(r.get('id')),
                intent_id=known.intent_id if known else '',
                strategy=known.strategy if known else 'external',
                venue=venue, symbol=r['symbol'], side=r['side'], type=r.get('type') or 'limit',
                amount=_dec(r.get('amount')) or Decimal('0'), price=_dec(r.get('price')),
                state=OrderState.NEW, filled=_dec(r.get('filled')) or Decimal('0')))
        return out

    async def fetch_positions(self, venue: str) -> dict[str, Decimal]:
        ex = self.ex[venue]
        out: dict[str, Decimal] = {}
        for p in await ex.ux_fetch_positions():
            contracts = _dec(p.get('contracts')) or Decimal('0')
            if contracts == 0:
                continue
            size = contracts * (_dec(p.get('contractSize')) or Decimal('1'))
            out[p['symbol']] = size if p.get('side') != 'short' else -size
        return out

    async def mid_price(self, venue: str, symbol: str) -> Decimal:
        t = await self.ex[venue].ux_fetch_ticker(symbol)
        if t.get('bid') and t.get('ask'):
            return (Decimal(str(t['bid'])) + Decimal(str(t['ask']))) / 2
        return Decimal(str(t['last']))

    # -- fills -----------------------------------------------------------------

    async def run_user_stream(self, venue: str) -> None:
        """Forever: venue trades → ``Fill`` → handler. Reconnects are ccxt.pro's job;
        a gap here is caught by the portfolio service's periodic reconciliation."""
        ex = self.ex[venue]
        while True:
            try:
                trades = await ex.watch_my_trades()
            except asyncio.CancelledError:
                raise
            except Exception as exc:                        # noqa: BLE001
                log.warning('user_stream_error venue=%s err=%s', venue, exc)
                await asyncio.sleep(1.0)
                continue
            for t in trades:
                await self.handle_trade(venue, t)

    async def handle_trade(self, venue: str, t: dict[str, Any]) -> Fill | None:
        tid = f'{venue}:{t.get("id")}'
        if tid in self._seen_set:
            return None
        if len(self._seen_trades) == self._seen_trades.maxlen:
            self._seen_set.discard(self._seen_trades[0])
        self._seen_trades.append(tid)
        self._seen_set.add(tid)

        order = self._orders_by_venue_id.get((venue, str(t.get('order'))))
        info = t.get('info') or {}
        coid = order.client_order_id if order else str(info.get('clientOrderId') or '')
        fee = t.get('fee') or {}
        fill = Fill(
            client_order_id=coid, venue_order_id=str(t.get('order')),
            strategy=order.strategy if order else 'external', venue=venue,
            symbol=t['symbol'], side=t['side'], price=Decimal(str(t['price'])),
            amount=Decimal(str(t['amount'])), fee=Decimal(str(fee.get('cost') or 0)),
            fee_currency=fee.get('currency') or 'USDT',
            ts=datetime.fromtimestamp((t.get('timestamp') or 0) / 1000, tz=timezone.utc),
            is_maker=t.get('takerOrMaker') == 'maker')
        if order is None:
            # A fill we did not place: manual trading on a systematic account, or a
            # second system on the same keys. Record it — the book must stay true —
            # and make noise.
            log.critical('external_fill venue=%s symbol=%s side=%s amount=%s',
                         venue, fill.symbol, fill.side, fill.amount)
        if self._on_fill is not None:
            await self._on_fill(fill)
        return fill

    # -- helpers ---------------------------------------------------------------

    def _merge(self, order: Order, raw: dict[str, Any]) -> Order:
        status = raw.get('status')
        filled = _dec(raw.get('filled')) or Decimal('0')
        if status in _STATUS:
            state = _STATUS[status]
        else:
            state = OrderState.PARTIALLY_FILLED if filled > 0 else OrderState.NEW
        # `filled` stays driven by the user stream; REST only informs state and id.
        return order.model_copy(update={
            'venue_order_id': str(raw.get('id')) if raw.get('id') is not None else None,
            'state': state, 'updated_at': self.clock.now()})

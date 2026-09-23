"""Execution service — the ONLY component that talks to a venue with trade rights.

Singleton by design (``RedisLock`` in multi-node deployments). Reads positions from
portfolio snapshots, never from the venue, and computes each order as the delta
between the risk-approved target and the strategy's own book.

Paper and live differ only in the ``Broker`` injected here.
"""
from __future__ import annotations

import logging

from ..bus import Bus
from ..clock import Clock, LiveClock
from ..events import EXEC_REPORT, ApprovedIntent, PortfolioSnapshot, StaleFeed, fill_subject
from ..execution.oms import Broker, OrderManager
from ..lab.fills import synthetic_book
from ..types import Bar, BookSnapshot, Fill, Position, TradeTick
from .common import LocalLock
from .stops import StopManager

log = logging.getLogger(__name__)


class ExecutionService:
    def __init__(self, bus: Bus, broker: Broker, *, default_venue: str,
                 clock: Clock | None = None, lock=None,
                 max_intent_age_s: float = 60.0, venue_stops: bool = True) -> None:
        self.bus = bus
        self.broker = broker
        self.default_venue = default_venue
        self.clock = clock or LiveClock()
        self.lock = lock or LocalLock()
        self.oms = OrderManager(broker, self.clock, on_fill=self._publish_fill)
        self.stops = StopManager(broker, self.clock) if venue_stops else None
        if hasattr(broker, 'set_fill_handler'):
            broker.set_fill_handler(self._route_fill)
        self.snapshot: PortfolioSnapshot | None = None
        self.books: dict[tuple[str, str], BookSnapshot] = {}
        self.rejected_no_lock = 0
        self.max_intent_age_s = max_intent_age_s
        self.dropped_stale = 0

    async def start(self) -> None:
        await self.lock.acquire()
        await self.bus.subscribe('exec.order', self._on_order)
        await self.bus.subscribe('portfolio.snapshot', self._on_snapshot)
        await self.bus.subscribe('feed.stale', self._on_stale)
        await self.bus.subscribe('md.*.book.>', self._on_book)
        await self.bus.subscribe('md.*.bar.>', self._on_bar)
        await self.bus.subscribe('md.*.trade.>', self._on_trade)

    # -- the order path ----------------------------------------------------------

    def _book_position(self, venue: str, symbol: str, strategy: str) -> Position | None:
        if self.snapshot is None:
            return None
        for p in self.snapshot.positions:
            if p.venue == venue and p.symbol == symbol and p.strategy == strategy:
                return p
        return None

    async def _on_order(self, _subject: str, msg) -> None:
        if not isinstance(msg, ApprovedIntent):
            return
        if not self.lock.held:
            self.rejected_no_lock += 1
            log.critical('exec_refused_lock_not_held intent=%s', msg.intent.intent_id)
            return
        intent = msg.intent
        # An approved intent that sat in a queue (execution restarting, bus backlog)
        # describes a market that no longer exists. Reducing intents are exempt:
        # a late exit is still an exit.
        age = (self.clock.now() - intent.created_at).total_seconds()
        if age > self.max_intent_age_s and intent.target.position != 0:
            self.dropped_stale += 1
            log.warning('exec_dropped_stale_intent id=%s age=%.0fs', intent.intent_id, age)
            return
        venue = intent.venue or self.default_venue
        if self.stops is not None:
            self.stops.note_intent(intent, venue)
        current = self._book_position(venue, intent.symbol, intent.strategy)
        for order in await self.oms.execute(intent, msg.decision, current, venue):
            await self.bus.publish(EXEC_REPORT, order)
        # A stop-only update produces no fill and therefore no new snapshot; re-sync
        # now so the venue stop moves immediately, not on the next price bar.
        if self.stops is not None and intent.stop is not None and self.snapshot is not None:
            await self.stops.sync(self.snapshot)

    async def _route_fill(self, fill: Fill) -> None:
        """Fills from the broker: OMS orders go through the OMS; venue-stop fills
        (which the OMS never placed) are published directly."""
        if self.stops is not None and self.stops.owns(fill.client_order_id):
            self.stops.on_stop_filled(fill.client_order_id)
            await self._publish_fill(fill)
            return
        await self.oms.on_fill(fill)

    async def _publish_fill(self, fill: Fill) -> None:
        await self.bus.publish(fill_subject(fill.venue), fill)

    async def _on_snapshot(self, _subject: str, snap) -> None:
        if isinstance(snap, PortfolioSnapshot) and (
                self.snapshot is None or snap.seq > self.snapshot.seq):
            self.snapshot = snap
            if self.stops is not None and self.lock.held:
                await self.stops.sync(snap)

    async def _on_stale(self, _subject: str, msg) -> None:
        """A stale book means resting quotes are priced off data that may be wrong."""
        if isinstance(msg, StaleFeed):
            n = await self.oms.cancel_all_for(msg.venue, msg.symbol)
            if n:
                log.warning('cancelled_on_stale venue=%s symbol=%s n=%d',
                            msg.venue, msg.symbol, n)

    # -- market data for the paper broker ----------------------------------------

    def book_for(self, venue: str, symbol: str) -> BookSnapshot | None:
        return self.books.get((venue, symbol))

    async def _on_book(self, _subject: str, book) -> None:
        if isinstance(book, BookSnapshot):
            self.books[(book.venue, book.symbol)] = book

    async def _on_bar(self, _subject: str, bar) -> None:
        # Bar-only feeds still need something to price paper fills against. A real
        # book, once one has arrived, always wins over the synthetic one.
        if isinstance(bar, Bar):
            key = (bar.venue, bar.symbol)
            existing = self.books.get(key)
            if existing is None or existing.sequence == -1:
                book = synthetic_book(bar.close, venue=bar.venue, symbol=bar.symbol,
                                      ts=bar.close_ts)
                self.books[key] = book.model_copy(update={'sequence': -1})
            if hasattr(self.broker, 'on_price'):
                # Paper venue stops trigger on the bar's extreme, as a real one would
                # have somewhere inside the bar.
                await self.broker.on_price(bar.venue, bar.symbol, bar.low)
                await self.broker.on_price(bar.venue, bar.symbol, bar.high)

    async def _on_trade(self, _subject: str, tick) -> None:
        if isinstance(tick, TradeTick) and hasattr(self.broker, 'on_trade_print'):
            await self.broker.on_trade_print(tick.venue, tick.symbol, tick.price,
                                             tick.amount)
            if hasattr(self.broker, 'on_price'):
                await self.broker.on_price(tick.venue, tick.symbol, tick.price)


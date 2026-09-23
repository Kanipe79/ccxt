"""Event-driven backtester.

It replays the recorded event stream through the *same* ``StrategyBase``, the *same*
``RiskEngine`` and the *same* ``OrderManager`` that run in production. The only
substitutions are ``SimClock`` for ``LiveClock`` and ``PaperBroker`` for the live broker.

Anything that is easy to get wrong is made structurally impossible instead:

* **Look-ahead** — the ``SimClock`` guard raises on any access to a future timestamp.
* **Same-bar fills** — an intent emitted on bar t's close is queued and fills at bar
  t+1's open, after a sampled latency. With bar-only data the book is synthetic
  (``synthetic_book``), so slippage is an assumption, not a measurement — say so.
* **Free limit fills** — handled by ``QueueModel`` in ``fills.py``.
* **Ignored funding** — applied at the venue's real funding timestamps.

Cross-validation obligation: the vectorized engine in ``vector.py`` must reproduce this
engine's PnL to within 2%. A larger gap is a bug in one of them, and you must find it
before you trust either.
"""
from __future__ import annotations

import logging
from collections.abc import Iterable, Iterator
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from decimal import Decimal
from typing import Any

from ..clock import SimClock
from ..execution.oms import OrderManager
from ..execution.paper import PaperBroker
from ..portfolio import Portfolio
from ..risk import RiskEngine, RiskState
from ..strategy import StrategyBase, StrategyContext
from ..types import Bar, BookSnapshot, Fill, Funding, Intent, TargetSpec, TradeTick
from .fills import FillSimulator, synthetic_book

log = logging.getLogger(__name__)

Event = Bar | TradeTick | BookSnapshot | Funding


@dataclass
class BacktestResult:
    equity_curve: list[tuple[datetime, float]] = field(default_factory=list)
    fills: list[Fill] = field(default_factory=list)
    intents: list[Intent] = field(default_factory=list)
    vetoes: int = 0
    risk_flattens: list[tuple[datetime, str]] = field(default_factory=list)
    attribution: dict[str, dict[str, float]] = field(default_factory=dict)
    starting_equity: float = 0.0

    @property
    def returns(self) -> list[float]:
        eq = [e for _, e in self.equity_curve]
        return [(b / a - 1.0) for a, b in zip(eq, eq[1:]) if a > 0]

    @property
    def final_equity(self) -> float:
        return self.equity_curve[-1][1] if self.equity_curve else self.starting_equity


class EventDrivenBacktester:
    def __init__(self, *, starting_equity: Decimal = Decimal('100000'),
                 risk: RiskEngine | None = None,
                 simulator: FillSimulator | None = None,
                 risk_budget: float = 1.0,
                 params: dict[str, Any] | None = None) -> None:
        self.starting_equity = starting_equity
        self.risk = risk or RiskEngine()
        self.sim = simulator or FillSimulator()
        self.risk_budget = risk_budget
        self.params = params or {}
        self._books: dict[tuple[str, str], BookSnapshot] = {}

    async def run(self, strategy_cls: type[StrategyBase],
                  events: Iterable[Event], *, start: datetime) -> BacktestResult:
        clock = SimClock(start)
        portfolio = Portfolio(self.starting_equity)
        self._day = self._week = None
        self._day_start = self._week_start = None
        result = BacktestResult(starting_equity=float(self.starting_equity))

        broker = PaperBroker(clock, self._book_for, simulator=self.sim,
                             on_fill=None)
        oms = OrderManager(broker, clock)

        ctx = StrategyContext(
            strategy=strategy_cls.name, clock=clock,
            equity=self.starting_equity, risk_budget=self.risk_budget,
            positions=portfolio.strategy_positions(strategy_cls.name),
            marks=portfolio.marks, params=self.params)
        strategy = strategy_cls(ctx)
        await strategy.on_start()

        async def handle_fill(fill: Fill) -> None:
            portfolio.apply_fill(fill)
            result.fills.append(fill)
            await strategy.on_fill(fill)

        broker._on_fill = handle_fill          # noqa: SLF001 - wiring, not reaching in
        oms._on_fill = handle_fill             # noqa: SLF001

        # Bar-driven intents wait for the NEXT bar of their symbol and fill at its
        # open. A decision made on bar t's close cannot trade at bar t's close: by the
        # time the order reaches the venue, that price is gone.
        pending: dict[str, list[Intent]] = {}

        for event in self._ordered(events):
            if isinstance(event, Bar) and event.closed:
                queued = pending.pop(event.symbol, [])
                if queued:
                    latency = timedelta(milliseconds=self.sim.costs.latency_ms(self.sim.rng))
                    clock.advance_to(max(clock.now(), event.ts + latency))
                    self._books[(event.venue, event.symbol)] = synthetic_book(
                        event.open, venue=event.venue, symbol=event.symbol, ts=clock.now())
                    for intent in queued:
                        await self._route(intent, oms, portfolio, clock, result)
                    # The strategy must see these fills before it sees this bar. Without
                    # this refresh it would act on a position one bar stale.
                    ctx._positions = portfolio.strategy_positions(strategy.name)  # noqa: SLF001
                    ctx.equity = portfolio.equity

            clock.advance_to(max(clock.now(), self._event_ts(event)))
            intents = await self._dispatch(strategy, event, portfolio, clock)
            for intent in intents:
                result.intents.append(intent)
                if isinstance(event, Bar):
                    pending.setdefault(intent.symbol, []).append(intent)
                else:
                    await self._route(intent, oms, portfolio, clock, result)

            if isinstance(event, Bar):
                prev_equity = portfolio.equity
                self._roll_periods(event.close_ts, prev_equity)
                portfolio.mark_to_market(event.symbol, event.close)
                # The strategy's read-only view must track the portfolio.
                ctx._positions = portfolio.strategy_positions(strategy.name)  # noqa: SLF001
                ctx.equity = portfolio.equity
                result.equity_curve.append((clock.now(), float(portfolio.equity)))

                # Portfolio-level limits are checked on every mark, not only when a
                # strategy wants to trade — a breach must flatten an idle book too.
                self.risk.check_limits(self._state(portfolio, clock))
                reason = self.risk.take_flatten_request()
                if reason:
                    result.risk_flattens.append((clock.now(), reason))
                    for pos in portfolio.all_positions():
                        flat = Intent(strategy=pos.strategy or 'risk', venue=pos.venue,
                                      symbol=pos.symbol,
                                      target=TargetSpec(position=Decimal('0')),
                                      urgency='immediate', reason=f'risk: {reason}',
                                      created_at=clock.now()).with_id()
                        pending.setdefault(pos.symbol, []).append(flat)

        result.attribution = portfolio.attribution()
        return result

    # -- internals -------------------------------------------------------------

    async def _dispatch(self, strategy: StrategyBase, event: Event,
                        portfolio: Portfolio, clock: SimClock) -> list[Intent]:
        if isinstance(event, Bar):
            if not event.closed:
                return []
            return await strategy.on_bar(event)
        if isinstance(event, BookSnapshot):
            self._books[(event.venue, event.symbol)] = event
            return await strategy.on_book(event)
        if isinstance(event, TradeTick):
            return await strategy.on_trade(event)
        if isinstance(event, Funding):
            for pos in portfolio.all_positions():
                if pos.venue == event.venue and pos.symbol == event.symbol:
                    portfolio.apply_funding(event.venue, event.symbol, pos.strategy,
                                            event.rate,
                                            portfolio.marks.get(event.symbol, Decimal('0')))
            return await strategy.on_funding(event)
        return []

    def _roll_periods(self, ts: datetime, equity_before_mark: Decimal) -> None:
        """Track real UTC-day and ISO-week starting equity. Passing starting equity
        instead would make every drawdown-from-inception look like a daily loss."""
        day = ts.date()
        week = ts.isocalendar()[:2]
        if self._day != day:
            self._day, self._day_start = day, equity_before_mark
        if self._week != week:
            self._week, self._week_start = week, equity_before_mark

    def _state(self, portfolio: Portfolio, clock: SimClock) -> RiskState:
        return RiskState(
            equity=portfolio.equity, peak_equity=portfolio.peak_equity,
            day_start_equity=self._day_start or portfolio.starting_equity,
            week_start_equity=self._week_start or portfolio.starting_equity,
            positions=portfolio.all_positions(), marks=dict(portfolio.marks),
            now=clock.now())

    async def _route(self, intent: Intent, oms: OrderManager, portfolio: Portfolio,
                     clock: SimClock, result: BacktestResult) -> None:
        state = self._state(portfolio, clock)
        decision = self.risk.evaluate(intent, state)
        if not decision.approved:
            result.vetoes += 1
            return
        venue = intent.venue or 'sim'
        await oms.execute(intent, decision,
                          portfolio.position(venue, intent.symbol, intent.strategy), venue)

    def _book_for(self, venue: str, symbol: str) -> BookSnapshot | None:
        return self._books.get((venue, symbol))

    @staticmethod
    def _event_ts(event: Event) -> datetime:
        # A bar becomes *observable* at its close, not its open. Using `ts` here would
        # both rewind the clock (bar N+1 opens when bar N closes) and hand the strategy
        # a bar four hours before it existed.
        return event.close_ts if isinstance(event, Bar) else event.ts

    @staticmethod
    def _ordered(events: Iterable[Event]) -> Iterator[Event]:
        """Events must be replayed in timestamp order; out-of-order replay is a
        subtle, silent form of look-ahead."""
        buffered = sorted(
            events,
            key=lambda e: e.close_ts if isinstance(e, Bar) else e.ts)
        yield from buffered

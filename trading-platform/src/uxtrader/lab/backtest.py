"""Event-driven backtester.

It replays the recorded event stream through the *same* ``StrategyBase``, the *same*
``RiskEngine`` and the *same* ``OrderManager`` that run in production. The only
substitutions are ``SimClock`` for ``LiveClock`` and ``PaperBroker`` for the live broker.

Anything that is easy to get wrong is made structurally impossible instead:

* **Look-ahead** — the ``SimClock`` guard raises on any access to a future timestamp.
* **Same-bar fills** — a signal computed on a bar close cannot fill before that close
  plus a sampled latency.
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
from ..types import Bar, BookSnapshot, Fill, Funding, Intent, TradeTick
from .fills import FillSimulator, synthetic_book

log = logging.getLogger(__name__)

Event = Bar | TradeTick | BookSnapshot | Funding


@dataclass
class BacktestResult:
    equity_curve: list[tuple[datetime, float]] = field(default_factory=list)
    fills: list[Fill] = field(default_factory=list)
    intents: list[Intent] = field(default_factory=list)
    vetoes: int = 0
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
        result = BacktestResult(starting_equity=float(self.starting_equity))

        broker = PaperBroker(clock, self._book_for, simulator=self.sim,
                             on_fill=None)
        oms = OrderManager(broker, clock)

        ctx = StrategyContext(
            strategy=strategy_cls.name, clock=clock,
            equity=self.starting_equity, risk_budget=self.risk_budget,
            positions=portfolio.positions(), marks=portfolio.marks, params=self.params)
        strategy = strategy_cls(ctx)
        await strategy.on_start()

        async def handle_fill(fill: Fill) -> None:
            portfolio.apply_fill(fill)
            result.fills.append(fill)
            await strategy.on_fill(fill)

        broker._on_fill = handle_fill          # noqa: SLF001 - wiring, not reaching in
        oms._on_fill = handle_fill             # noqa: SLF001

        for event in self._ordered(events):
            clock.advance_to(self._event_ts(event))
            intents = await self._dispatch(strategy, event, portfolio, clock)
            for intent in intents:
                result.intents.append(intent)
                await self._route(intent, oms, portfolio, clock, result)

            if isinstance(event, Bar):
                portfolio.mark_to_market(event.symbol, event.close)
                # The strategy's read-only view must track the portfolio.
                ctx._positions = portfolio.positions()      # noqa: SLF001
                ctx.equity = portfolio.equity
                result.equity_curve.append((clock.now(), float(portfolio.equity)))

        result.attribution = portfolio.attribution()
        return result

    # -- internals -------------------------------------------------------------

    async def _dispatch(self, strategy: StrategyBase, event: Event,
                        portfolio: Portfolio, clock: SimClock) -> list[Intent]:
        if isinstance(event, Bar):
            if not event.closed:
                return []
            # Fills happen after the bar closes plus latency. Enforced here so no
            # strategy can accidentally transact at the price it just decided on.
            clock.advance_to(event.close_ts + timedelta(
                milliseconds=self.sim.costs.latency_ms(self.sim.rng)))
            self._books[(event.venue, event.symbol)] = synthetic_book(
                event.close, venue=event.venue, symbol=event.symbol, ts=clock.now())
            return await strategy.on_bar(event)
        if isinstance(event, BookSnapshot):
            self._books[(event.venue, event.symbol)] = event
            return await strategy.on_book(event)
        if isinstance(event, TradeTick):
            return await strategy.on_trade(event)
        if isinstance(event, Funding):
            pos = portfolio.position(event.venue, event.symbol)
            if not pos.is_flat:
                portfolio.apply_funding(event.venue, event.symbol, pos.strategy,
                                        event.rate,
                                        portfolio.marks.get(event.symbol, Decimal('0')))
            return await strategy.on_funding(event)
        return []

    async def _route(self, intent: Intent, oms: OrderManager, portfolio: Portfolio,
                     clock: SimClock, result: BacktestResult) -> None:
        state = RiskState(
            equity=portfolio.equity, peak_equity=portfolio.peak_equity,
            day_start_equity=portfolio.starting_equity,
            week_start_equity=portfolio.starting_equity,
            positions=portfolio.positions(), marks=dict(portfolio.marks))
        decision = self.risk.evaluate(intent, state)
        if not decision.approved:
            result.vetoes += 1
            return
        venue = intent.venue or 'sim'
        await oms.execute(intent, decision,
                          portfolio.position(venue, intent.symbol), venue)

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

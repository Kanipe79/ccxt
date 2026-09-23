"""Portfolio service — the single source of truth for positions and PnL.

Consumes fills, marks and funding; publishes a ``PortfolioSnapshot`` after every
change. Periodically reconciles against the venues and, on drift, publishes a kill
command: RB-02 says halt first, investigate second.
"""
from __future__ import annotations

import logging
from decimal import Decimal

from ..bus import Bus
from ..events import CONTROL_KILL, PORTFOLIO_SNAPSHOT, Control, PortfolioSnapshot
from ..portfolio import Portfolio
from ..types import Bar, Fill, Funding

log = logging.getLogger(__name__)


class PortfolioService:
    def __init__(self, bus: Bus, starting_equity: Decimal, *, clock=None) -> None:
        self.bus = bus
        self.clock = clock
        self.portfolio = Portfolio(starting_equity)
        self.seq = 0
        self._day = self._week = None
        self.day_start = self.week_start = starting_equity

    async def start(self) -> None:
        await self.bus.subscribe('fill.>', self._on_fill)
        await self.bus.subscribe('md.*.bar.>', self._on_bar)
        await self.bus.subscribe('md.*.funding.>', self._on_funding)

    def snapshot(self) -> PortfolioSnapshot:
        p = self.portfolio
        self.seq += 1
        return PortfolioSnapshot(
            seq=self.seq, equity=p.equity, peak_equity=p.peak_equity,
            day_start_equity=self.day_start, week_start_equity=self.week_start,
            positions=tuple(p.all_positions()), marks=dict(p.marks),
            attribution=p.attribution(),
            **({'ts': self.clock.now()} if self.clock is not None else {}))

    async def publish(self) -> None:
        await self.bus.publish(PORTFOLIO_SNAPSHOT, self.snapshot())

    async def _on_fill(self, _subject: str, fill) -> None:
        if not isinstance(fill, Fill):
            return
        self.portfolio.apply_fill(fill)
        await self.publish()

    async def _on_bar(self, _subject: str, bar) -> None:
        if not isinstance(bar, Bar) or not bar.closed:
            return
        # Roll UTC-day / ISO-week baselines BEFORE marking, exactly as the backtester
        # does, so live daily-loss numbers mean the same thing as backtest ones.
        equity = self.portfolio.equity
        day, week = bar.close_ts.date(), bar.close_ts.isocalendar()[:2]
        if self._day != day:
            self._day, self.day_start = day, equity
        if self._week != week:
            self._week, self.week_start = week, equity
        self.portfolio.mark_to_market(bar.symbol, bar.close)
        await self.publish()

    async def _on_funding(self, _subject: str, f) -> None:
        if not isinstance(f, Funding):
            return
        mark = self.portfolio.marks.get(f.symbol)
        paid = False
        for pos in self.portfolio.all_positions():
            if pos.venue == f.venue and pos.symbol == f.symbol and mark:
                self.portfolio.apply_funding(f.venue, f.symbol, pos.strategy, f.rate, mark)
                paid = True
        if paid:
            await self.publish()

    async def reconcile(self, broker, venues: list[str]) -> list[str]:
        drift: list[str] = []
        for venue in venues:
            remote = await broker.fetch_positions(venue)
            drift += [f'{venue}:{s}' for s in self.portfolio.reconcile(venue, remote)]
        if drift:
            await self.bus.publish(CONTROL_KILL, Control(
                command='kill', reason=f'reconciliation drift: {", ".join(drift)}'))
        return drift

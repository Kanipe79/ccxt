"""Risk service — the hard gate between intents and orders.

Every intent passes through ``RiskEngine.evaluate``; approved ones go to execution as
``ApprovedIntent``, vetoes are published for the dashboard and alerts. On every
portfolio snapshot it re-checks the drawdown ladder and, when a limit breaks, issues
flatten orders for the whole book itself — it does not wait for strategies to agree.
"""
from __future__ import annotations

import logging
from collections import defaultdict, deque
from datetime import timedelta
from decimal import Decimal

from ..bus import Bus
from ..clock import Clock, LiveClock
from ..events import (
    EXEC_ORDER, RISK_STATUS, RISK_VETO, ApprovedIntent, Control, FeedRecovered,
    PortfolioSnapshot, RiskStatus, StaleFeed,
)
from ..risk import RiskEngine, RiskState, VarModel
from ..types import Bar, Intent, RiskDecision, TargetSpec

log = logging.getLogger(__name__)


class RiskService:
    def __init__(self, bus: Bus, engine: RiskEngine | None = None, *,
                 clock: Clock | None = None,
                 clusters: dict[str, str] | None = None) -> None:
        self.bus = bus
        self.engine = engine or RiskEngine()
        self.clock = clock or LiveClock()
        self.clusters = clusters or {}
        self.snapshot: PortfolioSnapshot | None = None
        self.var_model = VarModel()
        self.var_99 = 0.0
        self.stale: set[str] = set()
        self._orders: dict[str, deque] = defaultdict(deque)
        self.vetoes = 0

    async def start(self) -> None:
        await self.bus.subscribe('intent.>', self._on_intent)
        await self.bus.subscribe('portfolio.snapshot', self._on_snapshot)
        await self.bus.subscribe('feed.stale', self._on_stale)
        await self.bus.subscribe('feed.recovered', self._on_recovered)
        await self.bus.subscribe('control.>', self._on_control)
        await self.bus.subscribe('md.*.bar.>', self._on_bar)

    async def _on_bar(self, _subject: str, bar) -> None:
        if isinstance(bar, Bar) and bar.closed:
            self.var_model.update(bar.symbol, float(bar.close), bar.close_ts)

    def state(self) -> RiskState | None:
        snap = self.snapshot
        if snap is None:
            return None
        now = self.clock.now()
        rate = {}
        for strategy, times in self._orders.items():
            while times and now - times[0] > timedelta(minutes=1):
                times.popleft()
            rate[strategy] = len(times)
        return RiskState(
            equity=snap.equity, peak_equity=snap.peak_equity,
            day_start_equity=snap.day_start_equity, week_start_equity=snap.week_start_equity,
            positions=list(snap.positions), marks=dict(snap.marks), clusters=self.clusters,
            stale_feeds=set(self.stale), orders_this_min=rate, var_99_1d=self.var_99, now=now)

    async def _on_intent(self, _subject: str, intent) -> None:
        if not isinstance(intent, Intent):
            return
        state = self.state()
        if state is None:
            # No portfolio truth yet: nothing may trade. Fail closed.
            decision = RiskDecision(intent_id=intent.intent_id, approved=False,
                                    note='no portfolio snapshot yet')
        else:
            decision = self.engine.evaluate(intent, state)
        if decision.approved:
            self._orders[intent.strategy].append(self.clock.now())
            await self.bus.publish(EXEC_ORDER, ApprovedIntent(intent=intent, decision=decision))
        else:
            self.vetoes += 1
            await self.bus.publish(RISK_VETO, decision)

    async def _on_snapshot(self, _subject: str, snap) -> None:
        if not isinstance(snap, PortfolioSnapshot):
            return
        if self.snapshot is not None and snap.seq <= self.snapshot.seq:
            return                                  # out-of-order redelivery
        self.snapshot = snap
        self.var_99 = self.var_model.var_99(list(snap.positions), snap.marks, snap.equity)
        state = self.state()
        if state is not None:
            self.engine.check_limits(state)
        reason = self.engine.take_flatten_request()
        if reason:
            await self.flatten_all(f'risk: {reason}')
        await self.publish_status()

    def status(self) -> RiskStatus:
        L = self.engine.limits
        k = self.engine.kill
        usage: dict[str, float] = {}
        snap = self.snapshot
        if snap is not None and snap.equity > 0:
            eq = float(snap.equity)
            gross = sum(abs(float(p.quantity * snap.marks.get(p.symbol, 0))) for p in snap.positions)
            net = sum(float(p.quantity * snap.marks.get(p.symbol, 0)) for p in snap.positions)
            usage = {
                'drawdown': float((snap.peak_equity - snap.equity) / snap.peak_equity)
                if snap.peak_equity > 0 else 0.0,
                'daily_loss': max(0.0, -float(snap.equity / snap.day_start_equity - 1))
                if snap.day_start_equity > 0 else 0.0,
                'weekly_loss': max(0.0, -float(snap.equity / snap.week_start_equity - 1))
                if snap.week_start_equity > 0 else 0.0,
                'gross_leverage': gross / eq,
                'net_leverage': abs(net) / eq,
                'var_99_1d': self.var_99,
            }
        return RiskStatus(
            kill_global=not k.global_armed,
            disabled_strategies=tuple(sorted(k.disabled_strategies)),
            disabled_venues=tuple(sorted(k.disabled_venues)),
            halted_until=self.engine.entries_halted_until,
            limits={'drawdown': L.dd_stop_at, 'drawdown_halve': L.dd_halve_at,
                    'daily_loss': L.max_daily_loss, 'weekly_loss': L.max_weekly_loss,
                    'gross_leverage': L.max_gross_leverage, 'net_leverage': L.max_net_leverage,
                    'var_99_1d': L.max_var_99_1d, 'risk_per_trade': L.max_risk_per_trade,
                    'asset_fraction': L.max_asset_fraction, 'venue_fraction': L.max_venue_fraction},
            usage=usage, stale_symbols=tuple(sorted(self.stale)))

    async def publish_status(self) -> None:
        await self.bus.publish(RISK_STATUS, self.status())

    async def flatten_all(self, reason: str) -> int:
        """Risk authors these itself, per book. They are reducing, so pre-approved."""
        snap = self.snapshot
        if snap is None:
            return 0
        n = 0
        for pos in snap.positions:
            intent = Intent(strategy=pos.strategy, venue=pos.venue, symbol=pos.symbol,
                            target=TargetSpec(position=Decimal('0')), urgency='immediate',
                            reason=reason, created_at=self.clock.now()).with_id()
            decision = RiskDecision(intent_id=intent.intent_id, approved=True,
                                    adjusted_position=Decimal('0'), note=reason)
            await self.bus.publish(EXEC_ORDER, ApprovedIntent(intent=intent, decision=decision))
            n += 1
        log.critical('flatten_all reason=%s positions=%d', reason, n)
        return n

    async def _on_stale(self, _subject: str, msg) -> None:
        if isinstance(msg, StaleFeed):
            self.stale.add(msg.symbol)
            await self.publish_status()

    async def _on_recovered(self, _subject: str, msg) -> None:
        if isinstance(msg, FeedRecovered):
            self.stale.discard(msg.symbol)
            await self.publish_status()

    async def _on_control(self, _subject: str, cmd) -> None:
        if not isinstance(cmd, Control):
            return
        log.critical('control command=%s reason=%s strategy=%s venue=%s',
                     cmd.command, cmd.reason, cmd.strategy, cmd.venue)
        if cmd.command == 'kill':
            self.engine.kill.fire(cmd.reason, strategy=cmd.strategy, venue=cmd.venue)
            if cmd.strategy is None and cmd.venue is None:
                await self.flatten_all(f'kill: {cmd.reason}')
        elif cmd.command == 'flatten':
            await self.flatten_all(f'operator: {cmd.reason}')
        elif cmd.command == 'rearm':
            self.engine.kill.rearm(strategy=cmd.strategy, venue=cmd.venue)
        await self.publish_status()

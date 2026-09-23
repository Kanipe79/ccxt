"""Prometheus metrics derived from bus events.

Metric names match ``ops/grafana/alerts.yml``. One ``MetricsService`` per process; pass
in-process objects (rate limiters, feed monitors) for the gauges only they can see.
"""
from __future__ import annotations

from typing import Any

from prometheus_client import CollectorRegistry, Counter, Gauge, Histogram, start_http_server

from ..bus import Bus
from ..events import Control, PortfolioSnapshot
from ..types import Fill, Order, OrderState, RiskDecision

SLIPPAGE_BUCKETS = (-20, -10, -5, -2, 0, 2, 5, 10, 20, 50, 100)


class MetricsService:
    def __init__(self, bus: Bus, registry: CollectorRegistry | None = None, *,
                 slippage_model_bps: float = 3.0) -> None:
        self.bus = bus
        r = self.registry = registry or CollectorRegistry()
        self.equity = Gauge('ux_equity', 'Account equity', registry=r)
        self.drawdown = Gauge('ux_drawdown', 'Peak-to-trough drawdown fraction', registry=r)
        self.daily_pnl = Gauge('ux_daily_pnl_pct', 'PnL since UTC midnight, fraction', registry=r)
        self.position = Gauge('ux_position_notional', 'Signed notional per book',
                              ['strategy', 'symbol'], registry=r)
        self.fills = Counter('ux_fills_total', 'Fills', ['strategy', 'liquidity'], registry=r)
        self.slippage = Histogram('ux_slippage_bps', 'Fill slippage vs arrival (bps, + = cost)',
                                  ['strategy'], buckets=SLIPPAGE_BUCKETS, registry=r)
        # prometheus_client drops `_sum` from histograms that have negative buckets, and
        # slippage IS negative on price improvement — so the mean needs its own series:
        #   mean = ux_slippage_bps_signed_sum / ux_slippage_bps_count
        self.slippage_sum = Gauge('ux_slippage_bps_signed_sum', 'Σ signed slippage (bps)',
                                  ['strategy'], registry=r)
        self.slippage_model = Gauge('ux_slippage_model_bps', 'Modelled slippage', registry=r)
        self.slippage_model.set(slippage_model_bps)
        self.submitted = Counter('ux_orders_submitted_total', 'Orders sent', ['strategy'], registry=r)
        self.rejected = Counter('ux_orders_rejected_total', 'Orders rejected', ['strategy'], registry=r)
        self.vetoes = Counter('ux_risk_vetoes_total', 'Risk vetoes', ['limit'], registry=r)
        self.kill = Gauge('ux_kill_switch_active', 'Kill switch fired', ['scope'], registry=r)
        self.feed_age = Gauge('ux_feed_age_seconds', 'Seconds since last message', ['feed'], registry=r)
        self.ratelimit = Gauge('ux_ratelimit_utilisation', 'Fraction of weight budget used',
                               ['venue'], registry=r)
        self._monitors: list[Any] = []
        self._limiters: list[Any] = []
        self._known_books: set[tuple[str, str]] = set()

    def watch(self, *, monitors: list[Any] = (), limiters: list[Any] = ()) -> None:
        self._monitors.extend(monitors)
        self._limiters.extend(limiters)

    def refresh(self) -> None:
        """Pull gauges from in-process objects. Call before each scrape or on a timer."""
        for m in self._monitors:
            for key, h in m.snapshot().items():
                self.feed_age.labels(key).set(h['age'])
        for lim in self._limiters:
            self.ratelimit.labels(lim.venue).set(lim.utilisation)

    def serve(self, port: int = 9100) -> None:
        start_http_server(port, registry=self.registry)

    async def start(self) -> None:
        await self.bus.subscribe('portfolio.snapshot', self._on_snapshot)
        await self.bus.subscribe('fill.>', self._on_fill)
        await self.bus.subscribe('exec.report', self._on_report)
        await self.bus.subscribe('risk.veto', self._on_veto)
        await self.bus.subscribe('control.>', self._on_control)

    async def _on_snapshot(self, _s: str, snap) -> None:
        if not isinstance(snap, PortfolioSnapshot):
            return
        self.equity.set(float(snap.equity))
        if snap.peak_equity > 0:
            self.drawdown.set(float((snap.peak_equity - snap.equity) / snap.peak_equity))
        if snap.day_start_equity > 0:
            self.daily_pnl.set(float(snap.equity / snap.day_start_equity - 1))
        live = set()
        for p in snap.positions:
            mark = snap.marks.get(p.symbol)
            if mark is not None:
                self.position.labels(p.strategy, p.symbol).set(float(p.quantity * mark))
                live.add((p.strategy, p.symbol))
        for gone in self._known_books - live:              # closed books read as zero
            self.position.labels(*gone).set(0)
        self._known_books = live | self._known_books

    async def _on_fill(self, _s: str, f) -> None:
        if isinstance(f, Fill):
            self.fills.labels(f.strategy, 'maker' if f.is_maker else 'taker').inc()
            if f.slippage_bps is not None:
                self.slippage.labels(f.strategy).observe(f.slippage_bps)
                self.slippage_sum.labels(f.strategy).inc(f.slippage_bps)

    async def _on_report(self, _s: str, o) -> None:
        if isinstance(o, Order):
            self.submitted.labels(o.strategy).inc()
            if o.state is OrderState.REJECTED:
                self.rejected.labels(o.strategy).inc()

    async def _on_veto(self, _s: str, d) -> None:
        if isinstance(d, RiskDecision) and not d.approved:
            self.vetoes.labels(d.breaches[0].limit if d.breaches else 'other').inc()

    async def _on_control(self, _s: str, c) -> None:
        if isinstance(c, Control):
            scope = c.strategy or c.venue or 'global'
            if c.command == 'kill':
                self.kill.labels(scope).set(1)
            elif c.command == 'rearm':
                self.kill.labels(scope).set(0)

"""Fast backtester for parameter search — floats and arrays instead of events.

The event-driven engine is the one you *decide* with. This one is the one you *search*
with: no Pydantic models, no Decimal, no bus, no OMS — just a float loop over arrays.
The measured speed-up is asserted (not assumed) by
``tests/integration/test_cross_validation.py``.

It is only trustworthy because it is held to the cross-validation obligation in
docs/02 §3.4: on the same data it must reproduce the event engine's PnL within 2%.
``tests/integration/test_cross_validation.py`` enforces that. To make agreement
achievable it mirrors the event engine's semantics exactly:

* a target decided on bar t's close is executed at bar t+1's open, walking the same
  synthetic book (``fills.synthetic_book``) and paying the same taker fee;
* the same ``FillSimulator`` RNG is consumed in the same order (latency draw per
  execution batch, reject draw per market fill), so rejects line up;
* the REAL ``RiskEngine`` evaluates every target — sharing the risk code is deliberate:
  risk decisions are rare (one per trade), so this costs nothing, and a second
  hand-written copy of the risk ladder would be a second place for it to be wrong;
* equity is marked only at closes, day/week baselines roll on UTC boundaries, and
  risk-requested flattens are queued exactly as the event engine queues them.

What it deliberately does NOT model: order books, limit-order queues, funding, and
multiple symbols. Strategies that depend on those (S1, S3, S5) are validated on the
event engine only.
"""
from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from decimal import Decimal

import numpy as np
import pandas as pd

from ..risk import RiskEngine, RiskState
from ..types import AlgoSpec, Intent, Position, TargetSpec
from .fills import FillSimulator

Decide = Callable[[int, float, float], float | None]


def _d(x: float) -> Decimal:
    # float() first: under NumPy 2, repr(np.float64) is 'np.float64(…)'.
    return Decimal(repr(float(x)))
# decide(bar_index, equity_for_sizing, current_position) -> new target, or None


@dataclass
class VectorResult:
    equity: np.ndarray
    position: np.ndarray
    fills: list[tuple[int, float, float, float]] = field(default_factory=list)
    # (bar_index, qty_signed, avg_price, fee)
    vetoes: int = 0
    risk_flattens: list[tuple[int, str]] = field(default_factory=list)

    @property
    def returns(self) -> np.ndarray:
        e = self.equity
        return e[1:] / e[:-1] - 1.0

    @property
    def final_equity(self) -> float:
        return float(self.equity[-1])


class VectorBacktester:
    def __init__(self, *, starting_equity: float = 100_000.0,
                 risk: RiskEngine | None = None,
                 simulator: FillSimulator | None = None,
                 spread_bps: float = 2.0, level_size: float = 5.0,
                 level_step_bps: float = 1.0) -> None:
        self.starting_equity = starting_equity
        self.risk = risk or RiskEngine()
        self.sim = simulator or FillSimulator()
        # Must match fills.synthetic_book's defaults for the cross-check to hold.
        self.half_spread = spread_bps / 2 / 1e4
        self.level_size = level_size
        self.level_step = level_step_bps / 1e4

    def _walk(self, mid: float, qty: float, buy: bool) -> float:
        """Average price walking the synthetic ladder — same shape as synthetic_book."""
        remaining, cost, i = qty, 0.0, 0
        sign = 1.0 if buy else -1.0
        while remaining > 1e-15 and i < 10:
            px = mid * (1 + sign * (self.half_spread + i * self.level_step))
            take = min(remaining, self.level_size)
            cost += take * px
            remaining -= take
            i += 1
        filled = qty - max(remaining, 0.0)
        return cost / filled if filled > 0 else mid

    def run(self, bars: pd.DataFrame, decide: Decide, *, symbol: str = 'SIM',
            venue: str = 'sim', strategy: str = 'vector', timeframe_hours: float = 4.0
            ) -> VectorResult:
        o = bars['open'].to_numpy(float)
        c = bars['close'].to_numpy(float)
        ts_idx = pd.to_datetime(bars['ts'], utc=True)
        ts = [x.to_pydatetime() for x in ts_idx]
        close_idx = pd.DatetimeIndex(ts_idx) + pd.Timedelta(hours=timeframe_hours)
        # Period keys precomputed once: per-bar date()/isocalendar() calls were a
        # measurable share of the loop.
        # Via Timedelta.days, not asi8: asi8's unit (ns vs us) varies across pandas
        # versions, and dividing by the wrong constant silently merges days.
        day_key = (close_idx.normalize() - pd.Timestamp('1970-01-01', tz='UTC')).days.tolist()
        iso = close_idx.isocalendar()
        week_key = (iso['year'].to_numpy() * 100 + iso['week'].to_numpy()).tolist()
        o = o.tolist()
        c = c.tolist()
        n = len(c)
        bar_len = timedelta(hours=timeframe_hours)
        taker = self.sim.costs.taker_fee_bps / 1e4

        equity = self.starting_equity           # marked at the last close
        peak = equity
        pos = 0.0
        mark: float | None = None
        pending: list[tuple[float, str]] = []   # (target, strategy)
        day = week = None
        day_start = week_start = equity
        now = ts[0] if n else datetime.now(timezone.utc)

        eq_curve = np.empty(n)
        pos_curve = np.empty(n)
        out = VectorResult(equity=eq_curve, position=pos_curve)

        for t in range(n):
            # 1. execute queued targets at this bar's open
            if pending:
                latency = timedelta(milliseconds=self.sim.costs.latency_ms(self.sim.rng))
                now = max(now, ts[t] + latency)
                for target, strat in pending:
                    state = self._state(equity, peak, day_start, week_start, pos,
                                        mark, symbol, venue, strat, now)
                    intent = Intent(strategy=strat, symbol=symbol,
                                    target=TargetSpec(position=_d((target))),
                                    algo=AlgoSpec(), reason='vector').with_id()
                    decision = self.risk.evaluate(intent, state)
                    if not decision.approved or decision.adjusted_position is None:
                        out.vetoes += 1
                        continue
                    delta = float(decision.adjusted_position) - pos
                    if delta == 0:
                        continue
                    if self.sim.rng.random() < self.sim.costs.reject_rate:
                        continue                        # venue reject, as in FillSimulator
                    avg = self._walk(o[t], abs(delta), delta > 0)
                    fee = avg * abs(delta) * taker
                    ref = mark if mark is not None else o[t]
                    equity -= delta * (avg - ref) + fee
                    pos += delta
                    out.fills.append((t, delta, avg, fee))
                pending = []

            # 2. strategy decides on this bar's close
            close_ts = ts[t] + bar_len
            if close_ts > now:
                now = close_ts
            if day != day_key[t]:
                day, day_start = day_key[t], equity
            if week != week_key[t]:
                week, week_start = week_key[t], equity
            target = decide(t, equity, pos)
            if target is not None:
                pending.append((target, strategy))

            # 3. mark at the close
            if mark is not None:
                equity += pos * (c[t] - mark)
            mark = c[t]
            peak = max(peak, equity)
            eq_curve[t] = equity
            pos_curve[t] = pos

            # 4. portfolio-level limits, exactly as the event engine checks them —
            # but only building the (Decimal) risk state when a limit is within reach.
            # The float pre-check is deliberately conservative (0.1% margin); the
            # decision itself is still made by the real RiskEngine.
            if self._near_limit(equity, peak, day_start, week_start):
                state = self._state(equity, peak, day_start, week_start, pos, mark,
                                    symbol, venue, strategy, now)
                self.risk.check_limits(state)
            reason = self.risk.take_flatten_request()
            if reason and pos != 0:
                out.risk_flattens.append((t, reason))
                pending.append((0.0, strategy))
        return out

    def _near_limit(self, equity: float, peak: float, day_start: float,
                    week_start: float, margin: float = 0.001) -> bool:
        L = self.risk.limits
        if self.risk.entries_halted_until is not None or not self.risk.kill.global_armed:
            return True
        return ((peak > 0 and (peak - equity) / peak >= L.dd_stop_at - margin)
                or (week_start > 0 and equity / week_start - 1 <= -L.max_weekly_loss + margin)
                or (day_start > 0 and equity / day_start - 1 <= -L.max_daily_loss + margin))

    @staticmethod
    def _state(equity, peak, day_start, week_start, pos, mark, symbol, venue,
               strategy, now) -> RiskState:
        positions = {}
        if pos != 0:
            positions[symbol] = Position(venue=venue, symbol=symbol, strategy=strategy,
                                         quantity=_d((pos)))
        return RiskState(
            equity=_d((equity)), peak_equity=_d((peak)),
            day_start_equity=_d((day_start)),
            week_start_equity=_d((week_start)),
            positions=positions,
            marks={symbol: _d((mark))} if mark is not None else {},
            now=now)

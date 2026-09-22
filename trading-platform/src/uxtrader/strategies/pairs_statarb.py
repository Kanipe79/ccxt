"""S3 — Cointegration pairs / statistical arbitrage. Spec: docs/01-strategies.md §S3.

Kalman-filtered hedge ratio, z-score entries, and — the part that actually matters — an
ADF-breakdown blacklist. Cointegration is not a law of nature: a hack, a chain
migration, or a tokenomics change can permanently re-rate one leg. The high win rate
(62–70%) makes the losses feel like anomalies. They are not; they are the distribution.

The hedge ratio uses a Kalman filter rather than rolling OLS for a specific reason:
rolling OLS produces artificial jumps in beta when an outlier drops out of the back of
the window, and those jumps generate phantom signals. The filter degrades gracefully.

Never re-fit the window mid-trade beyond the Kalman update. That is look-ahead
laundering, and it will make your backtest beautiful and your live account poor.
"""
from __future__ import annotations

import logging
import math
from collections import deque
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from decimal import Decimal

from ..strategy import StrategyBase, StrategyContext
from ..types import AlgoKind, AlgoSpec, Bar, Intent

log = logging.getLogger(__name__)


class KalmanHedge:
    """State-space: log(P_a) = beta·log(P_b) + alpha + noise, beta/alpha random walks.

    ``delta`` is tuned so the implied beta half-life is ≈ 20 days: slow enough to be
    stable, fast enough to track a genuine structural shift.
    """

    def __init__(self, delta: float = 1e-5, obs_var: float = 1e-3) -> None:
        self.beta = 1.0
        self.alpha = 0.0
        self.P = [[1.0, 0.0], [0.0, 1.0]]
        self.Q = delta / (1 - delta)
        self.R = obs_var

    def update(self, log_a: float, log_b: float) -> float:
        x = [log_b, 1.0]
        for i in range(2):
            self.P[i][i] += self.Q

        pred = self.beta * x[0] + self.alpha * x[1]
        resid = log_a - pred

        px = [self.P[0][0] * x[0] + self.P[0][1] * x[1],
              self.P[1][0] * x[0] + self.P[1][1] * x[1]]
        s = x[0] * px[0] + x[1] * px[1] + self.R
        if s <= 0:
            return resid
        k = [px[0] / s, px[1] / s]

        self.beta += k[0] * resid
        self.alpha += k[1] * resid
        new_p = [[self.P[i][j] - k[i] * px[j] for j in range(2)] for i in range(2)]
        self.P = new_p
        return resid


@dataclass
class PairState:
    a: str
    b: str
    kalman: KalmanHedge = field(default_factory=KalmanHedge)
    spreads: deque[float] = field(default_factory=lambda: deque(maxlen=240))
    entry_z: float | None = None
    units: int = 0
    opened_at: datetime | None = None
    half_life_h: float = 24.0
    adf_p: float = 0.0
    adf_breaches: int = 0
    blacklisted_until: datetime | None = None
    last_a: float | None = None
    last_b: float | None = None


class PairsStatArb(StrategyBase):
    name = 'pairs_statarb'
    timeframe = '1h'
    warmup_bars = 260

    ENTRY_Z = 2.0
    SCALE_Z = (2.5, 3.0)
    EXIT_Z = 0.3
    HARD_STOP_Z = 4.0
    MAX_UNITS = 3
    MAX_PAIRS = 6
    RISK_PER_PAIR = 0.0075
    TIME_STOP_HALF_LIVES = 5.0
    ADF_BREAKDOWN_P = 0.10
    ADF_BREACHES_TO_BLACKLIST = 3
    BLACKLIST_DAYS = 30

    def __init__(self, ctx: StrategyContext) -> None:
        super().__init__(ctx)
        self._pairs: dict[str, PairState] = {}

    def add_pair(self, a: str, b: str, *, half_life_h: float, adf_p: float) -> None:
        """Only call this for pairs that passed the full qualification in docs/01 §S3:
        two-window ADF, half-life in [6h, 10d], spread correlation < 0.5 with every
        held pair, both legs > $30M volume, funding drag < 30% of expected PnL."""
        key = f'{a}|{b}'
        self._pairs[key] = PairState(a=a, b=b, half_life_h=half_life_h, adf_p=adf_p)

    def update_adf(self, key: str, p_value: float, now: datetime) -> list[Intent]:
        """Daily cointegration health check. Three consecutive failures ⇒ blacklist."""
        st = self._pairs.get(key)
        if st is None:
            return []
        st.adf_p = p_value
        if p_value > self.ADF_BREAKDOWN_P:
            st.adf_breaches += 1
            if st.adf_breaches >= self.ADF_BREACHES_TO_BLACKLIST:
                st.blacklisted_until = now + timedelta(days=self.BLACKLIST_DAYS)
                log.warning('s3_cointegration_broken pair=%s p=%.3f — blacklisted', key, p_value)
                return self._close(st, 'cointegration breakdown')
        else:
            st.adf_breaches = 0
        return []

    async def on_bar(self, bar: Bar) -> list[Intent]:
        if not bar.closed:
            return []
        intents: list[Intent] = []
        for key, st in self._pairs.items():
            if bar.symbol == st.a:
                st.last_a = float(bar.close)
            elif bar.symbol == st.b:
                st.last_b = float(bar.close)
            else:
                continue
            if st.last_a is None or st.last_b is None:
                continue
            intents.extend(self._evaluate(key, st, bar.close_ts))
        return intents

    def _evaluate(self, key: str, st: PairState, now: datetime) -> list[Intent]:
        if st.last_a is None or st.last_b is None or st.last_a <= 0 or st.last_b <= 0:
            return []
        if st.blacklisted_until and now < st.blacklisted_until:
            return []

        spread = st.kalman.update(math.log(st.last_a), math.log(st.last_b))
        st.spreads.append(spread)
        if len(st.spreads) < st.spreads.maxlen:
            return []

        mu = sum(st.spreads) / len(st.spreads)
        sd = (sum((v - mu) ** 2 for v in st.spreads) / (len(st.spreads) - 1)) ** 0.5
        if sd <= 0:
            return []
        z = (spread - mu) / sd

        if st.units != 0:
            return self._manage(key, st, z, sd, now)
        if len(self._open_pairs()) >= self.MAX_PAIRS:
            return []
        if abs(z) >= self.ENTRY_Z:
            return self._open(key, st, z, sd, now)
        return []

    def _open(self, key: str, st: PairState, z: float, sd: float,
              now: datetime) -> list[Intent]:
        st.entry_z = z
        st.units = 1
        st.opened_at = now
        return self._legs(key, st, z, sd, units=1,
                          reason=f'entry z={z:.2f} beta={st.kalman.beta:.3f}')

    def _manage(self, key: str, st: PairState, z: float, sd: float,
                now: datetime) -> list[Intent]:
        # Hard stop first, always. No averaging down past MAX_UNITS, no exceptions.
        if abs(z) >= self.HARD_STOP_Z:
            log.warning('s3_hard_stop pair=%s z=%.2f', key, z)
            return self._close(st, f'hard stop z={z:.2f}')

        if abs(z) <= self.EXIT_Z:
            return self._close(st, f'reverted to mean z={z:.2f}')

        if st.opened_at is not None:
            held_h = (now - st.opened_at).total_seconds() / 3600.0
            if held_h > self.TIME_STOP_HALF_LIVES * st.half_life_h:
                return self._close(st, f'time stop after {held_h:.0f}h')

        # Scale in, same direction only.
        if st.entry_z is not None and st.units < self.MAX_UNITS:
            same_side = (z > 0) == (st.entry_z > 0)
            level = self.SCALE_Z[st.units - 1] if st.units <= len(self.SCALE_Z) else None
            if same_side and level is not None and abs(z) >= level:
                st.units += 1
                return self._legs(key, st, z, sd, units=st.units,
                                  reason=f'scale to {st.units}u at z={z:.2f}')
        return []

    def _legs(self, key: str, st: PairState, z: float, sd: float, units: int,
              reason: str) -> list[Intent]:
        """Position sized so the distance to the hard stop is RISK_PER_PAIR of equity."""
        if st.last_a is None or st.last_b is None or st.entry_z is None:
            return []
        stop_distance = abs(self.HARD_STOP_Z - abs(st.entry_z)) * sd
        if stop_distance <= 0:
            return []
        risk = float(self.ctx.equity) * self.RISK_PER_PAIR
        notional_a = risk / stop_distance
        notional_b = notional_a * abs(st.kalman.beta)

        direction = -1 if z > 0 else 1       # z>0 ⇒ A rich ⇒ short A, long B
        qty_a = Decimal(str(direction * units * notional_a / st.last_a))
        qty_b = Decimal(str(-direction * units * notional_b / st.last_b))
        algo = AlgoSpec(kind=AlgoKind.POST_ONLY_PEG, cross_after_s=600)
        return [
            self.target(st.a, qty_a, reason=f'{key} A: {reason}', algo=algo),
            self.target(st.b, qty_b, reason=f'{key} B: {reason}', algo=algo),
        ]

    def _close(self, st: PairState, reason: str) -> list[Intent]:
        st.units = 0
        st.entry_z = None
        st.opened_at = None
        return [self.flatten(st.a, reason), self.flatten(st.b, reason)]

    def _open_pairs(self) -> list[str]:
        return [k for k, st in self._pairs.items() if st.units != 0]


def ou_half_life(spread: list[float]) -> float:
    """OU half-life in bars via the AR(1) coefficient of Δspread on lagged spread.

    Qualification filter: only trade pairs whose half-life is between 6 hours and 10
    days. Faster and you are fighting fees; slower and funding eats the trade while your
    capital sits dead.
    """
    if len(spread) < 30:
        return float('inf')
    lagged = spread[:-1]
    delta = [b - a for a, b in zip(spread[:-1], spread[1:])]
    mean_x = sum(lagged) / len(lagged)
    mean_y = sum(delta) / len(delta)
    cov = sum((x - mean_x) * (y - mean_y) for x, y in zip(lagged, delta))
    var = sum((x - mean_x) ** 2 for x in lagged)
    if var == 0:
        return float('inf')
    theta = cov / var
    if theta >= 0:
        return float('inf')        # not mean-reverting
    return -math.log(2) / theta

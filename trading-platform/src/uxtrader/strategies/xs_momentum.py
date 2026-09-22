"""S2 — Cross-sectional momentum rotation. Spec: docs/01-strategies.md §S2.

Long the top quintile, short the bottom quintile of a point-in-time universe, ranked by
vol-scaled 30d and 90d returns with a 3-day skip for short-term reversal.

Two implementation details carry most of the real-world performance, and neither is the
signal:

* **The point-in-time universe.** A backtest on today's top-40 overstates returns by an
  estimated 15 – 40% annualized — larger than most strategies' entire edge. The universe
  must be supplied by the caller from stored snapshots, which is why it is a parameter
  and not something this class computes.
* **The no-trade band.** Only rebalancing a name when its target weight moves more than
  25% relative cuts turnover ~40% for roughly 5% of gross return. That is one of the
  best trades available anywhere in this system.
"""
from __future__ import annotations

import logging
import math
from collections import defaultdict, deque
from decimal import Decimal

from ..strategy import StrategyBase, StrategyContext
from ..types import AlgoKind, AlgoSpec, Bar, Intent

log = logging.getLogger(__name__)


class CrossSectionalMomentum(StrategyBase):
    name = 'xs_momentum'
    timeframe = '1d'
    warmup_bars = 100

    SKIP_DAYS = 3              # short-term reversal
    LOOKBACK_SHORT = 30
    LOOKBACK_LONG = 90
    QUINTILE = 0.20
    NO_TRADE_BAND = 0.25       # relative weight change required to act
    VOL_TARGET = 0.15
    REBALANCE_WEEKDAY = 0      # Monday 00:00 UTC

    def __init__(self, ctx: StrategyContext) -> None:
        super().__init__(ctx)
        self._closes: dict[str, deque[float]] = defaultdict(
            lambda: deque(maxlen=self.LOOKBACK_LONG + self.SKIP_DAYS + 5))
        self._universe: list[str] = []
        self._last_rebalance_date = None
        self._regime_scalar = 1.0
        self._funding_apr: dict[str, float] = {}
        self._strategy_peak = Decimal('0')

    def set_universe(self, symbols: list[str]) -> None:
        """Point-in-time universe for the current date, supplied by data/universe.py.

        Must include delisted names with their real final prices. A universe that only
        contains survivors makes the short leg look far worse than it was and the long
        leg far better.
        """
        self._universe = list(symbols)

    def set_regime(self, *, btc_ema50: float, btc_ema200: float,
                   median_correlation: float, dispersion_pct: float) -> None:
        """Asymmetric response: scale DOWN on bad regimes, never up on good ones."""
        scalar = 1.0
        if btc_ema50 < btc_ema200:
            scalar *= 0.5
        if median_correlation > 0.85:
            scalar *= 0.5            # momentum is just beta here
        self._regime_scalar = scalar
        self._dispersion_pct = dispersion_pct

    async def on_bar(self, bar: Bar) -> list[Intent]:
        if not bar.closed:
            return []
        self._closes[bar.symbol].append(float(bar.close))

        ts = bar.close_ts
        if ts.weekday() != self.REBALANCE_WEEKDAY or ts.hour != 0:
            return []
        if self._last_rebalance_date == ts.date():
            return []
        self._last_rebalance_date = ts.date()

        if getattr(self, '_dispersion_pct', 50.0) < 25.0:
            log.info('s2_low_dispersion skipping rebalance')
            return []
        return self._rebalance()

    # -- the signal -------------------------------------------------------------

    def _score(self, symbol: str) -> float | None:
        closes = self._closes.get(symbol)
        need = self.LOOKBACK_LONG + self.SKIP_DAYS
        if closes is None or len(closes) < need:
            return None
        px = list(closes)
        skip = self.SKIP_DAYS
        p_now = px[-1 - skip]
        r30 = math.log(p_now / px[-1 - skip - self.LOOKBACK_SHORT])
        r90 = math.log(p_now / px[-1 - skip - self.LOOKBACK_LONG])
        s30 = self._realized_vol(px, self.LOOKBACK_SHORT)
        s90 = self._realized_vol(px, self.LOOKBACK_LONG)
        if s30 <= 0 or s90 <= 0:
            return None
        # Vol-scaling the returns BEFORE ranking is what turns a beta bet into a factor
        # bet. Ranking raw returns just ranks by volatility.
        return 0.5 * (r30 / s30) + 0.5 * (r90 / s90)

    @staticmethod
    def _realized_vol(px: list[float], window: int) -> float:
        rets = [math.log(b / a) for a, b in zip(px[-window - 1:-1], px[-window:])
                if a > 0 and b > 0]
        if len(rets) < 2:
            return 0.0
        m = sum(rets) / len(rets)
        return (sum((r - m) ** 2 for r in rets) / (len(rets) - 1)) ** 0.5 * math.sqrt(365)

    def _rebalance(self) -> list[Intent]:
        scored = [(s, v) for s in self._universe if (v := self._score(s)) is not None]
        if len(scored) < 10:
            log.warning('s2_universe_too_small n=%d', len(scored))
            return []

        scores = [v for _, v in scored]
        mean = sum(scores) / len(scores)
        sd = (sum((v - mean) ** 2 for v in scores) / max(1, len(scores) - 1)) ** 0.5
        if sd <= 0:
            return []
        z = {s: (v - mean) / sd for s, v in scored}

        ranked = sorted(z.items(), key=lambda kv: kv[1], reverse=True)
        n = max(1, int(len(ranked) * self.QUINTILE))
        longs = [s for s, _ in ranked[:n]]
        shorts = [s for s, _ in ranked[-n:]]

        # Drop shorts we would be paying heavily to hold.
        shorts = [s for s in shorts if self._funding_apr.get(s, 0.0) >= -0.25]

        gross = float(self.ctx.sleeve_equity) * self._regime_scalar
        intents: list[Intent] = []
        for leg, symbols, sign in (('long', longs, 1), ('short', shorts, -1)):
            if not symbols:
                continue
            # Inverse-vol weights within the leg: not equal-weight (ignores risk), not
            # score-weighted (overfits the score's cardinality).
            inv = {s: 1.0 / max(1e-6, self._realized_vol(list(self._closes[s]),
                                                         self.LOOKBACK_SHORT))
                   for s in symbols}
            total = sum(inv.values())
            for s in symbols:
                weight = inv[s] / total
                px = self._closes[s][-1]
                target_notional = gross * weight * sign
                target_qty = Decimal(str(target_notional / px))
                current = self.ctx.position(s)
                if self._within_band(current, target_qty):
                    continue
                intents.append(self.target(
                    s, target_qty,
                    reason=f'xs-mom {leg} z={z[s]:.2f} w={weight:.3f} regime={self._regime_scalar:.2f}',
                    algo=AlgoSpec(kind=AlgoKind.TWAP, duration_s=1800),
                    urgency='passive'))

        # Close anything no longer in either leg.
        keep = set(longs) | set(shorts)
        for s in list(self.ctx._positions):                 # noqa: SLF001 - read-only view
            if s not in keep and self.ctx.position(s) != 0:
                intents.append(self.flatten(s, 'dropped out of both quintiles'))
        return intents

    def _within_band(self, current: Decimal, target: Decimal) -> bool:
        """No-trade band: skip small adjustments. Worth ~40% of turnover."""
        if target == 0:
            return current == 0
        return abs(float((target - current) / target)) < self.NO_TRADE_BAND

    async def on_funding(self, funding) -> list[Intent]:
        self._funding_apr[funding.symbol] = funding.apr
        return []

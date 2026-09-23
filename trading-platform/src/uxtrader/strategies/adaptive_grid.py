"""S6 — Adaptive grid with trend filter and hard kill. Spec: docs/01-strategies.md §S6.

**A grid is a short-gamma, short-volatility position wearing a costume.** It sells small
amounts of optionality repeatedly, collects premium, and hands it all back in one trend.
Its 90% "win rate" is an artifact of measuring per-fill instead of per-position.

It is in this book at a 3% risk budget because it produces genuinely uncorrelated income
in range regimes. It is capped at 3% because ungated grids are the single most common way
retail bots blow up. **The kill switches below are not risk management bolted onto the
strategy — they are the strategy.** Removing them does not make it more profitable; it
makes it unbounded-loss.
"""
from __future__ import annotations

import logging
from decimal import Decimal

from ..strategy import StrategyBase, StrategyContext
from ..types import AlgoKind, AlgoSpec, Bar, Intent
from .indicators import ADX, ATR, EMA, RollingWindow

log = logging.getLogger(__name__)


class AdaptiveGrid(StrategyBase):
    name = 'adaptive_grid'
    timeframe = '1h'
    warmup_bars = 220

    SPACING_ATR_MULT = 0.6
    LEVELS_PER_SIDE = 12
    LEVEL_FRACTION = 0.0035        # of sleeve equity per level
    RECENTER_SPACINGS = 4
    MAX_INVENTORY_FRACTION = 0.25
    # kill thresholds
    ADX_MAX = 18.0
    BAND_EXIT_ATR_MULT = 2.5
    SLEEVE_DD_KILL = 0.06
    VOL_DOUBLE_KILL = 2.0
    MAX_FUNDING_APR = 0.10

    def __init__(self, ctx: StrategyContext) -> None:
        super().__init__(ctx)
        self._atr_h: dict[str, ATR] = {}
        self._atr_d: dict[str, ATR] = {}
        self._adx: dict[str, ADX] = {}
        self._ema50: dict[str, EMA] = {}
        self._ema200: dict[str, EMA] = {}
        self._bb: dict[str, RollingWindow] = {}
        self._center: dict[str, float] = {}
        self._active: dict[str, bool] = {}
        self._vol_24h_ago: dict[str, float] = {}
        self._funding_apr: dict[str, float] = {}
        self._sleeve_peak = Decimal('0')
        self._disabled_until = None

    async def on_start(self) -> None:
        for s in self.symbols:
            self._atr_h[s] = ATR(14)
            self._atr_d[s] = ATR(14)
            self._adx[s] = ADX(14)
            self._ema50[s] = EMA(50 * 24)
            self._ema200[s] = EMA(200 * 24)
            self._bb[s] = RollingWindow(20 * 24)
            self._active[s] = False

    async def on_funding(self, funding) -> list[Intent]:
        self._funding_apr[funding.symbol] = funding.apr
        return []

    async def on_bar(self, bar: Bar) -> list[Intent]:
        if not bar.closed or bar.symbol not in self._atr_h:
            return []
        s = bar.symbol
        high, low, close = float(bar.high), float(bar.low), float(bar.close)

        atr_h = self._atr_h[s].push(high, low, close)
        adx = self._adx[s].push(high, low, close)
        ema50 = self._ema50[s].push(close)
        ema200 = self._ema200[s].push(close)
        self._bb[s].push(close)

        if atr_h is None or adx is None or ema50 is None or ema200 is None:
            return []
        if not self._bb[s].ready:
            return []

        mean, sd = self._bb[s].mean(), self._bb[s].std()
        upper, lower = mean + 2 * sd, mean - 2 * sd
        position = self.ctx.position(s)

        # --- kill checks run BEFORE anything else, every bar, unconditionally ---
        kill = self._check_kills(s, close, upper, lower, atr_h, adx, ema50, ema200)
        if kill is not None:
            self._active[s] = False
            if position != 0:
                return [self.target(s, Decimal('0'), reason=f'GRID KILL: {kill}',
                                    algo=AlgoSpec(kind=AlgoKind.MARKET),
                                    urgency='immediate')]
            return []

        if not self._gate_open(s, close, upper, lower, adx, ema50, ema200):
            self._active[s] = False
            return []

        spacing = self.SPACING_ATR_MULT * atr_h
        center = self._center.get(s)
        if center is None or abs(close - center) > self.RECENTER_SPACINGS * spacing:
            self._center[s] = close
            self._active[s] = True
            log.info('s6_grid_recentered symbol=%s center=%.2f spacing=%.2f',
                     s, close, spacing)
        return self._grid_intents(s, close, spacing, position)

    # -- gates and kills --------------------------------------------------------

    def _gate_open(self, s: str, close: float, upper: float, lower: float,
                   adx: float, ema50: float, ema200: float) -> bool:
        if adx >= self.ADX_MAX:
            return False
        if not (lower <= close <= upper):
            return False
        if ema200 > 0 and abs(ema50 - ema200) / ema200 > 0.05:
            return False
        if abs(self._funding_apr.get(s, 0.0)) > self.MAX_FUNDING_APR:
            return False
        return True

    def _check_kills(self, s: str, close: float, upper: float, lower: float,
                     atr_h: float, adx: float, ema50: float,
                     ema200: float) -> str | None:
        atr_d = atr_h * 4.9                      # ≈ sqrt(24) scaling of hourly ATR
        if close > upper + self.BAND_EXIT_ATR_MULT * atr_d:
            return f'price above band by >{self.BAND_EXIT_ATR_MULT}xATR'
        if close < lower - self.BAND_EXIT_ATR_MULT * atr_d:
            return f'price below band by >{self.BAND_EXIT_ATR_MULT}xATR'
        if adx > 25.0:
            return f'ADX {adx:.1f} — trend established'
        prior = self._vol_24h_ago.get(s)
        if prior and atr_h > self.VOL_DOUBLE_KILL * prior:
            return 'realized vol doubled in 24h'
        self._vol_24h_ago[s] = atr_h
        sleeve = self.ctx.sleeve_equity
        if sleeve > self._sleeve_peak:
            self._sleeve_peak = sleeve
        if self._sleeve_peak > 0:
            dd = float((self._sleeve_peak - sleeve) / self._sleeve_peak)
            if dd > self.SLEEVE_DD_KILL:
                return f'sleeve drawdown {dd:.1%}'
        return None

    # -- grid -------------------------------------------------------------------

    def _grid_intents(self, s: str, close: float, spacing: float,
                      position: Decimal) -> list[Intent]:
        sleeve = float(self.ctx.sleeve_equity)
        level_notional = sleeve * self.LEVEL_FRACTION
        max_inventory = Decimal(str(sleeve * self.MAX_INVENTORY_FRACTION / close))
        center = self._center[s]

        # Target inventory is a linear function of distance from centre: buy as price
        # falls below it, sell as it rises. Equivalent to resting a ladder, but expressed
        # as a target position so it is idempotent under replay.
        offset_levels = (center - close) / spacing if spacing > 0 else 0.0
        capped = max(-self.LEVELS_PER_SIDE, min(self.LEVELS_PER_SIDE, offset_levels))
        target = Decimal(str(capped * level_notional / close))

        if abs(target) > max_inventory:
            target = max_inventory if target > 0 else -max_inventory

        tolerance = Decimal(str(level_notional / close)) / 2
        if abs(target - position) <= tolerance:
            return []
        return [self.target(
            s, target,
            reason=f'grid level {capped:+.1f} center={center:.2f} spacing={spacing:.2f}',
            algo=AlgoSpec(kind=AlgoKind.POST_ONLY_PEG, cross_after_s=None),
            urgency='passive', tolerance=tolerance)]

    async def on_stale(self, feed: str, age: float) -> list[Intent]:
        # Short vol: a stale feed means the grid cannot see the move that ends it.
        return [self.flatten(s, f'feed stale {age:.0f}s — short-vol book flattened')
                for s in self.symbols if self.ctx.position(s) != 0]

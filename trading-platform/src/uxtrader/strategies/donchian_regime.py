"""S4 — Donchian breakout with regime filter. The fully worked reference implementation.

Read this one first. Every other strategy in the package follows its shape:
streaming indicators, an explicit entry checklist, a stop that is computed before the
entry is taken, and target-position intents.

Why this strategy is the reference: **five parameters, and each one is there for a
reason you can state out loud.** Trend following has survived out-of-sample in every
liquid futures market since the 1970s precisely because there is so little to overfit.
Its 0.6 – 1.0 Sharpe is not a weakness to be engineered away — it is the price of that
robustness, and the six-to-eleven-month flat periods are what you are being paid to
tolerate. If you find yourself adding a sixth filter because the drawdown is
uncomfortable, you are converting a robust strategy into a fragile one.

Spec: docs/01-strategies.md §S4.
"""
from __future__ import annotations

import logging
from decimal import Decimal

from ..strategy import StrategyBase, StrategyContext
from ..types import AlgoKind, AlgoSpec, Bar, Fill, Funding, Intent, StopSpec
from .indicators import ADX, ATR, Donchian, EMA, RealizedVol, dec

log = logging.getLogger(__name__)

BARS_PER_YEAR_4H = 6 * 365


class DonchianRegime(StrategyBase):
    name = 'donchian_regime'
    timeframe = '4h'
    # The binding constraint is the daily EMA200: on 4h bars that is 200×6 = 1200
    # bars before the trend filter reports ready. Declaring less would let the
    # engine start a strategy that silently cannot signal.
    warmup_bars = 1250

    # --- the five parameters. Adding a sixth requires a reason and more data. ---
    CHANNEL = 55              # breakout lookback
    EXIT_CHANNEL = 20         # opposite-channel exit
    ATR_PERIOD = 14
    STOP_ATR_MULT = 2.5       # initial stop and chandelier trail distance
    RISK_FRACTION = 0.015     # S4 is the only strategy allowed 1.5%

    # --- filter thresholds. Set to avoid catastrophic entries, NOT to optimize hit rate.
    ADX_MIN = 20.0
    VOL_RATIO_BAND = (0.7, 1.8)
    RANGE_EXPANSION = 1.2
    MAX_FUNDING_APR = 0.60    # a breakout into extreme funding is frequently the top

    def __init__(self, ctx: StrategyContext) -> None:
        super().__init__(ctx)
        self._donchian: dict[str, Donchian] = {}
        self._exit_ch: dict[str, Donchian] = {}
        self._atr: dict[str, ATR] = {}
        self._adx: dict[str, ADX] = {}
        self._ema200_daily: dict[str, EMA] = {}
        self._vol_fast: dict[str, RealizedVol] = {}
        self._vol_slow: dict[str, RealizedVol] = {}
        self._funding_apr: dict[str, float] = {}
        # per-symbol trade state
        self._entry: dict[str, float] = {}
        self._stop: dict[str, float] = {}
        self._peak: dict[str, float] = {}
        self._adds: dict[str, int] = {}
        self._took_partial: dict[str, bool] = {}

    async def on_start(self) -> None:
        for symbol in self.symbols:
            self._donchian[symbol] = Donchian(self.CHANNEL)
            self._exit_ch[symbol] = Donchian(self.EXIT_CHANNEL)
            self._atr[symbol] = ATR(self.ATR_PERIOD)
            self._adx[symbol] = ADX(14)
            self._ema200_daily[symbol] = EMA(200 * 6)      # 200 daily ≈ 1200 × 4h
            self._vol_fast[symbol] = RealizedVol(10 * 6, BARS_PER_YEAR_4H)
            self._vol_slow[symbol] = RealizedVol(60 * 6, BARS_PER_YEAR_4H)
            self._adds[symbol] = 0
            self._took_partial[symbol] = False

    async def on_funding(self, funding: Funding) -> list[Intent]:
        self._funding_apr[funding.symbol] = funding.apr
        return []

    async def on_bar(self, bar: Bar) -> list[Intent]:
        if not bar.closed or bar.symbol not in self._donchian:
            return []

        s = bar.symbol
        high, low, close = float(bar.high), float(bar.low), float(bar.close)

        # Read indicator state BEFORE pushing this bar: the channel must not contain
        # the bar we are testing against it.
        channel_ready = self._donchian[s].ready
        upper, lower = self._donchian[s].upper, self._donchian[s].lower
        exit_upper = self._exit_ch[s].upper if self._exit_ch[s].ready else None
        exit_lower = self._exit_ch[s].lower if self._exit_ch[s].ready else None
        if not channel_ready:
            upper, lower = float('inf'), float('-inf')   # no breakout can fire yet

        atr = self._atr[s].push(high, low, close)
        adx = self._adx[s].push(high, low, close)
        ema200 = self._ema200_daily[s].push(close)
        vf = self._vol_fast[s].push(close)
        vs = self._vol_slow[s].push(close)
        self._donchian[s].push(high, low)
        self._exit_ch[s].push(high, low)

        if not channel_ready or atr is None:
            return []

        position = self.ctx.position(s)

        if position != 0:
            return self._manage_open(s, bar, close, atr, exit_upper, exit_lower, position)

        if ema200 is None or adx is None or vf is None or vs is None:
            return []
        return self._maybe_enter(s, bar, close, high, low, atr, adx, ema200, vf, vs,
                                 upper, lower)

    # -- entry ------------------------------------------------------------------

    def _maybe_enter(self, s: str, bar: Bar, close: float, high: float, low: float,
                     atr: float, adx: float, ema200: float, vol_fast: float,
                     vol_slow: float, upper: float, lower: float) -> list[Intent]:
        long_break = close > upper
        short_break = close < lower
        if not (long_break or short_break):
            return []

        direction = 1 if long_break else -1

        # 2. daily trend filter
        if direction == 1 and close <= ema200:
            return []
        if direction == -1 and close >= ema200:
            return []

        # 3. volatility regime — excludes dead markets AND post-blow-off exhaustion
        ratio = vol_fast / vol_slow if vol_slow > 0 else 0.0
        lo, hi = self.VOL_RATIO_BAND
        if not (adx > self.ADX_MIN or lo <= ratio <= hi):
            return []

        # 4. range expansion — a breakout on a narrow bar has no follow-through
        if (high - low) <= self.RANGE_EXPANSION * atr:
            return []

        # 5. crowding veto
        apr = self._funding_apr.get(s, 0.0)
        if direction == 1 and apr > self.MAX_FUNDING_APR:
            log.info('s4_funding_veto symbol=%s apr=%.2f', s, apr)
            return []
        if direction == -1 and apr < -self.MAX_FUNDING_APR:
            return []

        stop_distance = self.STOP_ATR_MULT * atr
        vol_scalar = self._vol_scalar(vol_slow)
        qty = self.ctx.size_by_risk(s, dec(stop_distance), self.RISK_FRACTION, vol_scalar)
        if qty <= 0:
            return []

        stop_price = close - direction * stop_distance
        self._entry[s] = close
        self._stop[s] = stop_price
        self._peak[s] = close
        self._adds[s] = 0
        self._took_partial[s] = False

        target = qty * direction
        return [self.target(
            s, target,
            reason=(f'donchian{self.CHANNEL} break {"up" if direction > 0 else "down"} '
                    f'close={close:.2f} adx={adx:.1f} volratio={ratio:.2f} apr={apr:.2f}'),
            algo=AlgoSpec(kind=AlgoKind.MARKET),
            stop=StopSpec(price=dec(stop_price),
                          trailing_atr_mult=self.STOP_ATR_MULT, venue_native=True),
        )]

    # -- management -------------------------------------------------------------

    def _manage_open(self, s: str, bar: Bar, close: float, atr: float,
                     exit_upper: float | None, exit_lower: float | None,
                     position: Decimal) -> list[Intent]:
        direction = 1 if position > 0 else -1
        entry = self._entry.get(s, close)
        stop = self._stop.get(s)
        r_unit = self.STOP_ATR_MULT * atr
        if stop is None or r_unit <= 0:
            return []

        r_multiple = direction * (close - entry) / r_unit

        # hard stop
        if (direction == 1 and close <= stop) or (direction == -1 and close >= stop):
            self._clear(s)
            return [self.flatten(s, f'stop hit at {close:.2f} (stop {stop:.2f})')]

        # opposite-channel exit
        if direction == 1 and exit_lower is not None and close < exit_lower:
            self._clear(s)
            return [self.flatten(s, f'donchian{self.EXIT_CHANNEL} exit')]
        if direction == -1 and exit_upper is not None and close > exit_upper:
            self._clear(s)
            return [self.flatten(s, f'donchian{self.EXIT_CHANNEL} exit')]

        # chandelier trail, ratcheting only, armed at +1R
        peak = self._peak.get(s, entry)
        peak = max(peak, close) if direction == 1 else min(peak, close)
        self._peak[s] = peak
        if r_multiple >= 1.0:
            trail = peak - direction * self.STOP_ATR_MULT * atr
            breakeven = entry
            candidate = max(trail, breakeven) if direction == 1 else min(trail, breakeven)
            if (direction == 1 and candidate > stop) or (direction == -1 and candidate < stop):
                self._stop[s] = candidate

        # partial at +2R
        if r_multiple >= 2.0 and not self._took_partial[s]:
            self._took_partial[s] = True
            return [self.target(s, position / 2,
                                reason=f'partial 50% at +{r_multiple:.1f}R',
                                algo=AlgoSpec(kind=AlgoKind.MARKET))]

        # pyramiding: +0.5R of risk at +1.5R and +3.0R, max 2 adds
        for level, n in ((1.5, 1), (3.0, 2)):
            if r_multiple >= level and self._adds[s] == n - 1:
                self._adds[s] = n
                add = self.ctx.size_by_risk(s, dec(r_unit), self.RISK_FRACTION * 0.5)
                self._stop[s] = entry          # combined position to breakeven
                return [self.target(s, position + add * direction,
                                    reason=f'pyramid #{n} at +{r_multiple:.1f}R',
                                    algo=AlgoSpec(kind=AlgoKind.MARKET),
                                    stop=StopSpec(price=dec(entry)))]
        return []

    # -- helpers ----------------------------------------------------------------

    def _vol_scalar(self, realized_vol: float, target: float = 0.15) -> float:
        if realized_vol <= 0:
            return 1.0
        return max(0.25, min(target / realized_vol, 2.0))

    def _clear(self, s: str) -> None:
        for d in (self._entry, self._stop, self._peak):
            d.pop(s, None)
        self._adds[s] = 0
        self._took_partial[s] = False

    async def on_fill(self, fill: Fill) -> None:
        log.info('s4_fill symbol=%s side=%s qty=%s price=%s',
                 fill.symbol, fill.side, fill.amount, fill.price)

    async def on_stale(self, feed: str, age: float) -> list[Intent]:
        # S4 holds through data gaps: its stops are venue-native (L3), its horizon is
        # days, and panic-flattening a trend position on a 6-second feed hiccup is a
        # much more reliable way to lose money than the gap itself.
        log.warning('s4_feed_stale feed=%s age=%.1f holding', feed, age)
        return []

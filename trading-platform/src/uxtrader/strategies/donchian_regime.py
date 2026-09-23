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


# ---------------------------------------------------------------------------------
# Float port for the vectorized engine (lab/vector.py).
#
# This duplicates the decision logic above on purpose: the vectorized engine is for
# parameter search, and it is only trustworthy because the cross-validation test
# proves it matches the event-driven class within 2%. If you change the class above,
# change this too — the test will fail until you do, which is the point.
# ---------------------------------------------------------------------------------

def vector_decider(bars, params: dict | None = None, *, risk_budget: float = 1.0,
                   funding_apr=None):
    """Return ``decide(t, equity, position) -> target | None`` for ``VectorBacktester``.

    ``bars`` is a DataFrame with open/high/low/close columns. ``params`` may override
    any of the class constants (CHANNEL, EXIT_CHANNEL, ATR_PERIOD, STOP_ATR_MULT,
    RISK_FRACTION, ADX_MIN, RANGE_EXPANSION, ...) — this is what Optuna varies.
    """
    import numpy as np

    P = {k: getattr(DonchianRegime, k) for k in (
        'CHANNEL', 'EXIT_CHANNEL', 'ATR_PERIOD', 'STOP_ATR_MULT', 'RISK_FRACTION',
        'ADX_MIN', 'VOL_RATIO_BAND', 'RANGE_EXPANSION', 'MAX_FUNDING_APR')}
    P.update(params or {})

    h = bars['high'].to_numpy(float)
    lo = bars['low'].to_numpy(float)
    c = bars['close'].to_numpy(float)
    n = len(c)
    apr = np.zeros(n) if funding_apr is None else np.asarray(funding_apr, float)

    # Indicators computed exactly as on_bar computes them: channels read BEFORE the
    # current bar is pushed, everything else after.
    ch, ex_ch = Donchian(int(P['CHANNEL'])), Donchian(int(P['EXIT_CHANNEL']))
    atr_i, adx_i, ema_i = ATR(int(P['ATR_PERIOD'])), ADX(14), EMA(200 * 6)
    vf_i = RealizedVol(10 * 6, BARS_PER_YEAR_4H)
    vs_i = RealizedVol(60 * 6, BARS_PER_YEAR_4H)
    ready = np.zeros(n, bool)
    up = np.full(n, np.inf)
    dn = np.full(n, -np.inf)
    ex_up = np.full(n, np.nan)
    ex_dn = np.full(n, np.nan)
    atr = np.full(n, np.nan)
    adx = np.full(n, np.nan)
    ema = np.full(n, np.nan)
    vf = np.full(n, np.nan)
    vs = np.full(n, np.nan)
    for t in range(n):
        if ch.ready:
            ready[t], up[t], dn[t] = True, ch.upper, ch.lower
        if ex_ch.ready:
            ex_up[t], ex_dn[t] = ex_ch.upper, ex_ch.lower
        a = atr_i.push(h[t], lo[t], c[t])
        d = adx_i.push(h[t], lo[t], c[t])
        e = ema_i.push(c[t])
        f = vf_i.push(c[t])
        s = vs_i.push(c[t])
        atr[t] = np.nan if a is None else a
        adx[t] = np.nan if d is None else d
        ema[t] = np.nan if e is None else e
        vf[t] = np.nan if f is None else f
        vs[t] = np.nan if s is None else s
        ch.push(h[t], lo[t])
        ex_ch.push(h[t], lo[t])

    k, rf = P['STOP_ATR_MULT'], P['RISK_FRACTION']
    st = {'entry': None, 'stop': None, 'peak': None, 'adds': 0, 'partial': False}

    def size(equity: float, stop_distance: float, risk_fraction: float,
             vol_scalar: float = 1.0) -> float:
        if stop_distance <= 0:
            return 0.0
        sleeve = equity * risk_budget
        return sleeve * min(risk_fraction, 0.015) / stop_distance * max(0.25, min(vol_scalar, 2.0))

    def clear() -> None:
        st.update(entry=None, stop=None, peak=None, adds=0, partial=False)

    def decide(t: int, equity: float, pos: float):
        if not ready[t] or np.isnan(atr[t]):
            return None
        close, a = c[t], atr[t]
        if pos != 0:
            direction = 1 if pos > 0 else -1
            entry = st['entry'] if st['entry'] is not None else close
            stop = st['stop']
            r_unit = k * a
            if stop is None or r_unit <= 0:
                return None
            r = direction * (close - entry) / r_unit
            if (direction == 1 and close <= stop) or (direction == -1 and close >= stop):
                clear()
                return 0.0
            if direction == 1 and not np.isnan(ex_dn[t]) and close < ex_dn[t]:
                clear()
                return 0.0
            if direction == -1 and not np.isnan(ex_up[t]) and close > ex_up[t]:
                clear()
                return 0.0
            peak = st['peak'] if st['peak'] is not None else entry
            peak = max(peak, close) if direction == 1 else min(peak, close)
            st['peak'] = peak
            if r >= 1.0:
                trail = peak - direction * k * a
                cand = max(trail, entry) if direction == 1 else min(trail, entry)
                if (direction == 1 and cand > stop) or (direction == -1 and cand < stop):
                    st['stop'] = cand
            if r >= 2.0 and not st['partial']:
                st['partial'] = True
                return pos / 2
            for level, m in ((1.5, 1), (3.0, 2)):
                if r >= level and st['adds'] == m - 1:
                    st['adds'] = m
                    add = size(equity, r_unit, rf * 0.5)
                    st['stop'] = entry
                    return pos + add * direction
            return None

        if np.isnan(ema[t]) or np.isnan(adx[t]) or np.isnan(vf[t]) or np.isnan(vs[t]):
            return None
        if close > up[t]:
            direction = 1
        elif close < dn[t]:
            direction = -1
        else:
            return None
        if (direction == 1 and close <= ema[t]) or (direction == -1 and close >= ema[t]):
            return None
        ratio = vf[t] / vs[t] if vs[t] > 0 else 0.0
        lo_b, hi_b = P['VOL_RATIO_BAND']
        if not (adx[t] > P['ADX_MIN'] or lo_b <= ratio <= hi_b):
            return None
        if (h[t] - lo[t]) <= P['RANGE_EXPANSION'] * a:
            return None
        if direction == 1 and apr[t] > P['MAX_FUNDING_APR']:
            return None
        if direction == -1 and apr[t] < -P['MAX_FUNDING_APR']:
            return None
        stop_distance = k * a
        vol_scalar = max(0.25, min(0.15 / vs[t], 2.0)) if vs[t] > 0 else 1.0
        qty = size(equity, stop_distance, rf, vol_scalar)
        if qty <= 0:
            return None
        st.update(entry=close, stop=close - direction * stop_distance, peak=close,
                  adds=0, partial=False)
        return qty * direction

    return decide

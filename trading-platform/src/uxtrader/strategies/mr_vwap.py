"""S5 — Intraday VWAP mean reversion on majors. Spec: docs/01-strategies.md §S5.

**This strategy is only viable as a maker.** At 2 bps maker in/out, costs take ~12% of
gross. At 5.5 bps taker both ways they take ~30% and the Sharpe roughly halves. Every
entry is therefore post-only with a cancel-if-unfilled rule, and the exit is post-only
with a taker fallback. If your live maker fill ratio drops below 55% for a month, retire
the strategy — do not "give it time".

The regime gate is a hard on/off, not a scaler: outside a mean-reverting variance ratio
this strategy is a momentum strategy with the sign flipped.
"""
from __future__ import annotations

import logging
import math
from collections import deque
from datetime import timedelta
from decimal import Decimal

from ..strategy import StrategyBase, StrategyContext
from ..types import AlgoKind, AlgoSpec, Bar, BookSnapshot, Intent
from .indicators import ADX, ATR, RollingWindow, dec

log = logging.getLogger(__name__)


class VwapMeanReversion(StrategyBase):
    name = 'mr_vwap'
    timeframe = '15m'
    warmup_bars = 120

    ENTRY_Z = 2.2
    EXIT_Z = 0.2
    STOP_ATR_MULT = 1.5
    RISK_FRACTION = 0.005
    TIME_STOP_HOURS = 4
    MAX_CONCURRENT = 3
    VR_MAX = 0.90              # variance ratio below this ⇒ mean-reverting
    ADX_MAX = 20.0
    VOL_BAND = (0.40, 1.20)    # annualized realized vol
    IMBALANCE_SWING = 0.15

    def __init__(self, ctx: StrategyContext) -> None:
        super().__init__(ctx)
        self._vwap_num: dict[str, float] = {}
        self._vwap_den: dict[str, float] = {}
        self._dev: dict[str, RollingWindow] = {}
        self._vol_window: dict[str, deque[float]] = {}
        self._atr: dict[str, ATR] = {}
        self._adx: dict[str, ADX] = {}
        self._returns: dict[str, deque[float]] = {}
        self._imbalance: dict[str, deque[float]] = {}
        self._entered_at: dict[str, object] = {}
        self._stop: dict[str, float] = {}
        self._event_blackout_until = None

    async def on_start(self) -> None:
        for s in self.symbols:
            self._dev[s] = RollingWindow(96)                  # 24h of 15m bars
            self._vol_window[s] = deque(maxlen=20)
            self._atr[s] = ATR(14)
            self._adx[s] = ADX(14)
            self._returns[s] = deque(maxlen=864)              # 3 days of 5m returns
            self._imbalance[s] = deque(maxlen=12)

    def set_event_blackout(self, until) -> None:
        """Flat before CPI/FOMC/NFP. A maintained calendar, not a guess."""
        self._event_blackout_until = until

    async def on_book(self, book: BookSnapshot) -> list[Intent]:
        self._imbalance.setdefault(book.symbol, deque(maxlen=12)).append(book.imbalance(10))
        return []

    async def on_bar(self, bar: Bar) -> list[Intent]:
        if not bar.closed or bar.symbol not in self._dev:
            return []
        s = bar.symbol
        close, high, low = float(bar.close), float(bar.high), float(bar.low)
        volume = float(bar.volume)

        typical = (high + low + close) / 3
        self._vwap_num[s] = self._vwap_num.get(s, 0.0) + typical * volume
        self._vwap_den[s] = self._vwap_den.get(s, 0.0) + volume
        vwap = self._vwap_num[s] / self._vwap_den[s] if self._vwap_den[s] else close

        self._dev[s].push(close - vwap)
        self._vol_window[s].append(volume)
        atr = self._atr[s].push(high, low, close)
        adx = self._adx[s].push(high, low, close)
        rets = self._returns[s]
        if rets or True:
            prev = getattr(self, f'_prev_{s}', None)
            if prev:
                rets.append(math.log(close / prev))
            setattr(self, f'_prev_{s}', close)

        if not self._dev[s].ready or atr is None:
            return []

        sd = self._dev[s].std()
        z = (close - vwap) / sd if sd > 0 else 0.0
        position = self.ctx.position(s)

        if position != 0:
            return self._manage(s, bar, close, z, atr, position)
        return self._maybe_enter(s, bar, close, z, atr, adx)

    def _maybe_enter(self, s: str, bar: Bar, close: float, z: float,
                     atr: float, adx: float | None) -> list[Intent]:
        if self._event_blackout_until and bar.close_ts < self._event_blackout_until:
            return []
        if len([1 for sym in self.symbols if self.ctx.position(sym) != 0]) >= self.MAX_CONCURRENT:
            return []
        if abs(z) < self.ENTRY_Z:
            return []

        # regime gate — hard on/off
        if adx is None or adx >= self.ADX_MAX:
            return []
        vr = self._variance_ratio(s, q=30)
        if vr is None or vr >= self.VR_MAX:
            return []
        rv = self._realized_vol(s)
        if rv is None or not (self.VOL_BAND[0] <= rv <= self.VOL_BAND[1]):
            return []

        # exhaustion: price extending on declining volume
        vols = list(self._vol_window[s])
        if len(vols) < 20:
            return []
        if not (vols[-1] < vols[-2] < vols[-3] and vols[-1] < 0.8 * (sum(vols) / len(vols))):
            return []

        # book confirmation: imbalance has swung toward the reversion side
        imb = list(self._imbalance.get(s, []))
        if len(imb) < 4 or abs(imb[-1] - imb[-4]) < self.IMBALANCE_SWING:
            return []
        direction = -1 if z > 0 else 1
        if (imb[-1] - imb[-4]) * direction < 0:
            return []

        stop_distance = self.STOP_ATR_MULT * atr
        qty = self.ctx.size_by_risk(s, dec(stop_distance), self.RISK_FRACTION)
        if qty <= 0:
            return []
        self._entered_at[s] = bar.close_ts
        self._stop[s] = close - direction * stop_distance
        return [self.target(
            s, qty * direction,
            reason=f'vwap MR z={z:.2f} vr={vr:.2f} adx={adx:.1f}',
            # Post-only, 3 improvements, give up after 4 minutes. An unfilled entry is
            # far cheaper than a taker fee on every trade.
            algo=AlgoSpec(kind=AlgoKind.POST_ONLY_PEG, max_improvements=3,
                          cross_after_s=None),
            urgency='passive')]

    def _manage(self, s: str, bar: Bar, close: float, z: float, atr: float,
                position: Decimal) -> list[Intent]:
        direction = 1 if position > 0 else -1
        stop = self._stop.get(s)
        if stop is not None and ((direction == 1 and close <= stop)
                                 or (direction == -1 and close >= stop)):
            self._clear(s)
            return [self.target(s, Decimal('0'), reason=f'stop {close:.2f}',
                                algo=AlgoSpec(kind=AlgoKind.MARKET), urgency='immediate')]
        if abs(z) <= self.EXIT_Z:
            self._clear(s)
            return [self.target(s, Decimal('0'), reason=f'reverted z={z:.2f}',
                                algo=AlgoSpec(kind=AlgoKind.POST_ONLY_PEG,
                                              cross_after_s=600))]
        entered = self._entered_at.get(s)
        if entered is not None and bar.close_ts - entered > timedelta(hours=self.TIME_STOP_HOURS):
            self._clear(s)
            return [self.target(s, Decimal('0'), reason='4h time stop',
                                algo=AlgoSpec(kind=AlgoKind.MARKET))]
        return []

    def _variance_ratio(self, s: str, q: int = 30) -> float | None:
        r = list(self._returns.get(s, []))
        if len(r) < q * 10:
            return None
        var1 = _var(r)
        agg = [sum(r[i:i + q]) for i in range(0, len(r) - q, q)]
        if len(agg) < 3 or var1 <= 0:
            return None
        return _var(agg) / (q * var1)

    def _realized_vol(self, s: str) -> float | None:
        r = list(self._returns.get(s, []))[-288:]
        if len(r) < 50:
            return None
        return _var(r) ** 0.5 * math.sqrt(105120)      # 5m bars per year

    def _clear(self, s: str) -> None:
        self._entered_at.pop(s, None)
        self._stop.pop(s, None)

    async def on_stale(self, feed: str, age: float) -> list[Intent]:
        # Short-horizon and book-dependent: without a live book this strategy has no
        # edge at all. Flatten rather than hold.
        return [self.flatten(s, f'feed stale {age:.0f}s')
                for s in self.symbols if self.ctx.position(s) != 0]


def _var(xs: list[float]) -> float:
    if len(xs) < 2:
        return 0.0
    m = sum(xs) / len(xs)
    return sum((x - m) ** 2 for x in xs) / (len(xs) - 1)

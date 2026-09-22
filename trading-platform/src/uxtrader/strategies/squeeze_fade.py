"""S7 — Funding/OI squeeze fade. Spec: docs/01-strategies.md §S7.

Liquidation cascades are mechanical: the venue's engine force-closes positions at
whatever price is available, and it is price-insensitive. The resulting print overshoots
fair value. Fading it is inventory provision at a moment of *guaranteed* adverse
selection — which is exactly why it pays.

By construction you are catching a falling knife on purpose. Two consequences encoded
below: **the stop is the strategy**, and **never add to the position** — if it is going
against you, the cascade is not over. One "just this once" override can exceed a year of
this strategy's profit.

Low frequency (2 – 6 setups/month) means high estimation error: ~30 trades a year gives
a standard error on the Sharpe of roughly ±0.5. Treat any single-year result as
uninformative.
"""
from __future__ import annotations

import logging
from collections import deque
from datetime import timedelta
from decimal import Decimal

from ..strategy import StrategyBase, StrategyContext
from ..types import AlgoKind, AlgoSpec, Bar, Intent, TradeTick
from .indicators import ATR, dec

log = logging.getLogger(__name__)


class SqueezeFade(StrategyBase):
    name = 'squeeze_fade'
    timeframe = '15m'
    warmup_bars = 200

    FUNDING_Z_MIN = 2.5
    OI_GROWTH_MIN = 0.15          # 24h
    RANGE_ATR_MULT = 3.0
    LIQ_PERCENTILE = 0.99
    CONFIRM_BARS = 2
    STOP_WICK_MULT = 1.2
    TARGET_R = 2.0
    TIME_STOP_HOURS = 12
    RISK_FRACTION = 0.0075

    def __init__(self, ctx: StrategyContext) -> None:
        super().__init__(ctx)
        self._atr: dict[str, ATR] = {}
        self._funding_hist: dict[str, deque[float]] = {}
        self._oi_hist: dict[str, deque[float]] = {}
        self._liq_volume: dict[str, deque[float]] = {}
        self._pending: dict[str, dict] = {}
        self._entered_at: dict[str, object] = {}
        self._stop: dict[str, float] = {}
        self._target_px: dict[str, float] = {}

    async def on_start(self) -> None:
        for s in self.symbols:
            self._atr[s] = ATR(14)
            self._funding_hist[s] = deque(maxlen=90)     # 30 days of 8h prints
            self._oi_hist[s] = deque(maxlen=96)
            self._liq_volume[s] = deque(maxlen=2880)     # 30 days of 15m bars

    async def on_funding(self, funding) -> list[Intent]:
        self._funding_hist.setdefault(funding.symbol, deque(maxlen=90)).append(funding.apr)
        return []

    async def on_trade(self, tick: TradeTick) -> list[Intent]:
        if tick.is_liquidation:
            self._liq_bar_volume = getattr(self, '_liq_bar_volume', {})
            key = tick.symbol
            self._liq_bar_volume[key] = self._liq_bar_volume.get(key, 0.0) + float(tick.amount)
        return []

    def observe_open_interest(self, symbol: str, oi_notional: float) -> None:
        self._oi_hist.setdefault(symbol, deque(maxlen=96)).append(oi_notional)

    async def on_bar(self, bar: Bar) -> list[Intent]:
        if not bar.closed or bar.symbol not in self._atr:
            return []
        s = bar.symbol
        high, low, close, open_ = (float(bar.high), float(bar.low),
                                   float(bar.close), float(bar.open))
        atr = self._atr[s].push(high, low, close)

        liq_vol = getattr(self, '_liq_bar_volume', {}).pop(s, 0.0)
        self._liq_volume[s].append(liq_vol)

        if atr is None:
            return []
        if self.ctx.position(s) != 0:
            return self._manage(s, bar, close)
        if any(self.ctx.position(sym) != 0 for sym in self.symbols):
            return []                  # one position at a time across all instruments

        pending = self._pending.get(s)
        if pending is not None:
            return self._confirm(s, bar, close, high, low, pending)
        self._detect(s, bar, open_, high, low, close, atr, liq_vol)
        return []

    def _detect(self, s: str, bar: Bar, open_: float, high: float, low: float,
                close: float, atr: float, liq_vol: float) -> None:
        # 1. crowding
        fh = list(self._funding_hist.get(s, []))
        if len(fh) < 30:
            return
        mean = sum(fh) / len(fh)
        sd = (sum((v - mean) ** 2 for v in fh) / (len(fh) - 1)) ** 0.5
        if sd <= 0:
            return
        fz = (fh[-1] - mean) / sd
        if abs(fz) < self.FUNDING_Z_MIN:
            return

        oi = list(self._oi_hist.get(s, []))
        if len(oi) < 96 or oi[-96] <= 0 or (oi[-1] / oi[-96] - 1) < self.OI_GROWTH_MIN:
            return

        # 2. cascade trigger
        if (high - low) <= self.RANGE_ATR_MULT * atr:
            return
        liqs = sorted(self._liq_volume[s])
        if len(liqs) < 100:
            return
        threshold = liqs[int(len(liqs) * self.LIQ_PERCENTILE)]
        if liq_vol < threshold:
            return

        # Fade the liquidated side: crowded longs (fz > 0) get liquidated downward,
        # so we buy.
        direction = 1 if fz > 0 else -1
        if direction == 1 and close >= open_:
            return
        if direction == -1 and close <= open_:
            return

        self._pending[s] = {
            'direction': direction, 'bar_high': high, 'bar_low': low,
            'bar_close': close, 'atr': atr, 'bars_waited': 0,
            'wick': (close - low) if direction == 1 else (high - close),
            'extreme': low if direction == 1 else high,
        }
        log.info('s7_cascade_detected symbol=%s fz=%.2f dir=%+d range/atr=%.1f',
                 s, fz, direction, (high - low) / atr)

    def _confirm(self, s: str, bar: Bar, close: float, high: float,
                 low: float, pending: dict) -> list[Intent]:
        pending['bars_waited'] += 1
        if pending['bars_waited'] > self.CONFIRM_BARS:
            self._pending.pop(s, None)
            return []
        # 3. reversal confirmation — close back inside the trigger bar's range
        if not (pending['bar_low'] <= close <= pending['bar_high']):
            return []
        direction = pending['direction']
        if direction == 1 and close <= pending['bar_close']:
            return []
        if direction == -1 and close >= pending['bar_close']:
            return []

        self._pending.pop(s, None)
        wick = max(pending['wick'], 0.2 * pending['atr'])
        stop = pending['extreme'] - direction * (self.STOP_WICK_MULT - 1.0) * wick
        stop_distance = abs(close - stop)
        if stop_distance <= 0:
            return []
        qty = self.ctx.size_by_risk(s, dec(stop_distance), self.RISK_FRACTION)
        if qty <= 0:
            return []

        self._stop[s] = stop
        self._entered_at[s] = bar.close_ts
        self._target_px[s] = close + direction * self.TARGET_R * stop_distance
        return [self.target(
            s, qty * direction,
            reason=f'squeeze fade dir={direction:+d} stop={stop:.2f}',
            algo=AlgoSpec(kind=AlgoKind.MARKET), urgency='immediate')]

    def _manage(self, s: str, bar: Bar, close: float) -> list[Intent]:
        position = self.ctx.position(s)
        direction = 1 if position > 0 else -1
        stop = self._stop.get(s)
        if stop is not None and ((direction == 1 and close <= stop)
                                 or (direction == -1 and close >= stop)):
            self._clear(s)
            return [self.target(s, Decimal('0'), reason=f'stop {close:.2f}',
                                algo=AlgoSpec(kind=AlgoKind.MARKET), urgency='immediate')]
        tgt = self._target_px.get(s)
        if tgt is not None and ((direction == 1 and close >= tgt)
                                or (direction == -1 and close <= tgt)):
            self._clear(s)
            return [self.target(s, Decimal('0'), reason=f'+{self.TARGET_R}R target',
                                algo=AlgoSpec(kind=AlgoKind.MARKET))]
        entered = self._entered_at.get(s)
        if entered is not None and bar.close_ts - entered > timedelta(hours=self.TIME_STOP_HOURS):
            self._clear(s)
            return [self.target(s, Decimal('0'), reason='12h time stop',
                                algo=AlgoSpec(kind=AlgoKind.MARKET))]
        return []

    def _clear(self, s: str) -> None:
        for d in (self._stop, self._entered_at, self._target_px):
            d.pop(s, None)

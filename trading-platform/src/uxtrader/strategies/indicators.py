"""Streaming indicators.

Computed incrementally on closed bars only. This is not a performance decision — it is a
correctness one: a vectorized ``ta.ema(whole_series)`` computed once and then sliced is
one of the five doors look-ahead bias comes through (docs/03 §1.1). An indicator that
can only see what it has been fed cannot cheat.
"""
from __future__ import annotations

from collections import deque
from decimal import Decimal


class RollingWindow:
    """Fixed-length window with O(1) mean/std via running sums.

    Recomputing a 360-element std on every bar made ``RealizedVol`` the single most
    expensive thing in S4 — in the live engine as well as in backtests. Running sums
    accumulate float error, so they are rebuilt exactly every ``RESYNC`` pushes; the
    drift between rebuilds is ~1e-15 relative, far below anything a threshold sees.
    """

    RESYNC = 4096

    def __init__(self, length: int) -> None:
        self.length = length
        self.values: deque[float] = deque(maxlen=length)
        self._sum = 0.0
        self._sumsq = 0.0
        self._pushes = 0

    def push(self, value: float) -> None:
        if len(self.values) == self.length:
            old = self.values[0]
            self._sum -= old
            self._sumsq -= old * old
        self.values.append(value)
        self._sum += value
        self._sumsq += value * value
        self._pushes += 1
        if self._pushes % self.RESYNC == 0:
            self._sum = sum(self.values)
            self._sumsq = sum(v * v for v in self.values)

    @property
    def ready(self) -> bool:
        return len(self.values) == self.length

    def max(self) -> float:
        # Safe during warmup: an empty window has no extreme, and returning ±inf makes
        # every breakout comparison correctly False rather than raising.
        return max(self.values) if self.values else float('-inf')

    def min(self) -> float:
        return min(self.values) if self.values else float('inf')

    def mean(self) -> float:
        n = len(self.values)
        return self._sum / n if n else 0.0

    def std(self) -> float:
        """Sample standard deviation (ddof=1)."""
        n = len(self.values)
        if n < 2:
            return 0.0
        var = (self._sumsq - self._sum * self._sum / n) / (n - 1)
        return var ** 0.5 if var > 0 else 0.0


class EMA:
    def __init__(self, period: int) -> None:
        self.period = period
        self.alpha = 2.0 / (period + 1)
        self.value: float | None = None
        self._n = 0

    def push(self, x: float) -> float | None:
        self._n += 1
        self.value = x if self.value is None else self.alpha * x + (1 - self.alpha) * self.value
        return self.value if self.ready else None

    @property
    def ready(self) -> bool:
        return self._n >= self.period


class ATR:
    """Wilder's ATR. The unit of risk for every stop in this book."""

    def __init__(self, period: int = 14) -> None:
        self.period = period
        self.value: float | None = None
        self._prev_close: float | None = None
        self._n = 0

    def push(self, high: float, low: float, close: float) -> float | None:
        tr = high - low
        if self._prev_close is not None:
            tr = max(tr, abs(high - self._prev_close), abs(low - self._prev_close))
        self._prev_close = close
        self._n += 1
        if self.value is None:
            self.value = tr
        else:
            self.value = (self.value * (self.period - 1) + tr) / self.period
        return self.value if self.ready else None

    @property
    def ready(self) -> bool:
        return self._n >= self.period


class ADX:
    """Average Directional Index — the trend-strength gate for S4 and S6."""

    def __init__(self, period: int = 14) -> None:
        self.period = period
        self._atr = ATR(period)
        self._plus = EMA(period)
        self._minus = EMA(period)
        self._dx = EMA(period)
        self._prev: tuple[float, float] | None = None

    def push(self, high: float, low: float, close: float) -> float | None:
        atr = self._atr.push(high, low, close)
        if self._prev is None:
            self._prev = (high, low)
            return None
        prev_high, prev_low = self._prev
        up, down = high - prev_high, prev_low - low
        plus_dm = up if (up > down and up > 0) else 0.0
        minus_dm = down if (down > up and down > 0) else 0.0
        self._prev = (high, low)
        if not atr:
            return None
        pdi = self._plus.push(100 * plus_dm / atr) or 0.0
        mdi = self._minus.push(100 * minus_dm / atr) or 0.0
        denom = pdi + mdi
        if denom == 0:
            return None
        return self._dx.push(100 * abs(pdi - mdi) / denom)


class Donchian:
    """Highest high / lowest low over `period`, EXCLUDING the current bar.

    Excluding the current bar is what makes the breakout test well-defined: comparing
    a bar's close to a channel that already contains that bar's high is a test that can
    never fire cleanly.
    """

    def __init__(self, period: int = 55) -> None:
        self.period = period
        self._highs = RollingWindow(period)
        self._lows = RollingWindow(period)

    def push(self, high: float, low: float) -> None:
        self._highs.push(high)
        self._lows.push(low)

    @property
    def ready(self) -> bool:
        return self._highs.ready

    @property
    def upper(self) -> float:
        return self._highs.max()

    @property
    def lower(self) -> float:
        return self._lows.min()


class RealizedVol:
    """Annualized realized volatility from log returns of closed bars."""

    def __init__(self, period: int, bars_per_year: float) -> None:
        self.window = RollingWindow(period)
        self.bars_per_year = bars_per_year
        self._prev: float | None = None

    def push(self, close: float) -> float | None:
        import math
        if self._prev is not None and self._prev > 0 and close > 0:
            self.window.push(math.log(close / self._prev))
        self._prev = close
        if not self.window.ready:
            return None
        return self.window.std() * math.sqrt(self.bars_per_year)


def dec(x: float) -> Decimal:
    return Decimal(str(x))

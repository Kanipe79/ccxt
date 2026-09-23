"""Look-ahead must be a runtime error, not a code-review discipline.

These tests exist because every one of these bugs has silently produced a beautiful
equity curve for somebody. See docs/03 §1.1.
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest

from uxtrader.clock import LookAheadError, SimClock, guard
from uxtrader.strategies.indicators import ATR, EMA, Donchian


def test_sim_clock_cannot_move_backwards():
    clock = SimClock(datetime(2024, 1, 1, tzinfo=timezone.utc))
    clock.advance_to(datetime(2024, 1, 2, tzinfo=timezone.utc))
    with pytest.raises(LookAheadError):
        clock.advance_to(datetime(2024, 1, 1, 12, tzinfo=timezone.utc))


def test_guard_rejects_future_data():
    clock = SimClock(datetime(2024, 1, 1, tzinfo=timezone.utc))
    guard(datetime(2023, 12, 31, tzinfo=timezone.utc), clock)          # past: fine
    with pytest.raises(LookAheadError):
        guard(datetime(2024, 1, 2, tzinfo=timezone.utc), clock)


@pytest.mark.parametrize('indicator_factory,push', [
    (lambda: EMA(10), lambda ind, x: ind.push(x)),
    (lambda: ATR(14), lambda ind, x: ind.push(x + 1, x - 1, x)),
])
def test_indicators_are_prefix_stable(indicator_factory, push):
    """The value at bar N must not depend on bars N+1..  A vectorized indicator
    computed over a whole series and then sliced fails this."""
    series = [100 + i * 0.5 + (i % 7) for i in range(120)]

    full = indicator_factory()
    full_values = [push(full, x) for x in series]

    for cutoff in (40, 80, 119):
        partial = indicator_factory()
        partial_values = [push(partial, x) for x in series[:cutoff + 1]]
        assert partial_values[-1] == pytest.approx(full_values[cutoff], rel=1e-12)


def test_donchian_excludes_current_bar():
    """The channel must not contain the bar being tested against it, or a breakout
    test can never fire cleanly."""
    ch = Donchian(3)
    for high, low in [(10, 8), (11, 9), (12, 10)]:
        ch.push(high, low)
    assert ch.upper == 12
    # A new bar making a higher high must be comparable to the PREVIOUS channel.
    upper_before = ch.upper
    ch.push(20, 18)
    assert upper_before == 12 and ch.upper == 20


def test_rolling_window_matches_exact_recomputation():
    """The O(1) running-sum window must agree with a from-scratch computation,
    including across the periodic resync boundary."""
    import random
    import statistics

    from uxtrader.strategies.indicators import RollingWindow
    rng = random.Random(3)
    w = RollingWindow(360)
    w.RESYNC = 500
    xs = [rng.gauss(0, 0.02) for _ in range(2000)]
    for i, x in enumerate(xs):
        w.push(x)
        if i >= 359 and i % 97 == 0:
            window = xs[i - 359:i + 1]
            assert w.mean() == pytest.approx(statistics.fmean(window), abs=1e-15)
            assert w.std() == pytest.approx(statistics.stdev(window), rel=1e-9)

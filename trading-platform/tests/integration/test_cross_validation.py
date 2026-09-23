"""Cross-validation obligation (docs/02 §3.4): the vectorized engine used for search
must reproduce the event-driven engine used for decisions to within 2%.

A larger gap is a bug in one of them. This test is what makes the fast engine safe to
optimize on.
"""
from __future__ import annotations

import time
from datetime import datetime, timezone
from decimal import Decimal

import numpy as np
import pandas as pd
import pytest

from uxtrader.data.history import bars_from_frame
from uxtrader.lab.backtest import EventDrivenBacktester
from uxtrader.lab.vector import VectorBacktester
from uxtrader.strategies.donchian_regime import DonchianRegime, vector_decider

SYM = 'BTC/USDT:USDT'


class S4(DonchianRegime):
    symbols = (SYM,)


def regime_series(n: int, seed: int) -> pd.DataFrame:
    """Regime-switching random walk: trends, chop and crashes, so stops, trails,
    partials, pyramids and (sometimes) risk flattens all get exercised."""
    rng = np.random.default_rng(seed)
    drift = np.repeat(rng.choice([-0.004, 0.0, 0.0, 0.004], size=n // 200 + 1), 200)[:n]
    vol = np.repeat(rng.choice([0.008, 0.015, 0.03], size=n // 150 + 1), 150)[:n]
    rets = drift + vol * rng.standard_normal(n)
    close = 30000 * np.exp(np.cumsum(rets))
    open_ = np.r_[30000, close[:-1]]
    wick = np.abs(rng.standard_normal(n)) * vol * close * 0.6
    high = np.maximum(open_, close) + wick
    low = np.minimum(open_, close) - wick
    ts = pd.date_range(datetime(2020, 1, 1, tzinfo=timezone.utc), periods=n, freq='4h')
    return pd.DataFrame({'ts': ts, 'open': open_.round(2), 'high': high.round(2),
                         'low': low.round(2), 'close': close.round(2),
                         'volume': np.full(n, 100.0)})


async def run_both(df: pd.DataFrame):
    bars = list(bars_from_frame(df, 'sim', SYM, '4h'))
    t0 = time.perf_counter()
    ev = await EventDrivenBacktester(starting_equity=Decimal('100000')).run(
        S4, bars, start=bars[0].ts)
    t_event = time.perf_counter() - t0
    t0 = time.perf_counter()
    vec = VectorBacktester(starting_equity=100_000.0).run(
        df, vector_decider(df), symbol=SYM, venue='sim', strategy=S4.name)
    t_vec = time.perf_counter() - t0
    return ev, vec, t_event, t_vec


@pytest.mark.parametrize('seed', list(range(1, 13)))
async def test_engines_agree_within_two_percent(seed):
    df = regime_series(3000, seed)
    ev, vec, _, _ = await run_both(df)

    ev_pnl = ev.final_equity - 100_000
    vec_pnl = vec.final_equity - 100_000
    # 2% of starting equity bounds the absolute gap; relative-to-PnL is meaningless
    # when PnL is near zero.
    assert abs(ev.final_equity - vec.final_equity) <= 0.02 * 100_000, (ev_pnl, vec_pnl)
    assert len(ev.fills) > 0, 'nothing traded — the comparison is vacuous'
    assert abs(len(ev.fills) - len(vec.fills)) <= max(1, len(ev.fills) // 20)


async def test_equity_curves_track_bar_by_bar():
    df = regime_series(3000, 7)
    ev, vec, _, _ = await run_both(df)
    ev_curve = np.array([e for _, e in ev.equity_curve])
    gap = np.max(np.abs(ev_curve - vec.equity)) / 100_000
    assert gap < 0.02, f'max bar-by-bar divergence {gap:.3%}'


async def test_risk_ladder_agrees_over_long_history():
    """~5.5 years of 4h bars through a crash regime: exercises the daily-loss halt,
    its midnight expiry, and the weekly kill in BOTH engines. This is the case that
    caught a pandas-version bug (asi8 in µs, not ns) that silently merged 'days', and
    (seed sweep) a partial-fill mismatch when an order exceeded synthetic book depth."""
    df = regime_series(12000, 9)
    ev, vec, _, _ = await run_both(df)
    assert ev.risk_flattens, 'scenario no longer exercises the risk ladder'
    assert [r for _, r in ev.risk_flattens] == [r for _, r in vec.risk_flattens]
    assert ev.final_equity == pytest.approx(vec.final_equity, rel=1e-6)
    assert len(ev.fills) == len(vec.fills)


async def test_orders_larger_than_the_book_fill_partially_in_both():
    """A cheap asset makes S4 size in thousands of units; both engines must fill only
    what the synthetic book holds and agree on the remainder being dropped."""
    df = regime_series(3000, 17)
    for col in ('open', 'high', 'low', 'close'):
        df[col] = df[col] / 60                       # ~$500 asset
    ev, vec, _, _ = await run_both(df)
    assert ev.final_equity == pytest.approx(vec.final_equity, rel=1e-6)
    assert len(ev.fills) == len(vec.fills)

"""Walk-forward analysis and Monte Carlo — gates G3 and G5 from docs/03.

Walk-forward is not "a backtest with a holdout". It re-fits parameters in every window
using only data available at that point, then trades the next window with them. What it
measures is not "does this strategy work" but the far more useful **"does my *process*
for choosing parameters work"** — which is the thing that has to survive contact with
the future.
"""
from __future__ import annotations

import logging
from collections.abc import Callable, Sequence
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from typing import Any

import numpy as np

from .metrics import Summary, deflated_sharpe, max_drawdown, sharpe, summarize

log = logging.getLogger(__name__)

# fit(train_slice) -> params ; evaluate(params, test_slice) -> per-period returns
FitFn = Callable[[Sequence[Any]], dict[str, Any]]
EvalFn = Callable[[dict[str, Any], Sequence[Any]], Sequence[float]]


@dataclass
class WindowResult:
    train_start: datetime
    train_end: datetime
    test_start: datetime
    test_end: datetime
    params: dict[str, Any]
    is_sharpe: float
    oos_sharpe: float
    oos_returns: list[float] = field(default_factory=list)


@dataclass
class WalkForwardReport:
    windows: list[WindowResult]
    stitched: Summary
    efficiency: float          # OOS Sharpe / IS Sharpe — the headline number
    positive_window_rate: float
    dsr: float

    @property
    def passes_g3(self) -> bool:
        """docs/03 G3: OOS ≥ 0.5 × IS, ≥ 60% of windows positive, DSR ≥ 0.95."""
        return (self.efficiency >= 0.5
                and self.positive_window_rate >= 0.60
                and self.dsr >= 0.95)


class WalkForward:
    def __init__(self, *, train_days: int = 365, test_days: int = 90,
                 anchored: bool = True) -> None:
        self.train_days = train_days
        self.test_days = test_days
        # Anchored: the training window grows. Rolling: it slides at fixed length.
        # Anchored is the more honest default — it is what you would actually do.
        self.anchored = anchored

    def run(self, data: Sequence[Any], timestamps: Sequence[datetime],
            fit: FitFn, evaluate: EvalFn, n_trials: int = 1) -> WalkForwardReport:
        if len(data) != len(timestamps):
            raise ValueError('data and timestamps must align')

        start, end = timestamps[0], timestamps[-1]
        windows: list[WindowResult] = []
        train_start = start
        train_end = start + timedelta(days=self.train_days)

        while train_end + timedelta(days=self.test_days) <= end:
            test_end = train_end + timedelta(days=self.test_days)
            tr = [i for i, t in enumerate(timestamps) if train_start <= t < train_end]
            te = [i for i, t in enumerate(timestamps) if train_end <= t < test_end]
            if len(tr) < 30 or len(te) < 5:
                train_end = test_end
                continue

            params = fit([data[i] for i in tr])
            is_ret = evaluate(params, [data[i] for i in tr])
            oos_ret = evaluate(params, [data[i] for i in te])

            windows.append(WindowResult(
                train_start=train_start, train_end=train_end,
                test_start=train_end, test_end=test_end, params=params,
                is_sharpe=sharpe(is_ret), oos_sharpe=sharpe(oos_ret),
                oos_returns=list(oos_ret)))

            if not self.anchored:
                train_start = train_start + timedelta(days=self.test_days)
            train_end = test_end

        if not windows:
            raise ValueError('no complete walk-forward windows — need more history')

        stitched_returns = [r for w in windows for r in w.oos_returns]
        s = summarize(stitched_returns)
        mean_is = float(np.mean([w.is_sharpe for w in windows]))
        mean_oos = float(np.mean([w.oos_sharpe for w in windows]))
        efficiency = mean_oos / mean_is if mean_is > 0 else 0.0
        positive = float(np.mean([w.oos_sharpe > 0 for w in windows]))

        return WalkForwardReport(
            windows=windows, stitched=s, efficiency=efficiency,
            positive_window_rate=positive,
            dsr=deflated_sharpe(s.sharpe, n_trials=max(n_trials, len(windows)),
                                n_obs=len(stitched_returns), skew=s.skew,
                                kurtosis=s.kurtosis))


# --- Monte Carlo (gate G5) ---------------------------------------------------

def bootstrap_drawdowns(trade_returns: Sequence[float], n_paths: int = 10_000,
                        seed: int = 7) -> dict[str, float]:
    """Reshuffle trade order to get the drawdown *distribution*, not the one sample.

    Your realized max drawdown is a single draw from this distribution, and usually not
    a pessimistic one. Size against the 95th percentile, not against what happened.
    """
    rng = np.random.default_rng(seed)
    r = np.asarray(list(trade_returns), dtype=float)
    if r.size == 0:
        return {}
    dds = np.empty(n_paths)
    for i in range(n_paths):
        path = rng.permutation(r)
        dds[i] = max_drawdown(np.cumprod(1 + path))
    return {
        'dd_median': float(np.median(dds)),
        'dd_p95': float(np.percentile(dds, 95)),
        'dd_p99': float(np.percentile(dds, 99)),
        'dd_max': float(dds.max()),
    }


def stress_costs(evaluate: Callable[[float], Sequence[float]],
                 multipliers: Sequence[float] = (1.0, 1.5, 2.0, 3.0)) -> dict[float, float]:
    """G5(c): re-run with costs scaled up. A strategy that dies at 2x costs is an
    execution bet, not an alpha bet, and it will not survive a stressed market."""
    return {m: sharpe(evaluate(m)) for m in multipliers}


def randomize_entry_timing(evaluate: Callable[[int], Sequence[float]],
                           shifts: Sequence[int] = (-2, -1, 0, 1, 2)) -> dict[int, float]:
    """G5(b): shift every entry by ±N bars. A strategy whose edge vanishes when entries
    move one bar is fitted to a specific, non-repeating sequence of prices."""
    return {s: sharpe(evaluate(s)) for s in shifts}

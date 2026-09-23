"""Performance metrics, including the ones that tell you the other ones are lying.

A raw Sharpe ratio from a backtest is close to meaningless without the trial count that
produced it. Over 200 Optuna trials on two years of daily data, **noise alone produces a
maximum Sharpe near 1.0**. ``deflated_sharpe`` and ``pbo`` are therefore not optional
extras; they are the metrics that decide whether the rest of the report means anything.

References: Bailey & López de Prado, "The Deflated Sharpe Ratio" (2014) and "The
Probability of Backtest Overfitting" (2015).
"""
from __future__ import annotations

import math
from dataclasses import asdict, dataclass
from itertools import combinations

import numpy as np

TRADING_DAYS = 365.0    # crypto trades every day


@dataclass
class Summary:
    n_obs: int
    total_return: float
    cagr: float
    sharpe: float
    sortino: float
    calmar: float
    max_drawdown: float
    volatility: float
    skew: float
    kurtosis: float
    win_rate: float
    profit_factor: float
    expectancy: float
    best: float
    worst: float
    var_95: float
    cvar_95: float

    def as_dict(self) -> dict[str, float | int]:
        return asdict(self)


def _arr(returns) -> np.ndarray:
    a = np.asarray(list(returns), dtype=float)
    return a[np.isfinite(a)]


def sharpe(returns, periods_per_year: float = TRADING_DAYS, rf: float = 0.0) -> float:
    r = _arr(returns)
    if r.size < 2:
        return 0.0
    excess = r - rf / periods_per_year
    sd = excess.std(ddof=1)
    return 0.0 if sd == 0 else float(excess.mean() / sd * math.sqrt(periods_per_year))


def sortino(returns, periods_per_year: float = TRADING_DAYS) -> float:
    r = _arr(returns)
    downside = r[r < 0]
    if downside.size < 2:
        return 0.0
    dd = downside.std(ddof=1)
    return 0.0 if dd == 0 else float(r.mean() / dd * math.sqrt(periods_per_year))


def max_drawdown(equity) -> float:
    e = _arr(equity)
    if e.size == 0:
        return 0.0
    peak = np.maximum.accumulate(e)
    return float(np.max((peak - e) / np.where(peak == 0, 1, peak)))


def profit_factor(returns) -> float:
    r = _arr(returns)
    gains, losses = r[r > 0].sum(), -r[r < 0].sum()
    if losses == 0:
        return float('inf') if gains > 0 else 0.0
    return float(gains / losses)


def summarize(returns, equity=None,
              periods_per_year: float = TRADING_DAYS) -> Summary:
    r = _arr(returns)
    if r.size == 0:
        return Summary(*([0] * 18))          # type: ignore[arg-type]
    eq = _arr(equity) if equity is not None else np.cumprod(1 + r)
    mdd = max_drawdown(eq)
    total = float(eq[-1] / eq[0] - 1) if eq.size > 1 and eq[0] else 0.0
    years = r.size / periods_per_year
    cagr = float((1 + total) ** (1 / years) - 1) if years > 0 and total > -1 else 0.0
    sr = sharpe(r, periods_per_year)
    wins = r[r > 0]
    return Summary(
        n_obs=int(r.size), total_return=total, cagr=cagr, sharpe=sr,
        sortino=sortino(r, periods_per_year),
        calmar=float(cagr / mdd) if mdd > 0 else 0.0,
        max_drawdown=mdd, volatility=float(r.std(ddof=1) * math.sqrt(periods_per_year)),
        skew=float(_skew(r)), kurtosis=float(_kurtosis(r)),
        win_rate=float(wins.size / r.size),
        profit_factor=profit_factor(r),
        expectancy=float(r.mean()),
        best=float(r.max()), worst=float(r.min()),
        var_95=float(np.percentile(r, 5)),
        cvar_95=float(r[r <= np.percentile(r, 5)].mean()) if r.size > 20 else float(r.min()),
    )


def _skew(r: np.ndarray) -> float:
    sd = r.std(ddof=0)
    return 0.0 if sd == 0 else float(((r - r.mean()) ** 3).mean() / sd ** 3)


def _kurtosis(r: np.ndarray) -> float:
    sd = r.std(ddof=0)
    return 3.0 if sd == 0 else float(((r - r.mean()) ** 4).mean() / sd ** 4)


# --- the anti-self-deception metrics -----------------------------------------

def expected_max_sharpe(n_trials: int, trial_sharpe_std: float = 1.0) -> float:
    """Expected maximum Sharpe from N independent trials of pure noise.

    Print this next to any optimized Sharpe. If your "best" result is near this number,
    you have found nothing at all.
    """
    if n_trials < 2:
        return 0.0
    from scipy.stats import norm
    gamma = 0.5772156649
    return trial_sharpe_std * (
        (1 - gamma) * norm.ppf(1 - 1 / n_trials)
        + gamma * norm.ppf(1 - 1 / (n_trials * math.e)))


def deflated_sharpe(observed_sharpe: float, n_trials: int, n_obs: int,
                    skew: float = 0.0, kurtosis: float = 3.0,
                    trial_sharpe_std: float = 1.0) -> float:
    """Probability the true Sharpe exceeds zero after correcting for selection bias.

    **Reject any strategy with DSR < 0.95.** The threshold is deliberately strict:
    under-rejecting costs you real money, over-rejecting costs you a strategy you can
    find again with more data.
    """
    from scipy.stats import norm
    if n_obs < 2:
        return 0.0
    sr0 = expected_max_sharpe(n_trials, trial_sharpe_std)
    denom = math.sqrt(max(1e-12,
                          1 - skew * observed_sharpe
                          + (kurtosis - 1) / 4 * observed_sharpe ** 2))
    return float(norm.cdf((observed_sharpe - sr0) * math.sqrt(n_obs - 1) / denom))


def pbo(performance_matrix: np.ndarray, n_splits: int = 16) -> float:
    """Probability of Backtest Overfitting (combinatorially symmetric CV).

    ``performance_matrix``: shape (n_observations, n_configurations) of per-period
    returns for each parameter configuration.

    Returns the fraction of splits where the in-sample-best configuration ranks below
    median out-of-sample. **PBO > 0.5 means your selection procedure is worse than
    picking at random.** Reject.
    """
    obs, n_cfg = performance_matrix.shape
    if n_cfg < 2:
        return 0.0
    block = obs // n_splits
    if block == 0:
        raise ValueError('not enough observations for n_splits')
    blocks = [performance_matrix[i * block:(i + 1) * block] for i in range(n_splits)]

    logits: list[float] = []
    half = n_splits // 2
    for is_idx in combinations(range(n_splits), half):
        oos_idx = [i for i in range(n_splits) if i not in is_idx]
        is_data = np.vstack([blocks[i] for i in is_idx])
        oos_data = np.vstack([blocks[i] for i in oos_idx])
        is_sr = np.array([sharpe(is_data[:, c]) for c in range(n_cfg)])
        oos_sr = np.array([sharpe(oos_data[:, c]) for c in range(n_cfg)])
        best = int(np.argmax(is_sr))
        rank = float((oos_sr < oos_sr[best]).sum()) / n_cfg
        rank = min(max(rank, 1e-6), 1 - 1e-6)
        logits.append(math.log(rank / (1 - rank)))
        if len(logits) >= 2000:             # the full C(16,8) is 12870; cap the work
            break
    return float(np.mean(np.array(logits) <= 0))


def parameter_stability(objective_by_params: dict[tuple, float],
                        best: tuple, tolerance: float = 0.20) -> float:
    """Fraction of the best objective retained by neighbours within ±`tolerance`.

    A strategy that loses more than 30% of its Sharpe when every parameter moves 20% is
    fitted to noise. Pick the centre of a plateau, never the peak.
    """
    best_val = objective_by_params[best]
    if best_val <= 0:
        return 0.0
    neighbours = [
        v for p, v in objective_by_params.items()
        if p != best and all(
            abs(a - b) <= tolerance * abs(b) if b else a == b for a, b in zip(p, best))
    ]
    return float(np.mean(neighbours) / best_val) if neighbours else 0.0

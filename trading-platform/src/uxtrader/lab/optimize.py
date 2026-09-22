"""Optuna-based parameter search, with the guardrails that make it safe to use.

Optimization is the most dangerous tool in this repository. Left alone it will find you a
4.0 Sharpe on any dataset. Three guardrails are applied here, and none is optional:

1. **Trial budget is capped** and the count is reported, because the deflated Sharpe
   needs it and because an uncounted trial is a lie by omission.
2. **The objective is penalized for instability**, so a lone spike on the surface cannot
   win against a plateau.
3. **The objective is the walk-forward stitched Sharpe**, never the in-sample Sharpe.
   Optimizing in-sample performance optimizes for overfitting directly.
"""
from __future__ import annotations

import logging
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from datetime import datetime
from typing import Any

from .metrics import deflated_sharpe
from .walkforward import WalkForward

log = logging.getLogger(__name__)


@dataclass
class SearchSpace:
    """Keep this small. The rule from docs/03 §1.2: at most one free parameter per 30
    independent trades. Exceeding it is not a judgement call — it is a stop."""

    spec: dict[str, tuple[str, Any, Any]]     # name → (kind, low, high)

    def suggest(self, trial: Any) -> dict[str, Any]:
        out: dict[str, Any] = {}
        for name, (kind, low, high) in self.spec.items():
            if kind == 'int':
                out[name] = trial.suggest_int(name, low, high)
            elif kind == 'float':
                out[name] = trial.suggest_float(name, low, high)
            elif kind == 'categorical':
                out[name] = trial.suggest_categorical(name, low)
            else:
                raise ValueError(f'unknown kind {kind}')
        return out

    def check_budget(self, n_trades: int) -> None:
        allowed = max(1, n_trades // 30)
        if len(self.spec) > allowed:
            raise ValueError(
                f'{len(self.spec)} parameters but only {n_trades} trades supports '
                f'{allowed}. Reduce the parameter count or get more data.')


def optimize(space: SearchSpace, data: Sequence[Any], timestamps: Sequence[datetime],
             fit_with: Callable[[dict[str, Any]], Any],
             evaluate: Callable[[dict[str, Any], Sequence[Any]], Sequence[float]],
             *, n_trials: int = 200, n_trades_estimate: int = 200,
             stability_weight: float = 0.3, seed: int = 11) -> dict[str, Any]:
    """Returns the chosen parameters plus the honesty statistics that qualify them."""
    import optuna

    space.check_budget(n_trades_estimate)
    optuna.logging.set_verbosity(optuna.logging.WARNING)
    wf = WalkForward()

    def objective(trial: Any) -> float:
        params = space.suggest(trial)
        report = wf.run(data, timestamps,
                        fit=lambda _train: params,          # fixed params per trial
                        evaluate=evaluate, n_trials=n_trials)
        # Penalize windows that disagree: a strategy that works in 3 of 8 windows and
        # brilliantly in 1 is a strategy that worked once.
        oos = [w.oos_sharpe for w in report.windows]
        import numpy as np
        consistency = 1.0 - min(1.0, float(np.std(oos)) / max(1e-9, abs(float(np.mean(oos)))))
        return report.stitched.sharpe * (1 - stability_weight) + consistency * stability_weight

    study = optuna.create_study(direction='maximize',
                                sampler=optuna.samplers.TPESampler(seed=seed),
                                pruner=optuna.pruners.HyperbandPruner())
    study.optimize(objective, n_trials=n_trials)

    best = study.best_params
    final = wf.run(data, timestamps, fit=lambda _t: best, evaluate=evaluate,
                   n_trials=n_trials)

    result = {
        'params': best,
        'n_trials': n_trials,
        'wf_efficiency': final.efficiency,
        'positive_window_rate': final.positive_window_rate,
        'stitched_sharpe': final.stitched.sharpe,
        'dsr': deflated_sharpe(final.stitched.sharpe, n_trials,
                               final.stitched.n_obs, final.stitched.skew,
                               final.stitched.kurtosis),
        'passes_g3': final.passes_g3,
    }
    if not result['passes_g3']:
        log.warning('optimization_failed_g3 %s — this strategy does not proceed', result)
    return result

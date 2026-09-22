"""The anti-self-deception metrics need tests more than the ordinary ones do."""
from __future__ import annotations

import numpy as np

from uxtrader.lab.metrics import (
    deflated_sharpe, expected_max_sharpe, max_drawdown, pbo, profit_factor, sharpe,
)


def test_sharpe_of_constant_returns_is_zero_volatility_safe():
    assert sharpe([0.01] * 50) == 0.0


def test_max_drawdown():
    assert max_drawdown([100, 120, 60, 90]) == 0.5


def test_profit_factor():
    assert profit_factor([1.0, 1.0, -1.0]) == 2.0


def test_expected_max_sharpe_grows_with_trials():
    """The core lesson of docs/03: more trials means a higher noise floor."""
    assert expected_max_sharpe(200) > expected_max_sharpe(20) > 0


def test_deflated_sharpe_punishes_many_trials():
    high_trials = deflated_sharpe(1.5, n_trials=500, n_obs=500)
    low_trials = deflated_sharpe(1.5, n_trials=5, n_obs=500)
    assert low_trials > high_trials


def test_pbo_on_pure_noise_is_near_half():
    """With no real signal, in-sample selection should be no better than random."""
    rng = np.random.default_rng(0)
    noise = rng.normal(0, 0.01, size=(512, 8))
    assert 0.25 <= pbo(noise, n_splits=8) <= 0.85

# Backtesting and Validation — How Not To Fool Yourself

> This is the most important document in the repository. A strategy that survives this
> pipeline and then loses money has taught you something. A strategy that skips this
> pipeline and makes money has taught you nothing, and you will not know when to stop.

## 1. The three biases, and the concrete defense against each

### 1.1 Look-ahead bias
Using information that was not available at decision time. In crypto it sneaks in through
five specific doors:

| Door | How it happens | Defense |
|---|---|---|
| Bar timestamps | Using the bar's `close` to decide, then filling at that same bar's `close` | **Signals on bar close, fills at next bar open, plus a latency delay.** Enforced by `SimClock`: the backtester refuses to give a strategy any data with `ts > clock.now()`. |
| Indicator warm-up | `ta.ema(series)` computed on the whole series, then sliced | Compute indicators inside the event loop, or verify that a prefix-truncated series gives identical values at every point. There is a unit test for this in `tests/unit/test_no_lookahead.py`. |
| Universe / listing data | Today's top-40 list applied to 2022 | Point-in-time universe snapshots — see §3. |
| Funding and fees | Today's fee tier or funding schedule applied historically | Store historical funding and fee schedules; version them. |
| Restatements | Venue OHLCV endpoints sometimes revise recent candles | Capture candles live and store them; never re-fetch history to "refresh" a research dataset. |

**The structural defense:** the `SimClock` is the only source of `now()` in the entire
codebase, and the backtest data adapter raises on any access to a future timestamp. Make
look-ahead a runtime error, not a code-review discipline.

### 1.2 Overfitting
The default outcome of any research process, not an occasional accident.

**Parameter budget.** Hard rule: **at most one free parameter per 30 independent trades**
in your training set. A strategy trading 12 times a year on 3 symbols over 4 years has
~144 trades → **a budget of 4 – 5 parameters, total, including thresholds you tuned by
eye.** S4 uses 5. S3 uses 6 and needs more data to justify them. This is not a guideline;
it is the binding constraint on what you are allowed to build.

**Deflated Sharpe Ratio.** After N trials, the expected maximum Sharpe from pure noise is
roughly `E[max SR] ≈ sqrt(2 ln N) / sqrt(T)` for T observations. Over 200 Optuna trials on
2 years of daily data, **noise alone produces a Sharpe near 1.0.** Compute the DSR
(Bailey & López de Prado) and report it next to every raw Sharpe. `lab/metrics.py`
implements it.

```python
def deflated_sharpe(sr, n_trials, n_obs, skew, kurt, sr_benchmark=0.0):
    """Probability the observed Sharpe exceeds the benchmark after trial-selection bias."""
    from scipy.stats import norm
    e_max = sr_benchmark + np.sqrt(np.var_trials) * (
        (1 - np.euler_gamma) * norm.ppf(1 - 1/n_trials)
        + np.euler_gamma * norm.ppf(1 - 1/(n_trials * np.e)))
    denom = np.sqrt(1 - skew*sr + (kurt-1)/4 * sr**2)
    return norm.cdf((sr - e_max) * np.sqrt(n_obs - 1) / denom)
```
**Reject anything with DSR < 0.95.**

**Probability of Backtest Overfitting (PBO).** Split the sample into 16 combinatorial
blocks, fit on each half-combination, test on the complement, and measure how often the
in-sample-best parameter set ranks below median out-of-sample. **PBO > 0.5 means your
optimization procedure is worse than random selection.** Reject.

**Parameter-plateau requirement.** Plot the objective surface. A strategy whose Sharpe
drops more than 30% when every parameter moves ±20% is fitted to noise. **Pick the center
of a plateau, never the peak.** If there is no plateau, there is no strategy.

**Trial counting.** Log *every* backtest you run, including the ones you ran "just to
look". Your effective trial count for DSR purposes is the total, not the number in your
final Optuna study. Self-honesty here is worth more than any statistic.

### 1.3 Survivorship bias
Crypto is the worst asset class for this — thousands of delisted tokens, and most data
vendors only carry survivors.

- Build the universe from **historical listing/delisting events**, not from today's
  symbol list. `data/universe.py` stores weekly snapshots with `listed_at` / `delisted_at`.
- Include delisted assets with their actual final prices. A token that went to zero must
  appear in the cross-sectional short leg's history.
- For S2 specifically: a backtest on today's top-40 will overstate returns by an estimated
  **15 – 40% annualized**. This single bias is larger than most strategies' entire edge.

---

## 2. Why ML on OHLCV usually fails here (and when it does not)

The signal-to-noise ratio on 5-minute crypto bars is roughly 0.01 – 0.05 Sharpe per
genuinely informative feature. With 40 features and 200k bars, a gradient-boosted tree has
enough capacity to memorize the noise, and cross-validation on time series leaks through
autocorrelation unless you use purged, embargoed splits.

If you want ML in this platform, the defensible uses are:
1. **Regime classification** — a 3-state HMM or simple clustering on realized vol,
   correlation, and dispersion, used to *gate* rule-based strategies. Low capacity, low
   parameter count, economically interpretable.
2. **Execution** — predicting short-horizon fill probability and queue position. Lots of
   data, fast feedback, immediately measurable.
3. **Meta-labeling** (López de Prado) — keep the rule-based entry, train a classifier to
   decide *position size*. The primary model provides the economic prior; ML only sizes.

Use purged k-fold with an embargo of at least the label horizon. Never use random
`train_test_split` on time-series data.

---

## 3. Point-in-time data discipline

Every research table carries three timestamps:
- `event_ts` — when the thing happened in the market.
- `available_ts` — when your system could first have known it (≥ `event_ts`, and strictly
  greater for anything published on a delay).
- `ingest_ts` — when you actually wrote it.

Queries in research **filter on `available_ts ≤ as_of`**, never on `event_ts`. This one
convention eliminates the majority of accidental look-ahead.

---

## 4. The validation pipeline — six gates

No strategy skips a gate. Each gate has an explicit numeric pass criterion.

```
  ┌─────────────────────────────────────────────────────────────────────┐
  │ G1  IN-SAMPLE DESIGN            40% of history, oldest              │
  │     Build the logic. Look at charts. Form the economic hypothesis.  │
  │     PASS: you can state in one sentence WHO pays you and WHY.       │
  │     If you cannot, stop here. No statistic rescues a missing edge.  │
  ├─────────────────────────────────────────────────────────────────────┤
  │ G2  PARAMETER SELECTION         same 40%, Optuna ≤ 200 trials       │
  │     PASS: plateau exists; Sharpe within 30% under ±20% perturbation │
  │           of every parameter; parameter count ≤ trades/30.          │
  ├─────────────────────────────────────────────────────────────────────┤
  │ G3  WALK-FORWARD                next 35% of history                 │
  │     Anchored WFA: train 12mo → test 3mo → roll. Re-fit each window. │
  │     PASS: OOS Sharpe ≥ 0.5 × IS Sharpe; ≥ 60% of windows positive;  │
  │           DSR ≥ 0.95; PBO ≤ 0.5.                                    │
  ├─────────────────────────────────────────────────────────────────────┤
  │ G4  OUT-OF-SAMPLE HOLDOUT       final 25%, touched exactly ONCE     │
  │     Run with the parameters G3 produced. One shot. No iteration.    │
  │     PASS: Sharpe ≥ 0.4 × IS, max DD ≤ 1.5 × IS max DD.              │
  │     FAIL ⇒ the strategy is dead. You do not get to re-tune and      │
  │     re-test; the holdout is now contaminated for this strategy.     │
  ├─────────────────────────────────────────────────────────────────────┤
  │ G5  MONTE CARLO + STRESS                                            │
  │     (a) Trade-order bootstrap, 10k paths → DD distribution          │
  │     (b) Randomize entry timing ±2 bars → Sharpe must survive        │
  │     (c) Double all costs → must remain profitable                   │
  │     (d) Replay the 2020-03-12, 2021-05-19, 2022-06, 2022-11,        │
  │         2024-08-05 and 2025 stress windows explicitly               │
  │     PASS: 95th-percentile MC drawdown ≤ your stated risk tolerance; │
  │           strategy survives (c) with Sharpe > 0.3.                  │
  ├─────────────────────────────────────────────────────────────────────┤
  │ G6  PAPER TRADING               minimum 30 trades OR 60 days        │
  │     Live data, live latency, paper fills, full production stack.    │
  │     PASS: realized slippage within 1.5× model; fill ratio within    │
  │           20% of assumption; PnL within 1σ of backtest expectation. │
  └─────────────────────────────────────────────────────────────────────┘
                                   ↓
  ┌─────────────────────────────────────────────────────────────────────┐
  │ LIVE, STAGED                                                        │
  │   Stage 1: 10% of target size, 30 trades or 30 days                 │
  │   Stage 2: 25%, 30 trades                                           │
  │   Stage 3: 50%, 60 days                                             │
  │   Stage 4: 100%                                                     │
  │ Demote one stage on: 2σ underperformance vs backtest over 30 trades,│
  │ slippage > 2× model, or any risk-limit breach.                      │
  │ Demotion is automatic and does not require a decision.              │
  └─────────────────────────────────────────────────────────────────────┘
```

**Minimum data requirements per strategy family:**

| Family | Minimum history | Minimum trades for a decision |
|---|---|---|
| S4 trend (4h) | 4 years, 5+ symbols | 150 |
| S2 XS momentum (weekly) | 4 years, 40-symbol PIT universe | 200 rebalances |
| S3 pairs (1h) | 2 years per pair | 100 round trips |
| S1 carry | 3 years of funding history | N/A — measure APR distribution, not trades |
| S5 intraday (15m) | 18 months | 400 |
| S7 squeeze fade | 4 years | 80 — and still treat it as provisional |

---

## 5. What a backtest report must contain

A report without every one of these is not reviewable, and you should refuse to deploy
from it — including when you are the only reviewer.

1. **Equity curve** with drawdown underwater plot, linear and log.
2. **Per-year and per-quarter** returns — a strategy that made all its money in Q1 2021 is
   a story about Q1 2021.
3. **Sharpe, Sortino, Calmar, profit factor, win rate, avg win/avg loss, expectancy.**
4. **Deflated Sharpe and PBO**, with the trial count used.
5. **Trade distribution:** histogram of returns, holding-period histogram, MAE/MFE scatter.
6. **Cost attribution:** gross PnL, fees, funding, modeled slippage, each as a % of gross.
   **If costs exceed 35% of gross, the strategy is an execution problem, not a strategy.**
7. **Exposure over time:** gross, net, per-asset, so you can see what it actually did.
8. **The worst 10 trades and the worst 5 drawdowns**, each with a chart and a one-line
   explanation of what happened. If you cannot explain your worst drawdown, you do not
   understand the strategy.
9. **Parameter sensitivity surface.**
10. **Regime breakdown** — performance in bull / bear / chop, classified ex-ante.

---

## 6. Continuous validation after deployment

Backtesting is not a phase that ends.

- **Nightly:** re-run the strategy over the last 90 days of live data with the backtester
  and diff against realized PnL. A persistent gap means the fill model has drifted.
- **Weekly:** slippage vs arrival price per symbol and per algo; fill ratio per algo.
- **Monthly:** re-estimate the strategy correlation matrix; re-screen the S3 pair universe
  and the S2 point-in-time universe.
- **Quarterly:** full walk-forward re-run with the new data appended. Parameters may move
  **only** if the new WFA supports it and the change is within the plateau — a parameter
  change that jumps to a new peak is a re-fit, and it resets that strategy to Stage 1.
- **Automatic retirement:** trailing-120-day net profit factor < 1.0 → size halved;
  < 0.9 → disabled pending research review. This rule runs without a human in the loop,
  because the human in the loop will always find a reason to wait one more month.

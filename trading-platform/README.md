# ux-trader

A systematic crypto trading platform: seven specified strategies and a production
architecture, built on **`uxcore`** — a hard fork of CCXT that adds the reliability
layer a trading system needs and upstream reasonably will not provide.

> **Read this first.** Every performance figure in these documents is a *prior*, not a
> backtest result. Nothing here has been backtested on real market data. The numbers come
> from the structural economics of each trade and from what these strategy families have
> historically delivered after realistic costs. `docs/03-backtesting-validation.md` exists
> so that you never have to take them on faith — run the pipeline and replace them with
> your own numbers.
>
> Trading is risky and most systematic retail operations lose money over a full cycle.
> Read `docs/05-risk-disclosures.md` before deploying capital.

## Documentation

| Doc | What it covers |
|---|---|
| [`docs/00-executive-summary.md`](docs/00-executive-summary.md) | Ranked strategy table, what's robust vs. what needs babysitting, what's discarded |
| [`docs/01-strategies.md`](docs/01-strategies.md) | The seven strategies, fully specified: parameters, entries, exits, sizing, expected characteristics |
| [`docs/02-architecture.md`](docs/02-architecture.md) | Why fork CCXT, system topology, module responsibilities, data flow, tech stack |
| [`docs/03-backtesting-validation.md`](docs/03-backtesting-validation.md) | **Read before writing a backtest.** The six validation gates, DSR/PBO, the three biases |
| [`docs/04-deployment-operations.md`](docs/04-deployment-operations.md) | Environments, secrets, Docker/K8s, monitoring, runbooks RB-01 – RB-05 |
| [`docs/05-risk-disclosures.md`](docs/05-risk-disclosures.md) | What can still go wrong. All of it. |
| [`docs/06-roadmap.md`](docs/06-roadmap.md) | v0.1 → production in 18 weeks, with gates |

## The strategies

| # | Strategy | Family | Exp. net Sharpe | Risk budget | Edge decay |
|---|---|---|---|---|---|
| S1 | Perp funding carry (delta-neutral) | Carry | 1.8 – 3.0 | 35% | Low |
| S2 | Cross-sectional momentum rotation | XS momentum | 0.8 – 1.4 | 20% | Low-med |
| S3 | Cointegration pairs / stat-arb | Stat-arb | 1.0 – 1.8 | 15% | **High** |
| S4 | Donchian breakout + regime filter | TS trend | 0.6 – 1.0 | 20% | **Very low** |
| S5 | Intraday VWAP mean reversion | MR / flow | 0.5 – 1.0 net | 5% | **High** |
| S6 | Adaptive grid + trend filter | Short vol | 1.0 – 2.0 in-regime | 3% | Medium |
| S7 | Funding/OI squeeze fade | Forced flow | 0.9 – 1.4 | 2% | Low |

Explicitly discarded: latency/cross-exchange arbitrage, triangular arbitrage, sub-minute
scalping, indicator-soup ML on OHLCV, social-sentiment alpha, copy trading. Reasons in
`docs/00`.

## Layout

```
src/uxcore/        the CCXT fork layer — rate limiting, error taxonomy, WS resilience, plugins
src/uxtrader/      the platform
  strategies/      S1–S7. Read donchian_regime.py first: it is the worked reference.
  services/        portfolio, risk, execution, strategy engine — one process or one container each
  lab/             event-driven + vectorized backtesters, fill models, walk-forward, DSR/PBO, Optuna
  execution/       OMS, paper and live brokers, algos, router
  data/            WS ingestion, historical backfill, Parquet store, resampler, PIT universe
  ops/             watchdog (kill-switch L2), panic flatten (RB-05), alerts, metrics
  api/             operator API + dashboard
  run.py, demo.py  entry points
config/            example configuration
ops/               Dockerfile, docker-compose, k8s manifests, Prometheus alerts
tests/             unit, uxcore (real ccxt), data, integration (incl. cross-validation), ops, api
docs/              the seven documents above
```

## Three design decisions that carry most of the safety

1. **Strategies emit `Intent`, never `Order`.** A strategy literally cannot place an
   order, so a strategy bug cannot breach a risk limit. The risk engine is a hard gate on
   the path from intent to execution.
2. **Intents carry a target *position*, not a delta.** Replaying "target = 1.5 BTC" three
   times leaves you with 1.5 BTC. Replaying "buy 1.5 BTC" three times ends your career.
   The whole pipeline is idempotent under message replay and restart.
3. **`SimClock` makes look-ahead a runtime error.** The backtest clock refuses to move
   backwards and the data adapter raises on any access to a future timestamp. It caught a
   real ordering bug in this repository's own backtester during development.

Plus three independent kill-switch layers — in-process, out-of-process watchdog, and
venue-native stop orders — because the one that saves you is the one you did not think
you needed.

## Quick start

```bash
pip install -e ".[dev]"
# The fork's own ccxt is used when you run from inside it:
export PYTHONPATH=src:../python

pytest tests -q                              # offline; the uxcore tests use the fork's real ccxt

python -m uxtrader.demo --token demo         # full platform on a synthetic feed
#   → http://127.0.0.1:8765  (dashboard; controls use the token)

# Backfill real history (needs network access to the venue):
python -m uxtrader.data.history --venue binanceusdm --symbols BTC/USDT:USDT \
    --timeframe 4h --since 2021-01-01 --out ./data --funding

# Paper trading, one process:
cp config/config.example.yaml config/config.yaml
cp config/strategies.example.yaml config/strategies.yaml
python -m uxtrader.run --role all
```

Run modes and the multi-container layout are in `docs/04-deployment-operations.md §3`.

## Status

**Built and tested (all offline):**
- **The fork layer:** `uxcore`, checked against this fork's own ccxt `binanceusdm` with only HTTP stubbed. Forced 429s, timeouts that landed or didn't, and unreachable reconciliation are all handled correctly.
- **Backtesting:** both backtesters. They agree exactly on S4, including risk halts and flattens.
- **Services:** portfolio, risk, execution and strategy engine talking over a message bus. Per-strategy books, operator kill → flatten → rearm, daily-loss halt and stale-feed blocking are tested end to end.
- **Data:** the historical loader, immutable Parquet store, WS ingestion and bar resampler.
- **Brokers:** paper and live, the live one tested offline only.
- **Ops:** alerts, Prometheus metrics, the operator API, and a dashboard verified in Chromium at desktop and phone widths.
- **Safety:** the L2 watchdog, which also acts on a global operator kill.

**Not built yet:**
- L3 venue-native stop orders
- execution algos and smart routing wired into the OMS
- the portfolio VaR computation
- ClickHouse/Postgres persistence (service state is in memory and rebuilt from venues)
- a React dashboard

**Not done at all:** validation of any strategy on real market data. The development
environment could not reach exchange APIs, so every performance figure in `docs/` is
still a prior. Run the backfill above, then the G1–G6 pipeline in `docs/03`.

Full component-by-component status: `docs/02-architecture.md §9`.

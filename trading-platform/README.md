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
src/uxtrader/      the platform — strategies, risk, execution, portfolio, backtester
  strategies/      S1–S7. Read donchian_regime.py first: it is the worked reference.
  lab/             backtester, fill models, walk-forward, DSR/PBO, Optuna
  execution/       OMS, routing, algos, paper broker
  ops/             watchdog (kill-switch L2), panic flatten (RB-05)
config/            example configuration
ops/               Dockerfile, docker-compose, k8s manifests, Prometheus alerts
tests/             unit + integration
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
pytest tests -q                       # 31 tests, all offline

# run S4 through the backtester on synthetic bars
PYTHONPATH=src python -c "
import asyncio, sys; sys.path.insert(0, 'tests/integration')
from test_backtest_smoke import synthetic_bars, S4, START
from uxtrader.lab.backtest import EventDrivenBacktester
from decimal import Decimal
r = asyncio.run(EventDrivenBacktester(starting_equity=Decimal('100000')).run(
    S4, synthetic_bars(), start=START))
print(f'intents={len(r.intents)} fills={len(r.fills)} final={r.final_equity:,.0f}')
print(r.attribution)"

# full single-node stack
docker compose -f ops/docker-compose.yml up -d
```

## Status

This is a **design and scaffold**, not a running trading system. What exists and is
verified: the domain model, risk engine, portfolio accounting, fill simulation, the
event-driven backtester, all seven strategy implementations, and the safety modules —
with 31 passing tests including an end-to-end backtest that exercises the real wiring
(signal → risk gate → OMS → paper fill → PnL attribution).

What does **not** exist yet: the data ingestion service, the NATS wiring between
processes, the live broker, the FastAPI/React dashboard, and the vectorized backtester.
Those are Phases 1 – 5 in `docs/06-roadmap.md`. The module paths referenced by
`ops/docker-compose.yml` (`uxtrader.data.ingest`, `uxtrader.risk_service`, …) are the
intended entry points for that work, not existing modules.

No strategy in this repository has been validated on real data. Every one of them must
pass gates G1 – G6 in `docs/03` before it sees capital.

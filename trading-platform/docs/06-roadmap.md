# Roadmap — v0.1 to Production

> **Where the code is now:** Phases 0, 2 and 4 are built and their gates pass offline
> (L3 venue stops included); Phase 1 is built with SQLite/Parquet persistence; Phase 5's
> tooling (launcher, dashboard, services on real NATS, alerts, metrics) is built, but the
> 30 days of paper trading it requires have not started.
> No strategy has passed Phase 3's real-data validation. The one blocker that cannot be
> solved in code is running `uxtrader.data.history` somewhere that can reach the venues.
> Per-component detail: `docs/02-architecture.md §9`.

Eighteen weeks at a realistic part-time-to-full-time pace for one competent engineer.
Each phase ends with a gate that is a *demonstration*, not a checkbox.

## Phase 0 — Foundations (weeks 1 – 2)
**Status: gate passed** — `tests/uxcore/test_phase0_gate.py`, against the fork's real ccxt.

- Fork CCXT, set up `upstream/master` tracking, monthly merge job.
- `uxcore`: error taxonomy, `@resilient` decorator, weight-aware rate limiter.
- Repo skeleton, `pyproject.toml`, ruff + mypy strict, CI.
- `docker compose` with ClickHouse, Postgres, Redis, NATS.

**Gate:** place and cancel an order on a venue testnet through `uxcore`, with a forced
429 and a forced timeout, and show correct classification and reconciliation for both.

## Phase 1 — Data (weeks 3 – 4)
**Status: built except ClickHouse.** Loader, immutable Parquet store, point-in-time reads, WS ingest, resampler. The 4-year backfill has not been run (no venue access from the build environment).

- `md-ingest` for one venue: WS trades, book, funding + REST backfill.
- ClickHouse schema, gap detection and repair.
- Historical loader: 4 years of OHLCV and funding for 40 symbols → Parquet.
- Point-in-time universe snapshots, including delisted assets.

**Gate:** reconstruct any historical bar from stored ticks and match the venue's own OHLCV
to within one tick. Prove the universe table returns the *2022* top-40 when asked for 2022.

## Phase 2 — Backtester (weeks 5 – 7)
**Status: gate passed** for S4 on synthetic data: the two engines agree exactly, and the look-ahead guard caught a real ordering bug.

- `SimClock` with the look-ahead runtime guard.
- Event-driven engine + realistic fill model (queue position, latency, partials).
- Vectorized engine for search.
- `metrics.py`: Sharpe, Sortino, Calmar, PF, **DSR, PBO**.
- Cross-validation harness: both engines must agree within 2%.

**Gate:** implement S4 (Donchian), run it through both engines, get agreement, and produce
the full report from `docs/03 §5`. Deliberately introduce a look-ahead bug and show the
guard catches it.

## Phase 3 — Strategy framework and first validation (weeks 6 – 9, overlaps)
**Status: framework done; validation not started.** Needs real data.

- `StrategyBase`, `Intent`, `StrategyContext`, the engine host.
- Implement S1 (carry) and S4 (trend) properly.
- Run the **full G1 – G5 pipeline** on both, including walk-forward and Monte Carlo.

**Gate:** two strategies with complete, honest validation reports. **If either fails a
gate, it does not proceed — and you will have learned more from that than from a pass.**

## Phase 4 — Risk and execution (weeks 10 – 12)
**Status: built.** Kill → flatten → rearm, daily-loss halt, stale-feed blocking, per-strategy books and L3 venue-native stops are tested end to end. **Still open:** algos not wired into the OMS.

- `risk.py`: the full check ladder, drawdown ladder, kill switch L1.
- `portfolio.py`: position truth, PnL attribution, reconciliation loop.
- `execution/`: OMS state machine, deterministic client IDs, startup reconciliation.
- Algos: `Market`, `PostOnlyPeg`, `TWAP`.
- `paper.py` paper broker sharing the backtester's fill model.
- **L2 watchdog**, separate process, separate credentials.

**Gate:** kill the execution engine mid-order and show that restart reconciles to the
correct position with no duplicate. Kill the *whole stack* and show the L2 watchdog
flattens within 90 seconds.

## Phase 5 — Paper trading (weeks 13 – 15)
**Status: tooling built; the 30-day run itself has not started.**

- Full stack in `paper`, running S1 + S4 continuously.
- Prometheus/Grafana boards 1 – 3, Telegram alerting, the daily report.
- Nightly backtest-vs-paper diff job.

**Gate:** 30 days of paper with slippage within 1.5× model and fill ratio within 20% of
assumption. **Do not shorten this phase.** It is where you find the bugs that cost money.

## Phase 6 — Staged live (weeks 16 – 18)
- Secrets in Vault, IP allowlist, withdrawal disabled, L3 venue-native stops.
- Stage 1 at 10% size with real capital you can afford to lose entirely.
- Runbooks written and rehearsed, including the laptop flatten drill.

**Gate:** 30 live trades with PnL within 1σ of the paper result, zero reconciliation
drifts, zero manual interventions.

## Phase 7 — Expansion (ongoing, month 5+)
Add strategies **one at a time**, each through the full G1 – G6 pipeline. Suggested order
by value-per-effort:
1. **S2** cross-sectional momentum — biggest diversification gain for the work.
2. **S3** pairs — highest Sharpe of the remainder, but the most ongoing maintenance.
3. **S7** squeeze fade — cheap to add once the framework exists.
4. **S6** grid — only after the kill-switch infrastructure has been proven in anger.
5. **S5** intraday MR — last, because it requires the maker execution stack (`PostOnlyPeg`
   with real queue modeling) to be genuinely good.

Then: second venue → third venue → dashboard polish → Rust order-book core **if and only
if** profiling says so.

## What to deliberately not build

- A strategy marketplace or plugin ecosystem for strategies. You have one user.
- A generic "any indicator" DSL. Write Python.
- Microsecond optimization anywhere. Your strategies are designed to not need it.
- Mobile apps. Telegram is your mobile app.
- Your own charting library. `lightweight-charts` is free and better.
- Multi-language support in the fork. Python only; the point of forking is to move fast.

## Definition of done for v1.0

- [ ] Two strategies through G1 – G6 with published reports
- [ ] 30 days paper, 30 live trades at staged size
- [ ] Three independent kill-switch layers, each tested by deliberately triggering it
- [ ] Reconciliation proven correct under process kill and network partition
- [ ] Runbooks RB-01 – RB-05 written and each rehearsed once
- [ ] Backup restore tested from scratch
- [ ] A daily report you actually read

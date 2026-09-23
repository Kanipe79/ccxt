# Platform Architecture — `ux-trader`

## 1. Why fork CCXT at all

Upstream CCXT is an excellent *unification layer* and a poor *trading system substrate*,
by design — it optimizes for breadth across 100+ venues and 6 languages, which forces
conservative choices. The fork (`uxcore`, "UnifiedExchange Core") exists to change exactly
five things. Everything else is tracked upstream and merged monthly.

| # | Upstream behaviour | `uxcore` behaviour | Why it matters |
|---|---|---|---|
| 1 | Fixed `rateLimit` sleep between calls | **Weight-aware token-bucket limiter** that reads `X-MBX-USED-WEIGHT-1M` / venue equivalents and backs off *before* the 429 | A 429 during a liquidation cascade means you cannot reduce risk. This is a safety feature, not a performance feature. |
| 2 | Error classes, no policy | **Unified error taxonomy with an attached retry policy per class** — `Transient`, `RateLimited`, `Rejected`, `Fatal`, `Ambiguous` | `Ambiguous` (timeout after send) is the dangerous one: you must reconcile, never blindly retry. Upstream cannot decide this for you; your platform must. |
| 3 | `ccxt.pro` reconnects, does not resync | **Sequence-gap detection + automatic snapshot resync + `on_stale` callbacks** into the strategy | A silently-wrong order book is worse than no order book. |
| 4 | Private/undocumented endpoints are not accepted upstream | **Plugin registry** — drop a module in `plugins/`, it patches the exchange class at load | You need the venue's portfolio-margin endpoint before CCXT supports it. |
| 5 | Sync-ish throughput | **uvloop + connection pooling + optional Rust order-book core** | Only where profiled. Python is fast enough for everything in this book except L2 book maintenance on 40 symbols. |

**Do not fork to change unified method semantics.** The moment your `fetch_ohlcv` returns
something different from CCXT's, every upstream merge becomes a conflict and every new
exchange needs hand-holding. Fork *around* the unified layer, not *through* it.

### Upstream sync policy
- `upstream/master` tracked as a remote; **monthly** merge, never rebase.
- All `uxcore` additions live in *new files* (`uxcore/ratelimit.py`, `uxcore/resilience.py`)
  or in subclasses. Exactly one upstream file is patched: the base `Exchange` class, via a
  mixin injected at import time — see `src/uxcore/exchange.py`.
- CI runs the upstream test suite plus `uxcore`'s own on every merge.

---

## 2. System topology

```
                         ┌───────────────────────────────────────────┐
                         │             OPERATOR SURFACE              │
                         │  Dashboard · Telegram · Grafana · CLI     │
                         └───────────────┬───────────────────────────┘
                                         │ WS + REST
                         ┌───────────────┴───────────────────────────┐
                         │      api-gateway (FastAPI, stateless)     │
                         └───────────────┬───────────────────────────┘
                                         │ NATS JetStream
   ┌──────────────┐   ┌──────────────────┴─────────────────┐   ┌──────────────────┐
   │  md-ingest   │──▶│           strategy-engine          │──▶│  execution-eng   │
   │ (1 per venue)│   │  (1 per strategy, N replicas)      │   │ (1, singleton)   │
   └──────┬───────┘   └──────────────────┬─────────────────┘   └────────┬─────────┘
          │                              │ intents                       │ orders
          │ ticks/books/                 ▼                               ▼
          │ funding      ┌───────────────────────────────┐   ┌────────────────────┐
          ▼              │       risk-engine (veto)      │◀──│   uxcore (fork)    │
   ┌──────────────┐      │  pre-trade gate · kill switch │   │  REST + WS + OMS   │
   │  ClickHouse  │      └───────────────┬───────────────┘   └────────┬───────────┘
   │ ticks,books, │                      │                             │
   │ ohlcv,funding│      ┌───────────────┴───────────────┐             ▼
   └──────┬───────┘      │  portfolio-service (truth)    │      ┌─────────────┐
          │              │  positions · PnL · exposure   │      │  EXCHANGES  │
          ▼              └───────────────┬───────────────┘      └─────────────┘
   ┌──────────────┐                      │
   │ Parquet/S3   │              ┌───────┴────────┐
   │  research    │              │   PostgreSQL   │
   └──────┬───────┘              │ orders,fills,  │
          │                      │ positions,cfg  │
          ▼                      └────────────────┘
   ┌──────────────┐
   │ research-lab │  backtest · walk-forward · Optuna · notebooks
   └──────────────┘
```

**Process boundaries chosen deliberately:**
- `md-ingest` is separate per venue so a venue's WS meltdown cannot stall other feeds.
- `strategy-engine` is horizontally scalable and **stateless between restarts** — all
  state is rebuilt from the event log plus `portfolio-service`.
- `execution-engine` is a **singleton with a distributed lock**. Two execution engines
  racing on the same account is the worst bug class in this domain; make it impossible
  rather than unlikely.
- `risk-engine` sits *between* strategy and execution as a hard gate. Strategies emit
  *intents*, never orders. This is the single most important architectural decision in the
  system: **a strategy bug cannot place an order that violates a limit, because strategies
  cannot place orders at all.**

---

## 3. Module responsibilities

### 3.1 `uxcore` — the forked exchange layer
```
src/uxcore/
├── exchange.py      # UXExchange mixin: retries, reconciliation, weight tracking
├── errors.py        # unified taxonomy + RetryPolicy per class
├── ratelimit.py     # weight-aware adaptive token bucket
├── resilience.py    # @resilient decorator, circuit breaker, ambiguous-op reconciler
├── ws.py            # sequence-gap detection, resync, staleness watchdog
├── plugin_registry.py # plugin registry for venue-specific endpoints
└── plugins/
    ├── binance_pm.py        # portfolio margin endpoints
    ├── bybit_unified.py
    └── hyperliquid_ws.py
```
Responsibilities: **connectivity and correctness only.** No strategy logic, no risk logic,
no persistence. If a change to `uxcore` needs to know what a strategy is, it is in the
wrong module.

### 3.2 `uxtrader.data` — data pipeline
- **Ingest:** WS-primary, REST-backfill. Every message is written to ClickHouse *and*
  published to NATS. Backfill jobs detect and repair gaps on startup and hourly.
- **Storage split:**
  - **ClickHouse** — ticks, L2 book snapshots + deltas, OHLCV, funding, open interest,
    liquidations. Columnar, compresses crypto tick data ~10:1, and answers
    "give me 2 years of 1m bars for 40 symbols" in under a second.
  - **PostgreSQL** — orders, fills, positions, strategy config, audit log. Anything that
    needs a transaction.
  - **Parquet on S3/MinIO** — immutable research datasets, versioned by build date.
    Backtests read Parquet, never the live database.
- **Point-in-time discipline:** every research table carries `as_of_ts` and the universe
  snapshot that was valid at that time. See `docs/03 §3`.

### 3.3 `uxtrader.strategy` — the strategy engine
Event-driven with a candle abstraction on top. A strategy implements any subset of:
```python
async def on_start(self, ctx)            -> None
async def on_bar(self, bar: Bar)         -> list[Intent]
async def on_trade(self, t: TradeTick)   -> list[Intent]
async def on_book(self, b: BookDelta)    -> list[Intent]
async def on_funding(self, f: Funding)   -> list[Intent]
async def on_fill(self, f: Fill)         -> None
async def on_stale(self, feed: str)      -> list[Intent]   # feed went stale
async def on_stop(self, reason: str)     -> list[Intent]
```
Hard rules enforced by the base class:
- Strategies return **`Intent`** objects, never call the exchange.
- Strategies get a read-only `ctx` view of their own positions and a *risk budget*, not
  the account.
- The same strategy class runs in backtest and live with **no branching on mode**. If you
  ever write `if self.live:` inside a strategy, the backtest no longer validates the
  thing you are running.
- Hot reload: the engine watches strategy modules, and on change it drains intents,
  snapshots state, re-imports, and restores. Positions are never touched by a reload.

### 3.4 `uxtrader.lab` — backtester
Two engines with a **cross-validation obligation**: any strategy must produce the same PnL
(within 2%) on both, or the discrepancy is a bug you must find.
- **Event-driven** (`lab/backtest.py`) — replays events through the same `StrategyBase`,
  `RiskEngine`, `OrderManager` and `PaperBroker` the live platform uses. Used for the
  *decision*. Bar-driven intents fill at the **next bar's open** after a sampled latency.
- **Vectorized** (`lab/vector.py`) — a float loop over arrays with a per-strategy port of
  the decision logic (currently S4: `donchian_regime.vector_decider`). It reuses the real
  `RiskEngine` on the rare bars that trade. Used for *search*.

Measured, not assumed (`tests/integration/test_cross_validation.py`): on 3,000 and 12,000
bars of 4h data the two engines produce **identical** final equity and fill counts,
including daily-loss halts and a weekly kill. The speed-up is only **~2x**. Once
`RollingWindow` became O(1) the event engine itself runs at roughly 20k bars/s: 5.5 years
of 4h bars take about 0.6 s. For bar-based strategies the event engine is fast enough to
optimize on directly. The vectorized engine earns its place as an *independent check on the
engine plumbing*: it has already caught one real bug, a pandas unit change that silently
merged trading days.

Fill realism in the event-driven engine:
- Market order: fills by walking the recorded L2 book, level by level, plus a latency
  delay of `N(250ms, 80ms)` before the book is consulted.
- Limit order: fills only when trade prints *cross* the price, and then only for
  `min(remaining, queue_adjusted_size)` where queue position is estimated from the book
  state at submission. **Assuming 100% fill at your limit is the single most common way a
  backtest lies to you.**
- Post-only: rejected if it would cross, exactly as the venue does.
- Funding: applied at the venue's actual funding timestamps from historical data.
- Partial fills, rejects, and a configurable 0.1% "exchange error" rate are simulated.

### 3.5 `uxtrader.execution` — OMS, SOR, algos
- **OMS:** every order has a client order ID that is deterministic
  (`{strategy}-{symbol}-{intent_hash}`) so that a restart can reconcile rather than
  duplicate. State machine: `PENDING_NEW → NEW → PARTIALLY_FILLED → FILLED | CANCELED |
  REJECTED | AMBIGUOUS`.
- **Reconciliation on startup** — mandatory, blocking: fetch open orders + positions from
  every venue, diff against PostgreSQL, and refuse to start trading if they disagree
  beyond tolerance. Alert and require operator acknowledgement.
- **Smart order routing:** for a multi-venue symbol, split by (fee tier, top-of-book
  depth, recent fill quality, current exposure vs venue cap). Not latency — you are not
  in that race.
- **Execution algos:** `Market`, `PostOnlyPeg` (re-peg on book move, N improvements then
  cross), `TWAP`, `POV` (participate at x% of volume), `Iceberg`. Strategy intents name an
  algo; the algo is what talks to `uxcore`.

### 3.6 `uxtrader.risk` — pre-trade gate and kill switch
Synchronous veto on the path from intent to order. Checks in order (cheapest first):
1. Kill-switch state (global / per-strategy / per-venue).
2. Feed staleness.
3. Per-trade risk fraction.
4. Post-trade exposure: gross, net, per-asset, per-venue, per-correlation-cluster.
5. Drawdown ladder (daily / weekly / peak-to-trough).
6. Portfolio 99% 1-day VaR, computed from a 60-day EWMA covariance of strategy PnL.
7. Rate of order placement (a runaway loop guard: max 60 orders/min/strategy).

**Kill-switch philosophy.** Three independent layers, because the one that saves you is
the one you did not think you needed:
- **L1, in-process:** the risk engine's drawdown ladder. Fast, but dies with the process.
- **L2, out-of-process:** a separate watchdog with its own exchange credentials
  (read + cancel-only API key where the venue supports it) that flattens everything if
  the main system stops heartbeating for 90 seconds, or if account equity drops more than
  X% in Y minutes. **It does not share a process, a container, or a code path with the
  trading system.**
- **L3, at the venue:** exchange-native stop-loss orders resting on every position, placed
  at a level wider than your software stop. If everything you own catches fire, the venue
  still closes the position. Re-placed on every position change.

Kill switches must be **trivially triggerable by a human**: one button in the dashboard,
one Telegram command, one CLI invocation — all three hitting L2, not L1.

### 3.7 `uxtrader.portfolio` — the single source of truth
**Positions are held per (venue, symbol, strategy).** S1, S4, S5, S6 and S7 all trade the
BTC perpetual; with one shared net position their target-position intents would
overwrite each other. Each strategy owns a virtual book, and fills are attributed by the
strategy on the order. The venue's net position is the sum of those books, and
reconciliation compares that sum. The risk engine judges an intent against the strategy's
*own* book (a strategy's exit is always allowed), and applies caps to the portfolio's
resulting *net* exposure (a strategy trading against the rest of the book is never capped).

Owns positions, average entry, realized/unrealized PnL, per-strategy attribution, and the
correlation matrix. Reconciles against venue state every 60 seconds. **Strategies and the
risk engine read positions only from here, never from the exchange directly** — otherwise
two components disagree about the book and you get double-sizing.

### 3.8 `uxtrader.ops` — observability
- **Prometheus metrics:** order latency histograms, fill ratio, slippage vs arrival price
  (the most important execution metric you can have), WS staleness, rate-limit weight
  consumed, per-strategy PnL, position counts, reconciliation drift.
- **Structured logs** (JSON, `structlog`) → Loki. Every log line carries `strategy`,
  `symbol`, `client_order_id`, `intent_id`.
- **Alerts:** Telegram/Discord for trading events, PagerDuty-equivalent for infra.
  Tiered: `INFO` (fills), `WARN` (staleness, reject rate), `CRIT` (kill switch,
  reconciliation drift, venue unreachable).
- **The daily report:** automated 08:00 UTC summary — PnL by strategy, slippage vs model,
  fill ratios, limit breaches, positions held, and anything that deviated from backtest
  expectations by more than 2σ.

---

## 4. Data flow — the live path, precisely

```
1. venue WS ──▶ md-ingest
                 ├─ parse via uxcore (unified structures)
                 ├─ sequence check; gap ⇒ REST snapshot resync
                 ├─ write ClickHouse (async batch, 1s flush)
                 └─ publish NATS  md.{venue}.{type}.{symbol}
                                   │
2. strategy-engine subscribes ─────┘
                 ├─ aggregates ticks → bars (or consumes bars)
                 ├─ StrategyBase.on_bar/on_trade/on_book
                 └─ emits Intent ──▶ NATS  intent.{strategy}
                                        │
3. risk-engine  ────────────────────────┘
                 ├─ reads portfolio-service snapshot (cached 1s)
                 ├─ runs the check ladder
                 └─ APPROVE ⇒ NATS exec.order   |   VETO ⇒ NATS risk.veto (+alert)
                                        │
4. execution-engine ────────────────────┘
                 ├─ selects venue (SOR), selects algo
                 ├─ deterministic client_order_id, persist PENDING_NEW to Postgres
                 ├─ uxcore.create_order (resilient, idempotent)
                 └─ on ack: persist NEW; on error: classify + policy
                                        │
5. venue WS user-data ──▶ md-ingest ────┘
                 └─ publish NATS fill.{venue} ⇒ portfolio-service updates positions
                                                ⇒ strategy-engine on_fill
                                                ⇒ risk-engine recomputes exposure
```

**Latency budget, signal to order ack:** bar close → strategy 5 – 20 ms; NATS hop 1 ms;
risk gate 2 – 10 ms; SOR + sign 3 ms; network + venue 80 – 300 ms. **Total 100 – 350 ms.**
Every strategy in `docs/01` is designed to be insensitive to this. If a strategy needs
better, it does not belong on this platform.

---

## 5. Tech stack and justification

| Layer | Choice | Why this and not the alternative |
|---|---|---|
| Language (primary) | **Python 3.12 + uvloop** | The research-to-production path is the whole game. pandas/numpy/statsmodels/Optuna have no equal, and a strategy you can backtest in a notebook and deploy unchanged is worth more than 10x throughput you do not need. uvloop gives ~2 – 4x on the asyncio event loop for free. |
| Hot paths | **Rust via PyO3**, only where profiled | L2 order-book maintenance across 40+ symbols and rolling feature computation are the only places Python has measurably hurt. Write them last, behind the same interface. **Do not start in Rust** — you will spend six months building infrastructure instead of finding edge. |
| Message bus | **NATS JetStream** | At-least-once delivery with persistence, sub-ms latency, one 15 MB binary, no ZooKeeper. Kafka is the right answer at 100k+ msg/s with multiple consumer organizations; you have neither. Redis Streams is a fine simpler alternative if you are already running Redis. |
| Tick/market DB | **ClickHouse** | Purpose-built for this shape. ~10:1 compression on tick data, and time-bucketed aggregations at hundreds of millions of rows/s. TimescaleDB is a reasonable alternative if you want one Postgres to rule them all; QuestDB if you want simpler ops. |
| Transactional DB | **PostgreSQL 16** | Orders and fills need ACID. Nothing else qualifies. |
| Research storage | **Parquet on S3/MinIO**, `polars`/`duckdb` to read | Immutable, versioned, cheap, and readable from a laptop. |
| Cache / locks | **Redis** | Distributed lock for the execution-engine singleton, hot position cache, rate-limit token buckets shared across processes. |
| Backtest compute | numpy/pandas + plain Python loops | Measured: the event engine runs ~20k bars/s, and the vectorized engine is ~2x faster than that (§3.4). Polars is worth adding for S2's cross-sectional ranking over 40+ symbols; profile first. |
| Optimization | **Optuna** (TPE + Hyperband pruning) | Better than grid search at the sample sizes here, and the pruner kills bad trials early. |
| API | **FastAPI + Pydantic v2** | Schema validation on every boundary; Pydantic models double as the message contracts on NATS. |
| Dashboard | **Implemented:** a single self-contained page served by FastAPI (no build step, no CDN), with WS push. **Planned:** React + TypeScript + lightweight-charts once candle charts and analytics justify a build pipeline. | A trading dashboard's first job is to be available when everything else is on fire. One static file with no third-party scripts has the fewest ways to fail. |
| Containers | **Docker + Kubernetes** (or Nomad for 1 – 3 nodes) | K8s is genuinely overkill under 5 services; start with `docker compose` and migrate when you have more than one node. Manifests provided either way. |
| Secrets | **HashiCorp Vault** or **SOPS + age** | API keys never in env vars in a repo, never in the image. Separate keys per environment, IP-allowlisted, withdrawal permission **off**. |
| Monitoring | **Prometheus + Grafana + Loki + Alertmanager** | Standard, free, and every library you use already exports to it. |
| CI | **GitHub Actions** | Lint, type-check (`mypy --strict` on `uxtrader/`), unit tests, a deterministic backtest regression test, and a paper-trading smoke test on every PR. |

### On the "rewrite it in Rust" question
Profile first. In this design the only components where Python is plausibly the binding
constraint are (a) L2 book maintenance at high symbol counts, (b) the event-driven
backtester over tick data, (c) rolling feature computation in the research loop. All three
are cleanly isolatable behind a Python interface. The exchange connectivity layer, the
strategy engine, the risk engine, and the OMS are all I/O-bound and will never be the
bottleneck for strategies operating on 15m – 1d signals.

---

## 6. Repository structure

As implemented. Items marked *planned* are designed above but not yet written.

```
trading-platform/
├── src/
│   ├── uxcore/                      # ── the CCXT hard-fork layer ──
│   │   ├── exchange.py              # UXExchangeMixin + make_exchange(); round_to_market
│   │   ├── errors.py                # 5-class taxonomy + retry policy; read vs write rules
│   │   ├── ratelimit.py             # weight-aware limiter, 30% reserve for cancels
│   │   ├── resilience.py            # @resilient, circuit breaker, ClientIdReconciler
│   │   ├── ws.py                    # FeedMonitor: staleness + sequence gaps
│   │   ├── plugin_registry.py       # plugin registry
│   │   └── plugins/binance_pm.py    # portfolio-margin endpoints (others planned)
│   └── uxtrader/
│       ├── types.py  events.py  bus.py  clock.py  config.py
│       ├── strategy.py              # StrategyBase, StrategyContext
│       ├── portfolio.py             # per-(venue, symbol, strategy) books, attribution
│       ├── risk.py                  # pre-trade gate, drawdown ladder, kill switch
│       ├── run.py                   # entry point: --role all|ingest|portfolio|risk|execution|engine
│       ├── demo.py                  # full platform on a synthetic feed + dashboard
│       ├── services/                # portfolio, risk, execution, strategy_engine, common (locks, heartbeats)
│       ├── execution/               # oms, paper, live, algos*, router*
│       ├── data/                    # ingest (WS), history (backfill), store (Parquet), resample, universe
│       ├── lab/                     # backtest, vector, fills, metrics (DSR/PBO), walkforward, optimize
│       ├── strategies/              # S1–S7 + indicators; donchian_regime.py is the reference
│       ├── ops/                     # watchdog (L2), panic (RB-05), alerts, metrics
│       └── api/                     # FastAPI app + static dashboard
├── config/                          # config.example.yaml, strategies.example.yaml
├── ops/                             # Dockerfile, docker-compose.yml, k8s/, grafana/
├── tests/                           # unit, uxcore (real ccxt, HTTP stubbed), data, integration, ops, api
└── docs/
```
\* `algos.py` and `router.py` exist but are **not yet wired into the OMS**: every order is
currently a single market or limit order.

## 7. Key interfaces

The three contracts that define the system. Everything else is implementation.

```python
# 1. A strategy emits intents. It cannot place orders.
class Intent(BaseModel):
    intent_id:   str
    strategy:    str
    symbol:      str
    side:        Literal['buy', 'sell']
    target:      TargetSpec          # absolute target position OR delta
    algo:        AlgoSpec            # Market | PostOnlyPeg | TWAP | POV
    stop:        StopSpec | None     # attached protective stop
    urgency:     Literal['passive', 'normal', 'immediate']
    reason:      str                 # logged; makes post-mortems possible
    valid_until: datetime

# 2. Risk approves or vetoes. Synchronous, deterministic, testable in isolation.
class RiskDecision(BaseModel):
    approved:    bool
    intent_id:   str
    adjusted_qty: Decimal | None     # risk may SHRINK, never grow
    breaches:    list[LimitBreach]

# 3. Execution translates an approved intent into venue orders.
class ExecutionReport(BaseModel):
    client_order_id: str             # deterministic — the idempotency key
    state:       OrderState
    filled:      Decimal
    avg_price:   Decimal | None
    venue:       str
    slippage_bps: float | None       # vs arrival price — track this obsessively
```

**Target-position semantics, not order semantics.** Intents specify *where the position
should be*, and the OMS computes the delta against `portfolio-service`. This makes the
system idempotent under message replay and restart: replaying "target = 1.5 BTC" three
times leaves you with 1.5 BTC; replaying "buy 1.5 BTC" three times ends your career.

---

## 8. Paper trading that is indistinguishable from live

Paper mode differs from live in **exactly one place**: `execution/paper.py` implements the
same `Broker` protocol as the live path but matches against the live WS order book instead
of sending to the venue. Consequences:
- Same market data, same latency, same strategy code, same risk gate, same OMS state
  machine, same database writes, same dashboard.
- Paper fills use the *same* fill model as the event-driven backtester, which means paper
  results are directly comparable to backtest results — and a divergence between them is a
  signal that your fill model is wrong.
- The paper broker injects the same simulated reject/partial-fill rates as the backtester.
- Selected by a single config flag; **no `if paper:` branches anywhere else in the
  codebase.** Grep for `is_paper` outside `execution/` should return nothing.

Run paper and live **side by side on the same signals** during the scale-up phase
(`docs/03 §6`), and diff the PnL daily. The gap is your true execution cost.

---

## 9. Implementation status

What the code does today, as distinct from the design above. Every "done" item is
covered by tests (`pytest tests`) and runs offline.

| Component | Status | Notes |
|---|---|---|
| `uxcore` taxonomy, limiter, retry, reconcile, breaker | **Done** | Verified against the fork's own ccxt `binanceusdm` (HTTP stubbed): forced 429, forced timeout, lost-then-found orders, unreachable reconciliation |
| Event-driven backtester | **Done** | Next-bar-open fills, real UTC day/week baselines, risk flattens |
| Vectorized backtester (S4) | **Done** | Identical results to the event engine; ~2x faster |
| Risk engine | **Done** except VaR | `var_99_1d` is an input nobody computes yet; the check is inert until the EWMA covariance job exists |
| Per-strategy books | **Done** | Portfolio, risk, OMS, services |
| Services over the bus | **Done** | Portfolio, risk, execution, strategy engine; InMemoryBus tested end to end; NatsBus written but **not run against a NATS server here** |
| Historical loader, Parquet store, resampler | **Done** | Tested through real ccxt parsing |
| WS ingestion | **Done** | Tested against a scripted ccxt.pro-shaped exchange, not a live socket |
| Paper broker | **Done** | Market orders walk the book; limit orders need trade prints |
| Live broker | **Done, untested live** | Rounding, flags, rejections, user-stream fills tested offline |
| L1 kill (risk) + operator kill → L2 watchdog | **Done** | Dashboard/API kill reaches both |
| L3 venue-native stops | **Planned** | `StopSpec.venue_native` is carried on intents; nothing places the stop yet |
| Execution algos (PostOnlyPeg, TWAP, POV) in the OMS | **Planned** | Classes exist; not wired |
| Smart order routing | **Planned** | `SmartRouter` exists; single default venue today |
| Alerts, Prometheus metrics, dashboard, API | **Done** | Dashboard verified in Chromium at desktop and phone widths |
| ClickHouse / Postgres persistence | **Planned** | Research data is Parquet; service state is in memory and rebuilt from venues on start |
| Real-data validation (G1–G6) of any strategy | **Not started** | The development environment could not reach exchange APIs |

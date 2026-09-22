# Deployment and Operations

## 1. Environments

| Env | Purpose | Data | Credentials | Who can deploy |
|---|---|---|---|---|
| `dev` | Local development | Parquet sample + venue testnet | Testnet keys | Anyone |
| `paper` | Gate G6, continuous shadow | Live WS feeds, paper fills | **Read-only** live keys | CI on merge to `main` |
| `prod-small` | Staged live, ≤ 25% size | Live | Trade-enabled, withdrawal **disabled**, IP-allowlisted | Manual, with approval |
| `prod` | Full size | Live | Same, separate key set | Manual, with approval |

`paper` runs **permanently**, on the same signals as `prod`, from the same image. The
daily paper-vs-prod PnL diff is your live execution-cost measurement.

## 2. Secrets

Rules, in order of importance:
1. **Withdrawal permission is disabled on every API key.** No exceptions, ever. If a
   strategy needs to move funds between venues, that is a manual operation or a separate,
   air-gapped key with a human approval step and an allowlisted destination address.
2. IP allowlist every key to your egress IPs. Use a static NAT gateway.
3. Separate key per environment and per venue. Never share a key between `paper` and `prod`.
4. Keys live in Vault (or SOPS-encrypted files with `age`), injected at container start,
   never baked into images, never in `.env` files in the repo.
5. The L2 watchdog gets its **own** key with only `read` + `cancel` + `reduce-only`
   permissions where the venue supports scoping.
6. Rotate quarterly and after any incident. Have the rotation runbook written *before* you
   need it.

```bash
# Example: SOPS + age
age-keygen -o ops/keys/age.key
sops --encrypt --age $(cat ops/keys/age.key.pub) config/secrets.yaml > config/secrets.enc.yaml
# At container start:
sops --decrypt config/secrets.enc.yaml > /run/secrets/config.yaml
```

## 3. Local / single-node deployment

`ops/docker-compose.yml` brings up the full stack. Start here; you do not need Kubernetes
for one node, and running K8s on one node is a way to spend your evenings on YAML instead
of research.

```bash
docker compose -f ops/docker-compose.yml up -d clickhouse postgres redis nats
docker compose -f ops/docker-compose.yml up -d md-ingest portfolio risk execution
docker compose -f ops/docker-compose.yml up -d strategy-engine api web
docker compose -f ops/docker-compose.yml up -d prometheus grafana loki watchdog
```

**Hardware:** 8 vCPU / 32 GB RAM / 1 TB NVMe handles 40 symbols across 3 venues with full
L2 capture comfortably. Tick+book storage runs roughly 2 – 8 GB/day/venue uncompressed
before ClickHouse compression at full L2 depth; budget accordingly or restrict capture to
top-20 book levels.

**Location:** put the trading host in the same region as your primary venue's API
endpoint (Tokyo `ap-northeast-1` for Binance/Bybit, or the venue's documented region).
This is worth 50 – 150 ms of round-trip and costs nothing. It is not a latency-arbitrage
play; it is about your stop orders arriving during a cascade.

## 4. Kubernetes deployment

Manifests in `ops/k8s/`. The points that matter:

- **`execution-engine` is a `StatefulSet` with `replicas: 1`** plus a Redis-based
  distributed lock. Belt and braces: never let a rolling update run two of them.
  `strategy.type: Recreate` for that workload, not `RollingUpdate`.
- **`md-ingest` is a `DaemonSet`-like `Deployment` per venue**, each with its own
  liveness probe on feed freshness (`/healthz` returns 503 if the newest message is
  older than 5 s). Kubernetes restarting a wedged ingest process is the single highest-
  value automation here.
- **PodDisruptionBudgets** on everything; **`priorityClassName: system-cluster-critical`**
  on `execution-engine`, `risk-engine`, and `watchdog`.
- **The watchdog runs in a different namespace, on a different node pool, with a
  different image and different credentials.** Its entire purpose is to survive whatever
  kills the trading stack. If it shares a failure domain, it is decoration.
- Resource requests: strategy engines are CPU-spiky on bar boundaries — set requests at
  p50 and limits at 3× p50, or you will get throttled at exactly the wrong moment.
- No `latest` tags. Deploy by digest.

## 5. Monitoring — the dashboards that matter

**Grafana board 1 — Trading (what you look at every day)**
Equity curve · PnL by strategy (day/week/month) · current positions with unrealized PnL ·
gross/net exposure vs limits · drawdown vs the ladder · risk-limit breach count ·
kill-switch state.

**Grafana board 2 — Execution (what tells you the edge is real)**
Slippage vs arrival price (p50/p95, by symbol and algo) · fill ratio by algo · maker/taker
split · order latency histogram · reject rate by reason · **backtest-vs-live PnL diff**.

**Grafana board 3 — Infrastructure**
WS feed staleness per venue · rate-limit weight consumed vs budget · reconciliation drift ·
message bus lag · DB write latency · container restarts.

**Alert rules** (`ops/grafana/alerts.yaml`):

| Severity | Condition | Route |
|---|---|---|
| CRIT | Kill switch fired | Telegram + phone |
| CRIT | Reconciliation drift > 0.1% of NAV | Telegram + phone |
| CRIT | Any venue unreachable > 60 s | Telegram + phone |
| CRIT | Daily loss limit hit | Telegram + phone |
| WARN | Feed stale > 10 s | Telegram |
| WARN | Slippage p95 > 2× model for 1 h | Telegram |
| WARN | Rate-limit weight > 70% of budget | Telegram |
| WARN | Order reject rate > 5% over 15 min | Telegram |
| INFO | Fills, daily report | Discord channel |

**Alert fatigue is a safety problem.** If a WARN fires more than twice a week without
action, either fix the cause or delete the alert. An operator who ignores alerts has no
alerts.

## 6. Runbooks

Write these before you need them. Each is a numbered list a tired person can follow at
3 a.m.

### RB-01 — Venue unreachable
1. Confirm scope: is it one venue or your egress? Check `curl` from the host and from a
   second network.
2. If one venue: the circuit breaker has already opened. Verify no orders are in
   `PENDING_NEW` older than 60 s; any that are go to `AMBIGUOUS` and must be reconciled.
3. Check venue status page and announcement feed.
4. If outage > 15 min: reduce that venue's risk budget to zero for new entries. Existing
   positions have venue-native stops (L3); confirm they are present.
5. On recovery: run the reconciliation job **before** re-enabling entries. Do not skip it
   because the numbers "look right".

### RB-02 — Reconciliation drift detected
1. **Halt new entries immediately** (this is automatic; verify it happened).
2. Pull venue positions and open orders; diff against `portfolio-service` and Postgres.
3. Identify the missing/extra fill. Most common causes: a fill that arrived during a WS
   gap, a manual trade on the account, or an `AMBIGUOUS` order that filled.
4. Correct the database from **venue truth**, never the reverse.
5. Log the incident with root cause. Three drifts in a month is an architecture problem,
   not an ops problem.

### RB-03 — Kill switch fired
1. Do not immediately re-arm. The switch fired for a reason.
2. Confirm all positions are flat (or at their intended reduced state) at every venue.
3. Determine which layer fired (L1/L2/L3) and why.
4. If drawdown-triggered: the re-arm requires a written one-paragraph explanation of the
   loss and an explicit decision. Weekly-limit breaches require a 24-hour cooling period.
5. Re-arm at **50% size** for the following 30 trades.

### RB-04 — Strategy underperforming vs backtest
1. Check execution first: slippage, fill ratio, rejects. Most "alpha decay" is execution
   decay.
2. Re-run the backtest over the live period with actual fills substituted. If the gap
   closes, it is execution. If not, it is the signal.
3. If the signal: check regime classification. Was this a regime the strategy is not meant
   for? If so, the regime gate is broken, not the strategy.
4. Apply the auto-retirement rules from `docs/03 §6`. Do not negotiate with them.

### RB-05 — Suspected key compromise
1. Revoke the key at the venue **first**, before investigating anything.
2. Confirm withdrawal permission was disabled (it was, per §2, which is why this is
   survivable).
3. Flatten positions from a freshly issued key.
4. Rotate every key in that environment, audit access logs, rebuild the host.

## 7. Backup and disaster recovery

- **PostgreSQL:** continuous WAL archiving to S3, PITR, nightly full. Test the restore
  monthly — an untested backup is a belief, not a backup.
- **ClickHouse:** daily partition backup to S3. Tick data loss is tolerable (it is
  research input, not state); order/position data loss is not.
- **Config:** everything in git, secrets in Vault with its own backup.
- **RTO target: 15 minutes to flat, 60 minutes to trading.** The first number matters far
  more than the second. Rehearse the "flatten everything from a laptop with only API keys"
  drill quarterly; `uxtrader/ops/panic.py` exists for exactly this and takes no dependency
  on the rest of the stack.

## 8. Change management

- `main` is always deployable. Feature work happens on branches.
- CI on every PR: `ruff`, `mypy --strict` on `src/uxtrader/`, unit tests, and a
  **deterministic backtest regression test** — a fixed dataset and seed must produce a
  byte-identical equity curve. This catches accidental behaviour changes in shared code
  better than any unit test.
- Strategy parameter changes are code changes and go through PR review, with the
  walk-forward report attached.
- Deploy to `paper` automatically, to `prod` manually, never on a Friday, never during a
  scheduled macro event, and never while you hold a position the change affects.

# Strategy Specifications

Every strategy below is specified to the level where it can be implemented without
further design decisions: exact indicators, exact parameters, exact entry/exit/filter
conditions, sizing formula, risk limits, and regime applicability.

## 0. Conventions used throughout

### 0.1 Cost model — assume these numbers everywhere

| Item | Assumption | Note |
|---|---|---|
| Taker fee, perps | **5.5 bps** (0.055%) | Binance/Bybit VIP0. Use 4.0 bps only if you actually hold the volume tier. |
| Maker fee, perps | **2.0 bps** | Some venues rebate; do not model a rebate you have not received. |
| Taker fee, spot | **10.0 bps** | 7.5 bps with fee-token discount. |
| Slippage, majors, < $100k clip | **2 – 4 bps** taker | Model as `0.5 × spread + impact`. |
| Slippage, alts, < $50k clip | **8 – 20 bps** | Widen to 40 bps in stressed regimes. |
| Impact model | `impact_bps = k × sqrt(clip_notional / ADV_notional) × 10⁴`, k ≈ 0.4 | Square-root law. Calibrate `k` per symbol from your own fills. |
| Latency, signal → ack | **150 – 400 ms** REST, 80 – 200 ms WS | Backtest must delay fills by at least one full bar-close + 250 ms. |
| Funding | Actual historical funding, per 8h/1h interval | Never assume zero. |
| Borrow (spot short) | 5 – 30% APR, often unavailable | Assume unavailable unless verified. |

**Round-trip cost floor for a taker-in/taker-out perp trade: ~15 bps including
slippage.** Any strategy whose average gross win is under 45 bps is fee-dominated and
must be re-engineered as a maker strategy or discarded.

### 0.2 Position sizing — the common core

All directional strategies use **fractional-Kelly-capped volatility targeting**, never
raw Kelly. The unit of allocation is *risk*, not capital.

```
# Per-trade sizing (stop-based, for S4, S5, S7)
risk_capital   = equity * risk_fraction          # risk_fraction ∈ [0.005, 0.015]
stop_distance  = k_atr * ATR(period)             # in price units
qty            = risk_capital / stop_distance    # in base units
notional       = qty * price

# Volatility targeting overlay (applies to every strategy sleeve)
sigma_target   = 0.15                            # 15% annualized per sleeve
sigma_realized = ewma_std(returns, halflife=20d) * sqrt(365)
scalar         = clip(sigma_target / sigma_realized, 0.25, 2.0)
qty           *= scalar

# Fractional Kelly cap (portfolio level, not per trade)
f_kelly        = mu_excess / sigma^2             # from walk-forward OOS estimates ONLY
f_used         = min(0.25 * f_kelly, f_max)      # quarter-Kelly, never more
```

**Why quarter-Kelly.** Full Kelly assumes you know `mu` and `sigma`. You do not. With a
30% standard error on `mu` — optimistic for a 3-year crypto sample — full Kelly
overbets by roughly 2x and the growth-optimal fraction collapses. Quarter-Kelly gives up
~44% of theoretical growth to cut variance by ~94% and makes the strategy survivable
through estimation error. **Hard ceiling: `risk_fraction ≤ 1.5%` per trade, and
aggregate one-day 99% VaR ≤ 4% of equity.**

### 0.3 Universal risk limits (enforced by `uxtrader.risk`, not by strategy code)

| Limit | Value | Action on breach |
|---|---|---|
| Risk per trade | ≤ 1.0% (1.5% for S4 only) | Reject order |
| Daily loss | −3.0% of equity | Flatten all, halt new entries until 00:00 UTC |
| Weekly loss | −6.0% of equity | Halt, require manual re-arm |
| Peak-to-trough drawdown | −12% | Halve all sizing |
| " | −18% | Full stop, mandatory research review |
| Gross exposure | ≤ 3.0x equity aggregate | Reject |
| Net directional exposure | ≤ 1.0x equity | Reject |
| Single venue exposure | ≤ 25% of NAV | Reject |
| Single asset exposure | ≤ 20% of NAV | Reject |
| Correlated cluster exposure | ≤ 35% of NAV per cluster (ρ > 0.7, 30d) | Reject |
| Consecutive strategy losses | 8 | Auto-disable that strategy, alert |
| Data staleness | > 5s on any subscribed feed | Cancel resting orders, block new entries |

---

## S1 — Perpetual Funding Carry (delta-neutral basis harvest)

**Family:** carry. **Risk budget: 35%.** **Expected net: 1.8 – 3.0 Sharpe, 3 – 6% max DD
in normal conditions, with a binary venue-loss tail.**

### Economic rationale
Perpetual swaps have no expiry, so they are pinned to spot by a funding payment exchanged
between longs and shorts. Crypto retail is structurally, persistently long and leveraged.
Therefore funding is positive most of the time, and the entity willing to be short the
perp and long the spot collects it. **This is not an arbitrage — you are paid to warehouse
the risk that the venue fails, that the basis blows out while you are margined, or that
you get auto-deleveraged.** The edge does not decay because the risk does not go away.

### Instruments
- Perp leg: USDT-margined perps on Binance, Bybit, OKX, Hyperliquid.
- Spot leg: same venue where possible — a cross-margin or portfolio-margin account nets
  the two legs and eliminates inter-venue transfer risk. Cross-venue only if you have
  verified withdrawal speed and have pre-positioned collateral.
- Universe filter: 24h perp volume > $50M, open interest > $20M, listed > 90 days,
  spot pair exists on the same venue.

### Signal
```
# Forward-looking funding estimate, EWMA over the last 21 funding prints
f_ewma      = ewma(funding_rate_history[-21:], halflife=7)
periods_yr  = 1095 if interval == '8h' else 8760      # 1h venues
apr_est     = f_ewma * periods_yr

# Basis sanity check — the perp must actually be above spot
basis_bps   = (perp_mark - spot_mid) / spot_mid * 1e4
```

### Entry
All must hold:
1. `apr_est > 12%` — the hurdle. Derivation: entry + exit costs ≈ 19 bps (2 legs × taker
   in and out, spot side at 10 bps). At a 7-day minimum hold, breakeven APR =
   `0.0019 / (7/365) = 9.9%`. 12% gives a 20% buffer.
2. `basis_bps > 5` — never short a perp trading *below* spot to collect funding; the
   convergence loss will exceed the carry.
3. Last 3 funding prints all positive (no coin-flip funding).
4. Venue exposure after the trade ≤ 25% of NAV, asset exposure ≤ 20%.
5. Estimated liquidation distance on the perp leg ≥ **35%** adverse move.

### Exit
Any of:
- `apr_est < 3%` → unwind at limit, no urgency.
- Two consecutive negative funding prints → unwind.
- `basis_bps < −10` → unwind immediately (taker), convergence is working against you.
- Liquidation distance < 20% and auto-top-up failed → **reduce both legs 50% immediately**.
- Venue risk trigger (see below) → unwind at any cost, market orders acceptable.

### Sizing
```
sleeve_equity  = equity * 0.35
per_asset_cap  = sleeve_equity * 0.15
notional       = min(per_asset_cap,
                     0.02 * open_interest_notional,     # never > 2% of OI
                     0.05 * adv_notional)               # never > 5% of daily volume
```
Gross leverage on the sleeve: **1.0x without portfolio margin, up to 2.5x with it**, and
only after you have confirmed how that venue computes maintenance margin on a hedged
book *in a stress event* — not in the docs, in a simulated stress on testnet.

### Risk management — this is where the strategy lives or dies
| Risk | Control |
|---|---|
| Venue insolvency | ≤ 25% NAV per venue. Daily proof-of-reserve / withdrawal smoke test: withdraw a token amount every day; if it takes > 30 min, **cut that venue's exposure 50%**. |
| ADL (auto-deleverage) | Keep your ADL ranking low by not running extreme leverage or extreme unrealized PnL on the short leg. Monitor the venue's ADL indicator; at 4/5 lights, reduce. |
| Mark-price divergence | Margin buffer ≥ 35%; auto top-up bot with a pre-funded reserve of 20% of sleeve equity held in stables *off* the trading venue but transferable in < 10 min. |
| Stablecoin depeg | Cap USDT-denominated notional at 60% of sleeve; diversify into USDC-margined where available. Depeg > 1% → unwind the affected leg. |
| Delisting | Exclude assets with < 90d listing history or on any venue's monitoring/innovation-zone tag. |
| Funding schedule change | Venues change funding intervals and caps with hours of notice. Subscribe to venue announcement feeds; treat a schedule change as an exit trigger pending re-evaluation. |

### Expected characteristics (prior)
- Gross carry: 8 – 25% APR on deployed notional in normal conditions; 40 – 120% APR in
  euphoric windows lasting days to weeks; near zero or negative in deep bear.
- Net after fees and infrastructure: **4 – 15% APR at 1.0x, 10 – 30% at 2.5x portfolio
  margin.**
- Sharpe 1.8 – 3.0 measured on the smooth part of the distribution.
- Max DD 3 – 6% in normal operation. **Tail: −100% of the capital at one venue.** The
  Sharpe is a lie about the tail; size by the tail, not the Sharpe.
- Win rate is meaningless here (~95% of funding intervals are positive). Use APR and tail.

### Regime suitability and regime detection
Best in bull and late-bull (high retail leverage). Compresses to nothing in bear. In
sustained negative funding, the **reverse carry** (long perp, short spot) is available
only if spot borrow exists at a rate below the funding you collect — usually it does not,
so the correct action in a bear regime is to **shrink this sleeve and reallocate risk to
S4.** Detect with: 30-day median funding APR across the top-20 universe. `> 8%` = carry
regime, `< 2%` = shrink to 40% of target, `< 0%` = flat.

### Implementation sketch
```python
import ccxt.pro as ccxtpro   # → uxcore in the platform
import pandas as pd, numpy as np

PERIODS = {'8h': 1095, '4h': 2190, '1h': 8760}

def funding_apr(history: list[dict], interval: str, halflife: int = 7) -> float:
    """EWMA-smoothed annualized funding from a list of CCXT funding-rate records."""
    s = pd.Series([h['fundingRate'] for h in history[-21:]], dtype=float)
    if len(s) < 8:
        return 0.0
    return float(s.ewm(halflife=halflife).mean().iloc[-1]) * PERIODS[interval]

async def scan(ex, universe: list[str], interval: str = '8h') -> pd.DataFrame:
    rows = []
    for sym in universe:
        hist   = await ex.fetch_funding_rate_history(sym, limit=21)
        ticker = await ex.fetch_ticker(sym)
        spot   = await ex.fetch_ticker(sym.split(':')[0])          # 'BTC/USDT:USDT' → 'BTC/USDT'
        apr    = funding_apr(hist, interval)
        basis  = (ticker['last'] - spot['last']) / spot['last'] * 1e4
        oi     = await ex.fetch_open_interest(sym)
        rows.append({'symbol': sym, 'apr': apr, 'basis_bps': basis,
                     'oi_notional': oi['openInterestValue'],
                     'adv': ticker['quoteVolume'],
                     'last3_positive': all(h['fundingRate'] > 0 for h in hist[-3:])})
    df = pd.DataFrame(rows)
    return df[(df.apr > 0.12) & (df.basis_bps > 5) & df.last3_positive
              & (df.oi_notional > 20e6) & (df.adv > 50e6)].sort_values('apr', ascending=False)
```
Production version: `src/uxtrader/strategies/funding_carry.py`.

---

## S2 — Cross-Sectional Momentum Rotation

**Family:** cross-sectional momentum. **Risk budget: 20%.** **Expected: 0.8 – 1.4 Sharpe
long/short, 20 – 30% max DD on the vol-targeted book.**

### Economic rationale
Underreaction to information plus flow-driven herding produce continuation in *relative*
performance over 1 – 3 months. Documented in equities, futures, currencies, and crypto.
The compensation is for bearing **momentum-crash risk**: the factor experiences violent,
skewed reversals when a beaten-down cohort rebounds during a volatility spike.

### Universe — construct point-in-time, this matters more than the signal
- Top 40 USDT perps by 30-day median dollar volume, **snapshotted weekly and stored**, so
  backtests use the universe as it was, not as it is. This is your survivorship-bias
  defense (`docs/03 §3`).
- Exclude: stablecoins, wrapped/staked derivatives of an asset already in the universe
  (WBTC, stETH), listings < 90 days old, anything with 30d median volume < $20M.
- Exclude assets whose venue tag is innovation/monitoring/seed.

### Signal — 4h bars aggregated to daily, rebalance weekly
```
r30  = log(P_t-3d / P_t-33d)         # skip the most recent 3 days (short-term reversal)
r90  = log(P_t-3d / P_t-93d)
s30  = realized_vol(returns_daily, 30) * sqrt(365)
s90  = realized_vol(returns_daily, 90) * sqrt(365)

score = 0.5 * zscore_cross_section(r30 / s30) + 0.5 * zscore_cross_section(r90 / s90)
```
Vol-scaling the returns before ranking is what turns a beta bet into a factor bet. Do not
rank raw returns.

### Portfolio construction
- **Long** the top quintile (8 names), **short** the bottom quintile (8 names).
- Weights within a leg: `w_i ∝ 1 / sigma_i`, normalized so each leg sums to 1.0 —
  inverse-vol, not equal-weight, not score-weighted (score-weighting overfits).
- Dollar-neutral: long notional = short notional. Residual BTC beta is hedged with a BTC
  perp overlay if `|beta_portfolio| > 0.15`.
- **Rebalance:** Monday 00:00 UTC. **No-trade band:** only adjust a position if the target
  weight differs from current by more than **25% relative**. This cuts turnover ~40% at a
  cost of ~5% of gross return — a very good trade.
- Execute with 30-minute TWAP slices, post-only with a 5-bps aggressive fallback after
  60% of the window.

### Filters and overlays
| Filter | Condition | Action |
|---|---|---|
| Regime | BTC daily EMA50 < EMA200 | Halve gross |
| Correlation | Median pairwise 30d correlation of universe > 0.85 | Halve gross (momentum is just beta here) |
| Dispersion | Cross-sectional stdev of 30d returns < 25th percentile of trailing year | Skip rebalance, hold |
| Funding | Short leg funding APR < −25% | Drop that name (you'd be paying to be short) |
| Crash brake | Strategy DD > 10% from its own peak | Cut gross 50% for 2 weeks, then re-enter at 75% |

### Sizing
```
sleeve_equity = equity * 0.20
gross         = sleeve_equity * vol_scalar        # vol_scalar targets 15% sleeve vol
                                                   # typically 0.6x – 1.2x of sleeve equity
```

### Expected characteristics (prior)
- Per-name hit rate 45 – 52% — *low, by design*. The edge is in the right tail.
- Profit factor 1.2 – 1.4, Sharpe 0.8 – 1.4.
- Max DD 20 – 30%, driven by 2 – 4 momentum crashes per decade that lose 15 – 20% in
  under two weeks.
- Turnover: ~35% of gross per week after the no-trade band. Annual cost drag ≈ 6 – 9% of
  gross — significant, which is why the band matters.

### Regime detection
Compute weekly: (a) BTC 50/200 EMA state; (b) median pairwise correlation; (c) cross-
sectional return dispersion percentile. Full gross only when trend is up **and**
correlation < 0.85 **and** dispersion > 40th percentile. Otherwise scale down. Never
scale *up* beyond 1.0 based on a favorable regime — asymmetric response only.

---

## S3 — Cointegration Pairs / Statistical Arbitrage

**Family:** stat-arb. **Risk budget: 15%.** **Expected: 1.0 – 1.8 Sharpe, 12 – 20% max
DD. Highest edge-decay risk in the book.**

### Economic rationale
Assets with a shared fundamental driver (two L1s, two L2s, two exchange tokens) trade with
a stationary log-price spread over medium horizons. Deviations are driven by idiosyncratic
flow and revert as relative-value traders and index rebalancers step in. You are paid for
providing liquidity to that flow and for bearing **cointegration-breakdown risk** — the
relationship can permanently change (a hack, a tokenomics change, a chain migration).

### Pair universe
Candidate clusters, screened monthly:
- `ETH / BTC` (the anchor pair — deepest, most reliable)
- L1: `SOL, AVAX, NEAR, APT, SUI, ADA` — all pairwise
- L2: `ARB, OP, MATIC/POL`
- Exchange tokens: `BNB, OKB, BGB`
- Memes are excluded. No shared fundamental driver, only shared mania.

### Pair qualification — a pair trades only if ALL hold
1. Engle-Granger ADF on the residual, 180-day window: **p < 0.05**.
2. Same test on a held-out 90-day window immediately after: **p < 0.10**. (Two-window
   confirmation is the single most effective anti-overfit filter here.)
3. Ornstein-Uhlenbeck half-life ∈ **[6 hours, 10 days]**. Below 6h you are fighting fees;
   above 10 days your capital is dead and funding eats the trade.
4. Spread return correlation with every currently-held pair < 0.5.
5. Both legs: 24h volume > $30M.
6. Combined funding drag over the expected hold < 30% of expected mean-reversion PnL.

### Hedge ratio — Kalman filter, not rolling OLS
```
# State-space: log(P_a,t) = beta_t * log(P_b,t) + alpha_t + eps_t
# beta and alpha follow a random walk.
# Tune Q/R so the implied beta half-life ≈ 20 days: slow enough to be stable,
# fast enough to track a genuine structural shift.
delta = 1e-5
Q     = delta / (1 - delta) * I(2)     # state covariance
R     = 0.001                          # observation variance — set from residual variance
```
Rolling OLS with a fixed window creates artificial jumps when an outlier exits the window;
the Kalman filter degrades gracefully. **Do not** re-estimate beta while a position is
open beyond the Kalman update — never re-fit the window mid-trade, that is look-ahead
laundering.

### Signal, entry, scaling
```
spread = log(P_a) - beta_t * log(P_b) - alpha_t
mu, sd = rolling_mean(spread, 240), rolling_std(spread, 240)    # 240 × 1h bars = 10 days
z      = (spread - mu) / sd
```
- Entry at `|z| > 2.0` — 1 unit.
- Scale in at `|z| > 2.5` — +1 unit; at `|z| > 3.0` — +1 unit. **Max 3 units.**
- Direction: `z > 0` → short A, long B (sized by `beta_t`). `z < 0` → the reverse.

### Exit
- **Target:** `|z| < 0.3`.
- **Time stop:** `5 × half_life`, flatten regardless of z.
- **Hard stop:** `|z| > 4.0` → flatten immediately. No exceptions, no averaging down past
  3 units. The loss at 4.0 from an average entry near 2.4 is ~1.6σ of spread — sized so
  that is 0.75% of equity.
- **Breakdown stop:** trailing-90d ADF p-value > 0.10 on three consecutive daily checks
  → flatten and **blacklist the pair for 30 days**.

### Sizing
```
sleeve_equity  = equity * 0.15
risk_per_pair  = equity * 0.0075                  # 0.75% at the hard stop
sigma_spread   = sd                               # in log units
stop_distance  = (4.0 - entry_z) * sigma_spread   # log-space distance to hard stop
notional_a     = risk_per_pair / stop_distance
notional_b     = notional_a * beta_t
```
Max 6 concurrent pairs; max 2 pairs sharing a leg.

### Expected characteristics (prior)
- Win rate 62 – 70% (high, because you exit at the mean).
- Profit factor 1.3 – 1.6. Average win ≈ 0.6 × average loss; the hard stop produces the
  left tail, and a broken cointegration produces the *real* left tail.
- Sharpe 1.0 – 1.8 when the qualification filters are honestly applied — 2.5+ in any
  backtest where they are not, which is how you know you have look-ahead.
- Max DD 12 – 20%.

### Regime suitability
Works best in **sideways and moderately trending** regimes. Dangerous during regime
transitions and idiosyncratic events (a chain outage, an exploit, an ETF approval for one
leg only). Halve gross when cross-sectional dispersion is in the top decile — that is
exactly when spreads break rather than revert.

---

## S4 — Donchian Breakout with Regime Filter (the workhorse)

**Family:** time-series trend. **Risk budget: 20%.** **Expected: 0.6 – 1.0 Sharpe,
25 – 35% max DD, long flat periods. The most robust strategy here.**

### Economic rationale
Trend following is compensation for providing liquidity to slow-moving capital and for
enduring long, painful drawdowns that most participants will not tolerate. It has
survived out-of-sample in every liquid futures market since the 1970s. **Its low Sharpe is
the price of its robustness.** The moment you "improve" it to a 2.0 backtest Sharpe with
extra filters, you have destroyed the thing that made it work.

### Instruments and timeframe
BTC, ETH, SOL perps as core; optionally 5 liquid alts. **4h bars**, signals evaluated on
bar close only, orders placed on the next bar open.

### Entry — long (short is symmetric)
All must hold at 4h bar close:
1. **Breakout:** `close > max(high[-55:-1])` — Donchian 55.
2. **Trend filter:** daily `close > EMA(200)`.
3. **Volatility regime:** `ADX(14) > 20` on 4h **OR** `sigma_10d / sigma_60d ∈ [0.7, 1.8]`.
   The band excludes both dead markets (no follow-through) and post-blow-off exhaustion.
4. **Range expansion:** breakout bar `(high − low) > 1.2 × ATR(14)`.
5. **Crowding veto:** funding APR < 60%. A breakout into extreme positive funding is
   frequently the top.

### Exit
- **Initial stop:** `entry − 2.5 × ATR(14)`.
- **Move to breakeven** once `+1R`.
- **Chandelier trail:** once `+1R`, stop = `max(close since entry) − 2.5 × ATR(14)`,
  ratcheting only.
- **Partial:** take 50% at `+2R`, trail the rest.
- **Donchian exit:** `close < min(low[-20:-1])` closes the remainder.
- **Time stop:** none. Trends need room.

### Pyramiding
Add `0.5R` at `+1.5R` and again at `+3.0R`, **max 2 adds**, each add's stop moved to the
combined-position breakeven. Total risk never exceeds the initial 1.5%.

### Sizing
```
risk_fraction = 0.015                     # S4 is the one strategy allowed 1.5%
qty           = (equity * risk_fraction) / (2.5 * ATR14)
qty          *= vol_scalar                # 15% sleeve vol target
```

### Expected characteristics (prior)
- **Win rate 33 – 40%.** You will be wrong about two trades in three. This is normal and
  is not a reason to change the system.
- Profit factor 1.3 – 1.6; average win ≈ 2.8 × average loss.
- Sharpe 0.6 – 1.0, max DD 25 – 35%, **longest flat period historically 6 – 11 months.**
- Trades per instrument: ~8 – 14 per year on 4h bars.

### Regime suitability
Makes its money in the 20% of the time markets trend, and bleeds the other 80%. Do not
add a filter that tries to sit out the bleed — every such filter in the literature removes
more winners than losers out-of-sample. The regime filter above is already at the edge of
what is safe; three of the five conditions are there to avoid *catastrophic* entries, not
to optimize the hit rate.

---

## S5 — Intraday VWAP Mean Reversion on Majors

**Family:** short-horizon mean reversion / flow. **Risk budget: 5%.** **Expected:
1.0 – 1.6 gross Sharpe, 0.5 – 1.0 net. Fee-dominated; maker execution is mandatory.**

### Economic rationale
Short-horizon dislocations come from impatient flow: liquidations, large market orders,
and index/ETF hedging. Reverting them is liquidity provision, and the compensation is for
inventory risk. **This only works if you are the maker.** As a taker you are paying
5.5 bps to capture a 30 bps move with a 60% hit rate — the math is marginal at best.

### Instruments and timeframe
BTC and ETH perps only. **15m bars** for the signal, WS order book and trades for
confirmation.

### Regime gate — trade only when the market is actually mean-reverting
```
# Variance ratio over 5-minute returns, q=30, trailing 3 days
VR = var(r_30bar) / (30 * var(r_1bar))
```
Trade only if **`VR < 0.90`** (mean-reverting), daily `ADX(14) < 20`, and annualized
realized vol ∈ [40%, 120%]. Outside this gate the strategy is **off**, not scaled down.

### Entry
```
z = (close - VWAP_session) / stdev(close - VWAP_session, 96)   # 96 × 15m = 24h
```
Enter counter-trend when all hold:
1. `|z| > 2.2`.
2. **Exhaustion:** the volume of the last 3 bars is *declining* while price extends —
   `vol[-1] < vol[-2] < vol[-3]` and `vol[-1] < 0.8 × SMA(vol, 20)`.
3. **Book confirmation:** top-10-level order-book imbalance from the WS feed has flipped
   *toward* the reversion side over the last 60 seconds (`imbalance = (bid_qty −
   ask_qty)/(bid_qty + ask_qty)`, require a 0.15 swing).
4. **No event veto:** not within 30 minutes of a scheduled macro release (CPI, FOMC, NFP)
   from a maintained calendar.
5. Entry is **post-only** at the current best on the reversion side, 3 price improvements
   max, cancel and skip if unfilled after 4 minutes.

### Exit
- Target `z → ±0.2`, exit post-only, taker fallback after 10 minutes.
- Stop: `1.5 × ATR(14, 15m)` from entry — **taker, immediate**.
- Time stop: 4 hours.
- Flat before any scheduled macro event.

### Sizing
`risk_fraction = 0.005` (0.5%), max 3 concurrent positions, one per symbol.

### Expected characteristics (prior)
- Win rate 60 – 68%, profit factor 1.15 – 1.35.
- Average gross win ~35 – 50 bps, average loss ~55 – 70 bps.
- **At 2 bps maker in / 2 bps maker out, costs take ~12% of gross. At 5.5 bps taker both
  ways, costs take ~30% and the Sharpe roughly halves.**
- Retire the strategy if the live maker fill ratio drops below 55% for a month, or if
  the trailing-90d net profit factor drops below 1.05.

### Honest assessment
This is the most crowded and most decay-prone strategy in the book. Budget quarterly
re-estimation of `z` thresholds and the VR gate, and be willing to turn it off
permanently. It earns its 5% allocation only because it is nearly uncorrelated with
S1 – S4.

---

## S6 — Adaptive Grid with Trend Filter and Hard Kill

**Family:** short volatility. **Risk budget: 3%.** **Expected: 1.0 – 2.0 Sharpe in
regime; the kill switch is the strategy.**

### Honest framing first
A grid is a short-gamma, short-volatility position. It sells small amounts of optionality
repeatedly and collects premium, then hands it all back in one trend. Its 90% "win rate"
is an artifact of measuring per-fill rather than per-position. It is included here at a
**3% risk budget** because it genuinely produces uncorrelated income in range regimes —
and it is capped at 3% because ungated grids are the single most common way retail bots
blow up.

### Instruments
BTC and ETH — spot, or perps at ≤ 2x. Never alts: the trend risk is unbounded and the
range assumption is weaker.

### Regime gate (checked hourly; all must hold or the grid does not exist)
1. Daily `ADX(14) < 18`.
2. Price within the daily Bollinger(20, 2) band.
3. Realized-vol percentile over the trailing year < 60th.
4. Daily `EMA(50)` and `EMA(200)` within 5% of each other (no established trend).
5. For perps: `|funding APR| < 10%` against the direction inventory will accumulate.

### Grid construction
```
spacing   = 0.6 * ATR(1h, 14)             # adaptive to current volatility
levels    = 12 per side
level_qty = (sleeve_equity * 0.0035) / price      # 0.35% of sleeve per level
recenter  = when |mid - grid_center| > 4 * spacing
```
Re-centering cancels and re-places; it does **not** close inventory.

### Inventory and kill
| Control | Value |
|---|---|
| Max net inventory | 25% of sleeve equity |
| Beyond cap | Stop adding on that side; the other side keeps working |
| **Kill 1** | Price exits the daily Bollinger band by `2.5 × ATR(daily)` → **flatten everything, taker** |
| **Kill 2** | Regime gate fails (EMA50/200 cross, ADX > 25) → stop new levels, exit inventory on the next favorable touch, hard exit after 24h |
| **Kill 3** | Sleeve drawdown > 6% → flatten, disable for 7 days |
| **Kill 4** | Realized vol doubles in 24h → flatten |

### Expected characteristics (prior)
- In-regime: 0.5 – 2.0% per month on sleeve equity.
- Profit factor 1.1 – 1.25 measured per *position*, not per fill.
- Max DD 15 – 25% **with** the kill switches; unbounded without them.
- Roughly 3 – 5 months of income lost in a single failed regime call per year.

---

## S7 — Funding / Open-Interest Squeeze Fade

**Family:** forced flow. **Risk budget: 2%.** **Expected: 0.9 – 1.4 Sharpe, low
frequency, high estimation error.**

### Economic rationale
Liquidation cascades are mechanical: leveraged positions are force-closed by the venue's
engine at whatever price is available, and the resulting print overshoots fair value
because the liquidation engine is price-insensitive. Fading it is inventory provision
during a moment of guaranteed adverse selection — which is precisely why it pays.

### Signal (15m bars + WS liquidation/trade feed)
All must hold:
1. **Crowding:** funding APR z-score over trailing 30 days `> 2.5` **and** open interest
   up `> 15%` over 24h.
2. **Cascade trigger:** a bar with `range > 3 × ATR(14)` against the crowded side,
   accompanied by a liquidation-volume print in the top 1% of the trailing 30-day
   distribution.
3. **Reversal confirmation:** within the next 2 bars, price closes back inside the trigger
   bar's range.
4. Direction: fade the liquidated side. Longs liquidated → **buy**.

### Entry / exit
- Entry: limit at the trigger bar's midpoint, valid 2 bars.
- Stop: `1.2 × wick_depth` beyond the extreme (wick_depth = extreme to trigger-bar close).
- Target: the 24h VWAP, or `+2R`, whichever comes first. Trail the last third.
- Time stop: 12 hours.
- **Never add to the position.** If it goes against you, the cascade is not over.

### Sizing
`risk_fraction = 0.0075`, one position at a time across all instruments.

### Expected characteristics (prior)
- 2 – 6 setups per month across BTC/ETH/SOL.
- Win rate 50 – 58%, profit factor 1.4 – 1.8, Sharpe 0.9 – 1.4.
- **Low frequency means high estimation error** — 30 trades a year gives you a standard
  error on the Sharpe of roughly ±0.5. Treat any single-year result as uninformative.

---

## 8. Portfolio construction across strategies

### Risk allocation (by risk contribution, not capital)
| Strategy | Risk budget | Rationale |
|---|---|---|
| S1 Carry | 35% | Highest Sharpe, structural edge, but the tail forces the cap |
| S2 XS momentum | 20% | Good Sharpe, high capacity, diversifies S4 |
| S4 Trend | 20% | Lowest Sharpe, but the most robust and the only long-vol sleeve |
| S3 Pairs | 15% | Good Sharpe, needs the most maintenance |
| S5 Intraday MR | 5% | Uncorrelated, decay-prone |
| S6 Grid | 3% | Uncorrelated income, short vol |
| S7 Squeeze fade | 2% | Uncorrelated, low frequency |

### Why this combination works
The key structural property: **S4 is long volatility, S6 and S1 are short volatility, and
S3/S5 are short volatility-of-volatility.** In a crash, S1 and S6 lose while S4 makes its
year. In a grind, S4 bleeds while S1/S6 pay the bills. Expected pairwise correlations:

```
        S1     S2     S4     S3     S5     S6     S7
S1    1.00   0.25   0.05   0.15   0.05   0.35   0.10
S2    0.25   1.00   0.55   0.20   0.00   0.15   0.05
S4    0.05   0.55   1.00   0.05  -0.10  -0.30  -0.20
S3    0.15   0.20   0.05   1.00   0.15   0.20   0.10
S5    0.05   0.00  -0.10   0.15   1.00   0.25   0.30
S6    0.35   0.15  -0.30   0.20   0.25   1.00   0.15
S7    0.10   0.05  -0.20   0.10   0.30   0.15   1.00
```
(Priors — re-estimate from your own live PnL on a 60-day rolling window and feed them into
the portfolio risk layer. The S2/S4 correlation of 0.55 is the one to watch; if it exceeds
0.75 for a month, cut one of them.)

### Combined expectation
**Sharpe 1.5 – 2.2, max drawdown 12 – 18%, net 25 – 60% annualized at 1.0x aggregate
gross.** Halve that for planning purposes. Any portfolio-level backtest that shows > 3.0
Sharpe has a bug — go find it.

### Capital staging
| Equity | Run | Rationale |
|---|---|---|
| < $25k | S1 + S4 only | Fees and minimum order sizes make the rest unviable |
| $25k – $100k | + S2, S6 | XS momentum needs ~16 positions; below this the clips are too small |
| $100k – $500k | + S3, S7 | Pairs need two legs per position and real margin headroom |
| > $500k | + S5, multi-venue | Maker infrastructure and fee tiers start to pay off |

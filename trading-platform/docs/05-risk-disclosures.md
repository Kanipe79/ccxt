# Risk Disclosures — What Can Still Go Wrong

Written plainly, because the failure modes below have each ended real trading operations.

## 1. The honest base rate

The majority of systematic retail and small-professional crypto operations lose money over
a full cycle. The ones that survive usually do so because of **risk control and
operational discipline**, not because of a better signal. Nothing in this repository
changes that base rate on its own. A well-implemented version of this portfolio is a
*reasonable* attempt at a positive-expectancy business; it is not a reliable income
stream, and it should not be funded with money you need.

## 2. Strategy-specific failure modes

### S1 Funding carry — the tail is binary
- **Venue insolvency.** FTX paid excellent funding right up until it did not exist. Your
  Sharpe of 2.5 describes the 99% of days when the venue is solvent. The 25%-per-venue
  cap is what actually protects you, and it caps your loss at **25% of NAV, not 0%.**
- **ADL / socialized loss.** During extreme moves your profitable short leg can be
  force-closed by the venue's deleveraging engine, leaving you long spot into a crash.
- **Stablecoin depeg.** Your collateral and your quote currency are the same asset. A 5%
  USDT depeg with a 2.5x levered book is a 12.5% hit on the sleeve.
- **Funding regime change.** Venues have changed funding caps and intervals with hours of
  notice, and a sustained bear market simply removes the trade.

### S2 Cross-sectional momentum — momentum crashes
- The factor's returns are negatively skewed with a fat left tail. Historical momentum
  crashes have taken 15 – 20% out of a vol-targeted book **in under two weeks**, and they
  cluster with volatility spikes, so they arrive exactly when your other sleeves hurt too.
- The short leg has unbounded loss and can become un-borrowable or un-shortable precisely
  when you most need to exit.

### S3 Pairs — cointegration is not a law of nature
- A "stationary" spread is stationary until a hack, an exploit, a chain migration, a
  tokenomics change, or an ETF approval on one leg permanently re-rates one asset. The
  ADF-breakdown blacklist limits the damage; it does not prevent the first loss.
- The strategy's high win rate makes losses feel like anomalies. They are not. They are
  the distribution.

### S4 Trend — the drawdowns are the product
- Expect **6 – 11 month flat-to-losing periods.** Most people abandon trend following
  during exactly these periods, which is why it still works. Your real risk is behavioural:
  you will want to "improve" it after month four.

### S5 Intraday MR — crowding and fee sensitivity
- The most competitive space in the book. Your counterparty is frequently a market maker
  with better fees, better latency, and a rebate. Plan for the edge to disappear.

### S6 Grid — short volatility wearing a costume
- The win rate is cosmetic. The loss distribution is one large left tail. Without the kill
  switches this strategy has unbounded loss, and the kill switches will sometimes fire at
  the worst possible price.

### S7 Squeeze fade — you are catching a falling knife on purpose
- By construction you enter during maximum adverse selection. The stop is the strategy; a
  single "just this once" override can exceed a year of the strategy's profit.

## 3. Platform and operational risks

| Risk | Consequence | Mitigation (and its residual) |
|---|---|---|
| **Software bug placing bad orders** | Direct, immediate loss | Risk engine as a hard gate; intents not orders; target-position semantics. Residual: a bug *in the risk engine*. Hence the independent L2 watchdog and venue-native L3 stops. |
| **Duplicate execution engine** | Double positions | Singleton + distributed lock + deterministic client order IDs. Residual: a split-brain during a network partition. |
| **Stale data → trading on old prices** | Adverse fills, wrong signals | Staleness watchdog, cancel-on-stale, feed freshness liveness probes. |
| **Silently wrong order book** | Worse than no book | Sequence-gap detection + snapshot resync. Residual: a venue that does not provide sequence numbers — for those, periodic full-snapshot comparison. |
| **Ambiguous order state** (timeout after send) | Duplicate or phantom position | Deterministic client IDs + mandatory reconciliation. **Never blind-retry a create_order.** |
| **Clock drift** | Signature rejection, wrong bar boundaries | chrony/NTP with monitoring; alert on > 100 ms drift. |
| **Rate limiting during a cascade** | You cannot reduce risk when it matters most | Weight-aware limiter reserves 30% headroom for cancel/reduce operations. This reserve is not optional. |
| **Key compromise** | Theft | Withdrawal disabled, IP allowlist, per-env keys. Residual: an attacker can still trade your account into losses — which is why position limits are also venue-side where possible. |
| **Cloud provider outage** | Total loss of control | L3 venue-native stops; rehearsed laptop-based flatten procedure. |
| **Your own manual intervention** | Historically a leading cause of loss | Manual trades on a systematic account are forbidden. If you must intervene, flatten via the kill switch — do not "manage" the position. |

## 4. Market-structure and regulatory risks

- **Liquidity is regime-dependent.** Order-book depth on alts can fall 80% in a stress
  event. Your slippage model, calibrated on normal conditions, will understate costs by
  3 – 10x precisely when you are trying to exit.
- **Exchange outages during volatility** are routine, not exceptional. Venues have gone
  down, suspended withdrawals, and cancelled trades during the exact moves your strategies
  are designed to capture.
- **Regulatory change** can remove a venue, a product, or a jurisdiction with little
  notice. Perpetual futures are not available to retail in several jurisdictions, and
  access rules change.
- **Tax treatment** of high-frequency derivatives trading varies enormously and can turn a
  profitable strategy into a loss after tax. Get advice before scaling, not after.

## 5. Model risks

- **Every number in `docs/01` is a prior, not a result.** They have not been backtested in
  this repository. Your own G1 – G6 pipeline output replaces them.
- **Correlations rise toward 1 in a crisis.** The diversification in `docs/01 §8` is
  weakest exactly when you need it. Size the portfolio assuming pairwise correlations of
  0.7 in a stress scenario, not the 0.15 you measured in calm markets.
- **Regime classification is itself a model** and it will be wrong at turning points —
  which is where the money is made and lost.
- **Backtest overfitting is the default outcome.** The pipeline in `docs/03` reduces the
  probability; it does not eliminate it.

## 6. What "acceptable drawdown" actually means

Decide these numbers **before** you deploy capital, write them down, and give someone else
the ability to hold you to them:

| Level | Portfolio DD | Response |
|---|---|---|
| Amber | −8% | Review every strategy's live-vs-backtest gap. No size change. |
| Orange | −12% | **Automatic** 50% size reduction across all strategies. |
| Red | −18% | **Automatic** full stop. Flatten. Mandatory two-week research review before any restart, at 25% size. |
| Black | −25% | Stop permanently. The model portfolio's 95th-percentile MC drawdown should be well below this; reaching it means something in your model is fundamentally wrong, not unlucky. |

**The kill switch philosophy in one sentence:** every stop must be automatic, because the
moment you most need it is the moment you will be most certain that it is wrong.

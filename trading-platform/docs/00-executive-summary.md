# Executive Summary — Strategy Portfolio and Platform

> **Epistemic status.** The performance figures in these documents are *priors*, not
> backtest output. Nothing here has been backtested inside this repository. They come
> from published cross-asset research on the same factor families (time-series momentum,
> cross-sectional momentum, carry, statistical arbitrage), from the structural economics
> of each trade, and from what these strategy families have historically delivered in
> crypto after realistic costs. **Treat every number as a hypothesis your own walk-forward
> pipeline must confirm or kill.** Section `docs/03-backtesting-validation.md` exists
> precisely so you do not have to trust these numbers.

## The thesis in one paragraph

There are exactly four durable sources of return in liquid crypto that a retail-to-small-
institutional operator can actually capture: (1) **carry** — perpetual funding paid by
leveraged directional traders to whoever will take the other side; (2) **time-series
trend** — slow, persistent, low-Sharpe, high-capacity, and the only factor with 50+ years
of out-of-sample evidence across every asset class; (3) **cross-sectional dispersion** —
relative winners keep winning over weeks; (4) **mean reversion around forced flow** —
liquidation cascades and inventory shocks that exist because someone *must* trade. Every
one of these is a compensation for bearing a real risk. Anything that is not a
compensation for a real risk is either a data-mining artifact or a race you will lose to
someone with a rack in Tokyo. This portfolio allocates only to the four.

## Ranked strategy table

Ranked by expected risk-adjusted return *net of realistic costs*, with implementation
difficulty and edge-decay risk.

| # | Strategy | Family | Exp. net Sharpe | Exp. Max DD | Capacity | Impl. difficulty | Edge decay | Risk budget |
|---|---|---|---|---|---|---|---|---|
| **S1** | Perp funding carry (delta-neutral) | Carry | **1.8 – 3.0** | 3 – 6% (tail: venue loss) | Very high | Medium | **Low** | 35% |
| **S2** | Cross-sectional momentum rotation | XS momentum | 0.8 – 1.4 | 20 – 30% | High | Low | Low-med | 20% |
| **S3** | Cointegration pairs / stat-arb | Stat-arb | 1.0 – 1.8 | 12 – 20% | Medium | High | **High** | 15% |
| **S4** | Donchian breakout + regime filter | TS trend | 0.6 – 1.0 | 25 – 35% | Very high | **Low** | **Very low** | 20% |
| **S5** | Intraday VWAP mean reversion | MR / flow | 1.0 – 1.6 gross, 0.5 – 1.0 net | 10 – 18% | Low | Medium | **High** | 5% |
| **S6** | Adaptive grid + trend filter | Short vol | 1.0 – 2.0 in-regime | 15 – 25% *with* kill switch | Medium | Medium | Medium | 3% |
| **S7** | Funding/OI squeeze fade | Forced flow | 0.9 – 1.4 | 10 – 20% | Low | Medium | Low | 2% |

Combined, risk-weighted, with the correlation structure described in
`docs/01-strategies.md §8`: **target portfolio Sharpe 1.5 – 2.2, max drawdown 12 – 18%,
net return 25 – 60% annualized on equity at 1.0x aggregate gross** — in a *good* two-year
window. Plan for half of that, and size so that the 95th-percentile bad outcome does not
end the operation.

## What is robust vs. what needs babysitting

**Robust — set it up and it keeps working:**
- **S4 (trend)** is the most robust thing in the book. Four parameters, works on wheat
  futures since 1970, works on BTC. It will also lose money for eight months at a stretch.
  Its low Sharpe is the *price* of its robustness; do not "improve" it into an overfit.
- **S1 (carry)** is structural: it is paid by the persistent long bias of leveraged retail.
  The edge does not decay, but the *tail* is brutal and binary (venue insolvency, ADL,
  stablecoin depeg). Your risk work here is operational and counterparty, not statistical.
- **S2 (XS momentum)** is the crypto version of a factor documented in 40+ markets. It
  suffers periodic momentum crashes that look nothing like its normal distribution.

**Needs continuous adaptation — budget ongoing research time:**
- **S3 (pairs)** — cointegration relationships genuinely break. Expect to rotate ~30% of
  your pair universe per quarter, and expect at least one pair per year to break *while
  you are in it*. The hard z-stop and the ADF-breakdown blacklist are non-negotiable.
- **S5 (intraday MR)** — the most fee-sensitive and the most crowded. It only survives if
  you are a maker. Re-fit thresholds quarterly; retire it without sentiment if the live
  maker fill ratio drops below 55%.
- **S6 (grid)** — this is a short-volatility position wearing a costume. Its win rate is
  cosmetic. It works until it doesn't, and the kill switch *is* the strategy.

## What is explicitly discarded, and why

| Discarded | Reason |
|---|---|
| Latency / cross-exchange arbitrage | You are racing firms with colocated servers and sub-ms inventory. By the time a CCXT REST or WS round trip completes, the spread is gone. Without colo you are systematically adversely selected. |
| Triangular arbitrage | Same, plus fee-inclusive triangles on a single venue are essentially never open on liquid pairs. |
| Sub-minute scalping | Requires the same infrastructure, plus your fee tier will not be the one that makes the math work. |
| Indicator-soup ML on OHLCV | Signal-to-noise on 5m bars is ~0.01 Sharpe per feature. 40 features on 2 years of data will backtest at 3.0 Sharpe and go live at −0.5. See `docs/03 §2`. |
| "AI sentiment" / social alpha as a primary signal | Non-stationary, unlicensable at scale, and the vendor is already trading it. Acceptable only as a *veto* filter. |
| Copy trading / signal groups | The alpha is negative by construction after the signal is distributed. |
| Anything with >8 free parameters per instrument | Cannot be validated at the sample sizes crypto gives you. |

## The platform in one paragraph

`ux-trader` is a hard fork of CCXT (`uxcore`) wrapped in an event-driven trading system.
The fork exists for four reasons that upstream CCXT reasonably will not solve for you:
a **weight-aware adaptive rate limiter** instead of a fixed sleep, a **unified error
taxonomy with typed retry policy**, a **self-healing WebSocket layer** with sequence-gap
detection and snapshot resync, and a **plugin system** for private/undocumented endpoints
you must not upstream. On top of it sit a strategy engine, a dual vectorized +
event-driven backtester with realistic fills, a live execution engine with smart order
routing, a hard-limit risk layer with a kill switch, a data pipeline (ClickHouse +
Parquet), and a FastAPI/React dashboard. Python 3.12 + uvloop is the primary stack; Rust
is used only where profiling proves it is needed (order-book maintenance, feature
computation). Full design in `docs/02-architecture.md`.

## Read in this order

1. `docs/01-strategies.md` — the seven strategies, fully specified.
2. `docs/02-architecture.md` — platform, modules, data flow, tech stack.
3. `docs/03-backtesting-validation.md` — how to not fool yourself. **Read this before writing a backtest.**
4. `docs/04-deployment-operations.md` — Docker/K8s, secrets, monitoring, runbooks.
5. `docs/05-risk-disclosures.md` — what can still go wrong. All of it.
6. `docs/06-roadmap.md` — v0.1 to production in 18 weeks.

Code skeletons live in `src/uxcore/` (the fork layer) and `src/uxtrader/` (the platform).

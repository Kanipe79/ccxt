"""S1 — Perpetual funding carry, delta-neutral. Spec: docs/01-strategies.md §S1.

The highest-Sharpe strategy in the book and the one whose Sharpe tells you the least.
You are short the perp and long the spot, collecting funding from leveraged longs. The
smooth part of the distribution is genuinely excellent. The tail is a venue going to
zero with your collateral inside it.

Consequently, most of the code below is risk plumbing rather than signal: margin
buffers, venue caps, basis convergence checks and unwind triggers. That ratio is
correct. The signal is four lines; the reason this strategy survives is everything else.
"""
from __future__ import annotations

import logging
from decimal import Decimal

from ..strategy import StrategyBase, StrategyContext
from ..types import AlgoKind, AlgoSpec, Bar, Funding, Intent

log = logging.getLogger(__name__)


class FundingCarry(StrategyBase):
    name = 'funding_carry'
    timeframe = '1h'

    # Entry hurdle. Derivation (docs/01 §S1): entry+exit ≈ 19 bps across both legs; at a
    # 7-day minimum hold that is 9.9% APR breakeven. 12% carries a 20% buffer.
    MIN_APR = 0.12
    EXIT_APR = 0.03
    MIN_BASIS_BPS = 5.0
    EXIT_BASIS_BPS = -10.0
    MIN_LIQ_DISTANCE = 0.35        # ≥35% adverse move to liquidation
    DERISK_LIQ_DISTANCE = 0.20     # below this, halve both legs immediately
    MAX_ASSET_FRACTION = 0.15      # of sleeve equity
    MAX_OI_FRACTION = 0.02         # never more than 2% of open interest
    MAX_ADV_FRACTION = 0.05

    def __init__(self, ctx: StrategyContext) -> None:
        super().__init__(ctx)
        self._apr: dict[str, float] = {}
        self._recent_rates: dict[str, list[float]] = {}
        self._basis_bps: dict[str, float] = {}
        self._liq_distance: dict[str, float] = {}
        self._oi_notional: dict[str, float] = {}
        self._adv_notional: dict[str, float] = {}

    # -- data in ---------------------------------------------------------------

    async def on_funding(self, funding: Funding) -> list[Intent]:
        s = funding.symbol
        self._apr[s] = funding.apr
        rates = self._recent_rates.setdefault(s, [])
        rates.append(float(funding.rate))
        del rates[:-21]
        return []

    def observe_market(self, symbol: str, *, basis_bps: float, liq_distance: float | None,
                       oi_notional: float, adv_notional: float) -> None:
        """Fed by the strategy engine from uxcore helpers each cycle.

        Kept as an explicit setter rather than an exchange call so the same code path
        runs unchanged in backtest, where these come from stored history.
        """
        self._basis_bps[symbol] = basis_bps
        if liq_distance is not None:
            self._liq_distance[symbol] = liq_distance
        self._oi_notional[symbol] = oi_notional
        self._adv_notional[symbol] = adv_notional

    # -- decisions --------------------------------------------------------------

    async def on_bar(self, bar: Bar) -> list[Intent]:
        if not bar.closed:
            return []
        s = bar.symbol
        position = self.ctx.position(s)
        mark = float(bar.close)

        if position != 0:
            return self._manage(s, mark)
        return self._maybe_enter(s, mark)

    def _maybe_enter(self, s: str, mark: float) -> list[Intent]:
        apr = self._apr.get(s, 0.0)
        basis = self._basis_bps.get(s, 0.0)
        rates = self._recent_rates.get(s, [])

        if apr < self.MIN_APR:
            return []
        # Never short a perp trading below spot to collect funding — the convergence
        # loss exceeds the carry.
        if basis < self.MIN_BASIS_BPS:
            return []
        if len(rates) < 3 or not all(r > 0 for r in rates[-3:]):
            return []
        liq = self._liq_distance.get(s)
        if liq is not None and liq < self.MIN_LIQ_DISTANCE:
            return []

        notional = self._sized_notional(s, mark)
        if notional <= 0:
            return []

        qty = Decimal(str(notional / mark))
        # Short the perp. The long spot leg is placed by the engine's hedge handler as
        # a paired intent on the spot symbol; both legs are sized from this number.
        return [self.target(
            s, -qty,
            reason=f'carry apr={apr:.1%} basis={basis:.1f}bps notional={notional:,.0f}',
            algo=AlgoSpec(kind=AlgoKind.POST_ONLY_PEG, cross_after_s=300),
            urgency='passive')]

    def _manage(self, s: str, mark: float) -> list[Intent]:
        apr = self._apr.get(s, 0.0)
        basis = self._basis_bps.get(s, 0.0)
        rates = self._recent_rates.get(s, [])
        position = self.ctx.position(s)

        # Margin emergency first — this one is immediate and takes liquidity.
        liq = self._liq_distance.get(s)
        if liq is not None and liq < self.DERISK_LIQ_DISTANCE:
            log.critical('s1_margin_emergency symbol=%s liq_distance=%.2f', s, liq)
            return [self.target(s, position / 2,
                                reason=f'liq distance {liq:.2f} — halving both legs',
                                algo=AlgoSpec(kind=AlgoKind.MARKET), urgency='immediate')]

        # Convergence working against us.
        if basis < self.EXIT_BASIS_BPS:
            return [self.flatten(s, f'basis {basis:.1f}bps — unwinding immediately')]

        # Carry gone.
        if apr < self.EXIT_APR:
            return [self.target(s, Decimal('0'),
                                reason=f'apr {apr:.1%} below exit hurdle',
                                algo=AlgoSpec(kind=AlgoKind.POST_ONLY_PEG,
                                              cross_after_s=1800),
                                urgency='passive')]

        # Two consecutive negative prints.
        if len(rates) >= 2 and rates[-1] < 0 and rates[-2] < 0:
            return [self.target(s, Decimal('0'),
                                reason='two consecutive negative funding prints',
                                algo=AlgoSpec(kind=AlgoKind.POST_ONLY_PEG),
                                urgency='passive')]
        return []

    def _sized_notional(self, s: str, mark: float) -> float:
        sleeve = float(self.ctx.sleeve_equity)
        caps = [sleeve * self.MAX_ASSET_FRACTION]
        oi = self._oi_notional.get(s, 0.0)
        adv = self._adv_notional.get(s, 0.0)
        if oi > 0:
            caps.append(oi * self.MAX_OI_FRACTION)
        if adv > 0:
            caps.append(adv * self.MAX_ADV_FRACTION)
        return min(caps)

    async def on_stale(self, feed: str, age: float) -> list[Intent]:
        # Short-vol and margined: a stale feed means we cannot see a basis blowout
        # coming. Stop adding, but do not panic-unwind a hedged book on a data gap.
        log.warning('s1_feed_stale feed=%s age=%.1fs — entries blocked', feed, age)
        return []

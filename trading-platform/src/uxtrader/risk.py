"""Pre-trade risk gate and the kill-switch ladder.

This module is the hard boundary between "a strategy had an idea" and "money moved".
It is synchronous, deterministic, and unit-testable in isolation with no I/O — which
means you can and must write a test for every limit in ``docs/01 §0.3``.

Three properties worth preserving if you modify this:
  * Risk may SHRINK an intent. It may never grow one.
  * Checks run cheapest-first, so a tripped kill switch costs microseconds.
  * Every veto is logged with the observed and allowed values. A veto you cannot explain
    afterwards is a veto you will eventually disable in frustration.
"""
from __future__ import annotations

import logging
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from decimal import Decimal

from .types import Intent, LimitBreach, Position, RiskDecision

log = logging.getLogger(__name__)


@dataclass
class RiskLimits:
    """Defaults mirror docs/01 §0.3. Override per deployment, never per strategy."""

    max_risk_per_trade: float = 0.010          # 0.015 allowed for trend only
    max_daily_loss: float = 0.030
    max_weekly_loss: float = 0.060
    dd_halve_at: float = 0.12
    dd_stop_at: float = 0.18
    max_gross_leverage: float = 3.0
    max_net_leverage: float = 1.0
    max_venue_fraction: float = 0.25
    max_asset_fraction: float = 0.20
    max_cluster_fraction: float = 0.35
    max_consecutive_losses: int = 8
    max_orders_per_min: int = 60
    max_feed_staleness_s: float = 5.0
    max_var_99_1d: float = 0.04


@dataclass
class RiskState:
    """Everything the gate needs to decide, and nothing it does not."""

    equity: Decimal
    peak_equity: Decimal
    day_start_equity: Decimal
    week_start_equity: Decimal
    # Every open (venue, symbol, strategy) book. A dict keyed by symbol is accepted
    # for convenience (single-strategy tests and the vector engine) and normalised.
    positions: list[Position] | dict[str, Position] = field(default_factory=list)
    marks: dict[str, Decimal] = field(default_factory=dict)
    clusters: dict[str, str] = field(default_factory=dict)    # symbol → cluster id
    stale_feeds: set[str] = field(default_factory=set)
    consecutive_losses: dict[str, int] = field(default_factory=dict)
    orders_this_min: dict[str, int] = field(default_factory=dict)
    var_99_1d: float = 0.0
    now: datetime | None = None         # required for the daily halt to expire

    def __post_init__(self) -> None:
        if isinstance(self.positions, dict):
            self.positions = list(self.positions.values())

    def strategy_qty(self, strategy: str, symbol: str) -> Decimal:
        return sum((p.quantity for p in self.positions            # type: ignore[union-attr]
                    if p.symbol == symbol and p.strategy == strategy), Decimal('0'))

    def net_qty(self, symbol: str) -> Decimal:
        return sum((p.quantity for p in self.positions            # type: ignore[union-attr]
                    if p.symbol == symbol), Decimal('0'))

    @property
    def drawdown(self) -> float:
        if self.peak_equity <= 0:
            return 0.0
        return float((self.peak_equity - self.equity) / self.peak_equity)

    @property
    def daily_pnl_pct(self) -> float:
        if self.day_start_equity <= 0:
            return 0.0
        return float((self.equity - self.day_start_equity) / self.day_start_equity)

    @property
    def weekly_pnl_pct(self) -> float:
        if self.week_start_equity <= 0:
            return 0.0
        return float((self.equity - self.week_start_equity) / self.week_start_equity)


def is_reducing(have: Decimal, target: Decimal) -> bool:
    """True when moving from `have` to `target` strictly lowers exposure without
    crossing through flat. A flip (long 10 → short 5) is NOT reducing: it opens a new
    position and must pass every check an entry would."""
    if have == 0:
        return False
    if target == 0:
        return True
    return (target > 0) == (have > 0) and abs(target) < abs(have)


class KillSwitch:
    """Layer 1 — in-process. Fast, and dies with the process, which is why L2 exists.

    Scopes are independent: a per-strategy kill does not stop the others, a global kill
    stops everything. Re-arming is always explicit and always manual.
    """

    def __init__(self) -> None:
        self.global_armed = True
        self.disabled_strategies: set[str] = set()
        self.disabled_venues: set[str] = set()
        self.reasons: list[str] = []

    def fire(self, reason: str, *, strategy: str | None = None,
             venue: str | None = None) -> None:
        self.reasons.append(reason)
        if strategy:
            self.disabled_strategies.add(strategy)
            log.critical('kill_switch_strategy strategy=%s reason=%s', strategy, reason)
        elif venue:
            self.disabled_venues.add(venue)
            log.critical('kill_switch_venue venue=%s reason=%s', venue, reason)
        else:
            self.global_armed = False
            log.critical('kill_switch_global reason=%s', reason)

    def blocked(self, strategy: str, venue: str | None) -> str | None:
        if not self.global_armed:
            return 'global kill switch'
        if strategy in self.disabled_strategies:
            return f'strategy {strategy} disabled'
        if venue and venue in self.disabled_venues:
            return f'venue {venue} disabled'
        return None

    def rearm(self, *, strategy: str | None = None, venue: str | None = None) -> None:
        """Explicit and manual, by design. See RB-03."""
        if strategy:
            self.disabled_strategies.discard(strategy)
        elif venue:
            self.disabled_venues.discard(venue)
        else:
            self.global_armed = True
        self.reasons.clear()


class RiskEngine:
    def __init__(self, limits: RiskLimits | None = None,
                 kill: KillSwitch | None = None) -> None:
        self.limits = limits or RiskLimits()
        self.kill = kill or KillSwitch()
        self.entries_halted_until: datetime | None = None
        self._flatten_reason: str | None = None

    # -- portfolio-level limits --------------------------------------------------

    def check_limits(self, state: RiskState) -> str | None:
        """The drawdown ladder. Called on every mark AND before every entry, so a
        breach flattens the book even when no strategy is trying to trade.

        Returns the reason entries are blocked, or None. Side effects are
        deliberate and ordered by severity:
          * peak-to-trough ≥ dd_stop_at → global kill (manual re-arm) + flatten
          * weekly loss                  → global kill (manual re-arm) + flatten
          * daily loss                   → halt entries until 00:00 UTC + flatten
        """
        L = self.limits
        dd = state.drawdown
        if dd >= L.dd_stop_at:
            if self.kill.global_armed:
                self.kill.fire(f'drawdown {dd:.1%} >= {L.dd_stop_at:.1%}')
                self._flatten_reason = 'drawdown stop'
            return 'drawdown_stop'
        if state.weekly_pnl_pct <= -L.max_weekly_loss:
            if self.kill.global_armed:
                self.kill.fire(f'weekly loss {state.weekly_pnl_pct:.2%}')
                self._flatten_reason = 'weekly loss limit'
            return 'weekly_loss'
        if state.daily_pnl_pct <= -L.max_daily_loss and not self._halted(state.now):
            now = state.now or datetime.now(timezone.utc)
            self.entries_halted_until = (now + timedelta(days=1)).replace(
                hour=0, minute=0, second=0, microsecond=0)
            self._flatten_reason = 'daily loss limit'
            log.critical('daily_loss_halt pnl=%.2f%% until=%s',
                         state.daily_pnl_pct * 100, self.entries_halted_until)
        if self._halted(state.now):
            return 'daily_loss'
        return None

    def take_flatten_request(self) -> str | None:
        """The engine calls this after each mark; a non-None reason means: emit a
        flattening intent for every open position now. Consumed exactly once."""
        reason, self._flatten_reason = self._flatten_reason, None
        return reason

    def _halted(self, now: datetime | None) -> bool:
        if self.entries_halted_until is None:
            return False
        if now is not None and now >= self.entries_halted_until:
            self.entries_halted_until = None
            return False
        return True

    # -- the per-intent ladder ---------------------------------------------------

    def evaluate(self, intent: Intent, state: RiskState) -> RiskDecision:
        breaches: list[LimitBreach] = []
        L = self.limits
        # Intents are strategy-level: compare against THIS strategy's book, and apply
        # the caps to the portfolio's resulting NET exposure.
        have = state.strategy_qty(intent.strategy, intent.symbol)
        target = intent.target.position

        # 0. De-risking is ALWAYS permitted — through a fired kill switch, a drawdown
        # stop, a stale feed, anything. A risk engine that can veto a stop-loss exit
        # is a risk engine that can hold a losing position open indefinitely.
        if is_reducing(have, target):
            return RiskDecision(intent_id=intent.intent_id, approved=True,
                                adjusted_position=target, note='risk-reducing')
        # Zero-change intents only move a protective stop. Blocking one would leave
        # the old, looser stop resting at the venue.
        if target == have and have != 0:
            return RiskDecision(intent_id=intent.intent_id, approved=True,
                                adjusted_position=target, note='stop update')

        # 1. kill switch — cheapest check first
        blocked = self.kill.blocked(intent.strategy, intent.venue)
        if blocked:
            return self._veto(intent, 'kill_switch', 1.0, 0.0, note=blocked)

        # 2. drawdown ladder / daily halt
        ladder = self.check_limits(state)
        if ladder is not None:
            observed = {'drawdown_stop': state.drawdown,
                        'weekly_loss': abs(state.weekly_pnl_pct),
                        'daily_loss': abs(state.daily_pnl_pct)}[ladder]
            allowed = {'drawdown_stop': L.dd_stop_at, 'weekly_loss': L.max_weekly_loss,
                       'daily_loss': L.max_daily_loss}[ladder]
            return self._veto(intent, ladder, observed, allowed)

        # 3. feed staleness — never size into a stale book
        if intent.symbol in state.stale_feeds:
            return self._veto(intent, 'stale_feed', L.max_feed_staleness_s,
                              L.max_feed_staleness_s, note=f'{intent.symbol} stale')

        # 4. runaway-loop guard
        n = state.orders_this_min.get(intent.strategy, 0)
        if n >= L.max_orders_per_min:
            self.kill.fire(f'order rate {n}/min', strategy=intent.strategy)
            return self._veto(intent, 'order_rate', float(n), float(L.max_orders_per_min))

        # 5. consecutive losses
        if state.consecutive_losses.get(intent.strategy, 0) >= L.max_consecutive_losses:
            self.kill.fire('consecutive losses', strategy=intent.strategy)
            return self._veto(intent, 'consecutive_losses',
                              float(state.consecutive_losses[intent.strategy]),
                              float(L.max_consecutive_losses))

        # 6. exposure caps — SHRINK rather than veto
        mark = state.marks.get(intent.symbol)
        if mark is None or mark <= 0:
            return self._veto(intent, 'no_mark', 0.0, 0.0, note='no mark price')

        allowed_qty = self._max_position(intent, state, mark)
        net_now = state.net_qty(intent.symbol)
        others = net_now - have
        new_net = others + target
        adjusted = target
        # Only shrink when this change pushes net exposure past the cap AND grows it.
        # A strategy trading against the rest of the book reduces net risk; capping it
        # would perversely keep the portfolio more exposed.
        if abs(new_net) > allowed_qty and abs(new_net) > abs(net_now):
            capped_net = allowed_qty if new_net > 0 else -allowed_qty
            lo, hi = min(have, target), max(have, target)
            adjusted = min(max(capped_net - others, lo), hi)   # never past the request
            breaches.append(LimitBreach(limit='position_cap',
                                        observed=float(abs(new_net) * mark / state.equity),
                                        allowed=float(allowed_qty * mark / state.equity),
                                        hard=False))

        # 7. portfolio VaR
        if state.var_99_1d > L.max_var_99_1d:
            return self._veto(intent, 'var_99', state.var_99_1d, L.max_var_99_1d)

        dd = state.drawdown
        if dd >= L.dd_halve_at:
            adjusted *= Decimal('0.5')
            breaches.append(LimitBreach(limit='drawdown_halve', observed=dd,
                                        allowed=L.dd_halve_at, hard=False))

        # Shrinking can never turn an increase into something *larger* than holding:
        # if the capped target is smaller than what we already hold (same side), hold.
        if have != 0 and (adjusted > 0) == (have > 0) and abs(adjusted) < abs(have):
            adjusted = have

        return RiskDecision(intent_id=intent.intent_id, approved=True,
                            adjusted_position=adjusted, breaches=tuple(breaches),
                            note='shrunk' if adjusted != target else '')

    # -- helpers ---------------------------------------------------------------

    def _max_position(self, intent: Intent, state: RiskState, mark: Decimal) -> Decimal:
        """Tightest of the asset / venue / cluster / gross caps, in base units."""
        L = self.limits
        eq = state.equity
        caps = [Decimal(str(L.max_asset_fraction)) * eq / mark]

        if intent.venue:
            venue_used = sum(p.notional(state.marks.get(p.symbol, Decimal('0')))
                             for p in state.positions
                             if p.venue == intent.venue and p.symbol != intent.symbol)
            caps.append(max(Decimal('0'),
                            Decimal(str(L.max_venue_fraction)) * eq - venue_used) / mark)

        cluster = state.clusters.get(intent.symbol)
        if cluster:
            cluster_used = sum(p.notional(state.marks.get(p.symbol, Decimal('0')))
                               for p in state.positions
                               if state.clusters.get(p.symbol) == cluster
                               and p.symbol != intent.symbol)
            caps.append(max(Decimal('0'),
                            Decimal(str(L.max_cluster_fraction)) * eq - cluster_used) / mark)

        gross_used = sum(p.notional(state.marks.get(p.symbol, Decimal('0')))
                         for p in state.positions if p.symbol != intent.symbol)
        caps.append(max(Decimal('0'),
                        Decimal(str(L.max_gross_leverage)) * eq - gross_used) / mark)

        return min(caps)

    def _veto(self, intent: Intent, limit: str, observed: float,
              allowed: float, note: str = '') -> RiskDecision:
        log.warning('risk_veto strategy=%s symbol=%s limit=%s observed=%.4f allowed=%.4f %s',
                    intent.strategy, intent.symbol, limit, observed, allowed, note)
        return RiskDecision(
            intent_id=intent.intent_id, approved=False, adjusted_position=None,
            breaches=(LimitBreach(limit=limit, observed=observed, allowed=allowed),),
            note=note or limit,
        )


def underlying(symbol: str) -> str:
    """'BTC/USDT:USDT' and 'BTC/USDT' → 'BTC'. Spot and perp of one asset are one risk."""
    return symbol.split('/')[0].split(':')[0]


class VarModel:
    """Conservative one-day 99% VaR.

    * Per-underlying volatility from an EWMA of squared log returns, normalised by
      elapsed *market* time (bar timestamps, never wall clock), so 1m and 4h inputs give
      the same daily number.
    * Positions are netted per underlying first: a delta-neutral carry trade (long
      spot, short perp) is close to zero risk, not twice the risk.
    * Across underlyings, correlation is taken as 1, which is an upper bound. That is
      deliberately pessimistic until a proper covariance estimate exists, and it is
      honest about being one.
    * Until an underlying has ``min_samples`` returns, it uses ``prior_annual_vol``
      (100%): unknown assets are assumed to be volatile.
    """

    Z99 = 2.326

    def __init__(self, halflife_samples: float = 500.0, min_samples: int = 30,
                 prior_annual_vol: float = 1.0) -> None:
        import math
        self.alpha = 1 - math.exp(-math.log(2) / halflife_samples)
        self.min_samples = min_samples
        self.prior_rate = prior_annual_vol ** 2 / (365 * 86400)     # variance per second
        self._last: dict[str, tuple[float, float]] = {}             # und → (price, ts)
        self._rate: dict[str, float] = {}
        self._n: dict[str, int] = {}

    def update(self, symbol: str, price: float, ts: datetime) -> None:
        import math
        und = underlying(symbol)
        t = ts.timestamp()
        prev = self._last.get(und)
        self._last[und] = (price, t)
        if prev is None or price <= 0 or prev[0] <= 0 or t <= prev[1]:
            return
        r = math.log(price / prev[0])
        rate = r * r / (t - prev[1])
        old = self._rate.get(und)
        self._rate[und] = rate if old is None else self.alpha * rate + (1 - self.alpha) * old
        self._n[und] = self._n.get(und, 0) + 1

    def daily_vol(self, symbol: str) -> float:
        und = underlying(symbol)
        rate = self._rate.get(und) if self._n.get(und, 0) >= self.min_samples else None
        return ((rate if rate is not None else self.prior_rate) * 86400) ** 0.5

    def var_99(self, positions: list[Position], marks: dict[str, Decimal],
               equity: Decimal) -> float:
        if equity <= 0:
            return 0.0
        net: dict[str, float] = {}
        rep: dict[str, str] = {}
        for p in positions:
            mark = marks.get(p.symbol)
            if mark is None:
                continue
            und = underlying(p.symbol)
            net[und] = net.get(und, 0.0) + float(p.quantity * mark)
            rep.setdefault(und, p.symbol)
        sigma = sum(abs(n) * self.daily_vol(rep[u]) for u, n in net.items())
        return self.Z99 * sigma / float(equity)

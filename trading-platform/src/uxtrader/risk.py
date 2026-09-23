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
    positions: dict[str, Position] = field(default_factory=dict)
    marks: dict[str, Decimal] = field(default_factory=dict)
    clusters: dict[str, str] = field(default_factory=dict)    # symbol → cluster id
    stale_feeds: set[str] = field(default_factory=set)
    consecutive_losses: dict[str, int] = field(default_factory=dict)
    orders_this_min: dict[str, int] = field(default_factory=dict)
    var_99_1d: float = 0.0
    now: datetime | None = None         # required for the daily halt to expire

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
        current = state.positions.get(intent.symbol)
        have = current.quantity if current else Decimal('0')
        target = intent.target.position

        # 0. De-risking is ALWAYS permitted — through a fired kill switch, a drawdown
        # stop, a stale feed, anything. A risk engine that can veto a stop-loss exit
        # is a risk engine that can hold a losing position open indefinitely.
        if is_reducing(have, target):
            return RiskDecision(intent_id=intent.intent_id, approved=True,
                                adjusted_position=target, note='risk-reducing')

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
        scale = Decimal('1')
        if abs(target) > allowed_qty:
            breaches.append(LimitBreach(limit='position_cap',
                                        observed=float(abs(target) * mark / state.equity),
                                        allowed=float(allowed_qty * mark / state.equity),
                                        hard=False))
            scale = allowed_qty / abs(target)

        # 7. portfolio VaR
        if state.var_99_1d > L.max_var_99_1d:
            return self._veto(intent, 'var_99', state.var_99_1d, L.max_var_99_1d)

        adjusted = target * scale
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
                             for p in state.positions.values()
                             if p.venue == intent.venue and p.symbol != intent.symbol)
            caps.append(max(Decimal('0'),
                            Decimal(str(L.max_venue_fraction)) * eq - venue_used) / mark)

        cluster = state.clusters.get(intent.symbol)
        if cluster:
            cluster_used = sum(p.notional(state.marks.get(p.symbol, Decimal('0')))
                               for p in state.positions.values()
                               if state.clusters.get(p.symbol) == cluster
                               and p.symbol != intent.symbol)
            caps.append(max(Decimal('0'),
                            Decimal(str(L.max_cluster_fraction)) * eq - cluster_used) / mark)

        gross_used = sum(p.notional(state.marks.get(p.symbol, Decimal('0')))
                         for p in state.positions.values() if p.symbol != intent.symbol)
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

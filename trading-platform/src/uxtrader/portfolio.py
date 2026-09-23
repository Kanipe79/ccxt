"""Position, PnL and exposure truth.

Everything else in the system reads positions from here and never from an exchange
directly. Two components with independent views of the book will eventually disagree,
and when they do you get double-sizing — the expensive kind of bug.
"""
from __future__ import annotations

import logging
from collections import defaultdict
from decimal import Decimal

from .types import Fill, Position

log = logging.getLogger(__name__)


class ReconciliationDrift(Exception):
    """Local state and venue state disagree beyond tolerance. Halt and follow RB-02."""


class Portfolio:
    """Positions are held per (venue, symbol, strategy).

    Several strategies trade the same instrument (S1, S4, S5, S6 and S7 all trade the
    BTC perpetual). If they shared one net position, their target-position intents
    would overwrite each other: S4 asks for +0.3, S1 asks for −0.5, and each "fixes"
    the other's position. So every strategy owns a virtual book, fills are attributed
    by the strategy on the order, and the venue's net position is the sum.
    """

    def __init__(self, starting_equity: Decimal) -> None:
        self.starting_equity = starting_equity
        self.cash = starting_equity
        self._positions: dict[tuple[str, str, str], Position] = {}
        self.marks: dict[str, Decimal] = {}
        self.realized_by_strategy: dict[str, Decimal] = defaultdict(Decimal)
        self.fees_by_strategy: dict[str, Decimal] = defaultdict(Decimal)
        self.funding_by_strategy: dict[str, Decimal] = defaultdict(Decimal)
        self.peak_equity = starting_equity

    # -- accessors -------------------------------------------------------------

    def position(self, venue: str, symbol: str, strategy: str) -> Position:
        key = (venue, symbol, strategy)
        if key not in self._positions:
            self._positions[key] = Position(venue=venue, symbol=symbol, strategy=strategy)
        return self._positions[key]

    def all_positions(self) -> list[Position]:
        """Every open (venue, symbol, strategy) book. What risk sums exposure over."""
        return [p for p in self._positions.values() if not p.is_flat]

    def strategy_positions(self, strategy: str) -> dict[str, Position]:
        """One strategy's view, keyed by symbol — what its StrategyContext sees."""
        out: dict[str, Position] = {}
        for p in self.all_positions():
            if p.strategy != strategy:
                continue
            if p.symbol in out:            # same symbol on two venues: sum quantities
                prev = out[p.symbol]
                out[p.symbol] = prev.model_copy(update={
                    'quantity': prev.quantity + p.quantity, 'avg_entry': None,
                    'venue': '*'})
            else:
                out[p.symbol] = p
        return out

    def net_quantity(self, venue: str, symbol: str) -> Decimal:
        """Σ over strategies — what the venue itself should report."""
        return sum((p.quantity for (v, s, _), p in self._positions.items()
                    if v == venue and s == symbol), Decimal('0'))

    # -- mutation --------------------------------------------------------------

    def apply_fill(self, fill: Fill) -> None:
        """Weighted-average entry with correct realized-PnL accounting on reduction,
        including the sign flip when a fill crosses through flat."""
        pos = self.position(fill.venue, fill.symbol, fill.strategy)
        signed = fill.amount if fill.side == 'buy' else -fill.amount
        old_qty, old_entry = pos.quantity, pos.avg_entry

        if old_qty == 0 or (old_qty > 0) == (signed > 0):
            # opening or adding
            total = old_qty + signed
            if old_entry is None or old_qty == 0:
                pos.avg_entry = fill.price
            else:
                pos.avg_entry = (old_entry * abs(old_qty) + fill.price * abs(signed)) / abs(total)
            pos.quantity = total
            if pos.opened_at is None:
                pos.opened_at = fill.ts
        else:
            # reducing, closing, or flipping
            closing = min(abs(signed), abs(old_qty))
            if old_entry is not None:
                direction = Decimal('1') if old_qty > 0 else Decimal('-1')
                realized = (fill.price - old_entry) * closing * direction
                pos.realized_pnl += realized
                self.realized_by_strategy[fill.strategy] += realized
                self.cash += realized
            remaining = abs(signed) - closing
            pos.quantity = old_qty + signed
            if remaining > 0:                       # flipped through flat
                pos.avg_entry = fill.price
                pos.opened_at = fill.ts
            elif pos.quantity == 0:
                pos.avg_entry = None
                pos.opened_at = None

        pos.fees_paid += fill.fee
        self.fees_by_strategy[fill.strategy] += fill.fee
        self.cash -= fill.fee
        pos.updated_at = fill.ts

    def apply_funding(self, venue: str, symbol: str, strategy: str,
                      rate: Decimal, mark: Decimal) -> Decimal:
        """Funding is paid by longs to shorts when the rate is positive."""
        pos = self.position(venue, symbol, strategy)
        if pos.is_flat:
            return Decimal('0')
        payment = -pos.quantity * mark * rate
        pos.funding_paid -= payment
        self.funding_by_strategy[strategy] += payment
        self.cash += payment
        return payment

    def mark_to_market(self, symbol: str, price: Decimal) -> None:
        self.marks[symbol] = price
        eq = self.equity
        if eq > self.peak_equity:
            self.peak_equity = eq

    # -- valuation -------------------------------------------------------------

    @property
    def unrealized(self) -> Decimal:
        return sum((p.unrealized_pnl(self.marks.get(p.symbol, p.avg_entry or Decimal('0')))
                    for p in self._positions.values()), Decimal('0'))

    @property
    def equity(self) -> Decimal:
        return self.cash + self.unrealized

    @property
    def gross_notional(self) -> Decimal:
        return sum((p.notional(self.marks.get(p.symbol, Decimal('0')))
                    for p in self._positions.values()), Decimal('0'))

    @property
    def net_notional(self) -> Decimal:
        return sum((p.quantity * self.marks.get(p.symbol, Decimal('0'))
                    for p in self._positions.values()), Decimal('0'))

    @property
    def drawdown(self) -> float:
        if self.peak_equity <= 0:
            return 0.0
        return float((self.peak_equity - self.equity) / self.peak_equity)

    # -- reconciliation --------------------------------------------------------

    def reconcile(self, venue: str, venue_positions: dict[str, Decimal],
                  tolerance: Decimal = Decimal('0.001')) -> list[str]:
        """Diff local state against venue truth. Returns the symbols that disagree.

        Local = the sum of every strategy's book at that venue. The caller MUST halt
        new entries on a non-empty result and follow RB-02. Venue state is
        authoritative; never "fix" the venue to match the database.
        """
        drift: list[str] = []
        symbols = {s for (v, s, _) in self._positions if v == venue} | set(venue_positions)
        for symbol in symbols:
            local = self.net_quantity(venue, symbol)
            remote = venue_positions.get(symbol, Decimal('0'))
            denom = max(abs(local), abs(remote), Decimal('1'))
            if abs(local - remote) / denom > tolerance:
                drift.append(symbol)
                log.critical('reconciliation_drift venue=%s symbol=%s local=%s remote=%s',
                             venue, symbol, local, remote)
        return drift

    def attribution(self) -> dict[str, dict[str, float]]:
        """Per-strategy PnL decomposition. Feeds the daily report.

        Costs as a share of gross is the number to watch: past ~35%, you have an
        execution problem rather than a strategy.
        """
        out: dict[str, dict[str, float]] = {}
        strategies = (set(self.realized_by_strategy) | set(self.fees_by_strategy)
                      | set(self.funding_by_strategy))
        for s in strategies:
            realized = float(self.realized_by_strategy.get(s, Decimal('0')))
            fees = float(self.fees_by_strategy.get(s, Decimal('0')))
            funding = float(self.funding_by_strategy.get(s, Decimal('0')))
            gross = realized + fees
            out[s] = {
                'gross_pnl': gross, 'fees': fees, 'funding': funding,
                'net_pnl': realized + funding,
                'cost_ratio': (fees / gross) if gross > 0 else 0.0,
            }
        return out

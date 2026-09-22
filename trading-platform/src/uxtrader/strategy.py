"""``StrategyBase`` — the contract every strategy implements.

The single most important property: **the same class runs in backtest, paper and live
with no branching on mode.** If you ever write ``if self.live:`` inside a strategy, the
backtest has stopped validating the thing you are actually running, and every number in
your report becomes fiction.

Strategies are also deliberately weak. They can read their own positions and their own
risk budget; they cannot see the account, cannot place orders, and cannot bypass the risk
engine. They return intents and the platform decides what, if anything, happens.
"""
from __future__ import annotations

import logging
from abc import ABC
from collections.abc import Sequence
from decimal import Decimal
from typing import Any

from .clock import Clock
from .types import (
    AlgoSpec, Bar, BookSnapshot, Fill, Funding, Intent, Position, StopSpec,
    TargetSpec, TradeTick,
)

log = logging.getLogger(__name__)


class StrategyContext:
    """Read-only view a strategy is allowed to have of the world."""

    def __init__(self, strategy: str, clock: Clock, *,
                 equity: Decimal, risk_budget: float,
                 positions: dict[str, Position],
                 marks: dict[str, Decimal],
                 params: dict[str, Any] | None = None) -> None:
        self.strategy = strategy
        self.clock = clock
        self.equity = equity              # total account equity
        self.risk_budget = risk_budget    # this strategy's share, e.g. 0.20
        self._positions = positions
        self._marks = marks
        self.params = params or {}

    @property
    def sleeve_equity(self) -> Decimal:
        return self.equity * Decimal(str(self.risk_budget))

    def position(self, symbol: str) -> Decimal:
        pos = self._positions.get(symbol)
        return pos.quantity if pos else Decimal('0')

    def mark(self, symbol: str) -> Decimal | None:
        return self._marks.get(symbol)

    def now(self):
        return self.clock.now()

    def size_by_risk(self, symbol: str, stop_distance: Decimal,
                     risk_fraction: float, vol_scalar: float = 1.0) -> Decimal:
        """Fixed-fractional sizing — the common core from docs/01 §0.2.

        ``risk_fraction`` is capped at 1.5% here as a second line of defence; the risk
        engine enforces the same limit independently. Two independent checks on the
        number that matters most is not redundancy, it is design.
        """
        capped = min(risk_fraction, 0.015)
        if stop_distance <= 0:
            return Decimal('0')
        qty = (self.sleeve_equity * Decimal(str(capped))) / stop_distance
        return qty * Decimal(str(max(0.25, min(vol_scalar, 2.0))))


class StrategyBase(ABC):
    """Subclass and implement whichever handlers you need.

    Every handler returns a list of ``Intent``. Returning ``[]`` is always valid and is
    what a strategy should do whenever it is unsure.
    """

    name: str = 'unnamed'
    symbols: Sequence[str] = ()
    timeframe: str = '1h'
    warmup_bars: int = 200

    def __init__(self, ctx: StrategyContext) -> None:
        self.ctx = ctx
        self._ready = False

    # -- lifecycle -------------------------------------------------------------

    async def on_start(self) -> None:
        """Warm up indicators here. Runs identically in backtest and live."""
        return None

    async def on_stop(self, reason: str) -> list[Intent]:
        """Called on shutdown, hot reload, or strategy disable.

        Default is to do nothing: a reload must not touch positions. Override only if
        flattening on stop is genuinely the right behaviour for this strategy.
        """
        return []

    # -- market data -----------------------------------------------------------

    async def on_bar(self, bar: Bar) -> list[Intent]:
        return []

    async def on_trade(self, tick: TradeTick) -> list[Intent]:
        return []

    async def on_book(self, book: BookSnapshot) -> list[Intent]:
        return []

    async def on_funding(self, funding: Funding) -> list[Intent]:
        return []

    # -- feedback --------------------------------------------------------------

    async def on_fill(self, fill: Fill) -> None:
        return None

    async def on_stale(self, feed: str, age: float) -> list[Intent]:
        """A feed went stale. Default: flatten nothing, but stop adding risk.

        The engine has already cancelled resting orders for the affected symbol. A
        strategy that *must* exit on stale data (anything short-vol) should override and
        return flattening intents.
        """
        log.warning('strategy=%s feed_stale feed=%s age=%.1fs', self.name, feed, age)
        return []

    # -- intent helpers --------------------------------------------------------

    def target(self, symbol: str, position: Decimal, *, reason: str,
               algo: AlgoSpec | None = None, stop: StopSpec | None = None,
               urgency: str = 'normal', tolerance: Decimal = Decimal('0'),
               venue: str | None = None) -> Intent:
        """Build a target-position intent. This is the only way a strategy acts."""
        return Intent(
            strategy=self.name, venue=venue, symbol=symbol,
            target=TargetSpec(position=position, tolerance=tolerance),
            algo=algo or AlgoSpec(), stop=stop, urgency=urgency,  # type: ignore[arg-type]
            reason=reason, created_at=self.ctx.now(),
        ).with_id()

    def flatten(self, symbol: str, reason: str) -> Intent:
        return self.target(symbol, Decimal('0'), reason=reason, urgency='immediate')

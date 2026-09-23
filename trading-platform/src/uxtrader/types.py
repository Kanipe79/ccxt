"""Core domain types. These are the contracts between every component.

Two decisions here carry most of the system's safety:

1. **Strategies emit ``Intent``, never ``Order``.** A strategy literally cannot place an
   order, so a strategy bug cannot breach a risk limit.
2. **Intents carry a target *position*, not a delta.** Replaying "target = 1.5 BTC" three
   times leaves you with 1.5 BTC. Replaying "buy 1.5 BTC" three times ends your career.
   This makes the whole pipeline idempotent under message replay and restart.
"""
from __future__ import annotations

import enum
import hashlib
from datetime import datetime, timezone
from decimal import Decimal
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field

Side = Literal['buy', 'sell']
Urgency = Literal['passive', 'normal', 'immediate']


def utcnow() -> datetime:
    return datetime.now(timezone.utc)


class Frozen(BaseModel):
    model_config = ConfigDict(frozen=True, extra='forbid')


# --- market data -------------------------------------------------------------

class Bar(Frozen):
    venue: str
    symbol: str
    timeframe: str
    ts: datetime                 # bar OPEN time
    open: Decimal
    high: Decimal
    low: Decimal
    close: Decimal
    volume: Decimal
    closed: bool = True          # strategies act only on closed bars

    @property
    def close_ts(self) -> datetime:
        return self.ts + _timeframe_delta(self.timeframe)


class TradeTick(Frozen):
    venue: str
    symbol: str
    ts: datetime
    price: Decimal
    amount: Decimal
    side: Side
    is_liquidation: bool = False


class BookLevel(Frozen):
    price: Decimal
    size: Decimal


class BookSnapshot(Frozen):
    venue: str
    symbol: str
    ts: datetime
    bids: tuple[BookLevel, ...]
    asks: tuple[BookLevel, ...]
    sequence: int | None = None

    @property
    def mid(self) -> Decimal:
        return (self.bids[0].price + self.asks[0].price) / 2

    @property
    def spread_bps(self) -> float:
        return float((self.asks[0].price - self.bids[0].price) / self.mid) * 1e4

    def imbalance(self, depth: int = 10) -> float:
        """(bid − ask) / (bid + ask) over the top `depth` levels. S5's book filter."""
        b = sum(float(lv.size) for lv in self.bids[:depth])
        a = sum(float(lv.size) for lv in self.asks[:depth])
        return (b - a) / (b + a) if (b + a) else 0.0


class Funding(Frozen):
    venue: str
    symbol: str
    ts: datetime
    rate: Decimal
    interval_hours: float = 8.0
    mark_price: Decimal | None = None

    @property
    def apr(self) -> float:
        return float(self.rate) * (24.0 / self.interval_hours) * 365.0


# --- intents -----------------------------------------------------------------

class AlgoKind(str, enum.Enum):
    MARKET = 'market'
    POST_ONLY_PEG = 'post_only_peg'
    TWAP = 'twap'
    POV = 'pov'
    ICEBERG = 'iceberg'


class AlgoSpec(Frozen):
    kind: AlgoKind = AlgoKind.MARKET
    duration_s: int | None = None        # TWAP/POV window
    participation: float | None = None   # POV target, e.g. 0.10
    max_improvements: int = 3            # PostOnlyPeg re-peg attempts before crossing
    cross_after_s: int | None = None     # give up on passive and take


class StopSpec(Frozen):
    """A protective stop that travels with the intent.

    ``venue_native=True`` asks the execution engine to rest a real stop order at the
    venue (kill-switch layer L3). Every position should carry one.
    """
    price: Decimal
    trailing_atr_mult: float | None = None
    venue_native: bool = True


class TargetSpec(Frozen):
    """Where the position should END UP. Absolute, signed, in base units."""
    position: Decimal                    # signed: + long, − short
    tolerance: Decimal = Decimal('0')    # no-trade band; skip if |delta| <= tolerance


class Intent(Frozen):
    intent_id: str = Field(default='')
    strategy: str
    venue: str | None = None             # None ⇒ let the router decide
    symbol: str
    target: TargetSpec
    algo: AlgoSpec = AlgoSpec()
    stop: StopSpec | None = None
    urgency: Urgency = 'normal'
    reason: str                          # logged; makes post-mortems possible
    created_at: datetime = Field(default_factory=utcnow)
    valid_until: datetime | None = None

    def with_id(self) -> Intent:
        """Deterministic id — the same logical intent hashes the same, so a replay
        after a restart is recognised as a duplicate instead of doubling the position."""
        if self.intent_id:
            return self
        payload = f'{self.strategy}|{self.symbol}|{self.target.position}|{self.created_at.isoformat()}'
        return self.model_copy(update={'intent_id': hashlib.sha256(payload.encode()).hexdigest()[:16]})


# --- risk --------------------------------------------------------------------

class LimitBreach(Frozen):
    limit: str
    observed: float
    allowed: float
    hard: bool = True                    # hard ⇒ veto; soft ⇒ shrink


class RiskDecision(Frozen):
    intent_id: str
    approved: bool
    adjusted_position: Decimal | None = None   # risk may SHRINK, never grow
    breaches: tuple[LimitBreach, ...] = ()
    note: str = ''


# --- orders and positions ----------------------------------------------------

class OrderState(str, enum.Enum):
    PENDING_NEW = 'pending_new'
    NEW = 'new'
    PARTIALLY_FILLED = 'partially_filled'
    FILLED = 'filled'
    CANCELED = 'canceled'
    REJECTED = 'rejected'
    AMBIGUOUS = 'ambiguous'      # sent, outcome unknown — blocks the strategy until resolved

    @property
    def terminal(self) -> bool:
        return self in {OrderState.FILLED, OrderState.CANCELED, OrderState.REJECTED}


class Order(BaseModel):
    model_config = ConfigDict(extra='forbid')

    client_order_id: str         # deterministic — the idempotency key
    venue_order_id: str | None = None
    intent_id: str
    strategy: str
    venue: str
    symbol: str
    side: Side
    type: str                    # 'limit' | 'market' | 'stop_market' ...
    amount: Decimal
    price: Decimal | None = None
    stop_price: Decimal | None = None    # trigger for 'stop_market' (kill-switch layer L3)
    state: OrderState = OrderState.PENDING_NEW
    filled: Decimal = Decimal('0')
    avg_price: Decimal | None = None
    arrival_price: Decimal | None = None   # mid at submit — slippage is measured off this
    created_at: datetime = Field(default_factory=utcnow)
    updated_at: datetime = Field(default_factory=utcnow)
    reduce_only: bool = False
    post_only: bool = False

    @property
    def remaining(self) -> Decimal:
        return self.amount - self.filled

    @property
    def slippage_bps(self) -> float | None:
        """Signed cost vs arrival price. The single most useful execution metric."""
        if self.avg_price is None or not self.arrival_price:
            return None
        sign = 1 if self.side == 'buy' else -1
        return float((self.avg_price - self.arrival_price) / self.arrival_price) * 1e4 * sign


class Fill(Frozen):
    client_order_id: str
    venue_order_id: str | None
    strategy: str
    venue: str
    symbol: str
    side: Side
    price: Decimal
    amount: Decimal
    fee: Decimal
    fee_currency: str
    ts: datetime
    is_maker: bool
    arrival_price: Decimal | None = None   # stamped by the OMS; slippage is measured off it

    @property
    def slippage_bps(self) -> float | None:
        if not self.arrival_price:
            return None
        sign = 1 if self.side == 'buy' else -1
        return float((self.price - self.arrival_price) / self.arrival_price) * 1e4 * sign


class Position(BaseModel):
    model_config = ConfigDict(extra='forbid')

    venue: str
    symbol: str
    strategy: str
    quantity: Decimal = Decimal('0')       # signed
    avg_entry: Decimal | None = None
    realized_pnl: Decimal = Decimal('0')
    fees_paid: Decimal = Decimal('0')
    funding_paid: Decimal = Decimal('0')
    opened_at: datetime | None = None
    updated_at: datetime = Field(default_factory=utcnow)

    @property
    def is_flat(self) -> bool:
        return self.quantity == 0

    def unrealized_pnl(self, mark: Decimal) -> Decimal:
        if self.is_flat or self.avg_entry is None:
            return Decimal('0')
        return (mark - self.avg_entry) * self.quantity

    def notional(self, mark: Decimal) -> Decimal:
        return abs(self.quantity) * mark


def _timeframe_delta(tf: str):
    from datetime import timedelta
    unit, n = tf[-1], int(tf[:-1])
    return {'m': timedelta(minutes=n), 'h': timedelta(hours=n),
            'd': timedelta(days=n), 'w': timedelta(weeks=n)}[unit]

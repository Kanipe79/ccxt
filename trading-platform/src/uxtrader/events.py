"""Message subjects and the wire envelope.

Every inter-process message is a Pydantic model from ``types.py`` wrapped in an
``Envelope`` that names its type. The same models are the contracts in-process, on
NATS, and in the database, so there is exactly one definition of what an ``Intent`` is.

Subject layout (NATS-style tokens, ``*`` = one token, ``>`` = the rest)::

    md.{venue}.{kind}.{symbol}     market data   kind ∈ bar|trade|book|funding
    intent.{strategy}              strategy → risk
    exec.order                     risk → execution (approved, possibly shrunk)
    exec.report                    execution → metrics, dashboard (order state)
    risk.veto                      risk → alerts/dashboard
    fill.{venue}                   execution → portfolio, strategies
    feed.stale / feed.recovered    ingest → risk, execution, strategies
    portfolio.snapshot             portfolio → risk, execution, strategy engine
    control.{command}              operator → services (kill, rearm, flatten)
    heartbeat.{service}            every service → watchdog
"""
from __future__ import annotations

from datetime import datetime
from typing import Any

from pydantic import BaseModel, ConfigDict, Field

from decimal import Decimal

from .types import (
    Bar, BookSnapshot, Fill, Funding, Intent, Order, Position, RiskDecision, TradeTick,
    utcnow,
)


def _token(symbol: str) -> str:
    """'BTC/USDT:USDT' → 'BTC-USDT-USDT'. NATS tokens may not contain '.', '/', ':'."""
    return symbol.replace('/', '-').replace(':', '-').replace('.', '_')


def md_subject(venue: str, kind: str, symbol: str) -> str:
    return f'md.{venue}.{kind}.{_token(symbol)}'


def intent_subject(strategy: str) -> str:
    return f'intent.{strategy}'


EXEC_ORDER = 'exec.order'
EXEC_REPORT = 'exec.report'
RISK_VETO = 'risk.veto'
FEED_STALE = 'feed.stale'
FEED_RECOVERED = 'feed.recovered'
PORTFOLIO_SNAPSHOT = 'portfolio.snapshot'
CONTROL_KILL = 'control.kill'
CONTROL_REARM = 'control.rearm'


def fill_subject(venue: str) -> str:
    return f'fill.{venue}'


def heartbeat_subject(service: str) -> str:
    return f'heartbeat.{service}'


class StaleFeed(BaseModel):
    model_config = ConfigDict(frozen=True)
    key: str
    venue: str
    symbol: str
    age_s: float
    ts: datetime = Field(default_factory=utcnow)


class FeedRecovered(BaseModel):
    model_config = ConfigDict(frozen=True)
    key: str
    venue: str
    symbol: str
    ts: datetime = Field(default_factory=utcnow)


class PortfolioSnapshot(BaseModel):
    """Portfolio truth, broadcast after every change. Risk, execution and the
    strategy engine read positions ONLY from here — never from a venue directly —
    so no two components can disagree about the book."""
    model_config = ConfigDict(frozen=True)
    seq: int
    equity: Decimal
    peak_equity: Decimal
    day_start_equity: Decimal
    week_start_equity: Decimal
    positions: tuple[Position, ...]
    marks: dict[str, Decimal]
    ts: datetime = Field(default_factory=utcnow)

    def position_map(self) -> dict[str, Position]:
        return {p.symbol: p for p in self.positions}


class Control(BaseModel):
    """Operator command. Every path that can stop trading — dashboard button,
    Telegram, CLI — publishes one of these."""
    model_config = ConfigDict(frozen=True)
    command: str                        # 'kill' | 'rearm' | 'flatten'
    reason: str
    strategy: str | None = None
    venue: str | None = None
    ts: datetime = Field(default_factory=utcnow)


class Heartbeat(BaseModel):
    model_config = ConfigDict(frozen=True)
    service: str
    ts: datetime = Field(default_factory=utcnow)
    detail: dict[str, Any] = Field(default_factory=dict)


class ApprovedIntent(BaseModel):
    """What risk hands to execution: the original intent plus the decision."""
    model_config = ConfigDict(frozen=True)
    intent: Intent
    decision: RiskDecision


REGISTRY: dict[str, type[BaseModel]] = {
    cls.__name__: cls
    for cls in (Bar, TradeTick, BookSnapshot, Funding, Intent, RiskDecision, Order,
                Fill, StaleFeed, FeedRecovered, PortfolioSnapshot, Control, Heartbeat,
                ApprovedIntent)
}


class Envelope(BaseModel):
    type: str
    subject: str
    payload: dict[str, Any]
    published_at: datetime = Field(default_factory=utcnow)

    @classmethod
    def wrap(cls, subject: str, msg: BaseModel) -> Envelope:
        name = type(msg).__name__
        if name not in REGISTRY:
            raise TypeError(f'{name} is not a registered message type')
        return cls(type=name, subject=subject, payload=msg.model_dump(mode='json'))

    def unwrap(self) -> BaseModel:
        return REGISTRY[self.type].model_validate(self.payload)

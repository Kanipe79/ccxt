"""Unified error taxonomy with an attached retry policy.

Upstream CCXT gives you a rich exception hierarchy but no *policy*: it cannot know
whether your application should retry, reconcile, or stop. This module maps every CCXT
exception onto one of five classes, each of which carries an explicit, testable policy.

The class that matters most is ``AMBIGUOUS``: a request that timed out *after* being sent.
You do not know whether the venue received it. Retrying it blindly is how you end up with
double the position you intended. The only correct response is reconciliation.
"""
from __future__ import annotations

import enum
from dataclasses import dataclass

try:  # pragma: no cover - ccxt is the forked upstream, present in production
    import ccxt
except ImportError:  # allows the module to be imported in docs/CI without ccxt
    ccxt = None  # type: ignore[assignment]


class ErrorClass(enum.Enum):
    """Five classes, five policies. Nothing else."""

    TRANSIENT = 'transient'      # network blip, venue 5xx — retry with backoff
    RATE_LIMITED = 'rate_limited'  # 429 / venue throttle — back off hard, shed load
    REJECTED = 'rejected'        # venue understood and said no — do not retry, log
    FATAL = 'fatal'              # auth, permission, bad symbol — stop, page a human
    AMBIGUOUS = 'ambiguous'      # timed out after send — RECONCILE, never blind-retry


@dataclass(frozen=True)
class RetryPolicy:
    max_attempts: int
    base_delay: float          # seconds
    max_delay: float
    jitter: float              # fraction of delay, uniform
    requires_reconcile: bool = False

    def delay_for(self, attempt: int) -> float:
        """Exponential backoff with full jitter, capped."""
        import random
        raw = min(self.base_delay * (2 ** attempt), self.max_delay)
        return raw * (1.0 + random.uniform(-self.jitter, self.jitter))


POLICIES: dict[ErrorClass, RetryPolicy] = {
    ErrorClass.TRANSIENT:    RetryPolicy(max_attempts=5, base_delay=0.25, max_delay=8.0, jitter=0.3),
    ErrorClass.RATE_LIMITED: RetryPolicy(max_attempts=6, base_delay=1.0, max_delay=60.0, jitter=0.4),
    ErrorClass.REJECTED:     RetryPolicy(max_attempts=0, base_delay=0.0, max_delay=0.0, jitter=0.0),
    ErrorClass.FATAL:        RetryPolicy(max_attempts=0, base_delay=0.0, max_delay=0.0, jitter=0.0),
    # One retry only, and only after the reconciler has confirmed the order does not exist.
    ErrorClass.AMBIGUOUS:    RetryPolicy(max_attempts=1, base_delay=2.0, max_delay=2.0,
                                         jitter=0.0, requires_reconcile=True),
}


class UXError(Exception):
    """Every error leaving uxcore is one of these."""

    def __init__(self, message: str, klass: ErrorClass, *,
                 venue: str = '', original: BaseException | None = None,
                 client_order_id: str | None = None) -> None:
        super().__init__(message)
        self.klass = klass
        self.venue = venue
        self.original = original
        self.client_order_id = client_order_id

    @property
    def policy(self) -> RetryPolicy:
        return POLICIES[self.klass]

    def __repr__(self) -> str:  # pragma: no cover
        return f'<UXError {self.klass.value} venue={self.venue!r} msg={self!s}>'


def _ccxt_mapping() -> list[tuple[type, ErrorClass]]:
    """Ordered most-specific-first; the first isinstance match wins."""
    if ccxt is None:
        return []
    return [
        # Fatal — a human must look at this.
        (ccxt.AuthenticationError,   ErrorClass.FATAL),
        (ccxt.PermissionDenied,      ErrorClass.FATAL),
        (ccxt.AccountSuspended,      ErrorClass.FATAL),
        (ccxt.BadSymbol,             ErrorClass.FATAL),
        (ccxt.NotSupported,          ErrorClass.FATAL),
        # Ambiguous — the request may have landed. Reconcile.
        (ccxt.RequestTimeout,        ErrorClass.AMBIGUOUS),
        (ccxt.InvalidNonce,          ErrorClass.AMBIGUOUS),
        # Rate limited.
        (ccxt.RateLimitExceeded,     ErrorClass.RATE_LIMITED),
        (ccxt.DDoSProtection,        ErrorClass.RATE_LIMITED),
        # Rejected — venue understood and declined. Retrying changes nothing.
        (ccxt.InsufficientFunds,     ErrorClass.REJECTED),
        (ccxt.InvalidOrder,          ErrorClass.REJECTED),
        (ccxt.OrderNotFound,         ErrorClass.REJECTED),
        (ccxt.BadRequest,            ErrorClass.REJECTED),
        # Transient — retry.
        (ccxt.OnMaintenance,         ErrorClass.TRANSIENT),
        (ccxt.ExchangeNotAvailable,  ErrorClass.TRANSIENT),
        (ccxt.NetworkError,          ErrorClass.TRANSIENT),
        (ccxt.ExchangeError,         ErrorClass.TRANSIENT),
    ]


def classify(exc: BaseException, *, venue: str = '',
             client_order_id: str | None = None,
             was_sent: bool = False) -> UXError:
    """Map any exception onto the taxonomy.

    ``was_sent`` promotes an otherwise-transient network error to AMBIGUOUS when the
    request body has already gone out on the wire. Callers that mutate state (create,
    cancel, edit) must pass ``was_sent=True``; read-only callers must not.
    """
    if isinstance(exc, UXError):
        return exc
    for exc_type, klass in _ccxt_mapping():
        if isinstance(exc, exc_type):
            if was_sent and klass is ErrorClass.TRANSIENT:
                klass = ErrorClass.AMBIGUOUS
            return UXError(str(exc), klass, venue=venue, original=exc,
                           client_order_id=client_order_id)
    klass = ErrorClass.AMBIGUOUS if was_sent else ErrorClass.TRANSIENT
    return UXError(str(exc), klass, venue=venue, original=exc,
                   client_order_id=client_order_id)

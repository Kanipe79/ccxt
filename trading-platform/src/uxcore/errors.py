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
    """Ordered most-specific-first; the first isinstance match wins.

    Order matters because CCXT's hierarchy nests: ``RateLimitExceeded``,
    ``InvalidNonce`` and ``OnMaintenance`` are all ``NetworkError`` subclasses, and
    ``PermissionDenied``/``AccountSuspended`` are ``AuthenticationError`` subclasses.
    """
    if ccxt is None:
        return []
    return [
        # Fatal — a human must look at this.
        (ccxt.AuthenticationError,   ErrorClass.FATAL),
        (ccxt.BadSymbol,             ErrorClass.FATAL),
        (ccxt.NotSupported,          ErrorClass.FATAL),
        # Rate limited — the venue refused before processing. Never ambiguous.
        (ccxt.RateLimitExceeded,     ErrorClass.RATE_LIMITED),
        (ccxt.DDoSProtection,        ErrorClass.RATE_LIMITED),
        # The venue refused before processing (clock skew / recvWindow). A retry with
        # a fresh timestamp is safe, so this is transient — NOT ambiguous.
        (ccxt.InvalidNonce,          ErrorClass.TRANSIENT),
        (ccxt.OnMaintenance,         ErrorClass.TRANSIENT),
        # Rejected — venue understood and declined. Retrying changes nothing.
        (ccxt.InsufficientFunds,     ErrorClass.REJECTED),
        (ccxt.InvalidOrder,          ErrorClass.REJECTED),
        (ccxt.BadRequest,            ErrorClass.REJECTED),
        # No usable response: we cannot know whether a mutating request landed.
        # These are the only classes promoted to AMBIGUOUS when was_sent=True.
        (ccxt.RequestTimeout,        ErrorClass.TRANSIENT),
        (ccxt.ExchangeNotAvailable,  ErrorClass.TRANSIENT),
        (ccxt.NetworkError,          ErrorClass.TRANSIENT),
        # The venue answered with an error we have no specific mapping for. It
        # answered, so the outcome is known: retry reads, do not retry writes.
        (ccxt.ExchangeError,         ErrorClass.TRANSIENT),
    ]


def _no_response_types() -> tuple[type, ...]:
    """Exceptions that mean *no usable response arrived* — the ambiguous family."""
    if ccxt is None:
        return ()
    return (ccxt.RequestTimeout, ccxt.ExchangeNotAvailable, ccxt.NetworkError)


def _refused_types() -> tuple[type, ...]:
    """NetworkError subclasses where the venue demonstrably refused the request."""
    if ccxt is None:
        return ()
    return (ccxt.RateLimitExceeded, ccxt.DDoSProtection, ccxt.InvalidNonce,
            ccxt.OnMaintenance)


def classify(exc: BaseException, *, venue: str = '',
             client_order_id: str | None = None,
             was_sent: bool = False) -> UXError:
    """Map any exception onto the taxonomy.

    ``was_sent`` must be True for calls that mutate venue state (create, edit) and
    False for reads. It changes two outcomes:

    * a *no-response* failure (timeout, dropped connection, 502/504) on a mutating
      call becomes AMBIGUOUS — the order may exist, so the caller must reconcile;
    * a generic ``ExchangeError`` on a mutating call becomes REJECTED — the venue
      answered, so blindly re-sending risks a duplicate for no benefit.

    Reads are never AMBIGUOUS: re-reading has no side effects.
    """
    if isinstance(exc, UXError):
        return exc
    klass: ErrorClass | None = None
    for exc_type, mapped in _ccxt_mapping():
        if isinstance(exc, exc_type):
            klass = mapped
            break

    if was_sent and not isinstance(exc, _refused_types()):
        if klass is None or isinstance(exc, _no_response_types()):
            # Unknown exception types (asyncio.TimeoutError, aiohttp errors, OSError)
            # are treated as no-response too: assume the worst.
            klass = ErrorClass.AMBIGUOUS
        elif klass is ErrorClass.TRANSIENT:
            klass = ErrorClass.REJECTED
    elif klass is None:
        klass = ErrorClass.TRANSIENT

    return UXError(str(exc) or type(exc).__name__, klass, venue=venue, original=exc,
                   client_order_id=client_order_id)

"""Retry, circuit breaking, and ambiguous-operation reconciliation."""
from __future__ import annotations

import asyncio
import functools
import logging
import time
from collections.abc import Awaitable, Callable
from typing import Any, Protocol, TypeVar

from .errors import ErrorClass, UXError, classify

log = logging.getLogger(__name__)
T = TypeVar('T')


class Reconciler(Protocol):
    """Resolves an AMBIGUOUS mutating call by asking the venue what actually happened."""

    async def order_exists(self, venue: str, symbol: str, client_order_id: str) -> bool: ...


class CircuitBreaker:
    """Per-venue breaker. Open ⇒ fail fast rather than pile requests onto a sick venue.

    States: closed → (failures ≥ threshold) → open → (after reset_after) → half_open
            → (one success) → closed  |  (one failure) → open
    """

    def __init__(self, threshold: int = 5, reset_after: float = 30.0) -> None:
        self.threshold = threshold
        self.reset_after = reset_after
        self._failures = 0
        self._opened_at = 0.0
        self.state = 'closed'

    def allow(self) -> bool:
        if self.state == 'open':
            if time.monotonic() - self._opened_at >= self.reset_after:
                self.state = 'half_open'
                return True
            return False
        return True

    def record_success(self) -> None:
        self._failures = 0
        self.state = 'closed'

    def record_failure(self) -> None:
        self._failures += 1
        if self.state == 'half_open' or self._failures >= self.threshold:
            self.state = 'open'
            self._opened_at = time.monotonic()
            log.error('circuit_breaker_open failures=%d', self._failures)


_BREAKERS: dict[str, CircuitBreaker] = {}


def breaker_for(venue: str) -> CircuitBreaker:
    return _BREAKERS.setdefault(venue, CircuitBreaker())


def resilient(*, mutating: bool = False, weight: int = 1,
              risk_reducing: bool = False) -> Callable[..., Any]:
    """Wrap an exchange coroutine with rate limiting, retry policy and reconciliation.

    ``mutating=True`` means the call changes state at the venue (create/cancel/edit).
    Those calls are the ones where a timeout becomes AMBIGUOUS, and where we must
    reconcile before a retry rather than retrying blind.
    """

    def decorator(fn: Callable[..., Awaitable[T]]) -> Callable[..., Awaitable[T]]:

        @functools.wraps(fn)
        async def wrapper(self: Any, *args: Any, **kwargs: Any) -> T:
            venue = getattr(self, 'id', 'unknown')
            breaker = breaker_for(venue)
            attempt = 0
            last: UXError | None = None

            while True:
                if not breaker.allow():
                    raise UXError(f'circuit open for {venue}', ErrorClass.TRANSIENT, venue=venue)

                limiter = getattr(self, 'limiter', None)
                try:
                    if limiter is not None:
                        async with limiter.acquire(weight, risk_reducing=risk_reducing):
                            result = await fn(self, *args, **kwargs)
                    else:
                        result = await fn(self, *args, **kwargs)
                except asyncio.CancelledError:
                    raise
                except BaseException as exc:  # noqa: BLE001 - classified immediately
                    err = classify(exc, venue=venue, was_sent=mutating,
                                   client_order_id=kwargs.get('client_order_id'))
                    last = err
                    breaker.record_failure()

                    if err.klass is ErrorClass.RATE_LIMITED and limiter is not None:
                        limiter.penalise(err.policy.delay_for(attempt))

                    policy = err.policy
                    if attempt >= policy.max_attempts:
                        log.error('call_failed venue=%s fn=%s class=%s attempts=%d msg=%s',
                                  venue, fn.__name__, err.klass.value, attempt, err)
                        raise err from exc

                    if policy.requires_reconcile:
                        reconciler: Reconciler | None = getattr(self, 'reconciler', None)
                        coid = kwargs.get('client_order_id')
                        symbol = kwargs.get('symbol') or (args[0] if args else None)
                        if reconciler is None or coid is None or symbol is None:
                            # Cannot prove the order did not land. Surface it; the OMS
                            # must mark the order AMBIGUOUS and pause the strategy.
                            log.error('ambiguous_unreconcilable venue=%s fn=%s', venue, fn.__name__)
                            raise err from exc
                        await asyncio.sleep(policy.delay_for(attempt))
                        if await reconciler.order_exists(venue, str(symbol), str(coid)):
                            log.warning('ambiguous_resolved_exists venue=%s coid=%s', venue, coid)
                            raise err from exc      # it landed; caller must fetch, not resend
                        log.warning('ambiguous_resolved_absent_retrying venue=%s coid=%s', venue, coid)

                    await asyncio.sleep(policy.delay_for(attempt))
                    attempt += 1
                    continue

                breaker.record_success()
                if limiter is not None:
                    limiter.observe_headers(getattr(self, 'last_response_headers', None))
                return result

            raise last or UXError('unreachable', ErrorClass.FATAL, venue=venue)

        return wrapper

    return decorator

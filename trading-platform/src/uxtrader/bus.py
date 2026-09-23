"""The message bus. One interface, two implementations.

``InMemoryBus`` runs the whole platform in one process — the backtester, the test
suite, and single-process paper trading all use it. ``NatsBus`` runs the same
components as separate services over NATS JetStream. Components only ever see the
``Bus`` protocol, so "one process" versus "seven containers" is a deployment choice,
not a code change.
"""
from __future__ import annotations

import logging
from collections.abc import Awaitable, Callable
from typing import Protocol

from pydantic import BaseModel

from .events import Envelope

log = logging.getLogger(__name__)

Handler = Callable[[str, BaseModel], Awaitable[None]]


class Bus(Protocol):
    async def publish(self, subject: str, msg: BaseModel) -> None: ...
    async def subscribe(self, pattern: str, handler: Handler) -> None: ...
    async def close(self) -> None: ...


def subject_matches(pattern: str, subject: str) -> bool:
    """NATS semantics: '*' matches exactly one token, '>' matches one or more."""
    p, s = pattern.split('.'), subject.split('.')
    for i, tok in enumerate(p):
        if tok == '>':
            return len(s) > i
        if i >= len(s) or (tok != '*' and tok != s[i]):
            return False
    return len(p) == len(s)


class InMemoryBus:
    """Synchronous-delivery bus: ``publish`` awaits every matching handler in
    subscription order. That makes backtests and tests deterministic, which a
    queue-based design would not be.

    Every message is round-tripped through the wire envelope, so anything that would
    fail to serialize on NATS fails here too — in tests, not in production.
    """

    def __init__(self, *, roundtrip: bool = True) -> None:
        self._subs: list[tuple[str, Handler]] = []
        self.roundtrip = roundtrip
        self.published: list[tuple[str, BaseModel]] = []
        self._depth = 0

    async def publish(self, subject: str, msg: BaseModel) -> None:
        if self.roundtrip:
            msg = Envelope.model_validate_json(
                Envelope.wrap(subject, msg).model_dump_json()).unwrap()
        self.published.append((subject, msg))
        self._depth += 1
        if self._depth > 64:
            self._depth -= 1
            raise RecursionError(f'publish depth > 64 on {subject}: handler feedback loop')
        try:
            for pattern, handler in list(self._subs):
                if subject_matches(pattern, subject):
                    await handler(subject, msg)
        finally:
            self._depth -= 1

    async def subscribe(self, pattern: str, handler: Handler) -> None:
        self._subs.append((pattern, handler))

    async def close(self) -> None:
        self._subs.clear()

    def of_type(self, cls: type[BaseModel]) -> list[BaseModel]:
        return [m for _, m in self.published if isinstance(m, cls)]


class NatsBus:
    """NATS JetStream transport. At-least-once delivery: handlers must be idempotent,
    which target-position intents and deterministic client order ids make them."""

    def __init__(self, url: str = 'nats://localhost:4222', *,
                 stream: str = 'UX', subjects: tuple[str, ...] = (
                     'md.>', 'intent.>', 'exec.>', 'risk.>', 'fill.>', 'feed.>',
                     'heartbeat.>')) -> None:
        self.url = url
        self.stream = stream
        self.subjects = subjects
        self._nc = None
        self._js = None
        self._subs: list[object] = []

    async def connect(self) -> NatsBus:
        import nats                                    # optional dependency
        self._nc = await nats.connect(self.url, max_reconnect_attempts=-1)
        self._js = self._nc.jetstream()
        try:
            await self._js.add_stream(name=self.stream, subjects=list(self.subjects))
        except Exception as exc:                        # noqa: BLE001 - exists already
            log.debug('stream_exists %s', exc)
        return self

    async def publish(self, subject: str, msg: BaseModel) -> None:
        if self._js is None:
            raise RuntimeError('NatsBus.connect() was not awaited')
        data = Envelope.wrap(subject, msg).model_dump_json().encode()
        # Market data is high-volume and replaceable: core NATS. Everything else is
        # state-changing and goes through JetStream for persistence and replay.
        if subject.startswith('md.') or subject.startswith('heartbeat.'):
            await self._nc.publish(subject, data)          # type: ignore[union-attr]
        else:
            await self._js.publish(subject, data)

    async def subscribe(self, pattern: str, handler: Handler) -> None:
        if self._nc is None:
            raise RuntimeError('NatsBus.connect() was not awaited')

        async def _cb(raw) -> None:
            try:
                env = Envelope.model_validate_json(raw.data)
                await handler(raw.subject, env.unwrap())
            except Exception:                           # noqa: BLE001
                # A poison message must not wedge the consumer. It is logged, and
                # JetStream has already persisted it for post-mortem replay.
                log.exception('handler_failed subject=%s', raw.subject)

        if pattern.startswith('md.') or pattern.startswith('heartbeat.'):
            sub = await self._nc.subscribe(pattern, cb=_cb)
        else:
            durable = pattern.replace('.', '_').replace('*', 'S').replace('>', 'R')
            sub = await self._js.subscribe(pattern, cb=_cb, durable=durable,
                                           manual_ack=False)
        self._subs.append(sub)

    async def close(self) -> None:
        if self._nc is not None:
            await self._nc.drain()


async def connect(url: str | None) -> Bus:
    """``None`` or 'memory://' → InMemoryBus; 'nats://…' → NatsBus."""
    if not url or url.startswith('memory://'):
        return InMemoryBus()
    return await NatsBus(url).connect()


__all__ = ['Bus', 'Handler', 'InMemoryBus', 'NatsBus', 'connect', 'subject_matches']

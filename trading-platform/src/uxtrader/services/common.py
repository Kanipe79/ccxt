"""Shared service plumbing: heartbeats, the singleton lock, and the CLI entry point."""
from __future__ import annotations

import asyncio
import logging
import os
import secrets
from typing import Any

from ..bus import Bus
from ..events import Heartbeat, heartbeat_subject

log = logging.getLogger(__name__)


async def heartbeat_loop(bus: Bus, service: str, interval: float = 5.0,
                         detail: Any = None) -> None:
    """The L2 watchdog flattens everything if these stop. Keep them cheap and dumb."""
    while True:
        try:
            await bus.publish(heartbeat_subject(service),
                              Heartbeat(service=service, detail=(detail() if detail else {})))
        except Exception as exc:                        # noqa: BLE001
            log.error('heartbeat_failed service=%s err=%s', service, exc)
        await asyncio.sleep(interval)


class LocalLock:
    """No-op lock for single-process deployments. Logs loudly so nobody mistakes it
    for protection in a multi-node setup."""

    held = True

    async def acquire(self) -> bool:
        log.warning('singleton_lock=LOCAL — only safe when exactly one process can run')
        return True

    async def release(self) -> None:
        return None


class RedisLock:
    """Lease-based singleton lock (SET NX PX + renewal).

    Fail-closed: if a renewal fails or the lease is lost, ``held`` goes False and the
    execution service stops accepting orders. Two execution engines on one account is
    the worst bug class in this domain; an idle one is merely inconvenient.
    """

    def __init__(self, url: str, key: str = 'ux:lock:execution', ttl_ms: int = 10_000) -> None:
        self.url = url
        self.key = key
        self.ttl_ms = ttl_ms
        self.token = secrets.token_hex(16)
        self.held = False
        self._redis: Any = None
        self._task: asyncio.Task[None] | None = None

    async def acquire(self, wait: bool = True) -> bool:
        import redis.asyncio as redis                 # optional dependency
        self._redis = redis.from_url(self.url)
        while True:
            if await self._redis.set(self.key, self.token, nx=True, px=self.ttl_ms):
                self.held = True
                self._task = asyncio.create_task(self._renew())
                log.warning('singleton_lock_acquired key=%s', self.key)
                return True
            if not wait:
                return False
            await asyncio.sleep(self.ttl_ms / 2000)

    async def _renew(self) -> None:
        script = ("if redis.call('get', KEYS[1]) == ARGV[1] then "
                  "return redis.call('pexpire', KEYS[1], ARGV[2]) else return 0 end")
        while self.held:
            await asyncio.sleep(self.ttl_ms / 3000)
            try:
                ok = await self._redis.eval(script, 1, self.key, self.token, self.ttl_ms)
            except Exception as exc:                  # noqa: BLE001
                log.critical('singleton_lock_renew_failed err=%s — failing closed', exc)
                ok = 0
            if not ok:
                self.held = False
                log.critical('singleton_lock_LOST key=%s — execution disabled', self.key)

    async def release(self) -> None:
        self.held = False
        if self._task:
            self._task.cancel()
        if self._redis is not None:
            script = ("if redis.call('get', KEYS[1]) == ARGV[1] then "
                      "return redis.call('del', KEYS[1]) else return 0 end")
            await self._redis.eval(script, 1, self.key, self.token)


def lock_from_env() -> LocalLock | RedisLock:
    url = os.environ.get('UX_LOCK_BACKEND', '')
    return RedisLock(url) if url.startswith('redis') else LocalLock()

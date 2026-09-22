"""Kill-switch layer 2 — the out-of-process watchdog.

This process exists to survive whatever kills the trading system. It therefore:

* runs in a **different container**, on a **different node**, in a **different
  namespace**, from a **different image**;
* uses its own API credentials, scoped to read + cancel + reduce-only;
* shares **no code path** with the trading stack beyond ccxt itself;
* has one job, and does it without asking anyone.

If any of those are violated it becomes decoration. The moment it shares a failure
domain with the thing it is watching, it stops being a safety system.

Triggers:
  1. No heartbeat from the trading system for ``heartbeat_timeout`` seconds.
  2. Account equity drops more than ``equity_drop_pct`` within ``equity_window`` seconds.
  3. An operator fires it manually (dashboard button, Telegram command, CLI).
"""
from __future__ import annotations

import asyncio
import logging
import time
from collections import deque
from dataclasses import dataclass, field

log = logging.getLogger('watchdog')


@dataclass
class WatchdogConfig:
    heartbeat_timeout: float = 90.0
    equity_drop_pct: float = 0.05
    equity_window: float = 300.0
    poll_interval: float = 5.0
    venues: tuple[str, ...] = ()
    dry_run: bool = False          # log what it WOULD do; test with this on first


@dataclass
class Watchdog:
    config: WatchdogConfig
    exchanges: dict[str, object] = field(default_factory=dict)
    alert: object | None = None
    _last_heartbeat: float = field(default_factory=time.monotonic)
    _equity: deque[tuple[float, float]] = field(default_factory=lambda: deque(maxlen=500))
    _fired: bool = False

    def heartbeat(self) -> None:
        """Called by the trading system's HTTP endpoint or a Redis key."""
        self._last_heartbeat = time.monotonic()

    async def run(self) -> None:
        log.warning('watchdog_started dry_run=%s venues=%s',
                    self.config.dry_run, self.config.venues)
        while True:
            await asyncio.sleep(self.config.poll_interval)
            try:
                reason = await self._check()
            except Exception as exc:                      # noqa: BLE001
                # A watchdog that dies on an exception is worse than no watchdog.
                log.error('watchdog_check_failed err=%s', exc)
                continue
            if reason and not self._fired:
                self._fired = True
                await self.fire(reason)

    async def _check(self) -> str | None:
        age = time.monotonic() - self._last_heartbeat
        if age > self.config.heartbeat_timeout:
            return f'no heartbeat for {age:.0f}s'

        equity = await self._total_equity()
        now = time.monotonic()
        self._equity.append((now, equity))
        cutoff = now - self.config.equity_window
        window = [e for t, e in self._equity if t >= cutoff]
        if len(window) >= 2:
            peak = max(window)
            if peak > 0 and (peak - equity) / peak > self.config.equity_drop_pct:
                return (f'equity dropped {(peak - equity) / peak:.1%} '
                        f'in {self.config.equity_window:.0f}s')
        return None

    async def _total_equity(self) -> float:
        total = 0.0
        for venue, ex in self.exchanges.items():
            try:
                balance = await ex.fetch_balance()            # type: ignore[attr-defined]
                total += float(balance.get('USDT', {}).get('total', 0.0))
            except Exception as exc:                          # noqa: BLE001
                log.error('watchdog_balance_failed venue=%s err=%s', venue, exc)
        return total

    async def fire(self, reason: str) -> None:
        """Cancel everything, then flatten everything. In that order."""
        log.critical('WATCHDOG_FIRING reason=%s dry_run=%s', reason, self.config.dry_run)
        if self.alert is not None:
            await self.alert.critical(f'WATCHDOG FIRED: {reason}')   # type: ignore[attr-defined]
        if self.config.dry_run:
            return

        for venue, ex in self.exchanges.items():
            try:
                await ex.cancel_all_orders()                  # type: ignore[attr-defined]
            except Exception as exc:                          # noqa: BLE001
                log.error('watchdog_cancel_failed venue=%s err=%s', venue, exc)

        for venue, ex in self.exchanges.items():
            try:
                for pos in await ex.fetch_positions():        # type: ignore[attr-defined]
                    qty = float(pos.get('contracts') or 0.0)
                    if qty == 0:
                        continue
                    side = 'sell' if (pos.get('side') == 'long') else 'buy'
                    await ex.create_order(pos['symbol'], 'market', side, abs(qty),
                                          None, {'reduceOnly': True})
                    log.critical('watchdog_flattened venue=%s symbol=%s qty=%s',
                                 venue, pos['symbol'], qty)
            except Exception as exc:                          # noqa: BLE001
                log.error('watchdog_flatten_failed venue=%s err=%s', venue, exc)

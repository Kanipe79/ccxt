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
    armed: bool = False
    fired_reason: str | None = None

    def heartbeat(self) -> None:
        """Called for every heartbeat the trading stack publishes.

        The heartbeat trigger arms on the FIRST heartbeat, not at process start:
        otherwise a watchdog deployed before the trading stack would "detect" a dead
        system and flatten the account during a routine rollout.
        """
        self._last_heartbeat = time.monotonic()
        if not self.armed:
            self.armed = True
            log.warning('watchdog_armed')

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

    async def on_control(self, raw: bytes) -> bool:
        """Operator kill arriving on ``control.kill``. Only a GLOBAL kill fires L2 —
        strategy/venue-scoped kills are L1's job. Parsed with plain json on purpose:
        no dependency on the trading stack's message classes."""
        import json
        try:
            env = json.loads(raw)
            payload = env.get('payload') or {}
        except (ValueError, AttributeError):
            log.error('watchdog_bad_control_message')
            return False
        if payload.get('command') != 'kill' or payload.get('strategy') or payload.get('venue'):
            return False
        if not self._fired:
            self._fired = True
            await self.fire(f"operator kill: {payload.get('reason', '')}")
        return True

    async def _check(self) -> str | None:
        age = time.monotonic() - self._last_heartbeat
        if self.armed and age > self.config.heartbeat_timeout:
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
        self.fired_reason = reason
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


async def main() -> int:
    """Entry point. Deliberately imports nothing from the rest of ``uxtrader``: heartbeats
    are read as raw NATS messages (any message on ``heartbeat.>`` counts), exchanges
    are plain ccxt, and credentials come from WATCHDOG_-prefixed env vars so that the
    trading stack's keys are never loaded here.

    Env: WATCHDOG_VENUES=binanceusdm,bybit  WATCHDOG_NATS=nats://nats:4222
         WATCHDOG_<VENUE>_APIKEY / _SECRET / _PASSWORD
         WATCHDOG_HEARTBEAT_TIMEOUT  WATCHDOG_EQUITY_DROP_PCT  WATCHDOG_DRY_RUN
    """
    import os

    import ccxt.async_support as ccxt
    import nats

    logging.basicConfig(level=logging.INFO, format='%(asctime)s %(levelname)s watchdog %(message)s')
    venues = tuple(v for v in os.environ.get('WATCHDOG_VENUES', '').split(',') if v)
    cfg = WatchdogConfig(
        heartbeat_timeout=float(os.environ.get('WATCHDOG_HEARTBEAT_TIMEOUT', 90)),
        equity_drop_pct=float(os.environ.get('WATCHDOG_EQUITY_DROP_PCT', 0.05)),
        dry_run=os.environ.get('WATCHDOG_DRY_RUN', 'true').lower() != 'false',
        venues=venues)
    exchanges = {}
    for v in venues:
        creds = {k: os.environ[f'WATCHDOG_{v.upper()}_{e}']
                 for k, e in (('apiKey', 'APIKEY'), ('secret', 'SECRET'), ('password', 'PASSWORD'))
                 if f'WATCHDOG_{v.upper()}_{e}' in os.environ}
        exchanges[v] = getattr(ccxt, v)({**creds, 'enableRateLimit': True})
    dog = Watchdog(cfg, exchanges)

    nc = await nats.connect(os.environ.get('WATCHDOG_NATS', 'nats://nats:4222'),
                            max_reconnect_attempts=-1)

    async def on_heartbeat(_msg) -> None:
        dog.heartbeat()
    await nc.subscribe('heartbeat.>', cb=on_heartbeat)

    async def on_control(msg) -> None:
        await dog.on_control(msg.data)
    # Core-NATS subscription to a JetStream subject still receives live publishes.
    await nc.subscribe('control.kill', cb=on_control)
    try:
        await dog.run()
    finally:
        await nc.drain()
        for ex in exchanges.values():
            await ex.close()
    return 0


if __name__ == '__main__':
    raise SystemExit(asyncio.run(main()))

"""Alerting: bus events → Telegram / Discord / log, with de-duplication.

Alert fatigue is a safety problem (docs/04 §5): an operator who has learned to ignore
alerts has no alerts. So every alert carries a de-dup key and a cooldown, INFO goes to
a low-priority channel only, and CRITICAL is reserved for things that need a human now.
"""
from __future__ import annotations

import logging
import os
import time
from collections.abc import Awaitable, Callable
from dataclasses import dataclass

from ..bus import Bus
from ..events import Control, PortfolioSnapshot, StaleFeed
from ..types import Fill, RiskDecision

log = logging.getLogger(__name__)

Sink = Callable[[str, str], Awaitable[None]]      # (severity, text)


@dataclass
class Alert:
    severity: str        # 'INFO' | 'WARN' | 'CRIT'
    key: str             # de-dup key
    text: str


def telegram_sink(token: str, chat_id: str) -> Sink:
    async def send(severity: str, text: str) -> None:
        import aiohttp
        url = f'https://api.telegram.org/bot{token}/sendMessage'
        async with aiohttp.ClientSession() as s:
            await s.post(url, json={'chat_id': chat_id, 'text': f'[{severity}] {text}',
                                    'disable_notification': severity == 'INFO'})
    return send


def discord_sink(webhook_url: str) -> Sink:
    async def send(severity: str, text: str) -> None:
        import aiohttp
        async with aiohttp.ClientSession() as s:
            await s.post(webhook_url, json={'content': f'**{severity}** {text}'})
    return send


async def log_sink(severity: str, text: str) -> None:
    level = {'INFO': logging.INFO, 'WARN': logging.WARNING}.get(severity, logging.CRITICAL)
    log.log(level, 'ALERT %s', text)


def sinks_from_env() -> dict[str, list[Sink]]:
    """CRIT and WARN go to Telegram (phone); INFO to Discord (a channel you skim)."""
    routes: dict[str, list[Sink]] = {'INFO': [log_sink], 'WARN': [log_sink], 'CRIT': [log_sink]}
    tg_token, tg_chat = os.environ.get('TELEGRAM_BOT_TOKEN'), os.environ.get('TELEGRAM_CHAT_ID')
    if tg_token and tg_chat:
        tg = telegram_sink(tg_token, tg_chat)
        routes['WARN'].append(tg)
        routes['CRIT'].append(tg)
    if os.environ.get('DISCORD_WEBHOOK_URL'):
        routes['INFO'].append(discord_sink(os.environ['DISCORD_WEBHOOK_URL']))
    return routes


class AlertService:
    COOLDOWN_S = {'INFO': 0.0, 'WARN': 600.0, 'CRIT': 60.0}

    def __init__(self, bus: Bus, routes: dict[str, list[Sink]] | None = None, *,
                 amber_dd: float = 0.08, orange_dd: float = 0.12,
                 clock: Callable[[], float] = time.monotonic) -> None:
        self.bus = bus
        self.routes = routes or sinks_from_env()
        self.amber_dd = amber_dd
        self.orange_dd = orange_dd
        self._clock = clock
        self._last_sent: dict[str, float] = {}
        self.sent: list[Alert] = []
        self.suppressed = 0

    async def start(self) -> None:
        await self.bus.subscribe('control.>', self._on_control)
        await self.bus.subscribe('risk.veto', self._on_veto)
        await self.bus.subscribe('feed.stale', self._on_stale)
        await self.bus.subscribe('fill.>', self._on_fill)
        await self.bus.subscribe('portfolio.snapshot', self._on_snapshot)

    async def emit(self, alert: Alert) -> bool:
        now = self._clock()
        last = self._last_sent.get(alert.key)
        if last is not None and now - last < self.COOLDOWN_S.get(alert.severity, 0):
            self.suppressed += 1
            return False
        self._last_sent[alert.key] = now
        self.sent.append(alert)
        for sink in self.routes.get(alert.severity, []):
            try:
                await sink(alert.severity, alert.text)
            except Exception as exc:                          # noqa: BLE001
                log.error('alert_sink_failed severity=%s err=%s', alert.severity, exc)
        return True

    async def _on_control(self, _s: str, cmd) -> None:
        if isinstance(cmd, Control):
            scope = cmd.strategy or cmd.venue or 'GLOBAL'
            await self.emit(Alert('CRIT', f'control:{cmd.command}:{scope}',
                                  f'{cmd.command.upper()} ({scope}): {cmd.reason}'))

    async def _on_veto(self, _s: str, d) -> None:
        if isinstance(d, RiskDecision) and not d.approved:
            limit = d.breaches[0].limit if d.breaches else d.note
            sev = 'CRIT' if limit in {'drawdown_stop', 'weekly_loss', 'daily_loss'} else 'WARN'
            await self.emit(Alert(sev, f'veto:{limit}', f'risk veto: {limit} — {d.note}'))

    async def _on_stale(self, _s: str, m) -> None:
        if isinstance(m, StaleFeed):
            await self.emit(Alert('WARN', f'stale:{m.key}',
                                  f'feed stale {m.key} ({m.age_s:.0f}s) — entries blocked'))

    async def _on_fill(self, _s: str, f) -> None:
        if isinstance(f, Fill):
            slip = f.slippage_bps
            extra = f' slip={slip:+.1f}bps' if slip is not None else ''
            await self.emit(Alert('INFO', f'fill:{f.client_order_id}:{f.ts.timestamp()}',
                                  f'{f.strategy} {f.side} {f.amount} {f.symbol} @ {f.price}{extra}'))
            if f.strategy == 'external':
                await self.emit(Alert('CRIT', f'external:{f.symbol}',
                                      f'EXTERNAL fill on {f.symbol} — manual trading or key reuse?'))

    async def _on_snapshot(self, _s: str, snap) -> None:
        if not isinstance(snap, PortfolioSnapshot) or snap.peak_equity <= 0:
            return
        dd = float((snap.peak_equity - snap.equity) / snap.peak_equity)
        if dd >= self.orange_dd:
            await self.emit(Alert('CRIT', 'dd:orange', f'drawdown {dd:.1%} — ORANGE: sizing halved'))
        elif dd >= self.amber_dd:
            await self.emit(Alert('WARN', 'dd:amber', f'drawdown {dd:.1%} — AMBER: review live vs backtest'))


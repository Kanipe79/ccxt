"""Operator API: read-only views of the platform plus the three control commands.

    uvicorn uxtrader.api.main:app --host 0.0.0.0 --port 8000     # bus from UX_BUS

State is built purely from bus events, so the API holds no authority of its own: it
can be restarted, scaled or deleted without touching trading. The only thing it can
*do* is publish a ``Control`` message — kill, flatten, rearm — and that requires the
bearer token in ``UX_API_TOKEN``. With no token configured, control is disabled
rather than open: an unauthenticated kill endpoint is an outage waiting for a
crawler, and an unauthenticated rearm endpoint is worse.
"""
from __future__ import annotations

import asyncio
import hmac
import json
import logging
import os
from collections import deque
from contextlib import asynccontextmanager
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Literal

from fastapi import Depends, FastAPI, Header, HTTPException, WebSocket, WebSocketDisconnect
from fastapi.responses import HTMLResponse
from pydantic import BaseModel

from ..bus import Bus, connect
from ..events import (
    CONTROL_KILL, CONTROL_REARM, Control, FeedRecovered, Heartbeat, PortfolioSnapshot,
    RiskStatus, StaleFeed, StrategyStatus,
)
from ..types import Bar, Fill, Order, RiskDecision

log = logging.getLogger(__name__)
STATIC = Path(__file__).parent / 'static'


class ApiState:
    def __init__(self, bus: Bus, history: int = 200, journal: Any = None) -> None:
        self.bus = bus
        self.journal = journal
        self.snapshot: PortfolioSnapshot | None = None
        self.equity_curve: deque[tuple[str, float]] = deque(maxlen=3000)
        self.fills: deque[Fill] = deque(maxlen=history)
        self.vetoes: deque[RiskDecision] = deque(maxlen=history)
        self.orders: deque[Order] = deque(maxlen=history)
        self.controls: deque[Control] = deque(maxlen=history)
        self.events: deque[dict[str, Any]] = deque(maxlen=400)
        self.candles: dict[str, deque[list[float]]] = {}
        self.strategies: dict[str, StrategyStatus] = {}
        self.risk: RiskStatus | None = None
        self.heartbeats: dict[str, datetime] = {}
        self.clients: set[WebSocket] = set()
        if journal is not None:
            self.equity_curve.extend(journal.equity(3000))
            self.events.extend(journal.events(400))

    async def start(self) -> None:
        for pattern in ('portfolio.snapshot', 'fill.>', 'risk.veto', 'risk.status',
                        'exec.report', 'control.>', 'heartbeat.>', 'strategy.status',
                        'feed.stale', 'feed.recovered', 'md.*.bar.>'):
            await self.bus.subscribe(pattern, self._on)

    def reset_run(self) -> None:
        """Called when the launcher starts a new run: live views restart, history stays."""
        self.snapshot = None
        self.strategies.clear()
        self.risk = None
        self.candles.clear()

    def _event(self, severity: str, kind: str, text: str, ts: datetime | None = None) -> None:
        if self.events and self.events[0]['kind'] == kind and self.events[0]['text'] == text:
            self.events[0]['count'] = self.events[0].get('count', 1) + 1
            return
        self.events.appendleft({'ts': (ts or datetime.now(timezone.utc)).isoformat(),
                                'severity': severity, 'kind': kind, 'text': text, 'count': 1})

    async def _on(self, subject: str, msg: Any) -> None:
        kind = type(msg).__name__
        push = True
        if isinstance(msg, PortfolioSnapshot):
            if self.snapshot is not None and msg.seq <= self.snapshot.seq:
                return
            self.snapshot = msg
            self.equity_curve.append((msg.ts.isoformat(), float(msg.equity)))
        elif isinstance(msg, Bar):
            c = self.candles.setdefault(msg.symbol, deque(maxlen=720))
            c.append([msg.ts.timestamp(), float(msg.open), float(msg.high), float(msg.low),
                      float(msg.close), float(msg.volume)])
            push = False                            # snapshots already announce new bars
        elif isinstance(msg, Fill):
            self.fills.appendleft(msg)
            verb = 'bought' if msg.side == 'buy' else 'sold'
            base = msg.symbol.split('/')[0]
            # The strategy id leads the text so the UI can swap in its display name.
            self._event('INFO', 'fill', f'{msg.strategy} {verb} {float(msg.amount):.4g} {base} '
                        f'@ {float(msg.price):,.2f}', msg.ts)
        elif isinstance(msg, RiskDecision):
            self.vetoes.appendleft(msg)
            limit = msg.breaches[0].limit if msg.breaches else 'veto'
            self._event('WARN', 'veto', f'risk veto — {limit}: {msg.note}')
        elif isinstance(msg, RiskStatus):
            self.risk = msg
        elif isinstance(msg, StrategyStatus):
            self.strategies[msg.name] = msg
        elif isinstance(msg, Order):
            self.orders.appendleft(msg)
            push = False
        elif isinstance(msg, Control):
            self.controls.appendleft(msg)
            scope = msg.strategy or msg.venue or 'all'
            self._event('CRIT' if msg.command == 'kill' else 'WARN', msg.command,
                        f'{msg.command.upper()} ({scope}): {msg.reason}', msg.ts)
        elif isinstance(msg, StaleFeed):
            self._event('WARN', 'feed', f'feed stale: {msg.symbol} ({msg.age_s:.0f}s)', msg.ts)
        elif isinstance(msg, FeedRecovered):
            self._event('INFO', 'feed', f'feed recovered: {msg.symbol}', msg.ts)
        elif isinstance(msg, Heartbeat):
            self.heartbeats[msg.service] = msg.ts
            return                                   # not pushed: too chatty
        if push:
            await self.broadcast({'type': kind, 'subject': subject})

    async def broadcast(self, payload: dict[str, Any]) -> None:
        text = json.dumps(payload, default=str)
        dead = []
        for ws in list(self.clients):
            try:
                await ws.send_text(text)
            except Exception:                        # noqa: BLE001
                dead.append(ws)
        for ws in dead:
            self.clients.discard(ws)

    def kill_active(self) -> bool:
        if self.risk is not None:
            return self.risk.kill_global
        for c in self.controls:
            if c.strategy is None and c.venue is None and c.command in ('kill', 'rearm'):
                return c.command == 'kill'
        return False


class ControlRequest(BaseModel):
    command: Literal['kill', 'flatten', 'rearm']
    reason: str
    strategy: str | None = None
    venue: str | None = None


class LaunchStrategy(BaseModel):
    id: str
    enabled: bool = True
    risk_budget: float
    stage: int = 1
    symbols: list[str] = []


class LaunchRequest(BaseModel):
    mode: Literal['demo', 'paper', 'live']
    venue: str = 'binanceusdm'
    strategies: list[LaunchStrategy]
    starting_equity: float = 100_000.0
    speed: float = 0.1                   # demo: real seconds per simulated minute
    confirm: str | None = None           # live: must equal 'LIVE'


class StopRequest(BaseModel):
    flatten: bool = True


def create_app(bus: Bus | None = None, *, token: str | None = None,
               journal: Any = None, launcher: Any = None) -> FastAPI:
    holder: dict[str, Any] = {}

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        b = bus or await connect(os.environ.get('UX_BUS', 'nats://nats:4222'), service='api')
        state = ApiState(b, journal=journal)
        await state.start()
        holder['state'] = state
        if launcher is not None:
            launcher.on_new_run = state.reset_run
        yield
        if launcher is not None and launcher.state == 'running':
            await launcher.stop(flatten=False)
        if bus is None:
            await b.close()

    app = FastAPI(title='ux-trader', lifespan=lifespan)
    expected = token if token is not None else os.environ.get('UX_API_TOKEN')

    def state() -> ApiState:
        return holder['state']

    def require_token(authorization: str | None = Header(default=None)) -> None:
        if not expected:
            raise HTTPException(403, 'control disabled: set UX_API_TOKEN')
        supplied = (authorization or '').removeprefix('Bearer ').strip()
        if not hmac.compare_digest(supplied.encode(), expected.encode()):
            raise HTTPException(401, 'bad or missing bearer token')

    @app.get('/', response_class=HTMLResponse)
    async def dashboard() -> str:
        return (STATIC / 'index.html').read_text()

    from fastapi.staticfiles import StaticFiles
    app.mount('/static', StaticFiles(directory=STATIC), name='static')

    @app.get('/api/health')
    async def health(s: ApiState = Depends(state)) -> dict[str, Any]:
        now = datetime.now(timezone.utc)
        return {'ok': True, 'snapshot_seq': s.snapshot.seq if s.snapshot else None,
                'services': {k: round((now - v).total_seconds(), 1) for k, v in s.heartbeats.items()}}

    @app.get('/api/state')
    async def full_state(s: ApiState = Depends(state)) -> dict[str, Any]:
        """Everything the dashboard draws, in one round trip."""
        snap = s.snapshot
        now = datetime.now(timezone.utc)
        return {
            'snapshot': snap.model_dump(mode='json') if snap else None,
            'equity_curve': list(s.equity_curve),
            'strategies': [st.model_dump(mode='json') | {'warm': st.warm}
                           for st in s.strategies.values()],
            'risk': s.risk.model_dump(mode='json') if s.risk else None,
            'kill_active': s.kill_active(),
            'control_enabled': bool(expected),
            'services': {k: round((now - v).total_seconds(), 1) for k, v in s.heartbeats.items()},
            'symbols': sorted(s.candles),
            'launcher': launcher.status() if launcher is not None else None,
        }

    @app.get('/api/candles')
    async def candles(symbol: str, limit: int = 240, s: ApiState = Depends(state)) -> list[list[float]]:
        return list(s.candles.get(symbol, []))[-limit:]

    @app.get('/api/events')
    async def events(limit: int = 100, s: ApiState = Depends(state)) -> list[dict]:
        return list(s.events)[:limit]

    @app.get('/api/launcher')
    async def launcher_info() -> dict[str, Any]:
        if launcher is None:
            raise HTTPException(404, 'this API is not running a launcher')
        return {'status': launcher.status(), 'catalog': launcher.catalog()}

    @app.post('/api/launcher/start', dependencies=[Depends(require_token)])
    async def launcher_start(req: LaunchRequest) -> dict[str, Any]:
        if launcher is None:
            raise HTTPException(404, 'this API is not running a launcher')
        try:
            await launcher.start(req)
        except ValueError as exc:
            raise HTTPException(400, str(exc)) from exc
        return launcher.status()

    @app.post('/api/launcher/stop', dependencies=[Depends(require_token)])
    async def launcher_stop(req: StopRequest) -> dict[str, Any]:
        if launcher is None:
            raise HTTPException(404, 'this API is not running a launcher')
        await launcher.stop(flatten=req.flatten)
        return launcher.status()

    @app.get('/api/snapshot')
    async def snapshot(s: ApiState = Depends(state)) -> dict[str, Any]:
        snap = s.snapshot
        return {'snapshot': snap.model_dump(mode='json') if snap else None,
                'equity_curve': list(s.equity_curve), 'kill_active': s.kill_active(),
                'control_enabled': bool(expected)}

    @app.get('/api/fills')
    async def fills(limit: int = 50, s: ApiState = Depends(state)) -> list[dict]:
        live = [f.model_dump(mode='json') | {'slippage_bps': f.slippage_bps}
                for f in list(s.fills)[:limit]]
        if len(live) < limit and s.journal is not None:
            # Key on stable fields: the journal's ISO timestamps ('+00:00') and Pydantic's
            # ('Z') differ as strings for the same instant.
            def key(f):
                return (f['client_order_id'], round(float(f['price']), 8), round(float(f['amount']), 8))
            seen = {key(f) for f in live}
            live += [f for f in s.journal.fills(limit) if key(f) not in seen][:limit - len(live)]
        return live

    @app.get('/api/vetoes')
    async def vetoes(limit: int = 50, s: ApiState = Depends(state)) -> list[dict]:
        return [v.model_dump(mode='json') for v in list(s.vetoes)[:limit]]

    @app.get('/api/orders')
    async def orders(limit: int = 50, s: ApiState = Depends(state)) -> list[dict]:
        return [o.model_dump(mode='json') for o in list(s.orders)[:limit]]

    @app.post('/api/control', dependencies=[Depends(require_token)])
    async def control(req: ControlRequest, s: ApiState = Depends(state)) -> dict[str, str]:
        subject = {'kill': CONTROL_KILL, 'rearm': CONTROL_REARM}.get(req.command, 'control.flatten')
        await s.bus.publish(subject, Control(command=req.command, reason=req.reason,
                                             strategy=req.strategy, venue=req.venue))
        log.critical('operator_control command=%s reason=%s', req.command, req.reason)
        return {'published': subject}

    @app.websocket('/ws')
    async def ws(websocket: WebSocket) -> None:
        await websocket.accept()
        s = state()
        s.clients.add(websocket)
        try:
            while True:
                await asyncio.wait_for(websocket.receive_text(), timeout=3600)
        except (WebSocketDisconnect, asyncio.TimeoutError):
            pass
        finally:
            s.clients.discard(websocket)

    return app


app = create_app()

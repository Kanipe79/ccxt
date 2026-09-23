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
from ..events import CONTROL_KILL, CONTROL_REARM, Control, Heartbeat, PortfolioSnapshot
from ..types import Fill, Order, RiskDecision

log = logging.getLogger(__name__)
STATIC = Path(__file__).parent / 'static'


class ApiState:
    def __init__(self, bus: Bus, history: int = 200) -> None:
        self.bus = bus
        self.snapshot: PortfolioSnapshot | None = None
        self.equity_curve: deque[tuple[str, float]] = deque(maxlen=2000)
        self.fills: deque[Fill] = deque(maxlen=history)
        self.vetoes: deque[RiskDecision] = deque(maxlen=history)
        self.orders: deque[Order] = deque(maxlen=history)
        self.controls: deque[Control] = deque(maxlen=history)
        self.heartbeats: dict[str, datetime] = {}
        self.clients: set[WebSocket] = set()

    async def start(self) -> None:
        await self.bus.subscribe('portfolio.snapshot', self._on)
        await self.bus.subscribe('fill.>', self._on)
        await self.bus.subscribe('risk.veto', self._on)
        await self.bus.subscribe('exec.report', self._on)
        await self.bus.subscribe('control.>', self._on)
        await self.bus.subscribe('heartbeat.>', self._on)

    async def _on(self, subject: str, msg: Any) -> None:
        kind = type(msg).__name__
        if isinstance(msg, PortfolioSnapshot):
            if self.snapshot is not None and msg.seq <= self.snapshot.seq:
                return
            self.snapshot = msg
            self.equity_curve.append((msg.ts.isoformat(), float(msg.equity)))
        elif isinstance(msg, Fill):
            self.fills.appendleft(msg)
        elif isinstance(msg, RiskDecision):
            self.vetoes.appendleft(msg)
        elif isinstance(msg, Order):
            self.orders.appendleft(msg)
        elif isinstance(msg, Control):
            self.controls.appendleft(msg)
        elif isinstance(msg, Heartbeat):
            self.heartbeats[msg.service] = msg.ts
            return                                   # not pushed: too chatty
        await self.broadcast({'type': kind, 'subject': subject,
                              'data': msg.model_dump(mode='json')})

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
        """Latest kill/rearm wins (per global scope)."""
        for c in self.controls:
            if c.strategy is None and c.venue is None and c.command in ('kill', 'rearm'):
                return c.command == 'kill'
        return False


class ControlRequest(BaseModel):
    command: Literal['kill', 'flatten', 'rearm']
    reason: str
    strategy: str | None = None
    venue: str | None = None


def create_app(bus: Bus | None = None, *, token: str | None = None) -> FastAPI:
    holder: dict[str, Any] = {}

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        b = bus or await connect(os.environ.get('UX_BUS', 'nats://nats:4222'))
        state = ApiState(b)
        await state.start()
        holder['state'] = state
        yield
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

    @app.get('/api/health')
    async def health(s: ApiState = Depends(state)) -> dict[str, Any]:
        now = datetime.now(timezone.utc)
        return {'ok': True, 'snapshot_seq': s.snapshot.seq if s.snapshot else None,
                'services': {k: round((now - v).total_seconds(), 1) for k, v in s.heartbeats.items()}}

    @app.get('/api/snapshot')
    async def snapshot(s: ApiState = Depends(state)) -> dict[str, Any]:
        snap = s.snapshot
        return {'snapshot': snap.model_dump(mode='json') if snap else None,
                'equity_curve': list(s.equity_curve), 'kill_active': s.kill_active(),
                'control_enabled': bool(expected)}

    @app.get('/api/fills')
    async def fills(limit: int = 50, s: ApiState = Depends(state)) -> list[dict]:
        return [f.model_dump(mode='json') | {'slippage_bps': f.slippage_bps}
                for f in list(s.fills)[:limit]]

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

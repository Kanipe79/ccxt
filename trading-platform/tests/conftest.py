"""Fixtures that drive the REAL ccxt ``binanceusdm`` class with only the HTTP layer
stubbed. Everything above ``fetch`` — request building, signing, response parsing,
CCXT's own error mapping inputs — is the genuine library code from this fork.
"""
from __future__ import annotations

import json
from collections.abc import Callable
from pathlib import Path
from typing import Any

import pytest

from uxcore import errors as ux_errors
from uxcore import resilience
from uxcore.errors import RetryPolicy

# The fork's own static fixture: real Binance market metadata, checked into ccxt.
MARKETS_FIXTURE = Path(__file__).resolve().parents[2] / 'ts/src/test/static/markets/binance.json'


@pytest.fixture(autouse=True)
def fast_policies(monkeypatch):
    """Same policy *shape* (attempt counts, reconcile requirement), millisecond delays."""
    fast = {k: RetryPolicy(v.max_attempts, 0.001, 0.002, 0.0, v.requires_reconcile)
            for k, v in ux_errors.POLICIES.items()}
    monkeypatch.setattr(ux_errors, 'POLICIES', fast)
    resilience._BREAKERS.clear()
    yield
    resilience._BREAKERS.clear()


class StubTransport:
    """Routes (method, path) to a queue of responses. A response that is an exception
    instance is raised, which is how we force 429s and timeouts."""

    def __init__(self) -> None:
        self.routes: dict[tuple[str, str], list[Any]] = {}
        self.calls: list[tuple[str, str, str]] = []
        self.headers: dict[str, str] = {}

    def on(self, method: str, path: str, *responses: Any) -> StubTransport:
        self.routes.setdefault((method, path), []).extend(responses)
        return self

    def count(self, method: str, path: str) -> int:
        return sum(1 for m, p, _ in self.calls if m == method and p == path)

    def bodies(self, method: str, path: str) -> list[str]:
        return [b for m, p, b in self.calls if m == method and p == path]

    def bind(self, exchange: Any) -> Callable[..., Any]:
        async def fetch(url, method='GET', headers=None, body=None):
            path = url.split('?')[0].split('.com/')[-1]
            query = url.split('?', 1)[1] if '?' in url else ''
            self.calls.append((method, path, (body or '') + query))
            queue = self.routes.get((method, path))
            if not queue:
                raise AssertionError(f'unexpected call {method} {path}')
            resp = queue.pop(0) if len(queue) > 1 else queue[0]
            exchange.last_response_headers = dict(self.headers)
            if isinstance(resp, BaseException):
                raise resp
            return resp
        return fetch


@pytest.fixture
def transport():
    return StubTransport()


@pytest.fixture
async def ex(transport):
    pytest.importorskip('ccxt')
    if not MARKETS_FIXTURE.exists():
        pytest.skip('binance markets fixture not present (running outside the fork)')
    from uxcore import make_exchange
    exchange = make_exchange('binanceusdm', {'apiKey': 'key', 'secret': 'secret'})
    markets = json.loads(MARKETS_FIXTURE.read_text())
    exchange.set_markets(list(markets.values()))
    exchange.fetch = transport.bind(exchange)
    yield exchange
    await exchange.close()


def order_json(coid: str = 'uxtest0001', status: str = 'NEW', **kw) -> dict[str, Any]:
    base = {'orderId': 4242, 'clientOrderId': coid, 'symbol': 'BTCUSDT', 'status': status,
            'price': '50000', 'avgPrice': '0', 'origQty': '0.010', 'executedQty': '0',
            'side': 'BUY', 'type': 'LIMIT', 'timeInForce': 'GTC', 'reduceOnly': False,
            'positionSide': 'BOTH', 'updateTime': 1700000000000}
    base.update(kw)
    return base

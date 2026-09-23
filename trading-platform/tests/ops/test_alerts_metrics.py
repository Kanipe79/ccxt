from __future__ import annotations

from decimal import Decimal

from prometheus_client import generate_latest

from uxtrader.events import CONTROL_KILL, FEED_STALE, Control, StaleFeed
from uxtrader.ops.alerts import AlertService
from uxtrader.ops.metrics import MetricsService

import sys
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'integration'))
from test_services_e2e import SYM, VENUE, Platform, spec  # noqa: E402


async def build():
    pf = Platform([spec()])
    sent = []

    async def capture(sev, text):
        sent.append((sev, text))
    alerts = AlertService(pf.bus, {'INFO': [capture], 'WARN': [capture], 'CRIT': [capture]})
    metrics = MetricsService(pf.bus)
    await alerts.start()
    await metrics.start()
    await pf.start()
    return pf, alerts, metrics, sent


def sample(metrics, name, **labels):
    return metrics.registry.get_sample_value(name, labels or None)


async def test_round_trip_is_measured():
    pf, _, metrics, sent = await build()
    await pf.minutes(15, 50000)
    assert sample(metrics, 'ux_fills_total', strategy='toggle', liquidity='taker') == 2
    assert sample(metrics, 'ux_orders_submitted_total', strategy='toggle') == 2
    # Paying the synthetic half-spread is a positive (cost) slippage on every fill.
    assert sample(metrics, 'ux_slippage_bps_count', strategy='toggle') == 2
    assert sample(metrics, 'ux_slippage_bps_signed_sum', strategy='toggle') > 0
    assert sample(metrics, 'ux_equity') is not None
    assert b'ux_position_notional' in generate_latest(metrics.registry)
    assert [s for s, _ in sent].count('INFO') == 2           # one per fill


async def test_kill_is_critical_and_visible():
    pf, _, metrics, sent = await build()
    await pf.bus.publish(CONTROL_KILL, Control(command='kill', reason='drill'))
    assert sample(metrics, 'ux_kill_switch_active', scope='global') == 1
    assert ('CRIT', 'KILL (GLOBAL): drill') in sent


async def test_repeated_warnings_are_deduplicated():
    pf, alerts, _, sent = await build()
    stale = StaleFeed(key=f'{VENUE}|{SYM}|book', venue=VENUE, symbol=SYM, age_s=7)
    for _ in range(5):
        await pf.bus.publish(FEED_STALE, stale)
    assert [s for s, _ in sent].count('WARN') == 1
    assert alerts.suppressed == 4


async def test_drawdown_ladder_alerts():
    pf, _, _, sent = await build()
    await pf.minutes(5, 50000)                   # long 0.1 BTC
    pf.portfolio.portfolio.peak_equity = Decimal('110000')    # simulate an earlier high
    await pf.minute_bar(50000)
    assert any(s == 'WARN' and 'AMBER' in t for s, t in sent)

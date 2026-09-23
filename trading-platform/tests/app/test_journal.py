from __future__ import annotations

import sys
from pathlib import Path

from uxtrader.api.main import ApiState
from uxtrader.events import Control
from uxtrader.services.journal import Journal

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'integration'))
from test_services_e2e import Platform, spec  # noqa: E402


async def test_history_survives_a_restart(tmp_path):
    pf = await Platform([spec()]).start()
    j = Journal(tmp_path / 'j.db', equity_every_s=0)
    await j.start(pf.bus)
    await pf.minutes(15, 50000)
    await pf.bus.publish('control.kill', Control(command='kill', reason='drill'))
    await pf.bus.publish('control.kill', Control(command='kill', reason='drill'))   # repeat
    j.close()

    j2 = Journal(tmp_path / 'j.db')
    assert len(j2.fills()) == 2 and j2.fills()[0]['strategy'] == 'toggle'
    assert len(j2.equity()) >= 15
    kills = [e for e in j2.events() if e['kind'] == 'kill']
    assert len(kills) == 1, 'consecutive duplicates must collapse'
    state = ApiState(pf.bus, journal=j2)
    assert len(state.equity_curve) >= 15 and state.events

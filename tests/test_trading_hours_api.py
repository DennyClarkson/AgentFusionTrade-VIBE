import copy
from datetime import datetime
from types import SimpleNamespace

import pytest
from fastapi.testclient import TestClient

from fusion.app import create_app
from fusion.ea_control import read_packet
from fusion.strategy import session_open


@pytest.mark.parametrize('start,end,weekends', [(22, 2, True), (0, 0, False), (6, 21, True)])
def test_saved_window_is_versioned_and_published_unchanged(configured, tmp_path, monkeypatch, start, end, weekends):
    store, broker, _ = configured
    app = create_app(store, broker)
    engine = app.state.engine
    native = engine.ea_controller
    broker.m = SimpleNamespace()
    monkeypatch.setattr(native, 'root', lambda: tmp_path)
    telemetry = {'connected': True, 'instance_id': 'test-boot', 'symbol': 'XAUUSD', 'entries_enabled': False,
                 'pending': False, 'positions': 0, 'command_revision': 0, 'strategy_revision': 0}
    monkeypatch.setattr(native, 'status', lambda: copy.deepcopy(telemetry))
    with TestClient(app) as client:
        boot = client.get('/api/bootstrap').json()
        headers = {'X-Fusion-Token': boot['token']}
        row = next(r for r in boot['configs'] if r['category'] == 'workflow' and r['active'])
        data = {**row['data'], 'session_start_utc': start, 'session_end_utc': end, 'block_weekends': weekends}
        stopped = client.post('/api/engine/stop', json={}, headers=headers)
        assert stopped.status_code == 200
        telemetry['command_revision'] = native.last_command['revision']
        response = client.put(f"/api/configs/workflow/{row['id']}", headers=headers,
                              json={'name': row['name'], 'data': data, 'expected_version': row['version']})
        assert response.status_code == 200, response.text
        saved = response.json()
        assert saved['version'] == row['version'] + 1 and saved['active']
        assert saved['data'] == data
        exported = client.post('/api/ea/parameters/export', json={}, headers=headers)
        assert exported.status_code == 200, exported.text
        assert exported.json()['status'] == 'published', 'publication alone is not application'
        packet = read_packet(tmp_path / 'execution.csv')
        assert int(packet['session_start_utc']) == start
        assert int(packet['session_end_utc']) == end
        assert int(packet['block_weekends']) == int(weekends)
        assert int(packet['revision']) == exported.json()['revision']
        assert not engine.running and not engine.armed and not broker.sent


def test_existing_python_gate_matches_editor_boundary_semantics():
    workflow = {'session_start_utc': 22, 'session_end_utc': 2, 'block_weekends': True}
    for stamp, allowed in [('2026-10-01T21:59:59+00:00', False), ('2026-10-01T22:00:00+00:00', True),
                           ('2026-10-02T01:59:59+00:00', True), ('2026-10-02T02:00:00+00:00', False),
                           ('2026-10-03T01:00:00+00:00', False)]:
        assert session_open(datetime.fromisoformat(stamp).timestamp(), workflow) == allowed

import json
import time

import pytest

from fusion.pid_manager import Decision, ManagerConfig, PIDManager, fresh, packet_read, packet_write


class Client:
    def __init__(self):
        self.calls = 0
        self.callback = lambda: None

    def complete(self, *args, **kwargs):
        self.calls += 1
        self.callback()
        return {"status": "ok", "content": json.dumps({"enabled": True, "reason": "维持当前参数", "parameters": {}}), "_protocol": [{"private": "hidden"}]}


@pytest.fixture
def native(tmp_path):
    data = dict(protocol="FUSION_PID1", boot="boot-A", session="session-A", login="1", server="demo", symbol="XAUUSD", magic="123", time=str(int(time.time())), armed="1", position="0", pending="0", kp="0.8", ki="0.04", kd="0.12", upper="1", lower="1", stop_atr="1.5", activation="3", allow_ai_parameters="0")
    path = tmp_path/"scope_telemetry.csv"
    packet_write(path, data)
    client = Client()
    manager = PIDManager(tmp_path, ManagerConfig(), client=client)
    yield manager, client, data, path
    manager.close()


def test_packets_follow_native_delimiter_and_reject_partial(tmp_path):
    p = tmp_path/"a.csv"
    packet_write(p, {"text": '"hello";\nworld'})
    assert packet_read(p) == {"text": '"hello"， world'}
    p.write_text("x;1\nx;2\nEND;2\n")
    with pytest.raises(ValueError): packet_read(p)


def test_fresh_validates_identity_structure(native):
    _, _, data, _ = native
    assert fresh(data)
    for key in ("boot", "session", "magic", "kp", "armed"):
        broken = dict(data); del broken[key]
        assert not fresh(broken)
    assert not fresh({**data, "kp": "nan"})
    assert not fresh({**data, "armed": "true"})


def test_singleton_does_not_reset_other_workers(native):
    manager, _, _, path = native
    with pytest.raises(RuntimeError, match="already running"):
        PIDManager(path.parent, ManagerConfig())


def test_each_fill_reviewed_once_and_public_results_hide_protocol(native):
    manager, client, _, path = native
    for deal in ("10", "11"):
        packet_write(path.parent/f"scope_deal_{deal}.csv", {"deal": deal, "reason": 4})
    assert manager.run_one(path)
    assert manager.run_one(path)
    assert client.calls == 2
    results = manager.db.execute("SELECT result FROM jobs WHERE kind='exit'").fetchall()
    assert len(results) == 2 and all("_protocol" not in r[0] for r in results)
    assert packet_read(path.parent/"scope_control.csv")["session"] == "session-A"
    manager.discover("scope_", packet_read(path))
    assert manager.db.execute("SELECT count(*) FROM jobs WHERE kind='exit'").fetchone()[0] == 2


def test_arm_change_during_model_never_publishes_old_permission(native):
    manager, client, data, path = native
    client.callback = lambda: packet_write(path, {**data, "session": "session-B"})
    manager.run_one(path)
    assert not (path.parent/"scope_control.csv").exists()
    assert manager.db.execute("SELECT state FROM jobs").fetchone()[0] == "done"


def test_queued_old_session_chat_is_cancelled(native):
    manager, client, data, path = native
    packet_write(path.parent/"scope_ask_1.csv", {"boot": "boot-A", "session": "old", "text": "old question"})
    manager.run_one(path)
    assert client.calls == 0
    assert manager.db.execute("SELECT state FROM jobs WHERE kind='chat'").fetchone()[0] == "cancelled"


def test_report_failure_cannot_repeat_completed_model(native, monkeypatch):
    manager, client, _, path = native
    import fusion.pid_manager as module
    original = module.packet_write
    def writer(file, values):
        if file.name.endswith("report.csv"): raise PermissionError("busy")
        return original(file, values)
    monkeypatch.setattr(module, "packet_write", writer)
    manager.run_one(path)
    manager.run_one(path)
    assert client.calls == 1
    assert manager.db.execute("SELECT state FROM jobs").fetchone()[0] == "done"


def test_control_write_failure_retries_persisted_result_without_model(native, monkeypatch):
    manager, client, _, path = native
    import fusion.pid_manager as module
    original = module.packet_write
    def writer(file, values):
        if file.name.endswith("control.csv"): raise PermissionError("busy")
        return original(file, values)
    monkeypatch.setattr(module, "packet_write", writer)
    with pytest.raises(PermissionError): manager.run_one(path)
    row = manager.db.execute("SELECT state,result FROM jobs").fetchone()
    assert row[0] == "prepared" and client.calls == 1
    revision = json.loads(row[1])["control"]["revision"]
    monkeypatch.setattr(module, "packet_write", original)
    manager.run_one(path)
    assert client.calls == 1
    assert packet_read(path.parent/"scope_control.csv")["revision"] == str(revision)


def test_prepared_result_cannot_authorize_restarted_session(native, monkeypatch):
    manager, client, data, path = native
    original = manager.finish_prepared
    monkeypatch.setattr(manager, "finish_prepared", lambda *a: None)
    manager.run_one(path)
    packet_write(path, {**data, "session": "new"})
    monkeypatch.setattr(manager, "finish_prepared", original)
    manager.run_one(path)
    assert client.calls == 1
    assert not (path.parent/"scope_control.csv").exists()


def test_telemetry_failure_after_model_retries_without_another_model(native, monkeypatch):
    manager, client, _, path = native
    import fusion.pid_manager as module
    original = module.packet_read
    def reader(file):
        if client.calls and file == path:
            raise PermissionError("busy after model completion")
        return original(file)
    monkeypatch.setattr(module, "packet_read", reader)
    with pytest.raises(PermissionError): manager.run_one(path)
    assert manager.db.execute("SELECT state FROM jobs").fetchone()[0] == "prepared"
    monkeypatch.setattr(module, "packet_read", original)
    manager.run_one(path)
    assert client.calls == 1
    assert manager.db.execute("SELECT state FROM jobs").fetchone()[0] == "done"


def test_no_permission_can_arm_paused_ea(native):
    manager, _, data, _ = native
    paused = {**data, "armed": "0"}
    result = manager.control(paused, paused, Decision(enabled=True, reason="test"))
    assert result["enabled"] == 0
    assert result["apply"] == 0


def test_unknown_model_parameter_rejected(native):
    manager, _, data, _ = native
    with pytest.raises(ValueError, match="unknown parameter"):
        manager.control(data, data, Decision(enabled=True, reason="test", parameters={"lots": 1}))

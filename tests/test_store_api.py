import pytest
from fastapi.testclient import TestClient

from fusion.app import create_app
from fusion.config import AI, Strategy
from fusion.store import Conflict


def test_revision_conflict_and_restart_preserve_config(configured):
    store, _, _ = configured
    row = next(x for x in store.configs() if x["category"] == "risk")
    saved = store.save("risk", "default", "Conservative", {**row["data"], "capital": 500}, row["version"])
    assert saved["version"] == row["version"]+1
    with pytest.raises(Conflict): store.save("risk", "default", "Old", row["data"], row["version"])
    from fusion.store import Store
    assert Store(store.path).active()[0]["risk"]["capital"] == 500
    assert len(store.history("risk", "default")) == saved["version"]


def test_validation_rejects_bad_values():
    with pytest.raises(ValueError): AI(base_url="http://example.com")
    with pytest.raises(ValueError): AI(api_key="accidental-secret")
    with pytest.raises(ValueError): Strategy(fast_ema=60, slow_ema=50)


def test_api_token_origin_and_host_boundaries(configured):
    store, broker, _ = configured
    with TestClient(create_app(store, broker)) as client:
        boot = client.get("/api/bootstrap").json()
        assert "DEEPSEEK" in str(boot)  # env NAME only
        assert client.post("/api/engine/stop").status_code == 403
        headers = {"X-Fusion-Token": boot["token"]}
        assert client.post("/api/engine/stop", headers=headers).status_code == 200
        assert client.get("/api/bootstrap", headers={"Origin": "https://evil.example"}).status_code == 403
        assert client.get("/api/bootstrap", headers={"Host": "evil.example"}).status_code == 400
        row = next(x for x in boot["configs"] if x["category"] == "risk")
        bad = {"name": "bad", "expected_version": row["version"], "data": {**row["data"], "capital": -1}}
        assert client.put("/api/configs/risk/default", headers=headers, json=bad).status_code == 422

import threading
import time

import pytest

from fusion.store import Conflict
from fusion.broker import BrokerError


def test_atomic_paper_state_rolls_back_entire_update_on_invalid_value(configured):
    store,_,_ = configured
    store.put_many({"paper_positions":[{"id":"old"}],"paper_trades":[]})
    with pytest.raises(ValueError): store.put_many({"paper_positions":[],"paper_trades":[{"pnl":float('nan')}]})
    assert store.get("paper_positions") == [{"id":"old"}]
    assert store.get("paper_trades") == []


def test_intent_resolution_and_ownership_are_one_transaction(configured):
    store,_,_ = configured
    store.intent("pending",{"request":{}})
    with pytest.raises(ValueError): store.resolve("pending","done",{}, {"ownership":float('nan')})
    assert store.unresolved()[0]["id"] == "pending"


def test_close_preflight_failure_creates_no_unknown_intent(configured):
    store,broker,engine = configured
    def fail(*args): raise BrokerError("known preflight refusal")
    broker.prepare_close = fail
    with pytest.raises(BrokerError): engine.close_demo(12)
    assert not store.unresolved()


def test_other_account_owned_exposure_blocks_config_changes(configured):
    store,_,engine = configured
    store.put("managed_demo_accounts",{"another|broker":[{"ticket":42}]})
    with pytest.raises(Conflict): engine.mutation_allowed()


def test_shadow_mcp_expected_mode_cannot_run_demo(configured):
    store,broker,engine = configured
    row = next(x for x in store.configs() if x["category"]=="workflow")
    store.save("workflow","default",row["name"],{**row["data"],"mode":"demo"},row["version"])
    engine.arm(123)
    result=engine.cycle(expected_mode="shadow")
    assert result["status"] == "error" and not broker.sent


def test_unknown_without_broker_evidence_stays_locked(configured):
    store,broker,engine = configured
    store.intent("unknown",{"account":[123,"Demo"],"request":{"magic":26010001,"symbol":"XAUUSD","comment":"fusion unique"}})
    result = engine.reconcile()
    assert result["unresolved"] == 1
    assert not broker.sent

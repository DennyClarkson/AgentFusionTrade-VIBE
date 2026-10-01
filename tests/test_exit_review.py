import copy
import json
import threading
import time
from types import SimpleNamespace

import pytest

from fusion.broker import MT5Broker, BrokerError
from fusion.ea_manager import EAManager
from fusion.exit_review import ExitReviewQueue
from fusion.store import Conflict


def select(store, category, **patch):
    row = next(r for r in store.configs() if r['active'] and r['category'] == category)
    store.save(category, row['id'], row['name'], {**row['data'], **patch}, row['version'])


def deal(ticket, reason='sl', **patch):
    stamp = int((time.time()-2)*1000)
    return {'ticket': ticket, 'order': 900, 'position_id': 800, 'symbol': 'XAUUSD', 'magic': 26010002,
            'type': 1, 'entry': 1, 'reason': 4 if reason == 'sl' else 5, 'exit_reason': reason,
            'time': stamp/1000, 'time_msc': stamp, 'time_raw': stamp/1000, 'time_msc_raw': stamp,
            'volume': .01, 'price': 2001, 'profit': 1 if reason == 'sl' else 2, **patch}


@pytest.fixture
def exits(configured, tmp_path):
    store, broker, engine = configured
    select(store, 'workflow', module='ea', mode='demo')
    engine.running = engine.armed = True
    broker.account_pin = (broker.login, 'Demo')
    manager = EAManager(engine, tmp_path)
    engine.ea_manager = manager
    cfg, _ = store.active()
    scope = manager.exit_reviews.scope(cfg, broker.status()['account'])
    manager.exit_reviews.begin(scope, time.time()-60)
    manager.initialize_exit_monitor(cfg)
    store.put('ea_last_review', time.time())
    engine.ea_controller.decision = {'enabled': True, 'expires_at': time.time()+3600}
    rows, calls = [], []

    def history(epoch, expected_account=None):
        account = broker.status()['account']
        if expected_account and tuple(expected_account) != (account['login'], account['server']):
            raise BrokerError('changed account')
        return {'account': account, 'captured_at': time.time(), 'deals': copy.deepcopy(rows)}

    broker.deal_history_snapshot = history
    broker.deals_since = lambda epoch: copy.deepcopy(rows)

    def complete(*args, **kwargs):
        calls.append(args[2])
        return {'status': 'ok', 'content': '已评估；保持原参数，样本不足。', 'tools': []}

    manager.client.complete = complete
    yield store, broker, engine, manager, rows, calls, scope
    manager.stop()
    if manager.review_thread and manager.review_thread.ident is not None:
        manager.review_thread.join(5)
    assert not broker.sent


def wait(manager):
    manager.review_thread.join(5)
    assert not manager.review_thread.is_alive()


def test_every_sl_tp_exit_fill_reviews_immediately_and_is_deduplicated(exits):
    store, broker, engine, manager, rows, calls, scope = exits
    rows.extend([deal(12, 'tp'), deal(11, 'sl')])  # same position, two fills, unordered history
    manager.maybe_review(); wait(manager)
    manager.maybe_review(); wait(manager)
    assert len(calls) == 2
    assert manager.exit_reviews.summary(scope)['completed'] == 2
    prompts = '\n'.join(json.dumps(c, ensure_ascii=False) for c in calls)
    assert '追踪止损' in prompts and '止盈' in prompts and '参数评估' in prompts
    manager.exit_scan_at = 0
    manager.maybe_review()
    assert len(calls) == 2  # history overlap never creates a duplicate review
    assert engine.running and engine.armed
    assert store.get('ea_proposal') is None  # review does not force a parameter mutation


def test_busy_manager_collects_each_event_for_later(exits):
    _, _, _, manager, rows, calls, scope = exits
    manager.busy = True
    rows.extend([deal(21), deal(22, 'tp')])
    manager.maybe_review()
    assert manager.exit_reviews.summary(scope)['pending'] == 2
    assert not calls
    manager.busy = False
    manager.maybe_review(); wait(manager)
    manager.maybe_review(); wait(manager)
    assert manager.exit_reviews.summary(scope)['completed'] == 2


def test_only_current_account_ea_symbol_and_protection_reason_qualify(exits):
    _, _, _, manager, rows, calls, scope = exits
    rows.extend([deal(31, magic=1234), deal(32, symbol='EURUSD'), deal(33, reason=None),
                 deal(34, time_msc=int((time.time()-3600)*1000)), deal(35, 'sl')])
    manager.maybe_review(); wait(manager)
    assert len(calls) == 1
    assert manager.exit_reviews.summary(scope)['recent'][0]['ticket'] == '35'


def test_old_history_is_not_replayed_but_delayed_out_of_order_rows_are_found(exits):
    store, broker, _, manager, rows, calls, scope = exits
    rows.append(deal(100))
    manager.maybe_review(); wait(manager)
    rows.append(deal(99, 'tp', time_msc=int((time.time()-30)*1000)))
    manager.exit_scan_at = 0
    manager.maybe_review(); wait(manager)
    assert len(calls) == 2
    cfg, _ = store.active()
    other = {**broker.status()['account'], 'login': 999}
    new_scope = manager.exit_reviews.scope(cfg, other)
    manager.exit_reviews.begin(new_scope)
    manager.exit_reviews.record(new_scope, cfg, store.active()[1], {'account': other, 'captured_at': time.time(), 'deals': rows})
    assert manager.exit_reviews.summary(new_scope)['pending'] == 0


def test_failed_review_remains_visible_retries_with_backoff_and_restart_does_not_launch(exits, tmp_path):
    store, broker, engine, manager, rows, calls, scope = exits
    rows.append(deal(41))
    manager.client.complete = lambda *a, **k: {'status': 'error', 'error': 'provider unavailable'}
    manager.maybe_review(); wait(manager)
    summary = manager.exit_reviews.summary(scope)
    assert summary['error'] == 1 and summary['completed'] == 0
    assert summary['recent'][0]['next_attempt_at'] > time.time()+50
    assert manager.exit_reviews.next(scope) is None
    engine.running = engine.armed = False
    restored = EAManager(engine, tmp_path)
    restored.maybe_review()
    assert not restored.busy and restored.review_thread is None
    assert restored.workspace()['review']['exit_reviews']['error'] == 1


def test_interrupted_claim_is_requeued_without_replaying_completed_events(exits):
    store, _, engine, manager, rows, _, scope = exits
    rows.append(deal(51))
    manager.busy = True; manager.maybe_review(); manager.busy = False
    event = manager.exit_reviews.next(scope)
    job = {'id': 'crashed-job', 'status': 'running', 'generation': engine.generation}
    manager.exit_reviews.claim(event, job)
    assert manager.exit_reviews.next(scope) is None
    recovered = ExitReviewQueue(store)
    assert recovered.next(scope)['ticket'] == '51'
    assert recovered.summary(scope)['pending'] == 1


def test_stopped_session_and_account_switch_cannot_consume_queued_event(exits):
    _, broker, engine, manager, rows, calls, scope = exits
    rows.append(deal(61))
    manager.busy = True; manager.maybe_review(); manager.busy = False
    event = manager.exit_reviews.next(scope)
    engine.stop()
    manager.maybe_review()
    with pytest.raises(Conflict): manager.start_job('review', exit_event=event)
    assert manager.exit_reviews.summary(scope)['pending'] == 1 and not calls
    engine.running = engine.armed = True
    broker.login = 456
    broker.account_pin = (456, 'Demo')
    with pytest.raises(Conflict): manager.start_job('review', exit_event=event)
    assert not calls


def test_account_change_during_review_blocks_next_tool_and_does_not_complete(exits):
    _, broker, _, manager, rows, _, scope = exits
    rows.append(deal(71))
    def complete(*args, **kwargs):
        broker.login = 456
        args[4]('get_strategy', {})
        raise AssertionError('account change should prevent tool use')
    manager.client.complete = complete
    manager.maybe_review(); wait(manager)
    assert manager.exit_reviews.summary(scope)['error'] == 1
    assert manager.exit_reviews.summary(scope)['completed'] == 0


def test_scan_failure_does_not_advance_checkpoint_or_drop_future_retry(exits):
    store, broker, _, manager, rows, _, scope = exits
    watch = copy.deepcopy(manager.exit_reviews.begin(scope))
    saved = broker.deal_history_snapshot
    broker.deal_history_snapshot = lambda *a: (_ for _ in ()).throw(BrokerError('history unavailable'))
    manager.maybe_review()
    assert manager.exit_reviews.begin(scope) == watch
    assert manager.workspace()['review']['exit_reviews']['scan_error']
    broker.deal_history_snapshot = saved
    rows.append(deal(81))
    manager.maybe_review(); wait(manager)
    assert manager.exit_reviews.summary(scope)['completed'] == 1


def test_disabled_review_keeps_events_queued_without_automatic_job(exits):
    store, _, _, manager, rows, calls, scope = exits
    select(store, 'ea', review_enabled=False)
    rows.append(deal(91))
    manager.maybe_review()
    assert not calls and manager.exit_reviews.summary(scope)['pending'] == 1
    select(store, 'ea', review_enabled=True)
    manager.maybe_review(); wait(manager)
    assert manager.exit_reviews.summary(scope)['completed'] == 1


def test_broker_history_uses_real_reason_and_applies_offset_once():
    module = SimpleNamespace(DEAL_REASON_SL=4, DEAL_REASON_TP=5, DEAL_ENTRY_OUT=1, DEAL_ENTRY_OUT_BY=3, DEAL_TYPE_BUY=0, DEAL_TYPE_SELL=1)
    broker = MT5Broker(module)
    broker.configure({'terminal_path': '', 'server_utc_offset_hours': 3})
    account = {'login': 123, 'server': 'Demo'}
    broker.status = lambda: {'connected': True, 'account': account.copy()}
    raw = [deal(1, 'sl'), deal(2, 'tp', entry=0), deal(3, 'tp', type=2), deal(4, 'sl', entry=2), deal(5, 'tp', entry=3)]
    broker.deals_since = lambda epoch: copy.deepcopy(raw)
    snapshot = broker.deal_history_snapshot(0, (123, 'Demo'))
    assert [d['exit_reason'] for d in snapshot['deals']] == ['sl', None, None, None, 'tp']
    first = snapshot['deals'][0]
    assert first['time'] == raw[0]['time']-10800
    assert first['time_msc'] == raw[0]['time_msc']-10800000
    assert first['time_raw'] == raw[0]['time'] and first['time_msc_raw'] == raw[0]['time_msc']
    with pytest.raises(BrokerError): broker.deal_history_snapshot(0, (456, 'Demo'))
    broker.deals_since = lambda epoch: (account.update(login=456) or copy.deepcopy(raw))
    with pytest.raises(BrokerError): broker.deal_history_snapshot(0, (123, 'Demo'))


def test_first_monitor_boundary_does_not_drop_same_millisecond_exit(exits):
    store, broker, _, manager, _, _, _ = exits
    cfg, versions = store.active()
    account = {**broker.status()['account'], 'login': 999}
    scope = manager.exit_reviews.scope(cfg, account)
    stamp = time.time()
    manager.exit_reviews.begin(scope, stamp+.0005)
    history = {'account': account, 'captured_at': stamp+1, 'deals': [deal(201, time_msc=int(stamp*1000))]}
    manager.exit_reviews.record(scope, cfg, versions, history)
    assert manager.exit_reviews.summary(scope)['pending'] == 1


def test_temporary_completion_storage_failure_retries_record_without_rerunning_ai(exits, monkeypatch):
    _, _, _, manager, rows, calls, scope = exits
    original = manager.exit_reviews.finish
    def fail_once(*args):
        monkeypatch.setattr(manager.exit_reviews, 'finish', original)
        raise OSError('temporary database error')
    monkeypatch.setattr(manager.exit_reviews, 'finish', fail_once)
    rows.append(deal(202))
    manager.maybe_review(); wait(manager)
    assert manager.pending_finalizations and not manager.busy
    assert manager.exit_reviews.summary(scope)['running'] == 1
    manager.maybe_review()
    assert not manager.pending_finalizations
    assert manager.exit_reviews.summary(scope)['completed'] == 1
    assert len(calls) == 1


def test_thread_start_failure_releases_busy_and_marks_event_retryable(exits, monkeypatch):
    _, _, _, manager, rows, _, scope = exits
    rows.append(deal(203))
    def fail_start(_): raise RuntimeError('thread unavailable')
    monkeypatch.setattr(threading.Thread, 'start', fail_start)
    with pytest.raises(RuntimeError): manager.maybe_review()
    assert not manager.busy and manager.active_job_id is None
    assert manager.exit_reviews.summary(scope)['error'] == 1
    assert manager.exit_reviews.summary(scope)['running'] == 0


def test_long_trade_history_does_not_overflow_chat_message_limit(exits):
    _, _, _, manager, rows, calls, scope = exits
    rows.extend(deal(300+i, comment='x'*2000) for i in range(50))
    manager.maybe_review(); wait(manager)
    assert len(calls) == 1 and manager.exit_reviews.summary(scope)['completed'] == 1
    message = json.loads(calls[0][-1]['content'])['current_question']
    assert len(message) < 8000 and '300' in message


def test_automatic_review_conversations_are_scoped_by_account(exits):
    store, broker, _, manager, rows, _, scope = exits
    rows.append(deal(401))
    manager.maybe_review(); wait(manager)
    first = store.get('ea_review_conversation')
    rows.clear()
    broker.login = 456; broker.account_pin = (456, 'Demo')
    cfg, _ = store.active()
    second_scope = manager.exit_reviews.scope(cfg, broker.status()['account'])
    manager.exit_reviews.begin(second_scope, time.time()-60)
    rows.append(deal(402, 'tp'))
    manager.maybe_review(); wait(manager)
    second = store.get('ea_review_conversation')
    assert first != second
    assert store.get('ea_review_conversation:'+scope) == first
    assert store.get('ea_review_conversation:'+second_scope) == second
    second_messages = store.conversation(second)['messages']
    assert all('401' not in m['content'] for m in second_messages)


def test_stop_during_model_call_keeps_event_for_a_later_user_started_session(exits):
    _, _, engine, manager, rows, _, scope = exits
    entered, release = threading.Event(), threading.Event()
    def complete(*args, **kwargs):
        entered.set(); assert release.wait(4)
        return {'status': 'ok', 'content': 'late result'}
    manager.client.complete = complete
    rows.append(deal(501))
    manager.maybe_review()
    try:
        assert entered.wait(2)
        engine.stop()
    finally:
        release.set(); wait(manager)
    manager.maybe_review()
    assert manager.exit_reviews.summary(scope)['pending'] == 1
    assert not engine.running and not engine.armed


def test_event_conversation_uses_its_validated_scope_without_second_account_lookup(exits):
    store, broker, _, manager, rows, _, scope = exits
    rows.append(deal(601))
    manager.busy = True; manager.maybe_review(); manager.busy = False
    event = manager.exit_reviews.next(scope)
    current = broker.status()
    lookups = []
    def status():
        lookups.append(True)
        return current if len(lookups) == 1 else {**current, 'account': {**current['account'], 'login': 456}}
    broker.status = status
    manager.review = lambda job_id: {'status': 'ok', 'content': 'fixture'}
    job = manager.start_job('review', exit_event=event)
    wait(manager)
    assert len(lookups) == 1
    assert store.get('ea_review_conversation:'+scope) == job['conversation_id']

import math
import pytest

from fusion.pid_envelope import EnvelopeConfig, EnvelopeExperiment, PIDCentre, simulate


def test_activation_does_not_fabricate_a_three_dollar_stop_ack():
    e = EnvelopeExperiment()
    row = e.step(0, e.price_for_profit(3))
    assert row['state'] == 'tracking'
    assert not row['lock_confirmed'] and not row['stop_pending']
    assert e.profit(row['broker_sl']) == pytest.approx(-6)
    row = e.step(.1, e.price_for_profit(3.5))
    assert row['stop_pending'] and not row['lock_confirmed']
    row = e.step(.6, e.price_for_profit(3.6))
    assert row['lock_confirmed']
    assert e.profit(row['broker_sl']) >= 3 - 1e-8


@pytest.mark.parametrize('side', [1, -1])
@pytest.mark.parametrize('boundary,sign', [('upper', 1), ('lower', -1)])
def test_crossing_uses_old_bounds_even_with_very_fast_pid(side, boundary, sign):
    e = EnvelopeExperiment(EnvelopeConfig(side=side, kp=100, max_speed=100))
    q = e.price_for_profit(5)
    e.step(0, q)
    row = e.step(.1, q + sign * 1.2)
    touches = [v for v in row['events'] if v['kind'] == 'local_touch']
    assert len(touches) == 1 and touches[0]['boundary'] == boundary
    assert e.pid.value == q
    assert row['state'] == 'exit_pending' and row['fill'] is None


def test_same_millisecond_quote_still_triggers_without_dividing_by_zero():
    e = EnvelopeExperiment()
    e.step(1, 2505)
    assert e.step(1, 2507)['state'] == 'exit_pending'
    assert math.isfinite(e.pid.value)
    with pytest.raises(ValueError): e.step(.9, 2504)


def test_pending_broker_stop_revalidates_at_ack():
    e = EnvelopeExperiment(EnvelopeConfig(upper_distance=3, lower_distance=3))
    e.step(0, 2504)
    assert e.pending_stop
    row = e.step(.5, 2503.3)
    assert not row['lock_confirmed']
    assert any(v['kind'] == 'stop_rejected' for v in row['events'])
    assert e.profit(e.broker_sl) == pytest.approx(-6)


def test_gap_can_fill_below_the_confirmed_protection_target():
    e = EnvelopeExperiment()
    e.step(0, 2505); e.step(.5, 2505)
    assert e.lock_confirmed
    e.step(.6, 2501); row = e.step(1, 2500)
    assert row['fill']['source'] == 'broker'
    assert row['fill']['profit'] < 3


def test_server_stop_works_offline_and_exit_is_not_repeated():
    e = EnvelopeExperiment()
    e.step(0, 2505); e.step(.5, 2505)
    e.step(.6, 2501, False)
    e.step(1, 2500, False)
    for i in range(2, 20): e.step(i, 2499, False)
    assert e.fill and e.fill['source'] == 'broker'
    assert sum(v['kind'] == 'fill' for v in e.events) == 1
    assert sum(v['kind'] == 'broker_touch' for v in e.events) == 1


def test_local_request_can_be_superseded_by_server_without_double_fill():
    e = EnvelopeExperiment()
    e.step(0, 2505); e.step(.5, 2505)
    e.step(.6, 2503.9)
    assert e.exit_request['source'] == 'local'
    e.step(.7, 2502)
    e.step(1.1, 2501)
    assert e.fill['source'] == 'broker'
    assert sum(v['kind'] == 'fill' for v in e.events) == 1


def test_clamped_pid_does_not_wind_up_and_never_overshoots():
    c = EnvelopeConfig(kp=10, ki=1, max_speed=.2)
    p = PIDCentre(0, c)
    for _ in range(1000): p.update(10, .1)
    assert p.value <= 10 and abs(p.integral) < .1
    before = p.value
    p.update(9, 10)
    assert 9 <= p.value <= before and abs(p.previous_error) <= 1.01


def test_reconnect_resets_pid_memory_using_local_update_time():
    e = EnvelopeExperiment()
    e.step(0, 2505)
    e.pid.integral = 5
    e.pid.derivative = 3
    for i in range(1, 101): e.step(i / 10, 2505, False)
    assert e.last_time == 10 and e.last_pid_time == 0
    e.step(10.1, 2505, True)
    assert e.pid.integral == 0 and e.pid.derivative == 0
    assert e.pid.value == 2505 and e.last_pid_time == 10.1


def test_synthetic_examples_are_causal_finite_and_not_performance_claims():
    for side in (-1, 1):
        normal = simulate(EnvelopeConfig(side=side), 'normal')
        assert normal['fill'] is None
        assert any(r['lock_confirmed'] for r in normal['rows'])
        for scenario in ('rise', 'fall', 'gap', 'offline'):
            result = simulate(EnvelopeConfig(side=side), scenario)
            assert all(math.isfinite(r['price']) for r in result['rows'])
            if result['fill']:
                assert sum(v['kind'] == 'fill' for v in result['events']) == 1


@pytest.mark.parametrize('patch', [{'kp': float('nan')}, {'units': 0}, {'side': 0}, {'lower_distance': -1}, {'ack_delay': -1}])
def test_invalid_parameters_rejected(patch):
    with pytest.raises(ValueError): EnvelopeConfig(**patch)

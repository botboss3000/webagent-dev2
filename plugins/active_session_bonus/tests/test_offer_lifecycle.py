from datetime import timedelta
from tempfile import TemporaryDirectory
from pathlib import Path
from plugins.active_session_bonus.config import BonusConfig, DEFAULTS
from plugins.active_session_bonus.qualification import (
    decay_floor, live_amount, remaining_amount, utcnow,
)
from plugins.active_session_bonus.store import BonusStore


def snapshot(task_id='task_1'):
    return {'task_id': task_id, 'task_revision': 14, 'work_revision': 5, 'done_step_count': 2, 'evidence_count': 4, 'task_status': 'in_progress'}


def policy(*, seconds=60, maximum=60):
    return {'flag': 'needs_input', 'max_bonus': maximum,
            'resolution_seconds': seconds, 'resolution_action': 'continue'}


def test_offer_decays_linearly_over_its_frozen_policy_timer_then_locks_on_activation():
    with TemporaryDirectory() as tmp:
        store = BonusStore(Path(tmp) / 'bonus.db')
        start = utcnow()
        offer = store.create_offer(user_id='u', session_id='s', closer_id='c',
                                   snapshot=snapshot(), policy=policy(), now=start)
        assert offer['initial_amount'] == 60
        # The visible value is linear over this specific offer's 60-second
        # policy window: 60 -> 35 halfway through -> 10 at the deadline.
        assert live_amount(offer, DEFAULTS, start + timedelta(seconds=5)) == 56
        assert live_amount(offer, DEFAULTS, start + timedelta(seconds=30)) == 35
        assert live_amount(offer, DEFAULTS, start + timedelta(seconds=60)) == 10
        locked = store.activate(offer['id'], 'u', now=start + timedelta(seconds=12))
        assert locked['status'] == 'activated'
        assert live_amount(offer, DEFAULTS, start + timedelta(seconds=12)) == 50
        assert locked['paused_amount'] == remaining_amount(offer, DEFAULTS, start + timedelta(seconds=12)) == 50
        assert store.offer(offer['id'], 'u')['remaining_amount'] == 50
        assert live_amount(store.offer(offer['id'], 'u'), DEFAULTS, start + timedelta(seconds=40)) == 50


def test_decay_floor_is_the_lowest_amount_the_wallet_can_pay():
    # The policy floor is 5 credits, below the wallet's one-cent quantum, so it
    # is raised to the smallest creditable amount instead of leaving an offer
    # that can be seen but never paid.
    assert decay_floor(DEFAULTS) == 10
    assert decay_floor(BonusConfig(minimum_offer=25)) == 30
    assert decay_floor(BonusConfig(minimum_offer=0)) == 0


def test_open_offer_reaches_floor_only_at_its_policy_deadline():
    with TemporaryDirectory() as tmp:
        store = BonusStore(Path(tmp) / 'bonus.db')
        start = utcnow()
        offer = store.create_offer(user_id='u', session_id='s', closer_id='c', snapshot=snapshot(), now=start)
        # The default 24-hour policy is still well above the floor after 23h.
        assert live_amount(offer, DEFAULTS, start + timedelta(hours=23)) == 13
        assert remaining_amount(offer, DEFAULTS, start + timedelta(hours=23)) == 20
        assert live_amount(offer, DEFAULTS, start + timedelta(seconds=DEFAULTS.offer_expiry_seconds)) == 10
        store.expire_due(now=start + timedelta(hours=23))
        assert store.offer(offer['id'], 'u')['status'] == 'offered'
        store.expire_due(now=start + timedelta(seconds=DEFAULTS.offer_expiry_seconds + 1))
        assert store.offer(offer['id'], 'u')['status'] == 'expired'


def test_a_zero_floor_offer_reaches_zero_only_at_its_policy_deadline():
    with TemporaryDirectory() as tmp:
        store = BonusStore(Path(tmp) / 'bonus.db', BonusConfig(minimum_offer=0))
        start = utcnow()
        offer = store.create_offer(user_id='u', session_id='s', closer_id='c',
                                   snapshot=snapshot(), policy=policy(seconds=60), now=start)
        assert live_amount(offer, store.config, start + timedelta(seconds=59)) == 1
        store.expire_due(now=start + timedelta(seconds=59))
        assert store.offer(offer['id'], 'u')['status'] == 'offered'
        store.expire_due(now=start + timedelta(seconds=61))
        assert store.offer(offer['id'], 'u')['status'] == 'expired'


def test_exactly_one_offer_per_closer_and_three_per_task():
    with TemporaryDirectory() as tmp:
        store = BonusStore(Path(tmp) / 'bonus.db'); now = utcnow()
        first = store.create_offer(user_id='u', session_id='s', closer_id='c1', snapshot=snapshot(), now=now)
        assert store.create_offer(user_id='u', session_id='s', closer_id='c1', snapshot=snapshot(), now=now)['id'] == first['id']
        assert [store.create_offer(user_id='u', session_id='s', closer_id=f'c{i}', snapshot=snapshot(), now=now)['initial_amount'] for i in (2, 3)] == [30, 10]
        assert store.create_offer(user_id='u', session_id='s', closer_id='c4', snapshot=snapshot(), now=now) is None

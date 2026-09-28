from datetime import timedelta
from pathlib import Path
from tempfile import TemporaryDirectory
from plugins.active_session_bonus.config import BonusConfig
from plugins.active_session_bonus.qualification import utcnow
from plugins.active_session_bonus.store import BonusStore

S = {'task_id':'t','task_revision':1,'work_revision':1,'done_step_count':0,'evidence_count':0}
def test_daily_cap_clamps_second_reservation():
    config = BonusConfig(daily_claim_cap=70, per_task_claim_cap=100)
    with TemporaryDirectory() as tmp:
        store=BonusStore(Path(tmp)/'b.db', config); now=utcnow()
        one=store.create_offer(user_id='u',session_id='s',closer_id='one',snapshot=S,now=now)
        store.activate(one['id'],'u',now=now); store.mark_candidate(one['id'],'reply1')
        assert store.reserve_claim(one['id'],now=now)['claimed_amount']==60; assert store.mark_claimed(one['id'],now=now)
        two=store.create_offer(user_id='u',session_id='s',closer_id='two',snapshot=S,now=now+timedelta(seconds=1))
        store.activate(two['id'],'u',now=now+timedelta(seconds=1)); store.mark_candidate(two['id'],'reply2')
        assert store.reserve_claim(two['id'],now=now+timedelta(seconds=1))['claimed_amount']==10

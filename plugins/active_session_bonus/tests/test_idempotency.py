from pathlib import Path
from tempfile import TemporaryDirectory
from plugins.active_session_bonus.qualification import utcnow
from plugins.active_session_bonus.store import BonusStore

S = {'task_id':'t','task_revision':1,'work_revision':1,'done_step_count':0,'evidence_count':0}
def test_claim_reservation_and_finalization_are_idempotent():
    with TemporaryDirectory() as tmp:
        store=BonusStore(Path(tmp)/'b.db'); now=utcnow()
        offer=store.create_offer(user_id='u',session_id='s',closer_id='c',snapshot=S,now=now)
        store.activate(offer['id'],'u',now=now); store.mark_candidate(offer['id'],'reply')
        a=store.reserve_claim(offer['id'],now=now); b=store.reserve_claim(offer['id'],now=now)
        assert a['claimed_amount']==b['claimed_amount']==60
        assert store.mark_claimed(offer['id'],now=now)
        assert store.mark_claimed(offer['id'],now=now)
        assert store.offer(offer['id'],'u')['status']=='claimed'

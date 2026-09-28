from plugins.active_session_bonus.qualification import qualifies

BASE = {'task_id':'task_1', 'baseline_work_revision':5, 'baseline_done_steps':2, 'baseline_evidence_count':4}

def current(**more):
    value = {'task_id':'task_1','work_revision':6,'done_step_count':2,'evidence_count':4,'task_status':'in_progress'}
    value.update(more); return value

def test_requires_work_revision_and_meaningful_progress():
    assert not qualifies(BASE, current(work_revision=5, done_step_count=3))
    assert not qualifies(BASE, current())
    assert qualifies(BASE, current(done_step_count=3))
    assert qualifies(BASE, current(evidence_count=5))
    assert qualifies(BASE, current(task_status='completed'))

def test_different_task_never_qualifies():
    assert not qualifies(BASE, current(task_id='task_2', done_step_count=3))

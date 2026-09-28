from app.agent.task_progress_config import (
    merge_task_progress_update,
    normalize_task_progress,
    resolve_task_progress,
)
from app import abilities


def test_task_progress_authoring_skill_is_always_on_with_the_ability():
    abilities.reload()
    feature = abilities.ability_feature_with_skill("task_progress")

    assert feature
    assert feature["skill_mode"] == "always_on"
    assert "one verifiable outcome" in feature["skill"]
    assert "evidence" in feature["skill_summary"].lower()




def test_legacy_active_task_subcap_is_accepted_but_not_retained():
    policy = normalize_task_progress({
        "enabled": True,
        "active_task_evidence_tokens": 12000,
        "completed_task_evidence_tokens": 24000,
    }, strict=True)

    assert policy["enabled"] is True
    assert policy["completed_task_evidence_tokens"] == 24000
    assert "active_task_evidence_tokens" not in policy


def test_merge_removes_legacy_subcap_and_resolves_without_manager_loop():
    agent = {"metadata": {"task_progress": {
        "enabled": True,
        "modes": ["auto"],
        "active_task_evidence_tokens": 12000,
    }}}

    merged = merge_task_progress_update(agent, {"completed_task_evidence_tokens": 30000})

    assert "active_task_evidence_tokens" not in merged
    assert merged["completed_task_evidence_tokens"] == 30000
    assert resolve_task_progress({"metadata": {"task_progress": merged}}, "auto")["enabled"] is True
    assert resolve_task_progress({"metadata": {"task_progress": merged}}, "ask")["enabled"] is False

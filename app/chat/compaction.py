"""Manual Context Control compaction under the controlled-chat run lease."""
from app.agent import turn_prewarm
from app.agent.compaction import build_compaction_notice, maybe_compact
from app.agent.context_control import get_context_settings
from .repository import digest


async def run_manual_compaction(execution, db, session):
    repo = execution.repo
    run_id, generation = execution.run_id, execution.generation
    session_id = session["id"]
    settings = await get_context_settings(db, session["agent_id"], session_id, repo.owner_id)
    outcome = "completed"
    kind = "notice"
    if not settings.get("enabled"):
        payload = {
            "text": "Context Control is not active for this agent. Enable it to compact this session.",
            "compaction": {"status": "no_op"},
        }
    else:
        info = await maybe_compact(db, repo.owner_id, session_id, settings, force=True)
        if info is None:
            payload = {
                "text": "Nothing to compact right now. No eligible older history was folded.",
                "compaction": {"status": "no_op"},
            }
        elif info.get("status") in {"failed", "in_progress"}:
            kind, outcome = "failure", "failed"
            payload = {
                "text": str(info.get("reason") or "Could not compact this session."),
                "system_outcome": "failed",
                "compaction": {"status": info["status"]},
            }
        else:
            turn_prewarm.invalidate(session_id)
            notice = await build_compaction_notice(
                repo.owner_id, session_id, info, trigger="manual",
            )
            payload = {"text": notice["detail"], "compaction": notice}
    run = await repo.run(run_id)
    return await repo.decide(
        run_id, generation, kind, payload,
        accepted=True, reasons=[], policy_digest=digest(run["policy"]),
        terminal=True, outcome=outcome,
    )

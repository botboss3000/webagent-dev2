import asyncio
import json
import sqlite3
import time
from pathlib import Path
from unittest.mock import AsyncMock, patch

from app.agent.manager_config import legacy_manager_view, resolve_manager_loop
from app.agent.subagent_contracts import (
    CONTRACT_SPECS,
    ContractSupervisor,
    _write_state,
    contracts_available,
    _request,
    _worker_name,
    recover_orphaned_contract_checks,
    stop_turn_workers,
    validate_request,
    validate_result,
)


class _Db:
    def __init__(self, path: Path):
        self.path = str(path)

    def _get_conn(self):
        conn = sqlite3.connect(self.path)
        conn.row_factory = sqlite3.Row
        return conn


def _agent():
    return {"metadata": {"manager": {
        "enabled": True,
        "contracts": {
            "enabled": True,
            "engine": "subagent",
            "failure_policy": "hybrid",
            "scout": {"enabled": True},
            "edit_review": {
                "enabled": True, "policy": "blocking", "max_checks": 2,
            },
            "close_review": {
                "enabled": True, "policy": "blocking", "max_rounds": 1,
            },
        },
    }}}


def _edit_assessment(kind="edit_gate", status="confirmed"):
    dimensions = (
        ["request_alignment", "plan_readiness", "first_edit_fit", "risk"]
        if kind == "plan_gate"
        else ["request_alignment", "plan_alignment", "operation_scope", "risk"]
    )
    return {"assessment": [
        {
            "dimension": dimension,
            "evidence": f"{dimension} evaluated from the supplied window",
            "status": status,
        }
        for dimension in dimensions
    ]}


def _supervisor(db):
    return ContractSupervisor(
        db=db, user_id="user-1", session_id="session-1", agent_id="agent-1",
        agent_rec=_agent(), turn_id="turn-1", generation="turn-1",
        execution_mode="auto",
    )


def test_contract_result_schema_rejects_mismatched_and_malformed_results():
    spec = CONTRACT_SPECS["edit_review"]
    request = _request(
        spec=spec, session_id="s", turn_id="t", generation="t",
        mission_digest="m", plan_revision=2, invocation=1,
        evidence={
            "review_kind": "edit_gate", "request": "change it",
            "working_context": "ctx", "edit": "edit_source a.py",
        },
    )
    valid = {
        "request_id": request["request_id"], "contract_id": spec.id,
        "version": spec.version, "decision": "revise", "reason": "unsafe edit",
        "findings": [{"message": "wrong file"}],
        "corrective_actions": ["target the requested module"],
        "payload": _edit_assessment(status="conflict"),
    }
    assert validate_request(request, spec)
    assert not validate_request({**request, "plan_revision": 3}, spec)
    assert validate_result(json.dumps(valid), request, spec)["decision"] == "revise"
    assert validate_result(json.dumps({**valid, "request_id": "stale"}), request, spec) is None
    assert validate_result(json.dumps({**valid, "version": 99}), request, spec) is None
    assert validate_result(json.dumps({**valid, "findings": [], "corrective_actions": []}), request, spec) is None
    assert validate_result("not json", request, spec) is None


def test_edit_review_pass_requires_all_gate_dimensions_confirmed():
    spec = CONTRACT_SPECS["edit_review"]
    request = _request(
        spec=spec, session_id="s", turn_id="t", generation="t",
        mission_digest="m", plan_revision=1, invocation=1,
        evidence={
            "review_kind": "plan_gate", "request": "change it",
            "working_context": "ctx", "edit": "edit_source a.py",
        },
    )
    result = {
        "request_id": request["request_id"], "contract_id": spec.id,
        "version": spec.version, "decision": "pass", "reason": "aligned",
        "findings": [], "corrective_actions": [],
        "payload": _edit_assessment("plan_gate"),
    }
    assert validate_result(json.dumps(result), request, spec)["decision"] == "pass"
    result["payload"]["assessment"][0]["status"] = "partial"
    assert validate_result(json.dumps(result), request, spec) is None
    result["payload"] = {"assessment": result["payload"]["assessment"][:-1]}
    assert validate_result(json.dumps(result), request, spec) is None


def test_run_scout_pass_requires_an_actionable_structured_intake():
    spec = CONTRACT_SPECS["run_scout"]
    request = _request(
        spec=spec, session_id="s", turn_id="t", generation="t",
        mission_digest="m", plan_revision=0, invocation=1,
        evidence={"combined_request": "change it", "prior_capsules": []},
    )
    payload = {
        "objective": "Change the requested behavior", "task_type": "feature",
        "constraints": [], "assumptions": [], "questions": [],
        "success_criteria": ["Observable behavior changes"],
        "plan": ["Inspect", "Implement", "Verify"],
        "expected_outputs": ["Updated source and tests"], "risks": [],
        "title_candidate": "Change behavior", "relationship": "new",
        "linked_prior_task_id": "", "linked_capsule_id": "",
        "relationship_confidence": 1.0, "relationship_reason": "No prior task",
    }
    result = {
        "request_id": request["request_id"], "contract_id": spec.id,
        "version": spec.version, "decision": "pass", "reason": "actionable",
        "findings": [], "corrective_actions": [], "payload": payload,
    }
    assert validate_result(json.dumps(result), request, spec)["decision"] == "pass"
    result["payload"] = {**payload, "success_criteria": []}
    assert validate_result(json.dumps(result), request, spec) is None


def test_close_contract_pass_requires_confirmed_structured_traceability():
    spec = CONTRACT_SPECS["close_alignment"]
    request = _request(
        spec=spec, session_id="s", turn_id="t", generation="t",
        mission_digest="m", plan_revision=0, invocation=1,
        evidence={
            "request": "ship it", "checklist": ["tests pass"],
            "completion_evidence": {},
        },
    )
    base = {
        "request_id": request["request_id"], "contract_id": spec.id,
        "version": spec.version, "decision": "pass", "reason": "confirmed",
        "findings": [], "corrective_actions": [],
    }
    assert validate_result(json.dumps(base), request, spec) is None
    partial = {
        **base,
        "payload": {"traceability": [{
            "criterion": "tests pass", "observable_outcome": "green suite",
            "evidence": "pytest event", "status": "partial",
        }]},
    }
    assert validate_result(json.dumps(partial), request, spec) is None
    confirmed = {
        **base,
        "payload": {"traceability": [{
            "criterion": "tests pass", "observable_outcome": "green suite",
            "evidence": "pytest event", "status": "confirmed",
        }]},
    }
    assert validate_result(json.dumps(confirmed), request, spec)["decision"] == "pass"


def test_close_evidence_contract_cannot_pass_an_unverified_assessment():
    spec = CONTRACT_SPECS["close_evidence"]
    request = _request(
        spec=spec, session_id="s", turn_id="t", generation="t",
        mission_digest="m", plan_revision=0, invocation=1,
        evidence={
            "request": "ship it", "checklist": ["tests pass"],
            "completion_evidence": {},
        },
    )
    result = {
        "request_id": request["request_id"], "contract_id": spec.id,
        "version": spec.version, "decision": "pass", "reason": "looks good",
        "findings": [], "corrective_actions": [],
        "payload": {"evidence_assessment": [{
            "criterion": "tests pass", "status": "unverified",
            "evidence_kind": "test", "strength": "behavioral",
            "freshness": "current run", "result": "not captured",
            "remaining_gap": "run the focused suite",
        }]},
    }
    assert validate_result(json.dumps(result), request, spec) is None
    result["payload"]["evidence_assessment"][0].update(
        status="confirmed", result="passed", remaining_gap="")
    assert validate_result(json.dumps(result), request, spec)["decision"] == "pass"


def test_close_contract_output_schemas_require_the_audit_matrices():
    alignment = CONTRACT_SPECS["close_alignment"].output_schema
    evidence = CONTRACT_SPECS["close_evidence"].output_schema
    assert "payload" in alignment["required"]
    assert alignment["properties"]["payload"]["required"] == ["traceability"]
    assert evidence["properties"]["payload"]["required"] == [
        "evidence_assessment",
    ]


def test_contract_configuration_is_opt_in_and_managed_edit_policy_owns_gates():
    disabled = resolve_manager_loop({"metadata": {"manager": {
        "enabled": False, "contracts": {"enabled": True},
    }}}, "auto")
    assert disabled["contracts"]["enabled"] is False

    runtime = legacy_manager_view(resolve_manager_loop(_agent(), "auto"))
    assert runtime["plan_gate"] == "blocking"
    assert runtime["edit_gate"] == "blocking"
    assert runtime["max_checks_by_kind"]["edit_gate"] == 2


def test_run_scout_worker_is_named_scout():
    assert _worker_name(CONTRACT_SPECS["run_scout"]) == "Scout"
    assert _worker_name(CONTRACT_SPECS["edit_review"]) == "Contract · edit_review"


def test_read_only_contract_worker_uses_orchestration_with_hard_denies():
    from plugins.abilities.Core.agent_orchestration import agent_orchestration as transport

    create = AsyncMock(return_value={"id": "spawn-1"})
    with patch.object(transport, "_create_spawn", new=create):
        asyncio.run(transport.create_contract_worker(
            user_id="u", orchestrator_session_id="s", orchestrator_agent_id="a",
            name="reviewer", system_prompt="review", output_contract="{}",
            permission_profile="source_read_only",
        ))
    kwargs = create.await_args.kwargs
    assert kwargs["abilities"] == ["codebase_admin"]
    assert kwargs["kind"] == "contract"
    assert kwargs["enforce_worker_limits"] is False
    denied = set(kwargs["forced_deny_tools"])
    assert {"db_query", "write_source", "delete_source", "run_command", "restart_server",
            "spawn_agent", "commit_and_push"}.issubset(denied)


def test_edit_reviewer_is_reused_and_budget_failures_are_persisted(tmp_path):
    db = _Db(tmp_path / "contracts.db")
    supervisor = _supervisor(db)
    creates = []
    sends = []

    async def create_worker(**_kwargs):
        assert _kwargs["permission_profile"] == "tool_free"
        creates.append(1)
        return {
            "id": "spawn-1", "spawn_session_id": "worker-session",
            "granted_abilities": ["codebase_admin"],
        }

    async def send_request(**kwargs):
        sends.append(kwargs)
        envelope = json.loads(kwargs["request"].split("\n", 1)[1])
        return {"status": "done", "reply": json.dumps({
            "request_id": envelope["request_id"], "contract_id": "edit_review",
            "version": envelope["version"], "decision": "pass",
            "reason": "matches the plan", "findings": [], "corrective_actions": [],
            "payload": _edit_assessment(envelope["evidence"]["review_kind"]),
        })}

    with (
        patch("plugins.abilities.Core.agent_orchestration.agent_orchestration.create_worker", new=create_worker),
        patch("plugins.abilities.Core.agent_orchestration.agent_orchestration.send_worker_request", new=send_request),
        patch("app.agent.run_fence.side_effects_allowed", new=AsyncMock(return_value=True)),
    ):
        first = asyncio.run(supervisor.review_edit(
            review_kind="plan_gate", request_text="change it", working_context="ctx",
            edit="edit_source a.py", plan=["change a"], plan_revision=1, invocation=1,
        ))
        second = asyncio.run(supervisor.review_edit(
            review_kind="edit_gate", request_text="change it", working_context="ctx2",
            edit="edit_source b.py", plan=["change a"], plan_revision=1, invocation=2,
        ))
        over_budget = asyncio.run(supervisor.review_edit(
            review_kind="edit_gate", request_text="change it", working_context="ctx3",
            edit="edit_source c.py", plan=["change a"], plan_revision=1, invocation=3,
        ))

    assert first["decision"] == second["decision"] == "pass"
    assert over_budget["decision"] == "inconclusive"
    assert len(creates) == 1
    assert len(sends) == 2
    for sent in sends:
        evidence = json.loads(sent["request"].split("\n", 1)[1])["evidence"]
        assert "ctx" not in json.dumps(evidence)
        assert "edit_source a.py" not in json.dumps(evidence)
        assert "tool_name" not in evidence
        assert "tool_arguments_hash" not in evidence
        assert evidence["edit"]["scope"] == "unverified"
    conn = db._get_conn()
    try:
        statuses = [row[0] for row in conn.execute(
            "SELECT status FROM run_contract_checks ORDER BY started_at",
        ).fetchall()]
    finally:
        conn.close()
    assert statuses == ["complete", "complete", "skipped"]


def test_close_reviewers_run_concurrently_and_do_not_share_verdicts(tmp_path):
    supervisor = _supervisor(_Db(tmp_path / "contracts.db"))
    entered = 0
    both_entered = asyncio.Event()
    seen = []

    async def execute(spec, **_kwargs):
        nonlocal entered
        entered += 1
        seen.append(spec.id)
        if entered == 2:
            both_entered.set()
        await asyncio.wait_for(both_entered.wait(), timeout=1)
        return ({
            "decision": "pass", "reason": f"{spec.id} passed",
            "findings": [], "corrective_actions": [],
        }, {"id": spec.id, "spawn_session_id": spec.id + "-session"})

    supervisor._execute = execute
    supervisor._stop_worker = AsyncMock()
    result = asyncio.run(supervisor.review_close(
        request_text="ship it", checklist=["tests pass"],
        completion_evidence={"verification_events": ["pytest passed"]}, round_no=1,
    ))

    assert result["decision"] == "pass"
    assert set(seen) == {"close_alignment", "close_evidence"}
    assert supervisor._stop_worker.await_count == 2


def test_contract_availability_follows_the_manager_master_switch():
    """Contracts are gated by the Manager Loop master switch.

    The legacy ``manager_chk`` loop node is now a derived display mirror, so
    turning it off must no longer suppress contract work — only the master
    switch does that.
    """
    class _AbilityDb:
        async def get_agent_connections(self, _agent_id):
            return [{
                "section": "ability", "connection_type": "agent_orchestration",
                "enabled": True,
            }]

    enabled = _agent()
    node_off = _agent()
    node_off["loop_logic"] = [{"node": "manager_chk", "enabled": False}]
    master_off = _agent()
    master_off["metadata"]["manager"] = {
        **master_off["metadata"]["manager"], "enabled": False,
    }
    with patch("app.abilities.app_function_enabled", return_value=True):
        assert asyncio.run(contracts_available(_AbilityDb(), "a", enabled, "auto"))
        assert asyncio.run(contracts_available(_AbilityDb(), "a", node_off, "auto"))
        assert not asyncio.run(contracts_available(
            _AbilityDb(), "a", master_off, "auto",
        ))


def test_stale_worker_result_is_discarded_and_persisted(tmp_path):
    db = _Db(tmp_path / "contracts.db")
    supervisor = _supervisor(db)
    creates = []

    async def create_worker(**_kwargs):
        creates.append(1)
        return {
            "id": "spawn-1", "spawn_session_id": "worker-session",
            "granted_abilities": ["codebase_admin"],
        }

    async def send_request(**kwargs):
        envelope = json.loads(kwargs["request"].split("\n", 1)[1])
        return {"status": "done", "reply": json.dumps({
            "request_id": envelope["request_id"], "contract_id": "edit_review",
            "version": envelope["version"], "decision": "block",
            "reason": "old generation", "findings": ["stale"],
            "corrective_actions": ["ignore"], "payload": _edit_assessment(status="conflict"),
        })}

    with (
        patch("plugins.abilities.Core.agent_orchestration.agent_orchestration.create_worker", new=create_worker),
        patch("plugins.abilities.Core.agent_orchestration.agent_orchestration.send_worker_request", new=send_request),
        patch("app.agent.run_fence.side_effects_allowed", new=AsyncMock(return_value=False)),
    ):
        result = asyncio.run(supervisor.review_edit(
            review_kind="edit_gate", request_text="change it", working_context="ctx",
            edit="edit_source a.py", plan=[], plan_revision=1, invocation=1,
        ))
    assert result["decision"] == "inconclusive"
    assert "stale" in result["reason"]
    assert creates == []
    conn = db._get_conn()
    try:
        assert conn.execute(
            "SELECT status FROM run_contract_checks",
        ).fetchone()[0] == "stale"
    finally:
        conn.close()


def test_orphaned_persistent_reviewer_is_recreated_for_same_request(tmp_path):
    db = _Db(tmp_path / "contracts.db")
    supervisor = _supervisor(db)
    _write_state(
        db, "session-1", "turn-1", reviewer_spawn_id="dead-spawn",
        reviewer_session_id="dead-session", plan_revision=1,
    )
    sends = []

    async def create_worker(**_kwargs):
        return {
            "id": "fresh-spawn", "spawn_session_id": "fresh-session",
            "granted_abilities": ["codebase_admin"],
        }

    async def send_request(**kwargs):
        sends.append(kwargs["spawn_id"])
        if kwargs["spawn_id"] == "dead-spawn":
            raise RuntimeError("orphaned after restart")
        envelope = json.loads(kwargs["request"].split("\n", 1)[1])
        return {"status": "done", "reply": json.dumps({
            "request_id": envelope["request_id"], "contract_id": "edit_review",
            "version": envelope["version"], "decision": "pass",
            "reason": "fresh review", "findings": [], "corrective_actions": [],
            "payload": _edit_assessment(envelope["evidence"]["review_kind"]),
        })}

    supervisor._stop_worker = AsyncMock()
    with (
        patch("plugins.abilities.Core.agent_orchestration.agent_orchestration.create_worker", new=create_worker),
        patch("plugins.abilities.Core.agent_orchestration.agent_orchestration.send_worker_request", new=send_request),
        patch("app.agent.run_fence.side_effects_allowed", new=AsyncMock(return_value=True)),
    ):
        result = asyncio.run(supervisor.review_edit(
            review_kind="edit_gate", request_text="change it", working_context="ctx",
            edit="edit_source a.py", plan=[], plan_revision=1, invocation=1,
        ))
    assert result["decision"] == "pass"
    assert sends == ["fresh-spawn"]  # Legacy reviewer context is never reused.
    assert supervisor._stop_worker.await_count == 1


def test_worker_ownership_is_durable_before_first_request(tmp_path):
    db = _Db(tmp_path / "contracts.db")
    supervisor = _supervisor(db)

    async def create_worker(**_kwargs):
        return {
            "id": "owned-spawn", "spawn_session_id": "owned-session",
            "granted_abilities": ["codebase_admin"],
        }

    async def send_request(**kwargs):
        conn = db._get_conn()
        try:
            check = conn.execute(
                "SELECT status,worker_spawn_id,worker_session_id "
                "FROM run_contract_checks"
            ).fetchone()
            state = conn.execute(
                "SELECT reviewer_spawn_id,reviewer_session_id "
                "FROM run_contract_state"
            ).fetchone()
        finally:
            conn.close()
        assert tuple(check) == ("running", "owned-spawn", "owned-session")
        assert tuple(state) == ("owned-spawn", "owned-session")
        envelope = json.loads(kwargs["request"].split("\n", 1)[1])
        return {"status": "done", "reply": json.dumps({
            "request_id": envelope["request_id"], "contract_id": "edit_review",
            "version": envelope["version"], "decision": "pass",
            "reason": "exact edit approved", "findings": [], "corrective_actions": [],
            "payload": _edit_assessment(envelope["evidence"]["review_kind"]),
        })}

    with (
        patch("plugins.abilities.Core.agent_orchestration.agent_orchestration.create_worker", new=create_worker),
        patch("plugins.abilities.Core.agent_orchestration.agent_orchestration.send_worker_request", new=send_request),
        patch("app.agent.run_fence.side_effects_allowed", new=AsyncMock(return_value=True)),
    ):
        result = asyncio.run(supervisor.review_edit(
            review_kind="edit_gate", request_text="change it", working_context="ctx",
            edit=json.dumps({"tool_call_id": "tc-1", "tool": "write_source",
                             "args": {"path": "a.py", "content": "x"}}),
            plan=[], plan_revision=1, invocation=1,
        ))
    assert result["decision"] == "pass"


def test_absolute_deadline_includes_worker_creation(tmp_path):
    db = _Db(tmp_path / "contracts.db")
    supervisor = _supervisor(db)

    async def hung_create(**_kwargs):
        await asyncio.sleep(10)

    started = time.monotonic()
    with patch(
        "plugins.abilities.Core.agent_orchestration.agent_orchestration.create_worker",
        new=hung_create,
    ):
        result, _worker = asyncio.run(supervisor._execute(
            CONTRACT_SPECS["edit_review"], timeout_override=1,
            evidence={
                "review_kind": "edit_gate", "request": "change it",
                "working_context": "ctx", "edit": "{}", "plan_revision": 1,
            },
        ))
    assert time.monotonic() - started < 2.5
    assert result["decision"] == "inconclusive"
    conn = db._get_conn()
    try:
        assert conn.execute("SELECT status FROM run_contract_checks").fetchone()[0] == "timeout"
    finally:
        conn.close()


def test_cancelled_review_is_terminal_and_worker_is_stopped(tmp_path):
    async def scenario():
        db = _Db(tmp_path / "contracts.db")
        supervisor = _supervisor(db)
        entered = asyncio.Event()

        async def create_worker(**_kwargs):
            return {
                "id": "cancel-spawn", "spawn_session_id": "cancel-session",
                "granted_abilities": ["codebase_admin"],
            }

        async def hung_send(**_kwargs):
            entered.set()
            await asyncio.Event().wait()

        supervisor._stop_worker = AsyncMock()
        with (
            patch("plugins.abilities.Core.agent_orchestration.agent_orchestration.create_worker", new=create_worker),
            patch("plugins.abilities.Core.agent_orchestration.agent_orchestration.send_worker_request", new=hung_send),
        ):
            task = asyncio.create_task(supervisor.review_edit(
                review_kind="edit_gate", request_text="change it", working_context="ctx",
                edit="{}", plan=[], plan_revision=1, invocation=1,
            ))
            await entered.wait()
            task.cancel()
            try:
                await task
            except asyncio.CancelledError:
                pass
        conn = db._get_conn()
        try:
            assert conn.execute("SELECT status FROM run_contract_checks").fetchone()[0] == "cancelled"
            state = conn.execute(
                "SELECT reviewer_spawn_id,reviewer_session_id,status FROM run_contract_state"
            ).fetchone()
            assert tuple(state) == (None, None, "stopped")
        finally:
            conn.close()
        supervisor._stop_worker.assert_awaited()

    asyncio.run(scenario())


def test_stop_turn_workers_uses_running_check_ownership(tmp_path):
    db = _Db(tmp_path / "contracts.db")
    supervisor = _supervisor(db)
    spec = CONTRACT_SPECS["close_alignment"]
    request = _request(
        spec=spec, session_id="session-1", turn_id="turn-1", generation="turn-1",
        mission_digest="m", plan_revision=0, invocation=1,
        evidence={"request": "ship", "checklist": [], "completion_evidence": {}},
    )
    from app.agent.subagent_contracts import _record_start, _record_worker
    _record_start(
        db, request_id=request["request_id"], session_id="session-1", turn_id="turn-1",
        spec=spec, request_hash=request["request_hash"], evidence={},
    )
    _record_worker(
        db, request_id=request["request_id"], spawn_id="audit-spawn",
        spawn_session_id="audit-session",
    )
    stop = AsyncMock()
    with patch(
        "plugins.abilities.Core.agent_orchestration.agent_orchestration.stop_worker",
        new=stop,
    ):
        asyncio.run(stop_turn_workers(db, "session-1", "turn-1"))
    stop.assert_awaited_once_with(
        spawn_id="audit-spawn", spawn_session_id="audit-session",
    )
    conn = db._get_conn()
    try:
        row = conn.execute(
            "SELECT status,decision FROM run_contract_checks"
        ).fetchone()
        assert tuple(row) == ("cancelled", "inconclusive")
    finally:
        conn.close()


def test_startup_recovery_cancels_checks_for_stopped_parent(tmp_path):
    class StoppedDb(_Db):
        async def run_state_get(self, _session_id):
            return {"turn_id": "turn-1", "stop_cause": "user_stop"}

    db = StoppedDb(tmp_path / "contracts.db")
    spec = CONTRACT_SPECS["edit_review"]
    request = _request(
        spec=spec, session_id="session-1", turn_id="turn-1", generation="turn-1",
        mission_digest="m", plan_revision=1, invocation=1,
        evidence={"review_kind": "edit_gate", "request": "change",
                  "working_context": "ctx", "edit": "{}"},
    )
    from app.agent.subagent_contracts import _record_start, _record_worker
    _record_start(
        db, request_id=request["request_id"], session_id="session-1", turn_id="turn-1",
        spec=spec, request_hash=request["request_hash"], evidence={},
    )
    _record_worker(
        db, request_id=request["request_id"], spawn_id="old-spawn",
        spawn_session_id="old-session",
    )
    assert asyncio.run(recover_orphaned_contract_checks(db)) == 1
    conn = db._get_conn()
    try:
        assert conn.execute("SELECT status FROM run_contract_checks").fetchone()[0] == "cancelled"
    finally:
        conn.close()


def test_startup_recovery_leaves_active_unreserved_request_for_redispatch(tmp_path):
    class ActiveDb(_Db):
        async def run_state_get(self, _session_id):
            return {"turn_id": "turn-1", "stop_cause": "server_restart"}

    db = ActiveDb(tmp_path / "contracts.db")
    spec = CONTRACT_SPECS["edit_review"]
    request = _request(
        spec=spec, session_id="session-1", turn_id="turn-1", generation="turn-1",
        mission_digest="m", plan_revision=1, invocation=1,
        evidence={"review_kind": "edit_gate", "request": "change",
                  "working_context": "ctx", "edit": "{}"},
    )
    from app.agent.subagent_contracts import _record_start
    _record_start(
        db, request_id=request["request_id"], session_id="session-1", turn_id="turn-1",
        spec=spec, request_hash=request["request_hash"], evidence={},
    )
    assert asyncio.run(recover_orphaned_contract_checks(db)) == 0
    conn = db._get_conn()
    try:
        assert conn.execute("SELECT status FROM run_contract_checks").fetchone()[0] == "running"
    finally:
        conn.close()


def test_worker_capacity_counts_only_active_user_task_spawns(monkeypatch):
    from plugins.abilities.Core.agent_orchestration import agent_orchestration as transport

    async def config(_agent_id):
        return {"max_concurrent_spawns": 10, "max_spawn_depth": 2}

    monkeypatch.setattr(transport, "_load_orch_config", config)
    monkeypatch.setattr(transport, "_spawns_list", lambda _session: [
        *({"status": "done", "kind": ""} for _ in range(100)),
        *({"status": "running", "kind": ""} for _ in range(9)),
        {"status": "running", "kind": "contract"},
    ])
    assert asyncio.run(transport._enforce_spawn_limits("agent", "session", 0)) is None

    monkeypatch.setattr(transport, "_spawns_list", lambda _session: [
        *({"status": "done", "kind": ""} for _ in range(100)),
        *({"status": "queued", "kind": ""} for _ in range(10)),
        {"status": "running", "kind": "contract"},
    ])
    blocked = asyncio.run(transport._enforce_spawn_limits("agent", "session", 0))
    assert "Concurrent worker spawn limit reached" in blocked

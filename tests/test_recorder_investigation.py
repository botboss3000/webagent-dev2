"""Tool-free investigation and explicit approval dispatch boundary tests."""
import asyncio
import json
from pathlib import Path
import pytest
from plugins.recorder import investigation as mod
from plugins.recorder.store import CaptureStore


@pytest.fixture
def store(tmp_path):
    value = CaptureStore(tmp_path / "recorder.sqlite3")
    capture = value.begin("alice", "tab")
    value.save_report(capture["id"], {"findings": [{"rule_id": "transcript.order", "evidence": ["e1"]}]})
    return value, capture["id"]


def plan():
    return {"approach": "Investigate owner ordering.", "files": ["ui/chat/js/chat-stream.js"],
            "steps": ["Compare owner ordering and fix the observed race."],
            "risks": ["Incomplete evidence."], "verification": ["Exercise warm-cache streaming."],
            "confidence": "low", "evidence": ["e1"]}


def test_plan_validation_rejects_unreviewed_paths_and_invented_evidence():
    sources = [{"path": "ui/chat/js/chat-stream.js"}]
    bad = {**plan(), "files": ["data/config/secrets.json"]}
    with pytest.raises(ValueError):
        mod._plan(json.dumps(bad), sources, {"e1"})
    with pytest.raises(ValueError):
        mod._plan(json.dumps({**plan(), "evidence": ["invented"]}), sources, {"e1"})


def test_source_reader_ignores_arbitrary_paths_and_symlink_escape(tmp_path):
    external = tmp_path / "secret.txt"
    external.write_text("DO NOT READ")
    target = tmp_path / "ui/chat/js/chat-stream.js"
    target.parent.mkdir(parents=True)
    target.symlink_to(external)
    assert mod.source_context(["transcript.order", "../../secret.txt"], tmp_path) == []


def test_successful_analysis_is_tool_free_bounded_and_deduplicated(store, monkeypatch):
    value, cid = store
    calls = []
    async def complete(owner, session, messages):
        calls.append(messages)
        assert len(messages) == 2
        assert "NO tools" in messages[0]["content"]
        assert len(messages[1]["content"]) <= 72000
        return json.dumps(plan())
    monkeypatch.setattr(mod, "_complete", complete)
    result = asyncio.run(mod.investigate(value, cid, "alice"))
    assert result["state"] == "plan_ready"
    assert result["plan"]["revision"] == 1
    assert result["mode"] == "tool_free_read_only"
    assert result["approval"] is None
    assert asyncio.run(mod.investigate(value, cid, "alice"))["job_id"] == result["job_id"]
    assert len(calls) == 1


def test_provider_failure_is_blocked_and_does_not_store_exception_secrets(store, monkeypatch):
    value, cid = store
    async def fail(*args):
        raise RuntimeError("secret-provider-key")
    monkeypatch.setattr(mod, "_complete", fail)
    result = asyncio.run(mod.investigate(value, cid, "alice"))
    assert result["state"] == "blocked"
    assert "secret-provider-key" not in json.dumps(result)
    assert result["plan"] is None


def test_investigation_checks_owner_before_model_call(store, monkeypatch):
    value, cid = store
    with pytest.raises(KeyError):
        asyncio.run(mod.investigate(value, cid, "mallory"))


def ready(value, cid):
    value.investigation(cid, state="plan_ready", plan={**plan(), "revision": 1, "source_fingerprints": {}})
    value.approve(cid, "alice", 1, "approve")
    return value.claim_execution(cid, "alice", 1)


def test_execution_rejects_unapproved_or_forged_revision(store):
    value, cid = store
    with pytest.raises(PermissionError):
        asyncio.run(mod.execute(value, cid, "alice", {"plan": {"revision": 1}}))
    approved = ready(value, cid)
    with pytest.raises(PermissionError):
        asyncio.run(mod.execute(value, cid, "alice", {**approved, "plan": {"revision": 2}}))


def test_execution_needs_explicit_agent_and_authenticated_request(store, monkeypatch):
    value, cid = store
    approved = ready(value, cid)
    monkeypatch.setattr(mod, "configured_execution_agent_id", lambda: "")
    result = asyncio.run(mod.execute(value, cid, "alice", approved, request=object()))
    assert result["state"] == "execution_blocked"


def test_execution_uses_native_auto_chat_once_and_binds_plan(store, monkeypatch):
    value, cid = store
    approved = ready(value, cid)
    calls = []
    monkeypatch.setattr(mod, "configured_execution_agent_id", lambda: "dedicated-dev")
    async def dispatch(request, owner, agent, session, message, client_id):
        calls.append((owner, agent, session, message, client_id))
        return {"status": "running"}
    monkeypatch.setattr(mod, "_dispatch", dispatch)
    result = asyncio.run(mod.execute(value, cid, "alice", approved, request=object()))
    assert result["state"] == "execution_started"
    assert calls[0][0:2] == ("alice", "dedicated-dev")
    assert "/home/ar/webagent-dev only" in calls[0][3]
    assert result["execution_session_id"] == calls[0][2]
    with pytest.raises(PermissionError):
        asyncio.run(mod.execute(value, cid, "alice", approved, request=object()))
    assert len(calls) == 1


def test_source_change_blocks_dispatch(store, monkeypatch):
    value, cid = store
    approved = ready(value, cid)
    changed = {**approved["plan"], "source_fingerprints": {"ui/chat/js/chat-stream.js": "stale"}}
    value.investigation(cid, plan=changed)
    monkeypatch.setattr(mod, "configured_execution_agent_id", lambda: "dedicated-dev")
    result = asyncio.run(mod.execute(value, cid, "alice", approved, request=object()))
    assert result["state"] == "execution_blocked"
    assert "source changed" in result["error"]


def test_native_error_response_is_not_reported_as_started(store, monkeypatch):
    value, cid = store
    approved = ready(value, cid)
    monkeypatch.setattr(mod, "configured_execution_agent_id", lambda: "dedicated-dev")
    async def dispatch(*args):
        return {"status": "error", "error": "Bridge unavailable"}
    monkeypatch.setattr(mod, "_dispatch", dispatch)
    result = asyncio.run(mod.execute(value, cid, "alice", approved, request=object()))
    assert result["state"] == "execution_blocked"


def test_real_chat_request_schema_accepts_dispatch_payload():
    from app.models.schemas import ChatRequest
    payload = ChatRequest(user_id="alice", session_id="new-session",
                          message="Approved scoped plan", agent_id="dedicated-dev",
                          execution_mode="auto", client_msg_id="recorder-capture-1")
    assert payload.execution_mode == "auto"
    assert payload.target_device is None
    assert payload.agent_id == "dedicated-dev"


def test_actual_event_neighborhood_included_and_raw_html_removed(store, monkeypatch):
    value, cid = store
    value.append(cid, [{"id": "before", "kind": "trace", "detail": {"decision": "cache-read"}},
                       {"id": "e1", "kind": "checkpoint", "html": "<private-input>",
                        "detail": {"decision": "stale-replace", "api_key": "do-not-forward"}},
                       {"id": "after", "kind": "trace", "detail": {"decision": "render-applied"}}])
    async def complete(owner, session, messages):
        bundle = json.loads(messages[1]["content"])
        events = bundle["captured_events"]
        assert {x["id"] for x in events} == {"before", "e1", "after"}
        assert events[0]["detail"]["decision"] == "stale-replace"
        assert "private-input" not in json.dumps(events)
        assert "do-not-forward" not in json.dumps(events)
        return json.dumps(plan())
    monkeypatch.setattr(mod, "_complete", complete)
    assert asyncio.run(mod.investigate(value, cid, "alice"))["state"] == "plan_ready"


def image_marker(value, cid):
    import base64
    png = base64.b64decode("iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mP8/x8AAwMCAO+aH1sAAAAASUVORK5CYII=")
    marker = value.marker(cid, {"note": "Ordering issue"}, png, "image/png")
    return marker, base64.b64encode(png).decode()


def test_linked_image_bytes_go_only_to_model_and_never_persisted_in_plan(store, monkeypatch):
    value, cid = store
    marker, encoded = image_marker(value, cid)
    async def complete(owner, session, messages):
        blocks = messages[1]["content"]
        image = next(x for x in blocks if x["type"] == "image_url")
        assert image["image_url"]["url"] == "data:image/png;base64," + encoded
        return json.dumps(plan())
    monkeypatch.setattr(mod, "_complete", complete)
    result = asyncio.run(mod.investigate(value, cid, "alice"))
    assert result["state"] == "plan_ready"
    assert result["screenshot_review"] == "llm_inferred"
    assert result["screenshot_marker_ids"] == [marker["id"]]
    assert encoded not in json.dumps(value.detail(cid, "alice"))


def test_image_unsupported_uses_one_text_retry_with_explicit_limitation(store, monkeypatch):
    value, cid = store
    marker, encoded = image_marker(value, cid)
    calls = []
    async def complete(owner, session, messages):
        calls.append(messages)
        if len(calls) == 1:
            assert isinstance(messages[1]["content"], list)
            raise RuntimeError("vision unsupported " + encoded)
        assert isinstance(messages[1]["content"], str)
        assert "Do not claim visual inspection" in messages[0]["content"]
        return json.dumps(plan())
    monkeypatch.setattr(mod, "_complete", complete)
    result = asyncio.run(mod.investigate(value, cid, "alice"))
    assert len(calls) == 2
    assert result["state"] == "plan_ready"
    assert result["screenshot_review"] == "unavailable"
    assert any("text-only fallback" in x for x in result["limitations"])
    assert encoded not in json.dumps(result)


def test_model_echo_of_image_bytes_is_rejected_before_persistence(store, monkeypatch):
    value, cid = store
    marker, encoded = image_marker(value, cid)
    async def complete(*args):
        return json.dumps({**plan(), "approach": encoded})
    monkeypatch.setattr(mod, "_complete", complete)
    result = asyncio.run(mod.investigate(value, cid, "alice"))
    assert result["state"] == "blocked"
    assert result["plan"] is None
    assert encoded not in json.dumps(result)


def test_context_budget_is_bounded_and_cache_source_map_exists(store):
    value, cid = store
    value.append(cid, [{"id": str(i), "kind": "trace", "detail": {"content": "x" * 2000}}
                       for i in range(200)])
    events, limitations = mod.capture_context(value, cid, {str(i) for i in range(200)}, [])
    assert len(events) <= 150
    assert len(json.dumps(events, ensure_ascii=False, separators=(",", ":"))) <= 16000
    sources = mod.source_context(["cache.revision"])
    assert "ui/chat/js/storage/storage-adapter.js" in {x["path"] for x in sources}


def test_marker_arriving_during_analysis_prevents_stale_plan_publication(store, monkeypatch):
    value, cid = store
    async def complete(*args):
        value.marker(cid, {"note": "A new issue after the model input was built"})
        return json.dumps(plan())
    monkeypatch.setattr(mod, "_complete", complete)
    result = asyncio.run(mod.investigate(value, cid, "alice"))
    assert result["state"] == "evidence_changed"
    assert result["plan"] is None
    assert result["approval"] is None


def test_marker_event_sequence_selects_matching_document_neighborhood(store):
    value, cid = store
    value.append(cid, [{"id": "a"+str(i), "kind": "trace", "client_id": "document-a", "seq": i}
                       for i in range(70)] +
                      [{"id": "b"+str(i), "kind": "trace", "client_id": "document-b", "seq": i}
                       for i in range(70)])
    events, limitations = mod.capture_context(value, cid, set(), [
        {"id": "marker", "client_id": "document-a", "event_seq": 15}])
    ids = {row["id"] for row in events}
    assert "a15" in ids
    assert "a10" in ids
    assert "a20" in ids
    assert "b15" not in ids
    assert events[0]["id"] == "a15"


def test_cleared_job_id_prevents_previous_model_restoring_a_plan(store, monkeypatch):
    value, cid = store
    async def complete(*args):
        value.investigation(cid, state="evidence_changed", job_id=None,
                            fingerprint=None, plan=None, approval=None)
        return json.dumps(plan())
    monkeypatch.setattr(mod, "_complete", complete)
    result = asyncio.run(mod.investigate(value, cid, "alice"))
    assert result["job_id"] is None
    assert result["plan"] is None

"""Recorder migration: preserve legacy evidence/lease behavior; capture policy is standalone."""
import asyncio
import collections
import json
from datetime import datetime, timedelta, timezone
from pathlib import Path

import jwt
import pytest
from fastapi import HTTPException

from plugins.recorder import legacy as recorder_module
from plugins.recorder.legacy import (
    DEFAULT_CLIENT_CONFIG,
    EnabledBody,
    RecordingBatch,
    RenderRecorder,
)


ROOT = Path(__file__).parents[1]


def _recorder() -> RenderRecorder:
    recorder = object.__new__(RenderRecorder)
    recorder.max_html_bytes = 400_000
    recorder._pending = collections.deque(maxlen=20_000)
    recorder._accepted = 0
    recorder._dropped = 0
    recorder._task = None
    return recorder


def _lease(monkeypatch, *, client_id="document-a", recording_id="capture-a"):
    monkeypatch.setattr(recorder_module, "get_secret", lambda: "test-secret")
    return recorder_module._issue_capture_lease(
        {"user_id": "admin-user"},
        client_id=client_id,
        recording_id=recording_id,
    )


def test_default_capture_is_unlimited_and_chat_scoped():
    assert DEFAULT_CLIENT_CONFIG["capture_duration_ms"] == 0
    assert DEFAULT_CLIENT_CONFIG["capture_max_bytes"] == 2_000_000
    assert DEFAULT_CLIENT_CONFIG["capture_max_operations"] == 5_000
    assert DEFAULT_CLIENT_CONFIG["capture_whole_page"] is False
    assert DEFAULT_CLIENT_CONFIG["capture_decorative_animations"] is False




def test_mutation_payload_preserves_capture_coordinates():
    payload = json.dumps({
        "version": 1,
        "frame": 3,
        "operations": [{"op": "text", "old_value": "loading", "value": "ready"}],
    })
    record = _recorder()._normalize_incoming(
        {
            "kind": "mutation",
            "ts": "2026-08-25T12:00:00Z",
            "recording_id": "capture-123",
            "elapsed_ms": 417,
            "label": "mutation-frame",
            "value_num": 1,
            "detail": {"frame": 3, "operations": 1},
            "html": payload,
        },
        user_id="admin",
        recv_ts="2026-08-25T12:00:01Z",
    )
    assert record is not None
    assert record["kind"] == "mutation"
    assert record["html"] == payload
    assert record["html_bytes"] == len(payload)
    assert json.loads(record["detail"]) == {
        "frame": 3,
        "operations": 1,
        "recording_id": "capture-123",
        "elapsed_ms": 417,
    }


def test_capture_coordinates_wrap_legacy_scalar_detail():
    record = _recorder()._normalize_incoming(
        {
            "kind": "console",
            "recording_id": "capture-456",
            "elapsed_ms": "9",
            "detail": "legacy detail",
        },
        user_id=None,
        recv_ts="2026-08-25T12:00:01Z",
    )
    assert record is not None
    assert json.loads(record["detail"]) == {
        "data": "legacy detail",
        "recording_id": "capture-456",
        "elapsed_ms": 9,
    }




















def test_lease_is_bound_to_document_recording_and_user(monkeypatch):
    issued = _lease(monkeypatch)
    payload = recorder_module._verify_capture_lease(issued["lease_token"])
    assert payload["scope"] == recorder_module.CAPTURE_LEASE_SCOPE
    assert payload["client_id"] == "document-a"
    assert payload["recording_id"] == "capture-a"
    assert payload["user_id"] == "admin-user"
    recorder_module._validate_lease_batch(
        [{"client_id": "document-a", "recording_id": "capture-a"}], payload
    )
    with pytest.raises(HTTPException, match="client mismatch"):
        recorder_module._validate_lease_batch(
            [{"client_id": "document-b", "recording_id": "capture-a"}], payload
        )
    with pytest.raises(HTTPException, match="recording id mismatch"):
        recorder_module._validate_lease_batch(
            [{"client_id": "document-a", "recording_id": "capture-b"}], payload
        )


def test_capture_lease_cannot_validate_as_a_login_token(monkeypatch):
    issued = _lease(monkeypatch)
    with pytest.raises(jwt.PyJWTError):
        jwt.decode(issued["lease_token"], "test-secret", algorithms=["HS256"])


def test_missing_expired_and_wrong_scope_leases_are_rejected(monkeypatch):
    monkeypatch.setattr(recorder_module, "get_secret", lambda: "test-secret")
    with pytest.raises(HTTPException, match="lease required"):
        recorder_module._verify_capture_lease("")
    now = datetime.now(timezone.utc)
    base = {
        "aud": recorder_module.CAPTURE_LEASE_AUDIENCE,
        "user_id": "admin-user",
        "client_id": "document-a",
        "recording_id": "capture-a",
        "iat": now - timedelta(minutes=2),
    }
    expired = jwt.encode(
        {**base, "scope": recorder_module.CAPTURE_LEASE_SCOPE, "exp": now - timedelta(seconds=1)},
        recorder_module._lease_signing_key(),
        algorithm="HS256",
    )
    with pytest.raises(HTTPException, match="lease expired"):
        recorder_module._verify_capture_lease(expired)
    wrong_scope = jwt.encode(
        {**base, "scope": "other", "exp": now + timedelta(minutes=1)},
        recorder_module._lease_signing_key(),
        algorithm="HS256",
    )
    with pytest.raises(HTTPException, match="lease scope"):
        recorder_module._verify_capture_lease(wrong_scope)


def test_control_issues_and_renews_same_recording_without_global_state(monkeypatch):
    class FakeRecorder:
        @staticmethod
        def client_config():
            return {"capture_duration_ms": 0}

    monkeypatch.setattr(recorder_module, "get_secret", lambda: "test-secret")
    monkeypatch.setattr(recorder_module, "_control_available", lambda: True)
    monkeypatch.setattr(recorder_module, "get_render_recorder", lambda: FakeRecorder())
    first = asyncio.run(recorder_module.set_recordings_enabled(
        EnabledBody(enabled=True, client_id="document-a"),
        _auth={"user_id": "admin-user"},
    ))
    renewed = asyncio.run(recorder_module.set_recordings_enabled(
        EnabledBody(enabled=True, client_id="document-a", recording_id=first["recording_id"]),
        _auth={"user_id": "admin-user"},
    ))
    status = asyncio.run(recorder_module.get_recordings_enabled(_auth={"user_id": "admin-user"}))
    assert first["enabled"] is True
    assert renewed["recording_id"] == first["recording_id"]
    assert status["enabled"] is False
    assert status["scope"] == "tab"


def test_ingest_uses_signed_identity_and_rejects_other_tab(monkeypatch):
    captured = {}

    class FakeRecorder:
        def ingest(self, records, *, user_id):
            captured["records"] = records
            captured["user_id"] = user_id
            return {"accepted": len(records), "dropped": 0}

    issued = _lease(monkeypatch)
    monkeypatch.setattr(recorder_module, "_control_available", lambda: True)
    monkeypatch.setattr(recorder_module, "get_render_recorder", lambda: FakeRecorder())
    row = {
        "kind": "meta",
        "client_id": "document-a",
        "recording_id": "capture-a",
        "user_id": "spoofed-user",
    }
    result = asyncio.run(recorder_module.ingest_recordings(
        RecordingBatch(records=[row], lease_token=issued["lease_token"])
    ))
    assert result["accepted"] == 1
    assert captured["user_id"] == "admin-user"
    with pytest.raises(HTTPException, match="client mismatch"):
        asyncio.run(recorder_module.ingest_recordings(RecordingBatch(
            records=[{**row, "client_id": "document-b"}],
            lease_token=issued["lease_token"],
        )))


def test_unavailable_function_blocks_lease_and_ingest(monkeypatch):
    class FakeRecorder:
        @staticmethod
        def client_config():
            return {}

    monkeypatch.setattr(recorder_module, "_control_available", lambda: False)
    monkeypatch.setattr(recorder_module, "get_render_recorder", lambda: FakeRecorder())
    with pytest.raises(HTTPException, match="unavailable"):
        asyncio.run(recorder_module.set_recordings_enabled(
            EnabledBody(enabled=True, client_id="document-a"),
            _auth={"user_id": "admin-user"},
        ))
    with pytest.raises(HTTPException, match="unavailable"):
        asyncio.run(recorder_module.ingest_recordings(RecordingBatch(records=[])))



def test_standalone_config_enables_every_required_channel_without_legacy_changes(monkeypatch):
    from plugins.recorder import config
    legacy_preferences = {"capture_duration_ms": 900, "capture_max_bytes": 500,
                          "capture_max_operations": 20, "capture_whole_page": False,
                          "capture_decorative_animations": False,
                          "capture_snapshots": False, "capture_lag": False,
                          "capture_js_errors": False, "capture_console": False,
                          "capture_network": False}
    class Preferences:
        @staticmethod
        def client_config():
            return dict(legacy_preferences)
    monkeypatch.setattr(recorder_module, "get_render_recorder", lambda: Preferences())
    configured = config.client_config()
    for channel in ("capture_snapshots", "capture_lag", "capture_js_errors",
                    "capture_console", "capture_network"):
        assert configured[channel] is True
        assert legacy_preferences[channel] is False
    assert configured["capture_duration_ms"] == 0
    assert configured["capture_max_bytes"] == 0
    assert configured["capture_max_operations"] == 0
    assert configured["capture_whole_page"] is False
    assert configured["capture_decorative_animations"] is False


def test_standalone_availability_no_longer_depends_on_ability_toggle(monkeypatch):
    from plugins.recorder import config
    monkeypatch.setattr(config, "settings", lambda: {"enabled": False})
    assert recorder_module._control_available() is False
    monkeypatch.setattr(config, "settings", lambda: {"enabled": True})
    assert recorder_module._control_available() is True

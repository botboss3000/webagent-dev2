import asyncio
from pathlib import Path
from unittest.mock import patch

from app.db.local import LocalBackend
from app.db import user_store


class FakeDb:
    def __init__(self, rows=None):
        self.rows = dict(rows or {})
        self.deleted = []

    async def auth_element_get(self, user_id, service, label="default"):
        return self.rows.get((user_id, service, label))

    async def auth_element_set(self, user_id, service, config, secret_ref="", label="default"):
        row = {
            "user_id": user_id, "service": service, "label": label,
            "config": config, "secret_ref": secret_ref,
        }
        self.rows[(user_id, service, label)] = row
        return row

    async def auth_element_delete(self, user_id, service, label="default"):
        self.deleted.append((user_id, service, label))
        return self.rows.pop((user_id, service, label), None) is not None

    async def auth_element_list(self, user_id, service):
        return [row for (owner, row_service, _label), row in self.rows.items()
                if owner == user_id and row_service == service]


def test_regular_user_model_secrets_live_beside_user_authority(tmp_path, monkeypatch):
    monkeypatch.setattr(LocalBackend, "_init_db", lambda self: None)
    user_dir = tmp_path / "user-one"
    backend = LocalBackend(str(user_dir / "user-one.db"), seed=False, plane="user")

    assert backend._agent_vault_path == str(user_dir / "user-one.user-secrets.db")


def test_user_purge_removes_owned_model_secret_sidecar(tmp_path, monkeypatch):
    monkeypatch.setattr(user_store, "USER_DATA_DIR", str(tmp_path))
    user_dir = tmp_path / "user-one"
    user_dir.mkdir()
    secret_path = user_dir / "user-one.user-secrets.db"
    secret_path.write_bytes(b"test-sidecar")
    (user_dir / "user-one.user-secrets.db-wal").write_bytes(b"test-wal")

    result = asyncio.run(user_store.purge_user_store_files("user-one"))

    assert result["sidecar_files"] == 2
    assert not secret_path.exists()
    assert not user_dir.exists()


def test_legacy_agent_model_becomes_uuid_owned_roster_row():
    from app.admin import settings

    migrated, changed = settings._legacy_agent_config_to_roster({
        "use_default": False,
        "provider": "openai",
        "model": "legacy-model",
        "api_key": "legacy-key",
    })

    assert changed is True
    assert "model" not in migrated
    assert "api_key" not in migrated
    assert len(migrated["multi_providers"]) == 1
    row = migrated["multi_providers"][0]
    assert row["model"] == "legacy-model"
    assert row["api_key"] == "legacy-key"
    assert row["credential_scope"] == "agent"
    assert len(row["entry_id"]) == 36


def test_user_byo_detection_reads_owned_credential(monkeypatch):
    from app.admin import settings

    async def owned(_user_id):
        return {"config": {"multi_providers": []}, "secret_ref": "owned-secret"}

    monkeypatch.setattr(settings, "_load_llm_element", owned)

    assert asyncio.run(settings.user_has_own_llm_config("user-one")) is True


def test_owned_model_credential_backend_uses_explicit_scope(monkeypatch):
    from app import model_credentials

    app_db, agent_db, user_db = object(), object(), object()
    monkeypatch.setattr("app.db.is_remote_db", lambda: False)
    monkeypatch.setattr("app.db.get_app_db", lambda: app_db)
    monkeypatch.setattr("app.db.get_agent_db", lambda agent_id: (agent_db, agent_id))
    monkeypatch.setattr("app.db.get_user_db", lambda user_id: (user_db, user_id))

    assert model_credentials.backend_for("platform") is app_db
    assert model_credentials.backend_for("agent", agent_id="agent-one") == (agent_db, "agent-one")
    assert model_credentials.backend_for("user", user_id="user-one") == (user_db, "user-one")


def test_legacy_central_credential_is_copied_then_removed(monkeypatch):
    from app import model_credentials

    key = ("user-one", "llm", "default")
    legacy = FakeDb({key: {
        "user_id": "user-one", "service": "llm", "label": "default",
        "config": {"multi_providers": []}, "secret_ref": "legacy-secret",
    }})
    owned = FakeDb()
    monkeypatch.setattr(model_credentials, "backend_for", lambda *args, **kwargs: owned)
    monkeypatch.setattr(model_credentials, "legacy_backend", lambda: legacy)

    row = asyncio.run(model_credentials.get_owned(
        "user", owner="user-one", service="llm", user_id="user-one",
    ))

    assert row["secret_ref"] == "legacy-secret"
    assert owned.rows[key]["secret_ref"] == "legacy-secret"
    assert key not in legacy.rows
    assert legacy.deleted == [key]


def test_agent_llm_config_moves_keys_to_owned_secret_bundle(monkeypatch):
    from app.admin import settings
    from app import model_credentials

    saved = {}

    async def set_owned(scope, *, owner, service, config, secret_ref, label="default", **kwargs):
        saved.update({
            "scope": scope, "owner": owner, "service": service,
            "config": config, "secret_ref": secret_ref, "label": label,
        })
        return {}

    monkeypatch.setattr(model_credentials, "set_owned", set_owned)
    config = {
        "use_default": False,
        "multi_providers": [{
            "entry_id": "row-one", "provider": "openai", "model": "gpt",
            "api_key": "agent-secret",
        }],
    }

    stripped = asyncio.run(settings._persist_agent_llm_config("agent-one", config))

    assert stripped["multi_providers"][0]["api_key"] == ""
    assert saved["scope"] == "agent"
    assert saved["owner"] == "_agent:agent-one"
    assert saved["service"] == "llm"
    assert "agent-secret" in saved["secret_ref"]
    assert "agent-secret" not in str(saved["config"])


def test_named_user_credential_is_redacted_and_isolated(monkeypatch):
    from fastapi import HTTPException
    from app.api import entitlements
    from app import model_credentials

    vault = FakeDb()
    monkeypatch.setattr(model_credentials, "backend_for", lambda *args, **kwargs: vault)
    monkeypatch.setattr(model_credentials, "legacy_backend", lambda: vault)
    caller = {"id": "user-one"}
    async def valid_credential(*_args):
        return None
    monkeypatch.setattr(entitlements, "_validate_api_model_credential", valid_credential)
    monkeypatch.setattr(entitlements, "request_user_id", lambda _request: caller["id"])
    body = entitlements.SavedModelCredentialRequest(
        label="Primary OpenAI", provider="openai",
        base_url="https://api.openai.com/v1", api_key="sk-user-one-secret",
    )

    saved = asyncio.run(entitlements.save_my_model_credential(body, object()))
    assert saved["credential"]["credential_suffix"] == "cret"
    assert "sk-user-one-secret" not in str(saved)

    listed = asyncio.run(entitlements.list_my_model_credentials(object()))
    assert listed["credentials"] == [{
        "label": "Primary OpenAI", "kind": "api_key", "provider": "openai",
        "base_url": "https://api.openai.com/v1", "auth_mode": "api_key",
        "credential_configured": True, "credential_suffix": "cret",
        "saved_at": listed["credentials"][0]["saved_at"],
    }]
    assert "secret_ref" not in str(listed)
    assert "sk-user-one-secret" not in str(listed)

    caller["id"] = "user-two"
    assert asyncio.run(entitlements.list_my_model_credentials(object())) == {"credentials": []}
    try:
        asyncio.run(entitlements.load_my_model_credential("Primary OpenAI", object()))
        raise AssertionError("other tenant unexpectedly loaded a credential")
    except HTTPException as error:
        assert error.status_code == 404

    caller["id"] = "user-one"
    loaded = asyncio.run(entitlements.load_my_model_credential("Primary OpenAI", object()))
    assert loaded["api_key"] == "sk-user-one-secret"

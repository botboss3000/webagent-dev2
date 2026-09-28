from copy import deepcopy
import asyncio
from unittest.mock import AsyncMock

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from app.admin import settings
from app.auth import identity
import app.db


PLATFORM_CONFIG = {
    "provider": "openai",
    "base_url": "https://api.openai.com/v1",
    "api_key": "platform-root-secret",
    "model": "gpt-test",
    "providers": {
        "openai": {
            "base_url": "https://api.openai.com/v1",
            "api_key": "provider-map-secret",
            "model": "gpt-test",
        }
    },
    "multi_providers": [
        {
            "provider": "openai",
            "base_url": "https://api.openai.com/v1",
            "api_key": "roster-secret",
            "model": "gpt-test",
        }
    ],
}


class FakeDB:
    def __init__(self):
        self.admins = {"admin-user"}

    async def is_user_admin(self, user_id):
        return user_id in self.admins


@pytest.fixture()
def client(monkeypatch):
    fake_db = FakeDB()
    monkeypatch.setattr(app.db, "get_db", lambda: fake_db)
    monkeypatch.setattr(
        identity,
        "decode_token",
        lambda token: {"user_id": token} if token in {"admin-user", "regular-user"} else None,
    )
    monkeypatch.setattr(
        settings,
        "_load_own_llm_config",
        AsyncMock(return_value=deepcopy(PLATFORM_CONFIG)),
    )
    fastapi_app = FastAPI()
    fastapi_app.include_router(settings.router)
    return TestClient(fastapi_app)


@pytest.mark.parametrize(
    "path",
    [
        "/admin/settings/provider",
        "/admin/settings/provider-bundle",
        "/admin/settings/multi-providers",
        "/admin/settings/models",
    ],
)
def test_provider_reads_require_authentication(client, path):
    response = client.get(path)
    assert response.status_code == 401


@pytest.mark.parametrize(
    "path",
    [
        "/admin/settings/provider",
        "/admin/settings/provider-bundle",
        "/admin/settings/multi-providers",
        "/admin/settings/models",
    ],
)
def test_non_admin_cannot_read_platform_or_inherited_config(client, path):
    response = client.get(path, headers={"Authorization": "Bearer regular-user"})
    assert response.status_code == 403
    assert "platform-root-secret" not in response.text
    assert "roster-secret" not in response.text


def test_admin_provider_read_reports_credentials_without_returning_them(client):
    response = client.get(
        "/admin/settings/provider",
        headers={"Authorization": "Bearer admin-user"},
    )
    assert response.status_code == 200
    body = response.json()
    assert body["api_key"] == ""
    assert body["credential_configured"] is False
    assert body["model"] == ""
    assert body["providers"]["openai"]["api_key"] == ""
    assert body["providers"]["openai"]["credential_configured"] is True
    assert "platform-root-secret" not in response.text
    assert "provider-map-secret" not in response.text


def test_admin_bundle_masks_default_provider_and_roster_keys(client):
    response = client.get(
        "/admin/settings/provider-bundle",
        headers={"Authorization": "Bearer admin-user"},
    )
    assert response.status_code == 200
    body = response.json()
    assert body["provider"]["api_key"] == ""
    assert body["provider"]["credential_configured"] is False
    assert body["provider"]["model"] == ""
    assert body["roster"][0]["api_key"] == ""
    assert body["roster"][0]["credential_configured"] is True
    assert "secret" not in response.text


def test_admin_bundle_loads_authenticated_owner_config(client, monkeypatch):
    loaded_owners = []

    async def load_owned(owner):
        loaded_owners.append(owner)
        return deepcopy(PLATFORM_CONFIG)

    monkeypatch.setattr(settings, "_load_own_llm_config", load_owned)
    response = client.get(
        "/admin/settings/provider-bundle",
        headers={"Authorization": "Bearer admin-user"},
    )

    assert response.status_code == 200
    assert loaded_owners == ["admin-user"]


def test_non_admin_cannot_mutate_platform_provider(client, monkeypatch):
    persist = AsyncMock()
    monkeypatch.setattr(settings, "_persist_llm_config", persist)
    response = client.post(
        "/admin/settings/provider",
        headers={"Authorization": "Bearer regular-user"},
        json={"provider": "openai", "api_key": "attacker-key", "model": "gpt-test"},
    )
    assert response.status_code == 403
    persist.assert_not_awaited()


def test_internal_runtime_resolver_never_inherits_unrelated_admin_secret(monkeypatch):
    class RuntimeDB:
        async def auth_element_get(self, user_id, service, label):
            if user_id == "regular-user":
                return None
            assert user_id == settings.PLATFORM_LLM_OWNER
            return {
                "config": {
                    "provider": "openai",
                    "base_url": "https://api.openai.com/v1",
                    "api_key": "",
                    "model": "gpt-test",
                },
                "secret_ref": "runtime-platform-secret",
            }

    async def no_owned_config(_user_id):
        return None

    monkeypatch.setattr(app.db, "get_db", lambda: RuntimeDB())
    monkeypatch.setattr(settings, "_load_own_llm_config", no_owned_config)
    resolved = asyncio.run(settings._resolve_user_config("regular-user"))
    assert resolved["api_key"] == ""


def test_admin_model_view_does_not_require_agent_membership_store(monkeypatch):
    class ModelViewDB:
        async def assert_session_owned(self, _user_id, _session_id):
            return None

        async def get_session_agent_id(self, _session_id):
            return "agent-one"

        async def get_agent_by_id(self, _agent_id):
            return {"id": "agent-one", "user_mode": "private"}

        async def is_user_admin(self, _user_id):
            return True

        async def is_agent_member(self, _agent_id, _user_id):
            raise AssertionError("admin path must not query membership storage")

    monkeypatch.setattr(settings, "request_user_id", lambda _request: "admin-user")
    monkeypatch.setattr(app.db, "get_db", lambda: ModelViewDB())

    user_id, agent = asyncio.run(settings._require_model_view_access(
        object(), "", "session-one",
    ))

    assert user_id == "admin-user"
    assert agent["id"] == "agent-one"

import asyncio
import json
from unittest.mock import AsyncMock

import httpx
import pytest
from fastapi import HTTPException

from app import model_credentials, openai_subscription
from app.api import entitlements, openai_subscription_auth as auth
from test_model_credential_ownership import FakeDb


@pytest.fixture
def vaults(monkeypatch):
    databases = {"platform": FakeDb(), "agent": FakeDb(), "user": FakeDb()}
    async def authorized(_request, scope, agent_id):
        if scope == "agent":
            return {"scope": "agent", "agent_id": agent_id}
        if scope == "user":
            return {"scope": "user", "user_id": "user-one"}
        return {"scope": "platform"}
    async def entry(request, scope, agent_id, entry_id, **_kwargs):
        target = await authorized(request, scope, agent_id)
        target["entry_id"] = entry_id
        return target
    monkeypatch.setattr(auth, "_authorized_scope", authorized)
    monkeypatch.setattr(auth, "_authorize_entry", entry)
    monkeypatch.setattr(entitlements, "_registered_caller", AsyncMock(return_value="user-one"))
    monkeypatch.setattr(model_credentials, "backend_for", lambda scope, **kwargs: databases[scope])
    monkeypatch.setattr(model_credentials, "legacy_backend", lambda: databases["platform"])
    monkeypatch.setattr(openai_subscription, "transport_auth", AsyncMock(return_value={"access_token": "test-access"}))
    monkeypatch.setattr(entitlements, "_validate_api_model_credential", AsyncMock())
    return databases


@pytest.mark.parametrize("scope,owner", [
    ("platform", "_platform"), ("agent", "_agent:agent-one"), ("user", "user-one"),
])
def test_api_credentials_use_selected_vault(vaults, scope, owner):
    body = entitlements.SavedModelCredentialRequest(
        label="Primary key", provider="openai", base_url="https://api.example/v1", api_key="test-key",
    )
    result = asyncio.run(entitlements.save_my_model_credential(body, object(), scope, "agent-one"))
    assert result["credential"]["credential_configured"]
    # Check the actual ownership seam, not only the endpoint's return projection.
    assert any(row["secret_ref"] == "test-key" for row in vaults[scope].rows.values())
    assert all(not db.rows for name, db in vaults.items() if name != scope)
    loaded = asyncio.run(entitlements.load_my_model_credential("Primary key", object(), scope, "agent-one"))
    assert loaded["api_key"] == "test-key"


@pytest.mark.parametrize("scope,owner", [
    ("platform", "_platform"), ("agent", "_agent:agent-one"), ("user", "user-one"),
])
def test_subscription_save_load_is_scoped_and_never_exposes_tokens(vaults, scope, owner):
    tokens = {"access_token": "test-access-secret", "refresh_token": "test-refresh-secret",
              "expires_at": 9999999999, "email": "account@example.test", "account_id": "test-account"}
    async def run():
        await openai_subscription._save_tokens(
            tokens, owner, "connected-row", scope=scope,
            user_id="user-one" if scope == "user" else "",
            agent_id="agent-one" if scope == "agent" else "",
        )
        body = auth.SubscriptionCredentialRequest(label="Primary subscription")
        result = await auth.save_subscription_credential(body, object(), scope, "agent-one", "connected-row")
        assert result["credential"]["credential_configured"] is True
        assert "test-access-secret" not in json.dumps(result)
        assert "test-refresh-secret" not in json.dumps(result)
        listed = await auth.saved_subscription_credentials(object(), scope, "agent-one")
        assert [row["label"] for row in listed["credentials"]] == ["Primary subscription"]
        assert "test-access-secret" not in json.dumps(listed)
        loaded = await auth.load_subscription_credential(body, object(), scope, "agent-one")
        assert loaded["entry_id"] != "connected-row"
        assert "test-refresh-secret" not in json.dumps(loaded)
        row = await model_credentials.get_owned(
            scope, owner=owner, service="openai_subscription_oauth", label=loaded["entry_id"],
            user_id="user-one" if scope == "user" else "",
            agent_id="agent-one" if scope == "agent" else "",
        )
        assert json.loads(row["secret_ref"]) == tokens
    asyncio.run(run())
    assert all(not db.rows for name, db in vaults.items() if name != scope)


def test_subscription_cannot_load_credential_from_another_vault(vaults):
    vaults["platform"].rows[("_platform", "saved_model_subscription", "App account")] = {
        "label": "App account", "config": {}, "secret_ref": json.dumps({
            "access_token": "test-access", "refresh_token": "test-refresh",
        }),
    }
    with pytest.raises(HTTPException) as error:
        asyncio.run(auth.load_subscription_credential(
            auth.SubscriptionCredentialRequest(label="App account"), object(), "agent", "agent-one",
        ))
    assert error.value.status_code == 404
    assert vaults["agent"].rows == {}


def test_unvalidated_subscription_is_not_saved(vaults, monkeypatch):
    monkeypatch.setattr(openai_subscription, "transport_auth", AsyncMock(side_effect=RuntimeError("invalid")))
    with pytest.raises(HTTPException) as error:
        asyncio.run(auth.save_subscription_credential(
            auth.SubscriptionCredentialRequest(label="Account"), object(), "agent", "agent-one", "pending-row",
        ))
    assert error.value.status_code == 409
    assert vaults["agent"].rows == {}


def test_unvalidated_api_key_is_not_saved(vaults, monkeypatch):
    monkeypatch.setattr(entitlements, "_validate_api_model_credential",
                        AsyncMock(side_effect=HTTPException(422, "Invalid credential")))
    body = entitlements.SavedModelCredentialRequest(
        label="Invalid key", provider="openai", base_url="https://api.example/v1", api_key="invalid",
    )
    with pytest.raises(HTTPException) as error:
        asyncio.run(entitlements.save_my_model_credential(body, object(), "platform"))
    assert error.value.status_code == 422
    assert vaults["platform"].rows == {}


@pytest.mark.parametrize("status,payload,valid", [
    (200, {"data": [{"id": "model"}]}, True),
    (401, {"error": "invalid key"}, False),
    (200, {"error": "invalid key"}, False),
])
def test_provider_validation_checks_http_and_payload(monkeypatch, status, payload, valid):
    original = httpx.AsyncClient
    async def handler(request):
        assert request.headers["authorization"] == "Bearer test-key"
        return httpx.Response(status, json=payload)
    monkeypatch.setattr(httpx, "AsyncClient", lambda **kwargs: original(
        **kwargs, transport=httpx.MockTransport(handler),
    ))
    if valid:
        asyncio.run(entitlements._validate_api_model_credential("https://api.example/v1", "test-key"))
    else:
        with pytest.raises(HTTPException):
            asyncio.run(entitlements._validate_api_model_credential("https://api.example/v1", "test-key"))


def test_non_admin_cannot_access_agent_or_app_credential_vault(monkeypatch):
    async def deny(*_args, **_kwargs):
        raise HTTPException(403, "Denied")
    monkeypatch.setattr(auth, "_authorized_scope", deny)
    for scope in ("platform", "agent"):
        with pytest.raises(HTTPException) as error:
            asyncio.run(entitlements.list_my_model_credentials(object(), scope, "agent-one"))
        assert error.value.status_code == 403


def test_loaded_subscriptions_reuse_refreshed_tokens_without_rebinding_replaced_accounts(vaults):
    owner = "_agent:agent-one"
    first = {"access_token": "first-access", "refresh_token": "first-refresh", "expires_at": 9999999999}
    refreshed = {**first, "access_token": "new-access", "refresh_token": "new-refresh"}
    replacement = {**first, "access_token": "other-account", "refresh_token": "other-refresh"}
    async def run():
        await openai_subscription._save_tokens(first, owner, "source", scope="agent", agent_id="agent-one")
        body = auth.SubscriptionCredentialRequest(label="Subscription")
        await auth.save_subscription_credential(body, object(), "agent", "agent-one", "source")
        loaded = await auth.load_subscription_credential(body, object(), "agent", "agent-one")
        await openai_subscription._save_tokens(refreshed, owner, loaded["entry_id"], scope="agent", agent_id="agent-one")
        assert await openai_subscription._load_tokens(owner, "source", scope="agent", agent_id="agent-one") == refreshed
        second = await auth.load_subscription_credential(body, object(), "agent", "agent-one")
        assert await openai_subscription._load_tokens(owner, second["entry_id"], scope="agent", agent_id="agent-one") == refreshed
        await openai_subscription._save_tokens(replacement, owner, "other-source", scope="agent", agent_id="agent-one")
        await auth.save_subscription_credential(body, object(), "agent", "agent-one", "other-source")
        # Replacing the reusable credential cannot switch an existing model's account.
        assert await openai_subscription._load_tokens(owner, loaded["entry_id"], scope="agent", agent_id="agent-one") == refreshed
        later = {**refreshed, "access_token": "later-access", "refresh_token": "later-refresh"}
        await openai_subscription._save_tokens(later, owner, loaded["entry_id"], scope="agent", agent_id="agent-one")
        assert await openai_subscription._load_tokens(owner, second["entry_id"], scope="agent", agent_id="agent-one") == later
        assert await openai_subscription._load_tokens(owner, "other-source", scope="agent", agent_id="agent-one") == replacement
    asyncio.run(run())


def test_reauthorizing_one_model_does_not_rebind_other_loaded_models(vaults):
    from test_chatgpt_subscription_transport import _jwt
    owner = "_agent:agent-one"
    first = {"access_token": "first-access", "refresh_token": "first-refresh",
             "account_id": "account-one", "email": "one@example.test", "expires_at": 9999999999,
             "id_token": _jwt({"chatgpt_account_id": "account-one", "email": "one@example.test"})}
    other = {**first, "account_id": "account-two", "email": "two@example.test",
             "access_token": "second-access", "refresh_token": "second-refresh",
             "id_token": _jwt({"chatgpt_account_id": "account-two", "email": "two@example.test"})}
    async def run():
        await openai_subscription._save_tokens(first, owner, "source", scope="agent", agent_id="agent-one")
        body = auth.SubscriptionCredentialRequest(label="Subscription")
        await auth.save_subscription_credential(body, object(), "agent", "agent-one", "source")
        loaded = await auth.load_subscription_credential(body, object(), "agent", "agent-one")
        await openai_subscription._save_tokens(other, owner, "source", scope="agent", agent_id="agent-one")
        assert await openai_subscription._load_tokens(owner, "source", scope="agent", agent_id="agent-one") == other
        assert await openai_subscription._load_tokens(owner, loaded["entry_id"], scope="agent", agent_id="agent-one") == first
    asyncio.run(run())

@pytest.mark.parametrize("scope,owner", [
    ("platform", "_platform"), ("agent", "_agent:agent-one"), ("user", "user-one"),
])
def test_subscription_authorization_with_database_json_config(vaults, monkeypatch, scope, owner):
    # SQLite auth_element_get/list return config as JSON text, unlike FakeDb.
    db = vaults[scope]
    original_get, original_list = db.auth_element_get, db.auth_element_list
    async def get(*args, **kwargs):
        row = await original_get(*args, **kwargs)
        return {**row, "config": json.dumps(row["config"])} if row else None
    async def listing(*args, **kwargs):
        return [{**row, "config": json.dumps(row["config"])}
                for row in await original_list(*args, **kwargs)]
    monkeypatch.setattr(db, "auth_element_get", get)
    monkeypatch.setattr(db, "auth_element_list", listing)
    async def run():
        target = dict(scope=scope, user_id="user-one" if scope == "user" else "",
                      agent_id="agent-one" if scope == "agent" else "", entry_id="device-row")
        await openai_subscription.reserve_entry(**target)
        await openai_subscription._save_tokens(
            {"access_token": "test-access", "refresh_token": "test-refresh",
             "expires_at": 9999999999}, owner, "device-row",
            **{key: value for key, value in target.items() if key != "entry_id"},
        )
        state = await openai_subscription.status(**target)
        assert state["chatgpt_authorized"] is True
        body = auth.SubscriptionCredentialRequest(label="Database subscription")
        await auth.save_subscription_credential(body, object(), scope, "agent-one", "device-row")
        listed = await auth.saved_subscription_credentials(object(), scope, "agent-one")
        assert listed["credentials"][0]["credential_configured"] is True
        loaded = await auth.load_subscription_credential(body, object(), scope, "agent-one")
        assert (await openai_subscription.status(
            **{**target, "entry_id": loaded["entry_id"]},
        ))["chatgpt_authorized"] is True
    asyncio.run(run())

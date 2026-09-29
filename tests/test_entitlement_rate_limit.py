import asyncio

import pytest
from fastapi import HTTPException
from starlette.requests import Request

import app.api.rate_limit as rate_limit
from app.models.schemas import ChatRequest


def _run(coro):
    loop = asyncio.new_event_loop()
    try:
        return loop.run_until_complete(coro)
    finally:
        loop.close()


def _request(ip="127.0.0.1", headers=None):
    return Request({
        "type": "http", "method": "POST", "path": "/chat",
        "headers": headers or [], "client": (ip, 1234),
    })


def _admitted_anon_request(monkeypatch, user_id, ip):
    request = _request(ip)
    source = rate_limit.client_source_hash(request)
    token = f"{user_id}|{source}"

    def decode(value):
        uid, bound_source = value.split("|", 1)
        return {
            "user_id": uid,
            "anon_admission": True,
            "anon_source": bound_source,
        }

    monkeypatch.setattr("app.auth.jwt.decode_token", decode)
    monkeypatch.setattr(
        rate_limit,
        "_record_risk",
        lambda *_args: {"score": 0, "reasons": []},
    )
    return _request(ip, headers=[(b"authorization", f"Bearer {token}".encode())])


@pytest.fixture(autouse=True)
def clear_buckets():
    rate_limit._BUCKETS.clear()
    yield
    rate_limit._BUCKETS.clear()


def test_registered_tier_window_has_stable_reason(monkeypatch):
    async def capabilities(_user_id, **_kwargs):
        return {"limits": {"messages_per_window": 2, "window_seconds": 60}}

    monkeypatch.setattr("app.entitlements.service.resolve_capabilities", capabilities)
    request = _request()
    _run(rate_limit.enforce_tier_chat("user-1", request))
    _run(rate_limit.enforce_tier_chat("user-1", request))
    with pytest.raises(HTTPException) as error:
        _run(rate_limit.enforce_tier_chat("user-1", request))
    assert error.value.status_code == 429
    assert error.value.detail["code"] == "rate_limited"
    assert error.value.detail["scope"] == "user_tier"
    assert error.value.headers["Retry-After"] == "60"


def test_registered_users_have_independent_buckets(monkeypatch):
    async def capabilities(_user_id, **_kwargs):
        return {"limits": {"messages_per_window": 1, "window_seconds": 60}}

    monkeypatch.setattr("app.entitlements.service.resolve_capabilities", capabilities)
    _run(rate_limit.enforce_tier_chat("user-1", _request()))
    _run(rate_limit.enforce_tier_chat("user-2", _request()))


def test_capability_failure_uses_restrictive_default(monkeypatch):
    async def capabilities(_user_id, **_kwargs):
        raise RuntimeError("unavailable")

    monkeypatch.setattr("app.entitlements.service.resolve_capabilities", capabilities)
    request = _request()
    for _ in range(30):
        _run(rate_limit.enforce_tier_chat("user-1", request))
    with pytest.raises(HTTPException) as error:
        _run(rate_limit.enforce_tier_chat("user-1", request))
    assert error.value.detail["code"] == "rate_limited"


def test_untrusted_private_peer_cannot_spoof_forwarded_ip(monkeypatch):
    monkeypatch.delenv("TRUSTED_PROXY_IPS", raising=False)
    monkeypatch.delenv("TRUST_PRIVATE_PROXIES", raising=False)
    request = _request(
        "192.168.1.50",
        headers=[(b"x-forwarded-for", b"198.51.100.99")],
    )

    assert rate_limit.client_ip(request) == "192.168.1.50"


def test_explicit_proxy_may_supply_forwarded_ip(monkeypatch):
    monkeypatch.setenv("TRUSTED_PROXY_IPS", "192.168.1.50/32")
    request = _request(
        "192.168.1.50",
        headers=[(b"x-forwarded-for", b"198.51.100.99")],
    )

    assert rate_limit.client_ip(request) == "198.51.100.99"


def test_distinct_browser_rotation_is_limited_per_source(monkeypatch, tmp_path):
    monkeypatch.setattr(rate_limit, "_ABUSE_DB_PATH", tmp_path / "abuse.sqlite")
    monkeypatch.setattr(rate_limit, "anon_session_limits", lambda: (50, 60))
    monkeypatch.setattr(rate_limit, "anon_identity_limits", lambda: (2, 3600))
    monkeypatch.setattr(rate_limit, "_record_source_event", lambda *_args: None)
    request = _request(
        "203.0.113.10",
        headers=[
            (b"user-agent", b"ExampleBrowser/1.0"),
            (b"accept-language", b"en-US"),
        ],
    )

    first = _run(rate_limit.enforce_anon_session_creation(request, "browser-a"))
    repeat = _run(rate_limit.enforce_anon_session_creation(request, "browser-a"))
    second = _run(rate_limit.enforce_anon_session_creation(request, "browser-b"))

    assert first["is_new"] is True
    assert repeat["is_new"] is False
    assert second["count"] == 2
    with pytest.raises(HTTPException) as error:
        _run(rate_limit.enforce_anon_session_creation(request, "browser-c"))
    assert error.value.status_code == 429
    assert error.value.detail["scope"] == "anonymous_identity_source"


def test_source_bucket_cannot_be_split_by_ip_or_user_agent_rotation(monkeypatch, tmp_path):
    monkeypatch.setattr(rate_limit, "_ABUSE_DB_PATH", tmp_path / "abuse.sqlite")
    monkeypatch.setattr(rate_limit, "anon_session_limits", lambda: (50, 60))
    monkeypatch.setattr(rate_limit, "anon_identity_limits", lambda: (2, 3600))
    monkeypatch.setattr(rate_limit, "_record_source_event", lambda *_args: None)
    first = _request(
        "198.51.100.10",
        headers=[(b"user-agent", b"Browser-A"), (b"accept-language", b"en-US")],
    )
    rotated = _request(
        "198.51.100.240",
        headers=[(b"user-agent", b"Browser-Z"), (b"accept-language", b"fr-FR")],
    )

    _run(rate_limit.enforce_anon_session_creation(first, "browser-a"))
    _run(rate_limit.enforce_anon_session_creation(rotated, "browser-b"))

    with pytest.raises(HTTPException) as error:
        _run(rate_limit.enforce_anon_session_creation(rotated, "browser-c"))
    assert error.value.status_code == 429
    assert error.value.detail["scope"] == "anonymous_identity_source"


def test_expired_browser_identity_is_not_permanently_allowlisted(monkeypatch, tmp_path):
    monkeypatch.setattr(rate_limit, "_ABUSE_DB_PATH", tmp_path / "abuse.sqlite")
    monkeypatch.setattr(rate_limit.time, "time", lambda: 1_000.0)
    source = "source"

    initial = rate_limit._reserve_source_identity(source, "browser-a", 1, 60)
    assert initial["allowed"] is True

    monkeypatch.setattr(rate_limit.time, "time", lambda: 1_061.0)
    replacement = rate_limit._reserve_source_identity(source, "browser-b", 1, 60)
    assert replacement["allowed"] is True
    rejected_old_identity = rate_limit._reserve_source_identity(
        source, "browser-a", 1, 60,
    )
    assert rejected_old_identity["allowed"] is False


def test_anonymous_tier_identity_limit_precedes_legacy_app_setting(monkeypatch, tmp_path):
    monkeypatch.setattr(rate_limit, "_ABUSE_DB_PATH", tmp_path / "abuse.sqlite")
    monkeypatch.setattr(rate_limit, "anonymous_chat_enabled", lambda: True)
    monkeypatch.setattr(rate_limit, "anonymous_auto_close_status", lambda: {"active": False})
    monkeypatch.setattr(rate_limit, "anon_chat_limits", lambda: (99, 60))
    monkeypatch.setattr(rate_limit, "anon_daily_chat_limits", lambda: (99, 86400))
    monkeypatch.setattr(rate_limit, "anon_chat_ip_limits", lambda: (99, 60))
    monkeypatch.setattr(rate_limit, "anon_global_chat_limits", lambda: (999, 60))
    monkeypatch.setattr(rate_limit, "anon_budget_limits", lambda: (999, 86400))
    monkeypatch.setattr(rate_limit, "_consume_native_budgets", lambda *_args: {"allowed": True})

    async def capabilities(_user_id, **_kwargs):
        return {"limits": {"messages_per_window": 99, "window_seconds": 60}, "anonymous": {
            "identity_messages": 1, "identity_window_seconds": 60,
            "daily_messages": 99, "daily_window_seconds": 86400,
            "network_messages": 99, "network_window_seconds": 60,
        }}

    monkeypatch.setattr("app.entitlements.service.resolve_capabilities", capabilities)
    request = _admitted_anon_request(monkeypatch, "anon_tier_limit", "203.0.113.10")
    _run(rate_limit.enforce_tier_chat("anon_tier_limit", request, message="hello"))
    with pytest.raises(HTTPException) as error:
        _run(rate_limit.enforce_tier_chat("anon_tier_limit", request, message="again"))
    assert error.value.detail["scope"] == "anonymous_tier_identity"
    assert error.value.detail["limit"] == 1


def test_anonymous_global_identity_ceiling_caps_permissive_tier(monkeypatch, tmp_path):
    monkeypatch.setattr(rate_limit, "_ABUSE_DB_PATH", tmp_path / "abuse.sqlite")
    monkeypatch.setattr(rate_limit, "anonymous_chat_enabled", lambda: True)
    monkeypatch.setattr(rate_limit, "anonymous_auto_close_status", lambda: {"active": False})
    monkeypatch.setattr(rate_limit, "anon_chat_limits", lambda: (1, 60))
    monkeypatch.setattr(rate_limit, "anon_daily_chat_limits", lambda: (99, 86400))
    monkeypatch.setattr(rate_limit, "anon_chat_ip_limits", lambda: (99, 60))
    monkeypatch.setattr(rate_limit, "anon_global_chat_limits", lambda: (999, 60))
    monkeypatch.setattr(rate_limit, "anon_budget_limits", lambda: (999, 86400))
    monkeypatch.setattr(rate_limit, "_consume_native_budgets", lambda *_args: {"allowed": True})

    async def capabilities(_user_id, **_kwargs):
        return {"limits": {"messages_per_window": 99, "window_seconds": 60}, "anonymous": {
            "identity_messages": 99, "identity_window_seconds": 60,
            "daily_messages": 99, "daily_window_seconds": 86400,
            "network_messages": 99, "network_window_seconds": 60,
        }}

    monkeypatch.setattr("app.entitlements.service.resolve_capabilities", capabilities)
    request = _admitted_anon_request(monkeypatch, "anon_global_ceiling", "203.0.113.20")
    _run(rate_limit.enforce_tier_chat("anon_global_ceiling", request, message="hello"))
    with pytest.raises(HTTPException) as error:
        _run(rate_limit.enforce_tier_chat("anon_global_ceiling", request, message="again"))
    assert error.value.detail["scope"] == "anonymous_identity_global"
    assert error.value.detail["limit"] == 1


def test_anonymous_message_limit_survives_process_bucket_reset(monkeypatch, tmp_path):
    monkeypatch.setattr(rate_limit, "_ABUSE_DB_PATH", tmp_path / "abuse.sqlite")
    # This test targets durable tier counters, not live app credit budgets.
    monkeypatch.setattr(rate_limit, "_consume_native_budgets", lambda *_args: {"allowed": True})

    async def capabilities(_user_id, **_kwargs):
        return {"limits": {"messages_per_window": 2, "window_seconds": 60}}

    monkeypatch.setattr("app.entitlements.service.resolve_capabilities", capabilities)
    monkeypatch.setattr(rate_limit, "anon_chat_ip_limits", lambda: (100, 60))
    request = _admitted_anon_request(monkeypatch, "anon_test", "203.0.113.11")
    _run(rate_limit.enforce_tier_chat("anon_test", request))
    _run(rate_limit.enforce_tier_chat("anon_test", request))

    rate_limit._BUCKETS.clear()  # simulate another worker / process restart
    with pytest.raises(HTTPException) as error:
        _run(rate_limit.enforce_tier_chat("anon_test", request))
    assert error.value.detail["scope"] == "user_tier"


def test_distributed_networks_hit_global_session_breaker(monkeypatch, tmp_path):
    monkeypatch.setattr(rate_limit, "_ABUSE_DB_PATH", tmp_path / "abuse.sqlite")
    monkeypatch.setattr(rate_limit, "anon_session_limits", lambda: (50, 60))
    monkeypatch.setattr(rate_limit, "anon_identity_limits", lambda: (10, 3600))
    monkeypatch.setattr(rate_limit, "anon_global_session_limits", lambda: (2, 3600))
    monkeypatch.setattr(rate_limit, "_record_source_event", lambda *_args: None)

    _run(rate_limit.enforce_anon_session_creation(_request("198.18.1.10"), "browser-a"))
    _run(rate_limit.enforce_anon_session_creation(_request("198.18.2.10"), "browser-b"))
    with pytest.raises(HTTPException) as error:
        _run(rate_limit.enforce_anon_session_creation(_request("198.18.3.10"), "browser-c"))

    assert error.value.detail["scope"] == "anonymous_global_session"
    assert error.value.detail["limit"] == 2


def test_anonymous_daily_allowance_requires_registration(monkeypatch, tmp_path):
    monkeypatch.setattr(rate_limit, "_ABUSE_DB_PATH", tmp_path / "abuse.sqlite")
    # Keep the daily-allowance assertion independent of live credit-budget state.
    monkeypatch.setattr(rate_limit, "_consume_native_budgets", lambda *_args: {"allowed": True})

    async def capabilities(_user_id, **_kwargs):
        return {"limits": {"messages_per_window": 100, "window_seconds": 60}}

    monkeypatch.setattr("app.entitlements.service.resolve_capabilities", capabilities)
    monkeypatch.setattr(rate_limit, "anon_daily_chat_limits", lambda: (2, 86400))
    monkeypatch.setattr(rate_limit, "anon_chat_ip_limits", lambda: (100, 60))
    monkeypatch.setattr(rate_limit, "anon_global_chat_limits", lambda: (100, 60))
    request = _admitted_anon_request(monkeypatch, "anon_daily", "198.18.20.10")

    _run(rate_limit.enforce_tier_chat("anon_daily", request))
    _run(rate_limit.enforce_tier_chat("anon_daily", request))
    with pytest.raises(HTTPException) as error:
        _run(rate_limit.enforce_tier_chat("anon_daily", request))

    assert error.value.detail["code"] == "registration_required"
    assert error.value.detail["scope"] == "anonymous_daily_allowance"


def test_distributed_networks_hit_global_chat_breaker(monkeypatch, tmp_path):
    monkeypatch.setattr(rate_limit, "_ABUSE_DB_PATH", tmp_path / "abuse.sqlite")

    async def capabilities(_user_id, **_kwargs):
        return {"limits": {"messages_per_window": 100, "window_seconds": 60}}

    monkeypatch.setattr("app.entitlements.service.resolve_capabilities", capabilities)
    monkeypatch.setattr(rate_limit, "anon_daily_chat_limits", lambda: (100, 86400))
    monkeypatch.setattr(rate_limit, "anon_chat_ip_limits", lambda: (100, 60))
    monkeypatch.setattr(rate_limit, "anon_global_chat_limits", lambda: (2, 60))
    monkeypatch.setattr(rate_limit, "_record_source_event", lambda *_args: None)

    _run(rate_limit.enforce_tier_chat("anon_a", _admitted_anon_request(monkeypatch, "anon_a", "198.18.31.10")))
    _run(rate_limit.enforce_tier_chat("anon_b", _admitted_anon_request(monkeypatch, "anon_b", "198.18.32.10")))
    with pytest.raises(HTTPException) as error:
        _run(rate_limit.enforce_tier_chat("anon_c", _admitted_anon_request(monkeypatch, "anon_c", "198.18.33.10")))

    assert error.value.detail["scope"] == "anonymous_global_chat"
    assert error.value.detail["limit"] == 2


def test_public_registration_has_ip_and_global_breakers(monkeypatch, tmp_path):
    monkeypatch.setattr(rate_limit, "_ABUSE_DB_PATH", tmp_path / "abuse.sqlite")
    monkeypatch.setattr(rate_limit, "public_registration_limits", lambda: (2, 3, 3600))
    monkeypatch.setattr(rate_limit, "_record_source_event", lambda *_args: None)

    _run(rate_limit.enforce_public_registration(_request("198.18.41.10")))
    _run(rate_limit.enforce_public_registration(_request("198.18.41.10")))
    with pytest.raises(HTTPException) as per_ip:
        _run(rate_limit.enforce_public_registration(_request("198.18.41.10")))
    assert per_ip.value.detail["scope"] == "public_registration_network"

    _run(rate_limit.enforce_public_registration(_request("198.18.42.10")))
    with pytest.raises(HTTPException) as global_limit:
        _run(rate_limit.enforce_public_registration(_request("198.18.43.10")))
    assert global_limit.value.detail["scope"] == "public_registration_global"


def test_anonymous_kill_switch_blocks_mint_and_chat_but_not_registered(monkeypatch, tmp_path):
    monkeypatch.setattr(rate_limit, "_ABUSE_DB_PATH", tmp_path / "abuse.sqlite")
    monkeypatch.setattr(rate_limit, "anonymous_chat_enabled", lambda: False)

    with pytest.raises(HTTPException) as mint_error:
        _run(rate_limit.enforce_anon_session_creation(_request(), "browser-a"))
    assert mint_error.value.detail["code"] == "registration_required"
    assert mint_error.value.detail["scope"] == "anonymous_chat_disabled"

    with pytest.raises(HTTPException) as chat_error:
        _run(rate_limit.enforce_tier_chat("anon_disabled", _request()))
    assert chat_error.value.detail["scope"] == "anonymous_chat_disabled"

    async def capabilities(_user_id, **_kwargs):
        return {"limits": {"messages_per_window": 10, "window_seconds": 60}}

    monkeypatch.setattr("app.entitlements.service.resolve_capabilities", capabilities)
    _run(rate_limit.enforce_tier_chat("registered-user", _request()))


def test_hard_anonymous_budget_caps_distributed_chat(monkeypatch, tmp_path):
    monkeypatch.setattr(rate_limit, "_ABUSE_DB_PATH", tmp_path / "abuse.sqlite")
    monkeypatch.setattr(rate_limit, "_record_source_event", lambda *_args: None)

    async def capabilities(_user_id, **_kwargs):
        return {"limits": {"messages_per_window": 100, "window_seconds": 60}}

    monkeypatch.setattr("app.entitlements.service.resolve_capabilities", capabilities)
    monkeypatch.setattr(rate_limit, "anonymous_chat_enabled", lambda: True)
    monkeypatch.setattr(rate_limit, "anon_daily_chat_limits", lambda: (100, 86400))
    monkeypatch.setattr(rate_limit, "anon_chat_ip_limits", lambda: (100, 60))
    monkeypatch.setattr(rate_limit, "anon_global_chat_limits", lambda: (100, 60))
    monkeypatch.setattr(rate_limit, "anon_budget_limits", lambda: (2, 86400))

    _run(rate_limit.enforce_tier_chat("anon_budget_a", _admitted_anon_request(monkeypatch, "anon_budget_a", "198.18.61.10")))
    _run(rate_limit.enforce_tier_chat("anon_budget_b", _admitted_anon_request(monkeypatch, "anon_budget_b", "198.18.62.10")))
    with pytest.raises(HTTPException) as error:
        _run(rate_limit.enforce_tier_chat("anon_budget_c", _admitted_anon_request(monkeypatch, "anon_budget_c", "198.18.63.10")))

    assert error.value.detail["code"] == "registration_required"
    assert error.value.detail["scope"] == "anonymous_budget_exhausted"
    assert error.value.detail["limit"] == 2


def test_blocking_chat_preserves_anonymous_budget_http_error(monkeypatch):
    from app.chat import execution as chat_api

    async def caller(_request, claimed_user_id):
        return claimed_user_id

    async def deny(_user_id, _request, **_kwargs):
        raise HTTPException(
            status_code=429,
            detail={
                "code": "registration_required",
                "scope": "anonymous_budget_exhausted",
            },
        )

    monkeypatch.setattr("app.auth.identity.assert_caller_is", caller)
    monkeypatch.setattr(rate_limit, "enforce_tier_chat", deny)
    request = ChatRequest(
        user_id="anon_budget",
        session_id="budget-test-session",
        message="hello",
    )

    with pytest.raises(HTTPException) as error:
        _run(chat_api._chat_impl(request, _request()))

    assert error.value.status_code == 429
    assert error.value.detail["scope"] == "anonymous_budget_exhausted"


def test_anonymous_admission_is_bound_to_minting_source(monkeypatch, tmp_path):
    monkeypatch.setattr(rate_limit, "_ABUSE_DB_PATH", tmp_path / "abuse.sqlite")
    monkeypatch.setattr(rate_limit, "anonymous_auto_close_status", lambda: {"active": False})
    monkeypatch.setattr(rate_limit, "_record_risk", lambda *_args: {"score": 0, "reasons": []})
    source = rate_limit.client_source_hash(_request("198.18.70.10"))
    monkeypatch.setattr("app.auth.jwt.decode_token", lambda _token: {
        "user_id": "anon_bound",
        "anon_admission": True,
        "anon_admission_id": "admission-a",
        "anon_source": source,
    })

    with pytest.raises(HTTPException) as error:
        _run(rate_limit.enforce_tier_chat(
            "anon_bound",
            _request("198.18.71.10", headers=[(b"authorization", b"Bearer copied")]),
            message="hello",
        ))

    assert error.value.status_code == 401
    assert error.value.detail["scope"] == "anonymous_admission"


def test_native_token_budget_refuses_without_partial_reservation(monkeypatch, tmp_path):
    monkeypatch.setattr(rate_limit, "_ABUSE_DB_PATH", tmp_path / "abuse.sqlite")
    controls = {
        **rate_limit.anon_native_controls(),
        "estimated_output_tokens": 10,
        "estimated_cost_per_1k_microusd": 0,
        "token_user_max": 20,
        "token_source_max": 100,
        "token_global_max": 100,
        "cost_user_microusd_max": 0,
        "cost_source_microusd_max": 0,
        "cost_global_microusd_max": 0,
        "spend_window": 3600,
    }
    monkeypatch.setattr(rate_limit, "anon_native_controls", lambda: controls)
    first = rate_limit._consume_native_budgets("anon_budget", "source", "1234567890")
    refused = rate_limit._consume_native_budgets("anon_budget", "source", "x" * 100)

    assert first["allowed"] is True
    assert refused["allowed"] is False
    assert refused["scope"] == "tokens_user"
    status = rate_limit._usage_status("estimated-token-source:source", 100, 3600)
    assert status["used"] == first["tokens"]


def test_anonymous_control_snapshot_exposes_guest_and_shared_network_allowances(monkeypatch, tmp_path):
    monkeypatch.setattr(rate_limit, "_ABUSE_DB_PATH", tmp_path / "abuse.sqlite")
    controls = {
        **rate_limit.anon_native_controls(),
        "token_user_max": 1000,
        "token_source_max": 2000,
        "token_global_max": 10000,
        "cost_user_microusd_max": 100000,
        "cost_source_microusd_max": 250000,
        "cost_global_microusd_max": 2500000,
        "spend_window": 86400,
    }
    monkeypatch.setattr(rate_limit, "anon_native_controls", lambda: controls)
    monkeypatch.setattr(rate_limit, "_source_for_user", lambda _user_id: "source-a")
    monkeypatch.setattr(rate_limit, "anonymous_budget_status", lambda: {"enabled": True})
    monkeypatch.setattr(rate_limit, "anonymous_auto_close_status", lambda: {"active": False})
    rate_limit._persistent_consume("estimated-cost-user:anon_a", 60000, 0, 86400)
    rate_limit._persistent_consume("estimated-cost-source:source-a", 187500, 0, 86400)
    rate_limit._persistent_consume("actual-cost-source:source-a", 125000, 0, 86400)

    snapshot = rate_limit.anonymous_control_snapshot(["anon_a"])
    user = snapshot["users"]["anon_a"]

    assert user["estimated_cost_microusd"]["percent"] == 60
    assert user["network_estimated_cost_microusd"]["percent"] == 75
    assert user["network_actual_cost_microusd"]["percent"] == 50
    assert user["network_estimated_cost_microusd"]["limit"] == 250000


def test_actual_network_cost_exhaustion_stops_subsequent_agent_work(monkeypatch, tmp_path):
    monkeypatch.setattr(rate_limit, "_ABUSE_DB_PATH", tmp_path / "abuse.sqlite")
    controls = {
        **rate_limit.anon_native_controls(),
        "cost_user_microusd_max": 500000,
        "cost_source_microusd_max": 250000,
        "cost_global_microusd_max": 2500000,
        "spend_window": 86400,
    }
    monkeypatch.setattr(rate_limit, "anon_native_controls", lambda: controls)
    monkeypatch.setattr(rate_limit, "_source_for_user", lambda _user_id: "source-a")

    status = rate_limit.record_anonymous_actual_usage("anon_a", 10, 10, 0.25)
    refused = rate_limit._consume_native_budgets("anon_a", "source-a", "continue")

    assert status["exhausted"] is True
    assert status["scope"] == "cost_source"
    assert refused["allowed"] is False
    assert refused["scope"] == "cost_source"


def test_agent_member_subject_records_actual_usage_and_appears_in_snapshot(monkeypatch, tmp_path):
    from app.agent.member_workspace import subject_id

    monkeypatch.setattr(rate_limit, "_ABUSE_DB_PATH", tmp_path / "abuse.sqlite")
    controls = {
        **rate_limit.anon_native_controls(),
        "token_user_max": 1000,
        "token_source_max": 2000,
        "token_global_max": 10000,
        "cost_user_microusd_max": 100000,
        "cost_source_microusd_max": 250000,
        "cost_global_microusd_max": 2500000,
        "spend_window": 86400,
    }
    monkeypatch.setattr(rate_limit, "anon_native_controls", lambda: controls)
    monkeypatch.setattr(rate_limit, "_source_for_user", lambda _user_id: "source-a")
    monkeypatch.setattr(rate_limit, "anonymous_budget_status", lambda: {"enabled": True})
    monkeypatch.setattr(rate_limit, "anonymous_auto_close_status", lambda: {"active": False})
    user_id = subject_id("shared_default", "visitor-1")

    status = rate_limit.record_anonymous_actual_usage(user_id, 10, 20, 0.01)
    snapshot = rate_limit.anonymous_control_snapshot()

    assert status["exhausted"] is False
    assert snapshot["global_usage"]["actual_tokens"]["used"] == 30
    assert snapshot["users"][user_id]["actual_tokens"]["used"] == 30
    assert snapshot["users"][user_id]["first_activity_at"].endswith("+00:00")
    assert snapshot["users"][user_id]["last_activity_at"].endswith("+00:00")
    assert user_id in rate_limit.anonymous_control_identity_ids()


def test_network_cost_allowance_never_renews(monkeypatch, tmp_path):
    monkeypatch.setattr(rate_limit, "_ABUSE_DB_PATH", tmp_path / "abuse.sqlite")
    clock = [1_000.0]
    monkeypatch.setattr(rate_limit.time, "time", lambda: clock[0])
    controls = {
        **rate_limit.anon_native_controls(),
        "estimated_output_tokens": 999,
        "estimated_cost_per_1k_microusd": 125000,
        "token_user_max": 0,
        "token_source_max": 0,
        "token_global_max": 0,
        "cost_user_microusd_max": 0,
        "cost_source_microusd_max": 250000,
        "cost_global_microusd_max": 0,
        "spend_window": 86400,
    }
    monkeypatch.setattr(rate_limit, "anon_native_controls", lambda: controls)

    first = rate_limit._consume_native_budgets("anon_a", "source-a", "")
    clock[0] += 86400 * 365
    second = rate_limit._consume_native_budgets("anon_b", "source-a", "")
    refused = rate_limit._consume_native_budgets("anon_c", "source-a", "")
    status = rate_limit._usage_status(
        "estimated-cost-source:source-a", 250000, 0,
    )

    assert first["allowed"] is True
    assert second["allowed"] is True
    assert refused["allowed"] is False
    assert refused["scope"] == "cost_source"
    assert refused["resets_in"] is None
    assert status["used"] == 250000
    assert status["remaining"] == 0
    assert status["resets_in"] is None


def test_guest_network_accounting_link_survives_identity_window_pruning(monkeypatch, tmp_path):
    monkeypatch.setattr(rate_limit, "_ABUSE_DB_PATH", tmp_path / "abuse.sqlite")
    clock = [1_000.0]
    monkeypatch.setattr(rate_limit.time, "time", lambda: clock[0])
    source = "source-a"
    browser = "browser-a"
    rate_limit._reserve_source_identity(source, browser, 5, 86400)
    rate_limit._bind_source_identity(source, browser, "anon_a")

    clock[0] += 86401
    rate_limit._reserve_source_identity("source-b", "browser-b", 5, 86400)

    conn = rate_limit._connect_abuse_db()
    try:
        assert conn.execute(
            "SELECT COUNT(*) FROM anonymous_source_identities WHERE source_hash=?",
            (source,),
        ).fetchone()[0] == 0
    finally:
        conn.close()
    assert rate_limit._source_for_user("anon_a") == source


def test_anonymous_concurrency_lease_is_released(monkeypatch, tmp_path):
    monkeypatch.setattr(rate_limit, "_ABUSE_DB_PATH", tmp_path / "abuse.sqlite")
    controls = {**rate_limit.anon_native_controls(), "max_concurrent_runs": 1, "run_lease_seconds": 60}
    monkeypatch.setattr(rate_limit, "anon_native_controls", lambda: controls)

    lease = rate_limit.begin_anonymous_run("anon_a", "session-a")
    with pytest.raises(RuntimeError, match="anonymous_concurrency_limit"):
        rate_limit.begin_anonymous_run("anon_b", "session-b")
    rate_limit.end_anonymous_run(lease)
    second = rate_limit.begin_anonymous_run("anon_b", "session-b")
    assert second
    rate_limit.end_anonymous_run(second)


def test_repeated_prompt_across_identities_triggers_progressive_risk(monkeypatch, tmp_path):
    monkeypatch.setattr(rate_limit, "_ABUSE_DB_PATH", tmp_path / "abuse.sqlite")
    one = rate_limit._record_risk("anon_a", "source-a", "same prompt", "a", "ua-a")
    two = rate_limit._record_risk("anon_b", "source-b", "same prompt", "b", "ua-b")
    three = rate_limit._record_risk("anon_c", "source-c", "same prompt", "c", "ua-c")
    four = rate_limit._record_risk("anon_d", "source-d", "same prompt", "d", "ua-d")

    assert one["score"] == 0
    assert two["score"] == 0
    assert three["score"] == 0
    assert four["score"] >= 3
    assert "repeated_prompt" in four["reasons"]


def test_anonymous_error_burst_activates_auto_close(monkeypatch, tmp_path):
    monkeypatch.setattr(rate_limit, "_ABUSE_DB_PATH", tmp_path / "abuse.sqlite")
    controls = {**rate_limit.anon_native_controls(), "error_max": 1, "error_window": 60}
    monkeypatch.setattr(rate_limit, "anon_native_controls", lambda: controls)
    closed = []
    monkeypatch.setattr(rate_limit, "_auto_close", lambda reason: closed.append(reason))

    rate_limit.record_anonymous_run_error("anon_a", "provider unavailable")
    rate_limit.record_anonymous_run_error("anon_b", "provider unavailable")

    assert closed == ["anonymous model error threshold exceeded"]

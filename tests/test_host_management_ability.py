from __future__ import annotations

import asyncio
import json
from pathlib import Path

import pytest

from app import abilities
from app.entitlements.abilities import ability_group
from app.entitlements.policy import ADMIN_OVERLAY, KNOWN_ABILITY_GROUPS


def run(coro):
    return asyncio.run(coro)


@pytest.fixture(scope="module")
def host_management():
    abilities.reload()
    module = abilities.ability_module("host_management")
    assert module is not None
    return module


def test_descriptor_is_a_web_host_connection_ability(host_management):
    entry = abilities.ability_entry("host_management")
    assert entry["group"] == "web"
    assert entry["ui_panel"] == "host_connections"
    assert entry["default_enabled"] is False
    assert entry["entitlement_group"] == "host_management"
    assert ability_group("host_management") == "host_management"
    assert "host_management" in KNOWN_ABILITY_GROUPS
    assert "host_management" in ADMIN_OVERLAY["ability_groups"]
    assert any(item["id"] == "host_management" for item in abilities.ability_routers())


def test_tool_contract_guards_dns_writes(host_management):
    tools = host_management.build_tools(agent_id="agent-1", user_id="user-1")
    assert set(tools) == {
        "host_list_connections", "host_list_zones", "host_list_dns_records",
        "host_upsert_dns_record",
    }
    assert host_management.DESTRUCTIVE == {"host_upsert_dns_record"}
    metadata = abilities.tool_metadata()["host_upsert_dns_record"]
    assert metadata["destructive"] is True
    assert metadata["requires_confirmation"] is True
    assert host_management.TOOL_SCHEMAS["host_upsert_dns_record"]["required"] == [
        "connection", "zone_id", "zone_name", "name", "value",
    ]


def test_public_profile_never_returns_provider_secret(host_management):
    row = {
        "label": "agent:agent-1:host_12345678",
        "config": {
            "name": "Production", "provider": "cloudflare", "provider_config": {},
        },
        "secret_ref": json.dumps({"api_token": "super-secret-token"}),
    }
    public = host_management._public_profile("agent-1", row)
    encoded = json.dumps(public)
    assert "super-secret-token" not in encoded
    assert "secret_ref" not in public
    assert public["configured"] is True
    assert public["credentials_set"] == {"api_token": True}


def test_blank_credential_update_keeps_saved_token(host_management):
    existing = {
        "name": "Production", "provider": "cloudflare", "provider_config": {},
        "credentials": {"api_token": "keep-me"},
    }
    config, credentials = host_management._normalize_profile({
        "name": "Renamed", "provider": "cloudflare", "provider_config": {},
        "credentials": {"api_token": ""},
    }, existing)
    assert config["name"] == "Renamed"
    assert credentials == {"api_token": "keep-me"}


def test_upsert_tool_delegates_to_selected_provider(monkeypatch, host_management):
    captured = {}

    class Provider:
        async def upsert_record(self, config, credentials, zone, record):
            captured.update(config=config, credentials=credentials, zone=zone, record=record)
            yield {"phase": "write", "message": "writing", "level": "info"}
            yield {"phase": "done", "result": {"ok": True, "message": "saved"}}

    async def profile(*_args, **_kwargs):
        return ({
            "name": "Production", "provider_config": {"account": "one"},
            "credentials": {"api_token": "hidden"},
        }, Provider())

    monkeypatch.setattr(host_management, "_tool_profile", profile)
    tools = host_management.build_tools(agent_id="agent-1")
    result = json.loads(run(tools["host_upsert_dns_record"](
        "Production", "zone-1", "example.com", "app", "203.0.113.8",
    )))
    assert result["status"] == "ok"
    assert result["events"][0]["phase"] == "write"
    assert captured["zone"] == {"id": "zone-1", "name": "example.com"}
    assert captured["record"]["value"] == "203.0.113.8"
    assert captured["credentials"] == {"api_token": "hidden"}


def test_agent_ability_panel_mounts_host_connection_manager():
    root = Path(__file__).resolve().parents[1]
    table = (root / "ui/shared/js/agent-ability-table.js").read_text(encoding="utf-8")
    panel = (root / "ui/shared/js/host-management-connections.js").read_text(encoding="utf-8")
    assert "buildHostConnectionsSection" in table
    assert "conn.ui_panel === 'host_connections'" in table
    assert "Cloudflare or another installed provider" in panel

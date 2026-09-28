from __future__ import annotations

import asyncio
import json
import socket
import threading
from pathlib import Path

import pytest

from app import abilities
from app.entitlements.abilities import ability_group
from app.entitlements.policy import ADMIN_OVERLAY, KNOWN_ABILITY_GROUPS


def run(coro):
    return asyncio.run(coro)


@pytest.fixture(scope="module")
def ssh():
    abilities.reload()
    module = abilities.ability_module("ssh_control")
    assert module is not None
    return module


def test_descriptor_is_non_admin_opt_in_with_auto_command_policy(ssh):
    entry = abilities.ability_entry("ssh_control")
    assert entry["group"] == "web"
    assert entry["entitlement_group"] == "ssh_control"
    assert entry["default_enabled"] is False
    assert ability_group("ssh_control") == "ssh_control"
    assert "ssh_control" in KNOWN_ABILITY_GROUPS
    assert "ssh_control" in ADMIN_OVERLAY["ability_groups"]

    metadata = abilities.tool_metadata()
    assert metadata["ssh_run_command"]["destructive"] is True
    assert metadata["ssh_run_command"]["requires_confirmation"] is False
    assert metadata["ssh_cancel_job"]["requires_confirmation"] is False
    assert metadata["ssh_delete_connection"]["requires_confirmation"] is True
    confirmed = abilities.confirm_gated_tools(force=True)
    assert "ssh_delete_connection" in confirmed
    assert "ssh_run_command" not in confirmed


def test_router_and_tool_contract_are_drop_in(ssh):
    assert any(item["id"] == "ssh_control" for item in abilities.ability_routers())
    tools = ssh.build_tools(user_id="user-1", agent_id="agent-1",
                            enabled_providers={"ssh_control"})
    assert set(tools) == {
        "ssh_request_connection", "ssh_list_connections", "ssh_test_connection",
        "ssh_delete_connection", "ssh_run_command", "ssh_start_job",
        "ssh_poll_job", "ssh_list_jobs", "ssh_cancel_job",
    }
    assert ssh.DESTRUCTIVE == {"ssh_delete_connection"}
    assert ssh.TOOL_SCHEMAS["ssh_run_command"]["required"] == ["connection_id", "command"]


def test_connection_card_carries_originating_chat_session(monkeypatch, ssh):
    async def allow(*_args, **_kwargs):
        return None

    monkeypatch.setattr(ssh, "_tool_gate", allow)
    tools = ssh.build_tools(
        user_id="user-1", agent_id="agent-1", session_id="session-1",
        enabled_providers={"ssh_control"},
    )
    payload = json.loads(run(tools["ssh_request_connection"]("10.0.0.9")))
    assert payload["session_id"] == "session-1"
    assert payload["ui"] == "ssh_connection_form"


@pytest.mark.parametrize("host", ["localhost", "localhost.", "127.0.0.1", "::1", "0.0.0.0"])
def test_local_webagent_targets_are_rejected(ssh, host):
    with pytest.raises(ValueError, match="cannot connect|local-only"):
        ssh._resolve_external_target(host, 22)


def test_resolver_connects_to_the_validated_numeric_address(monkeypatch, ssh):
    monkeypatch.setattr(ssh.socket, "getaddrinfo", lambda host, port, type: [
        (ssh.socket.AF_INET, ssh.socket.SOCK_STREAM, 6, "", ("10.50.0.9", port)),
    ])
    monkeypatch.setattr(ssh, "_local_interface_ips", lambda: {"10.50.0.2"})
    assert ssh._resolve_external_target("switch.lan", 22) == "10.50.0.9"


def test_public_profile_never_contains_secrets(ssh):
    config = {
        "connection_id": "one", "name": "Router", "host": "10.0.0.1",
        "port": 22, "username": "ops", "auth_method": "private_key",
        "host_key_fingerprint": "SHA256:abc", "host_key_type": "ssh-ed25519",
    }
    secrets = {
        "password": "do-not-return", "private_key": "PRIVATE",
        "key_passphrase": "phrase", "sudo_password": "sudo",
    }
    view = ssh._public_profile(config, secrets)
    encoded = json.dumps(view)
    assert "do-not-return" not in encoded
    assert "PRIVATE" not in encoded
    assert "phrase" not in encoded
    assert '"sudo"' not in encoded
    assert view["configured"] is True
    assert view["has_sudo_password"] is True


def test_profile_store_is_scoped_by_user_and_agent(monkeypatch, ssh):
    class FakeDB:
        def __init__(self):
            self.rows = {}

        async def auth_element_set(self, *, user_id, service, label, config, secret_ref):
            self.rows[(user_id, service, label)] = {
                "label": label, "config": config, "secret_ref": secret_ref,
            }

        async def auth_element_get(self, user_id, service, label):
            return self.rows.get((user_id, service, label))

        async def auth_element_list(self, user_id, service):
            return [row for (uid, svc, _), row in self.rows.items()
                    if uid == user_id and svc == service]

        async def auth_element_delete(self, user_id, service, label):
            return self.rows.pop((user_id, service, label), None) is not None

    fake = FakeDB()
    import app.db
    monkeypatch.setattr(app.db, "get_db", lambda: fake)
    config = {
        "agent_id": "agent-a", "connection_id": "conn-a", "name": "Host",
        "host": "10.0.0.8", "port": 22, "username": "ops",
        "auth_method": "password", "host_key_fingerprint": "SHA256:key",
    }
    run(ssh._save_profile("user-a", "agent-a", config, {"password": "secret"}))
    assert run(ssh._get_profile("user-a", "agent-a", "conn-a"))["secrets"]["password"] == "secret"
    assert run(ssh._get_profile("user-a", "agent-b", "conn-a")) is None
    assert run(ssh._get_profile("user-b", "agent-a", "conn-a")) is None
    public = run(ssh._list_profiles("user-a", "agent-a"))
    assert public[0]["configured"] is True
    assert "password" not in public[0]


def test_job_buffer_is_bounded_and_reports_cursor_gap(ssh):
    class Closeable:
        def close(self):
            pass

    job = ssh._Job(
        job_id="job", user_id="u", agent_id="a", connection_id="c",
        connection_name="Host", client=Closeable(), channel=Closeable(),
        started_at=0.0, max_runtime=60,
    )
    chunk = b"x" * 32_768
    for _ in range((ssh.MAX_OUTPUT_CHARS // len(chunk)) + 3):
        job.append("stdout", chunk)
    assert sum(len(event[3]) for event in job.events) <= ssh.MAX_OUTPUT_CHARS
    assert job.base_cursor > 0
    assert job.truncated is True


def test_save_rejects_a_host_key_that_changed_before_auth(monkeypatch, ssh):
    saved = []

    async def actor(request, agent_id):
        return "user-a"

    monkeypatch.setattr(ssh, "_api_actor", actor)
    monkeypatch.setattr(ssh, "_list_profiles", lambda *args, **kwargs: _async_value([]))
    monkeypatch.setattr(ssh, "_probe_host", lambda host, port: {
        "fingerprint": "SHA256:different", "key_type": "ssh-ed25519",
    })

    async def capture(*args):
        saved.append(args)

    monkeypatch.setattr(ssh, "_save_profile", capture)
    body = ssh.SaveConnectionRequest(
        agent_id="agent-a", name="Host", host="10.0.0.9", port=22,
        username="ops", auth_method="password", password="secret",
        expected_fingerprint="SHA256:trusted",
    )
    with pytest.raises(Exception) as exc:
        run(ssh.save_connection(body, object()))
    assert getattr(exc.value, "status_code", None) == 400
    assert "changed" in str(getattr(exc.value, "detail", "")).lower()
    assert saved == []


def test_password_command_runs_over_a_pinned_paramiko_transport(monkeypatch, ssh):
    paramiko = pytest.importorskip("paramiko")
    host_key = paramiko.RSAKey.generate(1024)
    ready = threading.Event()
    command_seen = []

    class Server(paramiko.ServerInterface):
        def check_auth_password(self, username, password):
            return (paramiko.AUTH_SUCCESSFUL
                    if username == "ops" and password == "secret"
                    else paramiko.AUTH_FAILED)

        def check_channel_request(self, kind, chanid):
            return paramiko.OPEN_SUCCEEDED if kind == "session" else paramiko.OPEN_FAILED_ADMINISTRATIVELY_PROHIBITED

        def check_channel_exec_request(self, channel, command):
            command_seen.append(command.decode("utf-8"))
            ready.set()
            return True

    listener = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    listener.bind(("127.0.0.1", 0))
    listener.listen(1)
    port = listener.getsockname()[1]

    def serve():
        transport = None
        try:
            client, _ = listener.accept()
            transport = paramiko.Transport(client)
            transport.add_server_key(host_key)
            transport.start_server(server=Server())
            channel = transport.accept(5)
            assert channel is not None
            assert ready.wait(5)
            channel.send(b"healthy\n")
            channel.send_stderr(b"notice\n")
            channel.send_exit_status(0)
            channel.close()
        finally:
            if transport is not None:
                transport.close()
            listener.close()

    thread = threading.Thread(target=serve, daemon=True)
    thread.start()
    monkeypatch.setattr(ssh, "_resolve_external_target", lambda host, target_port: "127.0.0.1")
    result = ssh._run_sync(
        {
            "host": "remote.test", "port": port, "username": "ops",
            "auth_method": "password", "host_key_fingerprint": ssh._fingerprint(host_key),
        },
        {"password": "secret", "sudo_password": ""},
        "uptime", 10, False,
    )
    thread.join(timeout=5)
    assert command_seen == ["uptime"]
    assert result["exit_code"] == 0
    assert result["stdout"] == "healthy\n"
    assert result["stderr"] == "notice\n"
    assert result["timed_out"] is False


async def _async_value(value):
    return value


def test_secure_card_posts_secrets_directly_and_chat_dispatch_is_payload_only():
    root = Path(__file__).resolve().parents[1]
    card = (root / "ui/ssh-control/ssh-connection-card.js").read_text(encoding="utf-8")
    activity = (root / "ui/shared/js/chat-activity.js").read_text(encoding="utf-8")
    assert "...authHeaders()" in card
    assert "'/api/v1/ssh-control/connections'" in card
    assert "expected_fingerprint" in card
    assert "_wipeSecrets(card)" in card
    assert "host: existing.host || payload.host || payload.name || ''" in card
    assert "card.querySelector('.sshcc-sub').textContent = payload.message" in card
    assert "_resumeOriginatingChat(payload, connectionName)" in card
    assert "app.sendChatMessage" in card
    assert "originSessionId !== String(app.currentSessionId || '')" in card
    assert "ssh_connection_form" in activity
    assert "renderSshConnectionCard(payload)" in activity
    assert "_SECURE_CARD_HANDOFF_TOOLS" in activity
    assert "password:" not in activity
    assert "private_key:" not in activity


def test_free_and_pro_defaults_do_not_implicitly_grant_ssh():
    root = Path(__file__).resolve().parents[1]
    for name in ("free.json", "pro.json", "anonymous.json"):
        data = json.loads((root / "app/defaults/experience-tiers" / name).read_text(encoding="utf-8"))
        assert "ssh_control" not in data["policy"]["ability_groups"]

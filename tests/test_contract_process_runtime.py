import asyncio
import json
import os
import subprocess
import sys
import time
from unittest.mock import patch


class _FakeProcess:
    def __init__(self, *, reply=None, hang=False, status="done", returncode=0):
        self.returncode = None
        self.reply = reply
        self.hang = hang
        self.status = status
        self.final_returncode = returncode
        self.stdin_payload = b""
        self.terminated = False
        self.killed = False

    async def communicate(self, payload):
        self.stdin_payload = payload
        if self.hang:
            await asyncio.Event().wait()
        self.returncode = self.final_returncode
        return json.dumps({
            "status": self.status, "reply": self.reply or "{}",
            "error": "contract subprocess deadline exceeded" if self.status == "timeout" else "",
            "duration_ms": 7,
        }).encode(), b"child log"

    def terminate(self):
        self.terminated = True
        self.returncode = -15

    def kill(self):
        self.killed = True
        self.returncode = -9

    async def wait(self):
        return self.returncode


def test_contract_process_json_boundary_handles_unicode_on_windows():
    payload = {
        "user_id": "user", "spawn_session_id": "session", "request": "review",
        "permission_profile": "invalid→profile", "generation": "turn",
    }
    env = os.environ.copy()
    env["PYTHONIOENCODING"] = "utf-8"
    env["PYTHONUTF8"] = "1"
    completed = subprocess.run(
        [sys.executable, "-m", "app.agent.contract_worker_process"],
        input=json.dumps(payload, ensure_ascii=False).encode("utf-8"),
        stdout=subprocess.PIPE, stderr=subprocess.PIPE, env=env, check=False,
    )
    result = json.loads(completed.stdout.decode("utf-8"))
    assert completed.returncode == 1
    assert result["status"] == "error"
    assert "invalid→profile" in result["error"]


def test_contract_process_enforces_its_own_absolute_deadline():
    from app.agent import contract_worker_process as worker

    writes = []
    exit_codes = []
    exited = __import__("threading").Event()

    def fake_exit(code):
        exit_codes.append(code)
        exited.set()

    with (
        patch.object(worker.os, "write", new=lambda _fd, data: writes.append(data)),
        patch.object(worker.os, "_exit", new=fake_exit),
    ):
        finished = worker._start_deadline_watchdog({
            "deadline_monotonic": time.monotonic() + 0.03,
        })
        assert exited.wait(0.5)
        finished.set()

    assert exit_codes == [124]
    assert json.loads(writes[0].decode("utf-8"))["status"] == "timeout"


def test_contract_turn_uses_stdin_and_clean_json_stdout_boundary():
    from plugins.abilities.Core.agent_orchestration import agent_orchestration as transport

    async def scenario():
        proc = _FakeProcess(reply='{"decision":"pass"}')
        command = []

        async def create(*args, **kwargs):
            command.extend(args)
            assert kwargs["stdin"] is asyncio.subprocess.PIPE
            assert kwargs["stdout"] is asyncio.subprocess.PIPE
            assert kwargs["stderr"] is asyncio.subprocess.PIPE
            assert kwargs["env"]["PYTHONIOENCODING"] == "utf-8"
            assert kwargs["env"]["PYTHONUTF8"] == "1"
            return proc

        with (
            patch.object(asyncio, "create_subprocess_exec", new=create),
            patch.object(transport, "_spawns_update"),
            patch.object(transport, "_spawns_heartbeat"),
            patch.object(transport, "_heartbeat_loop", new=lambda _sid: asyncio.sleep(3600)),
        ):
            result = await transport.send_contract_request(
                user_id="secret-user", agent_id="agent-1", spawn_id="spawn-1",
                spawn_session_id="worker-session", request="secret-request",
                permission_profile="source_read_only", generation="turn-1", timeout=2,
            )
        assert result["status"] == "done"
        assert result["reply"] == '{"decision":"pass"}'
        assert "secret-user" not in " ".join(str(part) for part in command)
        assert "secret-request" not in " ".join(str(part) for part in command)
        payload = json.loads(proc.stdin_payload)
        assert payload["user_id"] == "secret-user"
        assert payload["permission_profile"] == "source_read_only"
        assert payload["deadline_monotonic"] > 0

    asyncio.run(scenario())


def test_hung_contract_process_is_terminated_and_marked_timeout():
    from plugins.abilities.Core.agent_orchestration import agent_orchestration as transport

    async def scenario():
        proc = _FakeProcess(hang=True)
        statuses = []

        async def create(*_args, **_kwargs):
            return proc

        def update(_spawn_id, **values):
            statuses.append(values.get("status"))

        with (
            patch.object(asyncio, "create_subprocess_exec", new=create),
            patch.object(transport, "_spawns_update", new=update),
            patch.object(transport, "_spawns_heartbeat"),
            patch.object(transport, "_heartbeat_loop", new=lambda _sid: asyncio.sleep(3600)),
        ):
            try:
                await transport.send_contract_request(
                    user_id="u", spawn_id="timeout-spawn",
                    spawn_session_id="worker-session", request="review",
                    timeout=0.01,
                )
            except asyncio.TimeoutError:
                pass
            else:
                raise AssertionError("hung process did not time out")
        assert proc.terminated
        assert statuses[-2:] == ["stopping", "timeout"]
        assert "timeout-spawn" not in transport._CONTRACT_PROCESSES
        assert "timeout-spawn" not in transport._LIVE_RUNS

    asyncio.run(scenario())


def test_child_timeout_result_is_raised_and_spawn_stays_timeout():
    from plugins.abilities.Core.agent_orchestration import agent_orchestration as transport

    async def scenario():
        proc = _FakeProcess(status="timeout", returncode=124)
        statuses = []

        with (
            patch.object(asyncio, "create_subprocess_exec", new=AsyncMock(return_value=proc)),
            patch.object(transport, "_spawns_update", new=lambda _sid, **v: statuses.append(v.get("status"))),
            patch.object(transport, "_spawns_heartbeat"),
            patch.object(transport, "_heartbeat_loop", new=lambda _sid: asyncio.sleep(3600)),
        ):
            try:
                await transport.send_contract_request(
                    user_id="u", spawn_id="child-timeout",
                    spawn_session_id="worker-session", request="review", timeout=2,
                )
            except asyncio.TimeoutError:
                pass
            else:
                raise AssertionError("child timeout was not propagated")
        assert statuses[-1] == "timeout"

    from unittest.mock import AsyncMock
    asyncio.run(scenario())


def test_process_local_permission_profiles_remove_bootstrap_tools(monkeypatch):
    from app.agent.contract_permissions import clamp_runtime_agent, filter_tools

    monkeypatch.setenv("WEBAGENT_CONTRACT_SUBPROCESS", "1")
    monkeypatch.setenv("WEBAGENT_CONTRACT_PERMISSION_PROFILE", "tool_free")
    tools = {name: object() for name in (
        "memory", "load_tool", "read_source", "write_source", "spawn_agent",
    )}
    assert filter_tools(tools) == {}

    monkeypatch.setenv("WEBAGENT_CONTRACT_PERMISSION_PROFILE", "source_read_only")
    assert set(filter_tools(tools)) == {"read_source"}
    runtime = clamp_runtime_agent({
        "metadata": {"manager": {"enabled": True}},
        "loop_logic": [{"node": "interrupt_chk", "enabled": True}],
    })
    nodes = {item["node"]: item["enabled"] for item in runtime["loop_logic"]}
    assert nodes["memory_search"] is False
    assert nodes["memory_save"] is False
    assert nodes["manager_chk"] is False
    assert runtime["metadata"]["manager"]["enabled"] is False

"""Focused read-only wait behavior for the V3 build bridge."""
import asyncio
import importlib.util
import json
from pathlib import Path

import pytest

PATH = Path(__file__).resolve().parents[1] / "plugins/abilities/Administrator/v3_build/v3_build.py"
spec = importlib.util.spec_from_file_location("v3_build_wait_test", PATH)
module = importlib.util.module_from_spec(spec)
spec.loader.exec_module(module)


def run_wait():
    return json.loads(asyncio.run(module.build_tools(user_id="fixture-admin")["v3_build_wait"]("task-1234", 0)))


def receipt(job_id=None):
    return {"own": [{"task_id": "task-1234", "status": "open", "job_id": job_id}]}


def test_early_finisher_has_no_job_yet(monkeypatch):
    calls = []

    async def bridge(user, name, arguments):
        calls.append((name, arguments))
        return receipt()

    monkeypatch.setattr(module, "_bridge_request", bridge)
    assert run_wait()["status"] == "pending"
    assert calls == [("v3_build_work", {"action": "status"})]


@pytest.mark.parametrize("job_status,expected", [("completed", "completed"), ("failed", "failed")])
def test_terminal_job(monkeypatch, job_status, expected):
    calls = []

    async def bridge(user, name, arguments):
        calls.append(name)
        if name == "v3_build_work":
            return receipt("job-1234")
        if name == "v3_worker_job":
            return {"job_id": "job-1234", "status": job_status, "phase": "done", "reason": "bounded"}
        return {"containers": {"api": {"state": "running", "health": "healthy"},
                                "loop": {"state": "running", "health": "healthy"}}}

    monkeypatch.setattr(module, "_bridge_request", bridge)
    result = run_wait()
    assert result["status"] == expected
    assert result["reason"] == "bounded"
    assert calls == (["v3_build_work", "v3_worker_job", "v3_build_status"]
                     if job_status == "completed" else ["v3_build_work", "v3_worker_job"])
    if job_status == "completed":
        assert result["verified_healthy"] is True


def test_timeout_is_pending(monkeypatch):
    async def bridge(user, name, arguments):
        return receipt("job-1234") if name == "v3_build_work" else {"status": "running"}

    monkeypatch.setattr(module, "_bridge_request", bridge)
    assert run_wait()["status"] == "pending"


def test_disconnect_is_pending(monkeypatch):
    async def bridge(user, name, arguments):
        raise OSError("disconnected")

    monkeypatch.setattr(module, "_bridge_request", bridge)
    assert run_wait()["status"] == "pending"


def test_repeated_polling_only_reads(monkeypatch):
    calls = []
    states = iter(["running", "running", "failed"])

    async def bridge(user, name, arguments):
        calls.append((name, arguments))
        return receipt("job-1234") if name == "v3_build_work" else {
            "job_id": "job-1234", "status": next(states), "phase": "building"}

    async def no_delay(seconds):
        return None

    monkeypatch.setattr(module, "_bridge_request", bridge)
    monkeypatch.setattr(module.asyncio, "sleep", no_delay)
    result = json.loads(asyncio.run(module.build_tools(user_id="fixture-admin")[
        "v3_build_wait"]("task-1234", 1)))
    assert result["status"] == "failed"
    assert [name for name, _ in calls] == [
        "v3_build_work", "v3_worker_job", "v3_worker_job", "v3_worker_job"]
    assert all(name in {"v3_build_work", "v3_worker_job"} for name, _ in calls)


def test_finish_holds_original_call_until_terminal_result(monkeypatch):
    calls = []
    states = iter(["queued", "running", "completed"])

    async def bridge(user, name, arguments):
        calls.append((name, arguments))
        if name == "v3_build_work" and arguments["action"] == "finish":
            return {"status": "closed", "job_id": "job-1234"}
        if name == "v3_build_work":
            return receipt("job-1234")
        if name == "v3_worker_job":
            return {"status": next(states), "job_id": "job-1234", "phase": "building_api"}
        return {"containers": {"api": {"state": "running", "health": "healthy"},
                                "loop": {"state": "running", "health": "healthy"}}}

    async def no_delay(seconds):
        return None

    monkeypatch.setattr(module, "_bridge_request", bridge)
    monkeypatch.setattr(module.asyncio, "sleep", no_delay)
    result = json.loads(asyncio.run(module.build_tools(user_id="fixture-admin")[
        "v3_build_finish"]("task-1234", "a" * 64)))
    assert result["status"] == "completed" and result["verified_healthy"] is True
    assert [name for name, args in calls if name == "v3_build_work" and args["action"] == "finish"] == ["v3_build_work"]

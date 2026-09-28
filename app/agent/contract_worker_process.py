"""Isolated entrypoint for one contract-worker chat turn.

The parent orchestration adapter owns process lifetime, deadlines, heartbeats,
and spawn state.  This child only invokes the existing internal chat service for
an already-created worker session and returns one JSON object on stdout.  All
incidental application output is redirected to stderr so stdout remains a
machine-readable IPC channel.
"""

from __future__ import annotations

import asyncio
from contextlib import redirect_stdout
import json
import os
import sys
import threading
import time
from typing import Any, Dict


_PROFILES = {"tool_free", "source_read_only"}


def _start_deadline_watchdog(payload: Dict[str, Any]) -> threading.Event:
    """Enforce the absolute deadline even if model cancellation wedges."""
    finished = threading.Event()
    deadline = float(payload.get("deadline_monotonic") or 0)
    if deadline <= 0:
        return finished

    def _watch() -> None:
        if finished.wait(max(0.0, deadline - time.monotonic())):
            return
        result = {
            "status": "timeout", "reply": "",
            "error": "contract subprocess deadline exceeded",
            "error_type": "TimeoutError", "duration_ms": 0,
        }
        try:
            os.write(sys.stdout.fileno(), json.dumps(
                result, separators=(",", ":"),
            ).encode("utf-8"))
        finally:
            os._exit(124)

    threading.Thread(target=_watch, name="contract-deadline", daemon=True).start()
    return finished


def _configure_ipc_streams() -> None:
    """Force the machine-readable boundary to UTF-8 on every platform."""
    for stream, errors in ((sys.stdin, "strict"), (sys.stdout, "strict"),
                           (sys.stderr, "backslashreplace")):
        reconfigure = getattr(stream, "reconfigure", None)
        if callable(reconfigure):
            reconfigure(encoding="utf-8", errors=errors)


async def _run(payload: Dict[str, Any]) -> Dict[str, Any]:
    started = time.monotonic()
    user_id = str(payload.get("user_id") or "")
    session_id = str(payload.get("spawn_session_id") or "")
    message = str(payload.get("request") or "")
    profile = str(payload.get("permission_profile") or "tool_free")
    generation = str(payload.get("generation") or "")
    if not user_id or not session_id or not message:
        raise ValueError("contract subprocess request is missing required fields")
    if profile not in _PROFILES:
        raise ValueError(f"unknown contract permission profile: {profile}")
    os.environ["WEBAGENT_CONTRACT_PERMISSION_PROFILE"] = profile

    # Reuse the exact authenticated chat implementation without a loopback HTTP
    # request.  The worker clone/session already carries the clamped abilities.
    from starlette.requests import Request
    from app.api.chat import ChatRequest, run_internal_session_turn
    from app.auth.jwt import create_access_token

    # Contract turns cannot launch grandchildren. Permission profiles deny the
    # exposed shell/orchestration tools; these guards also close accidental
    # internal process-launch paths inside the isolated runtime.
    import subprocess as child_process

    def _child_process_denied(*_args: Any, **_kwargs: Any):
        raise PermissionError("contract subprocesses cannot create child processes")

    async def _async_child_process_denied(*_args: Any, **_kwargs: Any):
        raise PermissionError("contract subprocesses cannot create child processes")

    child_process.Popen = _child_process_denied  # type: ignore[assignment]
    asyncio.create_subprocess_exec = _async_child_process_denied  # type: ignore[assignment]
    asyncio.create_subprocess_shell = _async_child_process_denied  # type: ignore[assignment]
    os.system = _child_process_denied  # type: ignore[assignment]

    token = create_access_token(username=user_id, user_id=user_id)
    scope = {
        "type": "http", "http_version": "1.1", "method": "POST",
        "scheme": "http", "path": "/api/v1/chat", "raw_path": b"/api/v1/chat",
        "query_string": b"", "root_path": "",
        "headers": [(b"authorization", f"Bearer {token}".encode("utf-8"))],
        "client": ("127.0.0.1", 0), "server": ("127.0.0.1", 0),
    }
    response = await run_internal_session_turn(
        ChatRequest(message=message, user_id=user_id, session_id=session_id),
        Request(scope),
    )
    reply = str(getattr(response, "reply", "") or getattr(response, "response", "") or "")
    return {
        "status": "done", "reply": reply, "error": "",
        "generation": generation,
        "duration_ms": int((time.monotonic() - started) * 1000),
    }


def main() -> int:
    _configure_ipc_streams()
    started = time.monotonic()
    try:
        raw = sys.stdin.buffer.read()
        payload = json.loads(raw.decode("utf-8"))
        if not isinstance(payload, dict):
            raise ValueError("contract subprocess request must be an object")
        deadline_finished = _start_deadline_watchdog(payload)
        # Libraries and legacy diagnostics occasionally print to stdout.  Keep
        # the IPC stream clean without suppressing those diagnostics entirely.
        with redirect_stdout(sys.stderr):
            result = asyncio.run(_run(payload))
        deadline_finished.set()
        code = 0
    except BaseException as exc:  # process boundary must always return structure
        if "deadline_finished" in locals():
            deadline_finished.set()
        result = {
            "status": "error", "reply": "", "error": str(exc),
            "error_type": type(exc).__name__,
            "duration_ms": int((time.monotonic() - started) * 1000),
        }
        code = 1
    sys.stdout.write(json.dumps(result, ensure_ascii=False, separators=(",", ":")))
    sys.stdout.flush()
    return code


if __name__ == "__main__":
    os.environ["WEBAGENT_CONTRACT_SUBPROCESS"] = "1"
    raise SystemExit(main())

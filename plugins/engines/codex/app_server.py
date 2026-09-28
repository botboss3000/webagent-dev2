"""Small, supervised client for Codex App Server's stdio protocol.

Portal mode deliberately talks to this public protocol instead of reading
``~/.codex/state_*.sqlite`` or rollout JSONL files.  That keeps Codex as the
sole owner of its task store and makes this adapter insensitive to storage
schema changes.
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import shutil
import uuid
from collections import defaultdict
from pathlib import Path
from typing import Any, DefaultDict

logger = logging.getLogger(__name__)


class CodexAppServerError(RuntimeError):
    pass


class CodexAppServer:
    def __init__(self) -> None:
        self._process: asyncio.subprocess.Process | None = None
        self._reader_task: asyncio.Task | None = None
        self._stderr_task: asyncio.Task | None = None
        self._start_lock = asyncio.Lock()
        self._write_lock = asyncio.Lock()
        self._next_id = 0
        self._pending: dict[int, asyncio.Future] = {}
        self._thread_events: DefaultDict[str, list[asyncio.Queue]] = defaultdict(list)

    async def _ensure_started(self) -> None:
        if self._process and self._process.returncode is None:
            return
        async with self._start_lock:
            if self._process and self._process.returncode is None:
                return
            executable = _find_codex_executable()
            if not executable:
                raise CodexAppServerError("Codex CLI is not installed or is not on PATH.")
            self._process = await asyncio.create_subprocess_exec(
                executable,
                "-c",
                "features.code_mode_host=true",
                "app-server",
                stdin=asyncio.subprocess.PIPE,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
                limit=32 * 1024 * 1024,
            )
            self._reader_task = asyncio.create_task(self._read_stdout())
            self._stderr_task = asyncio.create_task(self._read_stderr())
            await self.request("initialize", {
                "clientInfo": {
                    "name": "webagent-codex-portal",
                    "title": "WebAgent Codex Portal",
                    "version": "1.0.0",
                },
                # Current Codex tasks default to paginated history.  The
                # thread/turns/list method is capability-gated, and is the only
                # supported way to read those transcripts.
                "capabilities": {"experimentalApi": True},
            }, ensure_started=False)
            await self.notify("initialized", {}, ensure_started=False)

    async def _write(self, message: dict[str, Any]) -> None:
        process = self._process
        if not process or not process.stdin:
            raise CodexAppServerError("Codex App Server is not running.")
        wire = (json.dumps(message, separators=(",", ":")) + "\n").encode()
        async with self._write_lock:
            process.stdin.write(wire)
            await process.stdin.drain()

    async def request(
        self,
        method: str,
        params: dict[str, Any] | None = None,
        *,
        timeout: float = 45.0,
        ensure_started: bool = True,
    ) -> Any:
        if ensure_started:
            await self._ensure_started()
        self._next_id += 1
        request_id = self._next_id
        future = asyncio.get_running_loop().create_future()
        self._pending[request_id] = future
        try:
            await self._write({"method": method, "id": request_id, "params": params or {}})
            return await asyncio.wait_for(future, timeout=timeout)
        finally:
            self._pending.pop(request_id, None)

    async def notify(
        self, method: str, params: dict[str, Any] | None = None, *, ensure_started: bool = True
    ) -> None:
        if ensure_started:
            await self._ensure_started()
        await self._write({"method": method, "params": params or {}})

    async def _read_stdout(self) -> None:
        assert self._process and self._process.stdout
        try:
            while line := await self._process.stdout.readline():
                try:
                    message = json.loads(line)
                except Exception:
                    logger.debug("Ignoring non-JSON Codex App Server output: %r", line[:300])
                    continue
                if "id" in message and ("result" in message or "error" in message):
                    future = self._pending.get(message.get("id"))
                    if future and not future.done():
                        if message.get("error"):
                            future.set_exception(CodexAppServerError(str(message["error"])))
                        else:
                            future.set_result(message.get("result"))
                    continue
                # App Server may ask the client for approval. Portal turns use
                # approvalPolicy=never, but fail closed if a future server build
                # still asks rather than leaving the server blocked forever.
                if "id" in message and message.get("method"):
                    await self._write({
                        "id": message["id"],
                        "error": {"code": -32001, "message": "Portal does not grant interactive approvals."},
                    })
                    continue
                params = message.get("params") or {}
                thread_id = _event_thread_id(params)
                if thread_id:
                    for queue in list(self._thread_events.get(thread_id, ())):
                        queue.put_nowait(message)
        except asyncio.CancelledError:
            return
        except Exception as exc:
            logger.exception("Codex App Server reader failed: %s", exc)
        finally:
            error = CodexAppServerError("Codex App Server exited.")
            for future in list(self._pending.values()):
                if not future.done():
                    future.set_exception(error)

    async def _read_stderr(self) -> None:
        assert self._process and self._process.stderr
        while line := await self._process.stderr.readline():
            logger.debug("codex app-server: %s", line.decode(errors="replace").rstrip())

    async def list_threads(self, limit: int = 50, cursor: str | None = None) -> dict[str, Any]:
        params: dict[str, Any] = {
            "limit": max(1, min(int(limit), 200)),
            "sortKey": "updated_at",
            "sortDirection": "desc",
            "archived": False,
        }
        if cursor:
            params["cursor"] = cursor
        return await self.request("thread/list", params)

    async def start_thread(
        self,
        *,
        cwd: str,
        model: str | None = None,
        execution_mode: str = "ask",
    ) -> dict[str, Any]:
        params: dict[str, Any] = {
            "cwd": cwd,
            "approvalPolicy": "never",
            "sandbox": _sandbox_mode(execution_mode),
            "historyMode": "paginated",
        }
        if model:
            params["model"] = model
        return await self.request("thread/start", params)

    async def read_thread(self, thread_id: str) -> dict[str, Any]:
        """Read one native task, supporting both Codex history contracts."""
        summary = await self.read_thread_summary(thread_id)
        thread = dict((summary or {}).get("thread") or {})
        if thread.get("historyMode") != "paginated":
            return await self.request(
                "thread/read", {"threadId": thread_id, "includeTurns": True}
            )

        turns: list[dict[str, Any]] = []
        cursor: str | None = None
        while True:
            params: dict[str, Any] = {
                "threadId": thread_id,
                "limit": 100,
                "sortDirection": "asc",
                "itemsView": "full",
            }
            if cursor:
                params["cursor"] = cursor
            page = await self.request("thread/turns/list", params)
            turns.extend((page or {}).get("data") or [])
            cursor = (page or {}).get("nextCursor")
            if not cursor:
                break
        thread["turns"] = turns
        return {"thread": thread}

    async def read_thread_summary(self, thread_id: str) -> dict[str, Any]:
        """Read task metadata without materializing its transcript."""
        return await self.request(
            "thread/read", {"threadId": thread_id, "includeTurns": False}
        )

    async def run_turn(
        self,
        thread_id: str,
        text: str,
        *,
        cwd: str | None = None,
        model: str | None = None,
        effort: str | None = None,
        execution_mode: str = "ask",
        timeout: float = 3600.0,
    ) -> dict[str, Any]:
        queue: asyncio.Queue = asyncio.Queue()
        self._thread_events[thread_id].append(queue)
        owns_writer = False
        try:
            params: dict[str, Any] = {
                "threadId": thread_id,
                "input": [{"type": "text", "text": text}],
                "approvalPolicy": "never",
                "sandboxPolicy": _sandbox_policy(execution_mode),
            }
            if cwd:
                params["cwd"] = cwd
            if model:
                params["model"] = model
            if effort:
                params["effort"] = effort
            try:
                # A thread/start result is already loaded, and it has no rollout
                # to resume until its first turn begins. Starting directly is
                # therefore required for a brand-new Portal task.
                started = await self.request("turn/start", params)
                owns_writer = True
            except CodexAppServerError as exc:
                # After WebAgent restarts, persisted Codex tasks exist on disk but
                # are not loaded in this App Server process. Resume only in that
                # explicit case, then retry the same turn once.
                error_text = str(exc).lower()
                if not any(
                    marker in error_text
                    for marker in ("thread not loaded", "thread not found")
                ):
                    raise
                try:
                    await self.request(
                        "thread/resume", {"threadId": thread_id, "excludeTurns": True}
                    )
                    owns_writer = True
                except CodexAppServerError as resume_exc:
                    # Another Codex client can own the task while its turn is
                    # running. Queue the follow-up instead of stealing or
                    # forking the task's single native writer.
                    if "already has an active writer" not in str(resume_exc).lower():
                        raise
                    queued = await self.request("thread/queue/add", {
                        "threadId": thread_id,
                        "clientUserMessageId": str(uuid.uuid4()),
                        "input": [{"type": "text", "text": text}],
                    })
                    return {"queued": True, "queue": queued}
                started = await self.request("turn/start", params)
            turn_id = str((started or {}).get("turn", {}).get("id") or (started or {}).get("id") or "")
            while True:
                event = await asyncio.wait_for(queue.get(), timeout=timeout)
                if event.get("method") != "turn/completed":
                    continue
                event_params = event.get("params") or {}
                event_turn = event_params.get("turn") or {}
                event_turn_id = str(event_params.get("turnId") or event_turn.get("id") or "")
                if not turn_id or not event_turn_id or event_turn_id == turn_id:
                    if owns_writer:
                        try:
                            await self.request("thread/unsubscribe", {"threadId": thread_id})
                        except CodexAppServerError as exc:
                            logger.warning("Could not release Codex writer for %s: %s", thread_id, exc)
                    return event_params
        finally:
            listeners = self._thread_events.get(thread_id, [])
            if queue in listeners:
                listeners.remove(queue)
            if not listeners:
                self._thread_events.pop(thread_id, None)

    async def interrupt(self, thread_id: str, turn_id: str | None = None) -> Any:
        params: dict[str, Any] = {"threadId": thread_id}
        if turn_id:
            params["turnId"] = turn_id
        return await self.request("turn/interrupt", params)

    async def account(self, *, refresh: bool = False) -> dict[str, Any]:
        """Return Codex's managed account state without reading credential files."""
        result = await self.request("account/read", {"refreshToken": bool(refresh)})
        return result if isinstance(result, dict) else {}

    async def start_chatgpt_login(self) -> dict[str, Any]:
        """Begin the supported headless ChatGPT device-code flow."""
        result = await self.request(
            "account/login/start", {"type": "chatgptDeviceCode"}
        )
        return result if isinstance(result, dict) else {}

    async def logout(self) -> None:
        await self.request("account/logout", {})

    async def models(self) -> list[dict[str, Any]]:
        """Return picker-visible models available to the authorized account."""
        result = await self.request("model/list", {
            "limit": 200,
            "includeHidden": False,
        })
        return list((result or {}).get("data") or [])

    async def rate_limits(self) -> dict[str, Any]:
        result = await self.request("account/rateLimits/read", {})
        return result if isinstance(result, dict) else {}


def _event_thread_id(params: dict[str, Any]) -> str:
    thread = params.get("thread") or {}
    turn = params.get("turn") or {}
    return str(params.get("threadId") or thread.get("id") or turn.get("threadId") or "")


def _sandbox_policy(mode: str) -> dict[str, Any]:
    if mode == "auto":
        return {"type": "dangerFullAccess"}
    if mode == "wkspc":
        return {"type": "workspaceWrite"}
    return {"type": "readOnly"}


def _sandbox_mode(mode: str) -> str:
    if mode == "auto":
        return "danger-full-access"
    if mode == "wkspc":
        return "workspace-write"
    return "read-only"


app_server = CodexAppServer()


def _find_codex_executable() -> str | None:
    """Prefer the desktop app's current CLI over a stale global npm install."""
    override = str(os.environ.get("WEBAGENT_CODEX_PATH") or "").strip()
    if override and Path(override).is_file():
        return override
    local = str(os.environ.get("LOCALAPPDATA") or "").strip()
    if os.name == "nt" and local:
        root = Path(local) / "OpenAI" / "Codex" / "bin"
        candidates = list(root.glob("*/codex.exe")) if root.is_dir() else []
        if candidates:
            return str(max(candidates, key=lambda path: path.stat().st_mtime_ns))
    return shutil.which("codex")

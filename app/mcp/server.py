"""MCP JSON-RPC server — one process per CLI invocation.

Transports:
  stdio (default) — newline-delimited JSON-RPC over stdin/stdout. Spawned by a
      CLI (claude) that connects to it as a child process.
  http            — streamable-HTTP MCP on 127.0.0.1:<port> (POST /). Spawned by
      the codex engine, which hands Codex the URL via `-c mcp_servers.<name>.url`
      config override. Codex then connects over loopback HTTP, so there is no
      child-spawn/path/quoting fragility on Windows.

Usage (spawned by an engine adapter):
    python -m app.mcp.server --user-id <uid> --agent-id <aid> --session-id <sid>
                            [--mode auto|ask|plan] [--transport stdio|http]
                            [--port <n>] [--allowed-tools tool1,tool2]

Implements the three MCP lifecycle methods (initialize, notifications/initialized,
ping) plus tools/list and tools/call.

Tools are resolved lazily on first tools/list or tools/call via
``resolve_tools_for_engine()`` and cached for the process lifetime — the same
freshness guarantee the native loop gets, because the process lives for exactly
one turn. initialize responds instantly so a parent CLI with a short handshake
timeout never fails on tool loading.
"""
from __future__ import annotations

import argparse
import asyncio
import json
import logging
import os
import sys
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any, Dict, Optional

# ── sys.path bootstrap ─────────────────────────────────────────────────────────
# This server is spawned as a child process by a local CLI (claude/codex), which
# may run it from ANY working directory and may not inherit PYTHONPATH. Anchor
# the project root explicitly so the (lazy) app imports below always resolve.
_PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
if _PROJECT_ROOT not in sys.path:
    sys.path.insert(0, _PROJECT_ROOT)

# ── stderr hygiene ─────────────────────────────────────────────────────────────
# stdout is the MCP transport; stderr is diagnostics. A parent CLI that spawns
# this process without draining stderr (Codex does not always) deadlocks the
# handshake once the pipe fills, so by default stderr is discarded. Set
# WEBAGENT_MCP_LOG=<level> to keep diagnostics on stderr for debugging.
_MCP_LOG = os.environ.get("WEBAGENT_MCP_LOG", "").strip().upper()
if _MCP_LOG:
    logging.basicConfig(level=getattr(logging, _MCP_LOG, logging.WARNING),
                        format="%(asctime)s [mcp] %(levelname)s %(message)s",
                        stream=sys.stderr)
else:
    try:
        sys.stderr = open(os.devnull, "w", encoding="utf-8")
    except Exception:
        pass
    logging.disable(logging.CRITICAL)

# App imports follow — they may log at import time (e.g. the Telegram "no token"
# warning), which must land on the discarded stderr above, never on a pipe.
# NOTE: app.mcp.bridge / app.mcp.schemas are deliberately NOT imported here —
# they pull in the whole app stack (takes ~3s) which would blow the parent's
# MCP handshake timeout. They are imported lazily inside _ensure_tools() and
# the tools/list handler instead.

logger = logging.getLogger("mcp.server")

# ── MCP protocol constants (2024-11-05) ────────────────────────────────────────
PROTOCOL_VERSION = "2024-11-05"
SERVER_NAME = "webagent-mcp"
SERVER_VERSION = "1.0.0"

# ── State (resolved once, used for the process lifetime) ───────────────────────
_tools: dict = {}          # name → (handler, info) — from bridge.py
_destructive: set = set()  # tool names flagged destructive
_ask_tools: set = set()    # tool names that require confirmation
_execution_mode: str = "auto"
_tools_initialized = False
_tools_lock = asyncio.Lock()
_expected_turn_id: Optional[str] = None



# ── JSON-RPC parser (line-delimited) ───────────────────────────────────────────

def _read_message() -> Optional[dict]:
    """Read one newline-delimited JSON-RPC message from stdin.

    Returns None when stdin is exhausted or a parse error is unrecoverable.
    """
    while True:
        line = sys.stdin.readline()
        if not line:
            return None  # EOF — parent CLI exited
        line = line.strip()
        if not line:
            continue
        try:
            return json.loads(line)
        except json.JSONDecodeError:
            # MCP spec: servers SHOULD respond with ParseError and continue.
            _write({
                "jsonrpc": "2.0",
                "id": None,
                "error": {"code": -32700, "message": "Parse error"},
            })
            continue


def _write(msg: dict) -> None:
    sys.stdout.write(json.dumps(msg, ensure_ascii=False) + "\n")
    sys.stdout.flush()


def _ok(request_id, result):
    return {"jsonrpc": "2.0", "id": request_id, "result": result}


def _err(request_id, code: int, message: str):
    return {"jsonrpc": "2.0", "id": request_id, "error": {"code": code, "message": message}}


# ── Handlers ───────────────────────────────────────────────────────────────────

async def _ensure_tools() -> None:
    """Pin task generation and resolve tools once for this CLI process."""
    global _tools, _destructive, _ask_tools, _tools_initialized, _expected_turn_id
    async with _tools_lock:
        if _tools_initialized:
            return
        # Heavy imports deferred: they pull in the app stack (~3s), which must not
        # run during the parent's initialize handshake. First tools/list pays it.
        from app.mcp.bridge import resolve_tools_for_engine  # noqa: PLC0415
        from app.db import get_db
        from app.agent.task_state import current_state
        state = current_state(get_db(), _ARGS.session_id)
        _expected_turn_id = str(state["turn_id"]) if state else None

        # Parse any allowed-tools override from the engine (optional comma-separated
        # deny list — same contract as the native loop's allowed_tools).
        _allowed_raw = _ARGS.allowed_tools or ""
        allowed_list = [t.strip() for t in _allowed_raw.split(",") if t.strip()] or None

        agent_tpl = getattr(_ARGS, "agent_template_id", None) or None

        _tools = await resolve_tools_for_engine(
            user_id=_ARGS.user_id,
            agent_id=_ARGS.agent_id,
            session_id=_ARGS.session_id,
            execution_mode=_execution_mode,
            allowed_tools=allowed_list,
            agent_template_id=agent_tpl,
        )

        _destructive = {n for n, (_, info) in _tools.items() if info.get("destructive")}
        _ask_tools = {n for n, (_, info) in _tools.items() if info.get("requires_confirmation")}
        _tools_initialized = True


async def _handle_initialize(msg: dict) -> dict:
    """Respond instantly — heavy tool resolution is deferred to first use."""
    rid = msg.get("id")
    if rid is None:
        return None  # notification — MCP spec says don't respond

    # Echo the client's requested protocol version: Codex's MCP client asks for
    # 2025-06-18 and rejects a hardcoded 2024-11-05 reply (it retries then gives
    # up). Our method surface is identical across both, so echoing is safe.
    _params = msg.get("params") if isinstance(msg.get("params"), dict) else {}
    _client_ver = str(_params.get("protocolVersion") or "")
    _ver = _client_ver if _client_ver.startswith("20") else PROTOCOL_VERSION

    return _ok(rid, {
        "protocolVersion": _ver,
        "capabilities": {"tools": {}},
        "serverInfo": {"name": SERVER_NAME, "version": SERVER_VERSION},
    })


async def _handle_tools_list(msg: dict) -> dict:
    rid = msg.get("id")
    if rid is None:
        return None

    await _ensure_tools()

    from app.mcp.schemas import tool_to_mcp_schema, prune_schema_for_mode  # noqa: PLC0415

    schemas = []
    for name, (_, info) in sorted(_tools.items()):
        schemas.append(tool_to_mcp_schema(
            name=name,
            title=name,
            params=info.get("parameters", {}),
        ))

    schemas = prune_schema_for_mode(schemas, _execution_mode, _destructive, _ask_tools)

    return _ok(rid, {"tools": schemas})


async def _handle_tools_call(msg: dict) -> dict:
    rid = msg.get("id")
    if rid is None:
        return None

    await _ensure_tools()

    params = msg.get("params", {}) if isinstance(msg.get("params"), dict) else {}
    name = str(params.get("name") or "")
    arguments = params.get("arguments", {})
    if not isinstance(arguments, dict):
        arguments = {}

    if not name:
        return _err(rid, -32602, "Missing required parameter: name")

    from app.mcp.bridge import execute_mcp_tool  # noqa: PLC0415

    result = await execute_mcp_tool(
        name=name,
        arguments=arguments,
        tools=_tools,
        user_id=_ARGS.user_id,
        session_id=_ARGS.session_id,
        execution_mode=_execution_mode,
        expected_turn_id=_expected_turn_id,
    )
    return _ok(rid, result)


# ── Main loop ──────────────────────────────────────────────────────────────────

_METHOD_TABLE = {
    "initialize":       _handle_initialize,
    "tools/list":       _handle_tools_list,
    "tools/call":       _handle_tools_call,
    "ping":             lambda _: _ok(None, {}),  # handled inline
}


async def _dispatch(msg: dict) -> Optional[dict]:
    """Route one JSON-RPC message to its handler. Returns the response, or None
    for notifications (no response expected)."""
    method = str(msg.get("method") or "")

    if method == "ping":
        return _ok(msg.get("id"), {})

    if method == "notifications/initialized":
        return None

    handler = _METHOD_TABLE.get(method)
    if handler is None:
        rid = msg.get("id")
        return _err(rid, -32601, f"Method not found: {method}") if rid is not None else None

    try:
        if method in {"tools/list", "tools/call"}:
            # The launcher supplies the authenticated turn owner. Preserve that
            # scope across this subprocess boundary before any raw state access.
            from app.auth.workspace import actor_context, workspace_scope
            with workspace_scope(actor_context(_ARGS.user_id)):
                return await handler(msg)
        return await handler(msg)
    except Exception as exc:
        logger.exception("MCP handler %s failed", method)
        rid = msg.get("id")
        return _err(rid, -32603, f"Internal error: {exc}") if rid is not None else None


async def run_stdio() -> None:
    global _execution_mode

    _execution_mode = _ARGS.mode
    if _execution_mode not in ("auto", "ask", "plan"):
        _execution_mode = "auto"

    while True:
        msg = await asyncio.to_thread(_read_message)
        if msg is None:
            break
        response = await _dispatch(msg)
        if response is not None:
            _write(response)


# ── HTTP transport (streamable MCP over POST /) ───────────────────────────────

_http_loop: Optional[asyncio.AbstractEventLoop] = None


def _start_http_loop() -> None:
    global _http_loop
    if _http_loop is not None:
        return
    loop = asyncio.new_event_loop()
    _http_loop = loop
    threading.Thread(target=loop.run_forever, daemon=True, name="mcp-http-loop").start()


class _McpHttpHandler(BaseHTTPRequestHandler):
    """Minimal streamable-HTTP MCP endpoint: JSON-RPC requests via POST /.

    Only non-streaming responses are produced, which the MCP streamable-HTTP
    spec permits (the client's Accept header includes application/json)."""
    server_version = "webagent-mcp/1.0"

    def do_POST(self) -> None:
        global _execution_mode
        _execution_mode = _ARGS.mode
        if _execution_mode not in ("auto", "ask", "plan"):
            _execution_mode = "auto"
        try:
            ln = int(self.headers.get("Content-Length") or 0)
            raw = self.rfile.read(ln)
            req = json.loads(raw or b"{}") if isinstance(raw, bytes) else json.loads(raw or "{}")
        except Exception:
            resp = {"jsonrpc": "2.0", "id": None,
                    "error": {"code": -32700, "message": "Parse error"}}
        else:
            _is_notification = "id" not in req
            fut = asyncio.run_coroutine_threadsafe(_dispatch(req), _http_loop)
            try:
                resp = fut.result(timeout=300)
            except Exception as exc:
                resp = {"jsonrpc": "2.0", "id": req.get("id"),
                        "error": {"code": -32603, "message": f"Internal error: {exc}"}}
            if _is_notification:
                # MCP streamable HTTP: notifications are answered 202 Accepted
                # with no body (a 200+JSON reply to a notification is rejected
                # by strict clients like Codex's).
                try:
                    self.send_response(202)
                    self.send_header("Content-Length", "0")
                    self.end_headers()
                except (BrokenPipeError, ConnectionResetError):
                    pass
                return
            if resp is None:
                resp = {"jsonrpc": "2.0", "id": req.get("id"), "result": {}}
        payload = json.dumps(resp, ensure_ascii=False).encode("utf-8")
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(payload)))
        self.end_headers()
        try:
            self.wfile.write(payload)
        except (BrokenPipeError, ConnectionResetError):
            pass

    def do_GET(self) -> None:
        # No SSE streams are ever produced; GET has no meaning here.
        self.send_response(405)
        self.send_header("Content-Length", "0")
        self.end_headers()

    def log_message(self, *args) -> None:  # silence request logging
        pass


def run_http(port: int) -> None:
    _start_http_loop()
    srv = ThreadingHTTPServer(("127.0.0.1", port), _McpHttpHandler)
    try:
        srv.serve_forever()
    finally:
        srv.server_close()


# ── CLI entry point ────────────────────────────────────────────────────────────

def _parse_args():
    p = argparse.ArgumentParser(description="WebAgent MCP Bridge (stdio)")
    p.add_argument("--user-id", default=os.environ.get("WEBAGENT_MCP_USER_ID", ""),
                   help="User id (default: $WEBAGENT_MCP_USER_ID)")
    p.add_argument("--agent-id", default=os.environ.get("WEBAGENT_MCP_AGENT_ID", ""),
                   help="Agent id (default: $WEBAGENT_MCP_AGENT_ID)")
    p.add_argument("--session-id", default=os.environ.get("WEBAGENT_MCP_SESSION_ID", ""),
                   help="Session id (default: $WEBAGENT_MCP_SESSION_ID)")
    p.add_argument("--mode", default=os.environ.get("WEBAGENT_MCP_MODE", "auto"),
                   choices=["auto", "ask", "plan"],
                   help="Execution mode (default: auto, or $WEBAGENT_MCP_MODE)")
    p.add_argument("--allowed-tools", default=os.environ.get("WEBAGENT_MCP_ALLOWED_TOOLS", ""),
                   help="Comma-separated tool names to DENY (same as native allowed_tools)")
    p.add_argument("--agent-template-id", default=os.environ.get("WEBAGENT_MCP_AGENT_TEMPLATE_ID", ""),
                   help="Agent template id (optional)")
    p.add_argument("--transport", default="stdio", choices=["stdio", "http"],
                   help="Transport: stdio (default) or http (streamable MCP on 127.0.0.1)")
    p.add_argument("--port", type=int, default=0,
                   help="Port for --transport http (0 = ephemeral; the engine picks and passes it)")
    global _ARGS
    _ARGS = p.parse_args()
    _missing = [k for k in ("user_id", "agent_id", "session_id") if not getattr(_ARGS, k, "")]
    if _missing:
        print(f"Missing required argument(s): {', '.join('--' + m.replace('_', '-') for m in _missing)} "
              f"(pass on the command line or set the WEBAGENT_MCP_* env vars)", file=sys.stderr)
        sys.exit(2)


_ARGS: argparse.Namespace = None   # set by _parse_args()


def main():
    _parse_args()

    # Force UTF-8 on both streams so tool results with non-ASCII text survive.
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8")
    if hasattr(sys.stdin, "reconfigure"):
        sys.stdin.reconfigure(encoding="utf-8")

    try:
        if _ARGS.transport == "http":
            run_http(_ARGS.port)
        else:
            asyncio.run(run_stdio())
    except KeyboardInterrupt:
        pass
    except Exception as exc:
        logger.exception("MCP server terminated: %s", exc)
        sys.exit(1)


if __name__ == "__main__":
    main()

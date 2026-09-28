"""Terminal Control ability — SELF-CONTAINED drop-in.

See app/abilities/__init__.py for the contract. This file declares the ability
(FEATURE / .json) AND carries its full runtime: the terminal-tool factory plus
its handlers, schemas and destructive set all live here — nothing in core.

The tools let an agent drive interactive terminal programs. They wrap the same
persistent PTY session registry the browser terminal panel uses
(``app/api/terminal.py``), so a session an agent opens is a first-class session:
it shows up in the sidebar, survives reconnects, and a human can attach to
**watch or take over** at any time. That shared session is what makes the
"hybrid" experience work — the agent drives headlessly, you can grab the wheel.

The tools are deliberately low-level and program-agnostic so they drive ANY
terminal app — Claude Code, Gemini CLI, Aider, a REPL, an installer prompt:

  terminal_open   — start a program in a new session
  terminal_read   — see what's currently on screen
  terminal_send   — type text / keys into it
  terminal_wait   — block until it reacts and settles (waiting for input)
  terminal_list   — list the caller's live sessions
  terminal_close  — kill a session
  terminal_tunnel — hand the terminal to the user to drive directly (always-on;
                    deliberately not listed in the .json tools so it is not a
                    separately-seeded discoverable tool)

Driving an interactive agent like Claude Code is then a recipe the model runs
with these primitives: open it, wait, read the prompt, answer, wait, read…

Gated by the ``terminal_control`` ability (off by default — this is effectively
shell access). build_tools(...) is injected by app/tools/loader.py only when
that ability is on; the loader reads TOOL_SCHEMAS / DESTRUCTIVE AFTER the call.
"""

from __future__ import annotations

import logging

logger = logging.getLogger(__name__)


# Cap how much screen text we hand back per call, so a chatty program can't blow
# the context window. The agent can ask for more via tail_chars if it must.
_DEFAULT_TAIL = 4000
_MAX_TAIL = 20000


# Populated inside build_tools() from the constants below (the loader reads
# these AFTER calling build_tools), so the gate always matches the runtime.
TOOL_SCHEMAS: dict = {}
DESTRUCTIVE: set = set()


def _err(msg: str, **extra) -> str:
    import json
    out = {"status": "error", "error": msg}
    out.update(extra)
    return json.dumps(out)


def _build_terminal_tools(user_id: str, chat_session_id: str = ""):
    """Return {tool_name: handler} bound to this user. Mirrors the
    build_delegation_tools(user_id) pattern used elsewhere in the loader.

    `chat_session_id` is the live chat session — needed by terminal_tunnel to
    bind *this* conversation to a terminal so the user can drive it directly.
    (The per-tool `session_id` argument is the *terminal* session, distinct from
    this chat session.)"""

    import json
    from typing import List, Optional

    from app.api import terminal as _term

    async def terminal_open(command: str = "", name: str = "", wait: bool = True) -> str:
        """Open a NEW terminal session and optionally run a command in it.

        Use this to launch any interactive program you want to drive — e.g.
        command="claude" to start Claude Code, "python" for a REPL, "aider", or
        leave it blank for a plain shell you'll type into. The session is shown
        to the user in the Terminal panel so they can watch and take over.

        Returns the session_id (needed for every other terminal tool) plus the
        first screen once the program has settled.
        """
        try:
            session_id, sess = await _term.open_agent_session(
                user_id, command=command or "", name=name or "",
            )
        except _term.SessionCapExceeded as e:
            return _err(str(e))
        except Exception as e:
            return _err(f"could not open terminal: {e}")
        screen = ""
        settled = False
        if wait:
            settled = await sess.wait_idle(quiet_secs=0.6, timeout=30.0)
            screen = sess.read_text(tail_chars=_DEFAULT_TAIL)
        return json.dumps({
            "status": "ok",
            "session_id": session_id,
            "name": sess.name,
            "command": sess.launch_command,
            "settled": settled,
            "ended": sess.ended,
            "screen": screen,
            "note": "The user can watch and take over this session in the Terminal panel. "
                    "Use terminal_send/terminal_read with this session_id to drive it.",
        })

    async def terminal_read(session_id: str, tail_chars: int = _DEFAULT_TAIL,
                            raw: bool = False) -> str:
        """Read what's currently on the terminal screen (recent output).

        ANSI colour/cursor codes are stripped by default for readability; pass
        raw=true for the unmodified bytes. Note that for full-screen interactive
        UIs the cleaned text may still contain redraw artifacts."""
        sess = await _term.get_owned_session(session_id, user_id)
        if sess is None:
            return _err("no such session (it may have closed)", session_id=session_id)
        tail = max(1, min(int(tail_chars or _DEFAULT_TAIL), _MAX_TAIL))
        screen = sess.read_text(tail_chars=tail, strip_ansi=not raw)
        return json.dumps({
            "status": "ok", "session_id": session_id,
            "ended": sess.ended, "paused": sess.paused, "screen": screen,
        })

    async def terminal_send(session_id: str, text: str = "", enter: bool = True,
                            keys: Optional[List[str]] = None, wait: bool = True,
                            quiet_secs: float = 0.6, timeout: float = 30.0) -> str:
        """Type input into a terminal session.

        `text` is typed as-is; `enter=true` appends Enter (so a typed command
        runs). `keys` sends named special keys IN ORDER for answering prompts /
        navigating menus: e.g. ["enter"], ["ctrl+c"], ["down","down","enter"],
        ["y","enter"]. Recognised: enter, tab, space, esc, backspace, up, down,
        left, right, home, end, pageup, pagedown, ctrl+c/d/z/l/a/e/u/k/w/r.

        With wait=true (default) it then blocks until the program reacts and
        settles, and returns the resulting screen — so you can act on the
        program's response in the same step."""
        sess = await _term.get_owned_session(session_id, user_id)
        if sess is None:
            return _err("no such session (it may have closed)", session_id=session_id)
        if sess.paused:
            return _err(
                "session is paused — a human has taken over control. Ask them to "
                "resume (or do not send input) until the pause is lifted.",
                session_id=session_id, paused=True,
            )
        if sess.ended:
            return _err("the program in this session has exited", session_id=session_id, ended=True)

        unknown: List[str] = []
        if text:
            sess.send_text(text, enter=False)
        if keys:
            unknown = sess.send_keys(list(keys))
        if enter and text:
            sess.send_keys(["enter"])

        settled = False
        screen = ""
        if wait:
            settled = await sess.wait_idle(
                quiet_secs=max(0.1, float(quiet_secs)),
                timeout=max(0.5, float(timeout)),
            )
            screen = sess.read_text(tail_chars=_DEFAULT_TAIL)
        out = {
            "status": "ok", "session_id": session_id,
            "settled": settled, "ended": sess.ended, "screen": screen,
        }
        if unknown:
            out["unknown_keys"] = unknown
        return json.dumps(out)

    async def terminal_wait(session_id: str, quiet_secs: float = 0.6,
                            timeout: float = 30.0) -> str:
        """Block until the program reacts and then goes quiet for `quiet_secs`
        (i.e. it's done and waiting for input), or `timeout` seconds pass. Use
        this when a command may take a while; then terminal_read the result.
        Returns whether it settled plus the current screen."""
        sess = await _term.get_owned_session(session_id, user_id)
        if sess is None:
            return _err("no such session (it may have closed)", session_id=session_id)
        settled = await sess.wait_idle(
            quiet_secs=max(0.1, float(quiet_secs)),
            timeout=max(0.5, float(timeout)),
        )
        return json.dumps({
            "status": "ok", "session_id": session_id, "settled": settled,
            "ended": sess.ended, "screen": sess.read_text(tail_chars=_DEFAULT_TAIL),
        })

    async def terminal_list() -> str:
        """List your live terminal sessions (agent-opened and human-opened),
        with their names, the command each is running, and whether a human has
        paused/taken one over."""
        sessions = await _term.snapshot_sessions(user_id)
        return json.dumps({"status": "ok", "count": len(sessions), "sessions": sessions})

    async def terminal_close(session_id: str) -> str:
        """Kill a terminal session and the program running in it."""
        closed = await _term.close_session(session_id)
        return json.dumps({"status": "ok" if closed else "error",
                           "closed": closed, "session_id": session_id})

    async def terminal_tunnel(session_id: str) -> str:
        """Hand a terminal session over to the USER to drive directly through chat.

        After you call this, you step aside: the user's chat messages are typed
        straight into the program in `session_id` (NOT sent to you), and the
        program's output streams live into the chat. Use this when the user wants
        to operate a program themselves — e.g. they say "start claude and let me
        drive it" or "let me type into this". Typically: terminal_open the
        program first, then terminal_tunnel its session_id.

        The user ends the tunnel with the "Hand back" button in the chat (they
        cannot end it by typing, since typing goes to the program). While the
        tunnel is active you will not receive their messages, and that traffic is
        kept out of your context — so you can pick back up cleanly afterwards.
        """
        if not session_id:
            return _err("session_id is required (the terminal to hand over)")
        if not chat_session_id:
            return _err("no live chat session to attach a tunnel to (this run "
                        "has no UI session — e.g. a background/event run)")
        from app.agent.terminal_tunnel import enable_tunnel
        from app.db import get_db
        try:
            cfg = await enable_tunnel(
                get_db(), user_id, chat_session_id, session_id, mediated=True,
            )
        except ValueError as e:
            return _err(str(e), session_id=session_id)
        except Exception as e:
            return _err(f"could not start tunnel: {e}", session_id=session_id)
        return json.dumps({
            "status": "ok",
            "tunnel": "active",
            "session_id": session_id,
            "command": cfg.get("command", ""),
            "note": "You are now a pass-through. The user drives this program; "
                    "their messages go to it, not to you, and are excluded from "
                    "your context. Stop here — do not keep calling tools. The "
                    "user ends the tunnel with the 'Hand back' button.",
        })

    return {
        "terminal_open": terminal_open,
        "terminal_read": terminal_read,
        "terminal_send": terminal_send,
        "terminal_wait": terminal_wait,
        "terminal_list": terminal_list,
        "terminal_close": terminal_close,
        "terminal_tunnel": terminal_tunnel,
    }


# JSON-Schema parameter definitions, keyed by tool name. The loader pairs these
# with the handlers above when building ToolInfo entries.
_TERMINAL_TOOL_SCHEMAS = {
    "terminal_open": {
        "type": "object",
        "properties": {
            "command": {"type": "string", "description": "Program/command to run on open, e.g. 'claude', 'python', 'aider'. Blank = plain shell."},
            "name": {"type": "string", "description": "Short label for the session shown to the user (optional)."},
            "wait": {"type": "boolean", "description": "Wait for the program to settle and return the first screen (default true)."},
        },
        "required": [],
    },
    "terminal_read": {
        "type": "object",
        "properties": {
            "session_id": {"type": "string", "description": "The session to read (from terminal_open/terminal_list)."},
            "tail_chars": {"type": "integer", "description": f"How many trailing characters of screen text to return (default {_DEFAULT_TAIL}, max {_MAX_TAIL})."},
            "raw": {"type": "boolean", "description": "Return raw bytes incl. ANSI codes instead of cleaned text (default false)."},
        },
        "required": ["session_id"],
    },
    "terminal_send": {
        "type": "object",
        "properties": {
            "session_id": {"type": "string", "description": "The session to type into."},
            "text": {"type": "string", "description": "Text to type (e.g. a command, or an answer to a prompt)."},
            "enter": {"type": "boolean", "description": "Append Enter after `text` so a command runs (default true)."},
            "keys": {"type": "array", "items": {"type": "string"}, "description": "Named special keys to send in order, e.g. ['ctrl+c'] or ['down','enter']."},
            "wait": {"type": "boolean", "description": "After sending, wait for the program to react and settle, then return the screen (default true)."},
            "quiet_secs": {"type": "number", "description": "Seconds of silence that count as 'settled' (default 0.6)."},
            "timeout": {"type": "number", "description": "Max seconds to wait for settle (default 30)."},
        },
        "required": ["session_id"],
    },
    "terminal_wait": {
        "type": "object",
        "properties": {
            "session_id": {"type": "string", "description": "The session to wait on."},
            "quiet_secs": {"type": "number", "description": "Seconds of silence that count as 'settled' (default 0.6)."},
            "timeout": {"type": "number", "description": "Max seconds to wait (default 30)."},
        },
        "required": ["session_id"],
    },
    "terminal_list": {"type": "object", "properties": {}, "required": []},
    "terminal_close": {
        "type": "object",
        "properties": {
            "session_id": {"type": "string", "description": "The session to kill."},
        },
        "required": ["session_id"],
    },
    "terminal_tunnel": {
        "type": "object",
        "properties": {
            "session_id": {"type": "string", "description": "The terminal session to hand over to the user (from terminal_open/terminal_list)."},
        },
        "required": ["session_id"],
    },
}

# Tools that write/act (vs. read-only) — the loader marks these destructive so
# the write-mode guardrail can require confirmation before the agent launches a
# program or types into one.
_TERMINAL_DESTRUCTIVE = {"terminal_open", "terminal_send", "terminal_close"}


def build_tools(*, user_id: str = "", session_id: str = "", agent_id: str = "",
                agent_template_id: str = "", enabled_providers=None, **_ctx):
    """Return {tool_name: handler} for the terminal-control tools.

    Builds the handler dict from the self-contained factory above, and refreshes
    module-level TOOL_SCHEMAS / DESTRUCTIVE from this module's own constants (the
    loader reads them AFTER calling build_tools). The factory also returns
    `terminal_tunnel` (hands the terminal to the user); it is returned here but
    left out of the .json `tools` list so it stays always-on rather than seeded
    discoverable.
    """
    TOOL_SCHEMAS.clear()
    TOOL_SCHEMAS.update(_TERMINAL_TOOL_SCHEMAS)
    DESTRUCTIVE.clear()
    DESTRUCTIVE.update(_TERMINAL_DESTRUCTIVE)

    return _build_terminal_tools(user_id, session_id)

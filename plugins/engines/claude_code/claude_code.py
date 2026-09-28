"""
Local Claude Code engine — drives the `claude` CLI installed on THIS machine and
renders its output through WebAgent's chat panel.

A "Local Claude Code" agent (built from the `local-claude` template, which carries
metadata.engine = "claude_code") runs its whole turn here instead of WebAgent's
LLM loop. We launch `claude` in headless structured-streaming mode, translate its
stream-json records into WebAgent's chat events, and persist the same interactions
rows the normal loop writes — so the UI, live streaming, and reload all work with
no frontend change.

Behaviour (see temp/claude-code-engine-spec.md + plan):
  • Same authority as running `claude` yourself: act-freely by default, full user
    permissions, NOT folder-jailed. Admin-only at runtime.
  • Starting folder = the configured working directory (a starting point, not a
    fence). Required; the agent refuses with a clear message until it is set.
  • Conversation continuity: the Claude session id is stored in the WebAgent
    session's metadata and resumed each turn, so the thread remembers itself.
  • Resumable in the real CLI: `claude` already writes each headless run's full
    transcript to ~/.claude/projects/<cwd>/<id>.jsonl (so it's resumable by exact
    id), but it does NOT add the prompt to ~/.claude/history.jsonl — the index the
    interactive `claude --resume` PICKER lists sessions from. So after each turn we
    append one faithful entry to that index (see _record_cli_resume_history), making
    a headless WebAgent session show up in the picker with a sensible name and be
    resumable by hand. Append-only on the index; the transcript files `claude` owns
    are never touched. Disable with WEBAGENT_CLAUDE_CLI_HISTORY=0.
  • /compact (one-shot): folds this chat with Context Control, drops the stored
    Claude id, and seeds the NEXT turn's brand-new `claude` session with a compact
    recap — resetting Claude's own runaway context. See compact_restart().
  • Cost is shown informational only — Claude spends against its OWN login; we do
    NOT route it through WebAgent's credit/billing debit path.
  • Pasted images + attached files: the bytes are written to local files under
    data/uploads/claude-attachments/ and their absolute paths are appended to the
    prompt, so `claude` reads them with its own Read tool (images natively). The
    default loop's base64 inlining / vision-describe step is bypassed for this
    agent — see _materialize_attachments below + the engine-agent guards in
    app/api/chat.py.

Windows note: `claude` is spawned with a blocking subprocess.Popen on a worker
thread (never asyncio.create_subprocess_exec — broken on this host's Windows
SelectorEventLoop, same lesson as the tmux helper in app/api/terminal.py). Its
stdout lines are pushed onto an asyncio.Queue the generator drains.
"""

import asyncio
import json
import logging
import os
import re
import shlex
import shutil
import subprocess
import tempfile
import threading
import time
import uuid
from typing import Any, AsyncGenerator, Dict, List, Optional, Tuple

from plugins.engines._heartbeat import start_heartbeat
from plugins.engines._usage import record_engine_usage

logger = logging.getLogger(__name__)

# The id this engine answers to — matched against an agent's metadata.engine by
# the generic dispatch in app/agent/loop.py:stream_agent_events.
ENGINE_ID = "claude_code"

# How long to block on the output queue before re-checking the interrupt flag.
_POLL_TIMEOUT = 0.4

# Where pasted images / attached files are materialized so the local `claude` can
# read them off disk with its own Read tool. Under data/uploads/ (a gitignored
# runtime dir), in a dedicated subfolder so it's easy to prune. Mirrors the
# terminal image-paste relay in app/api/terminal.py.
def _attach_dir() -> str:
    try:
        from app.util.paths import project_root
        root = project_root()
    except Exception:
        root = os.getcwd()
    return os.path.join(root, "data", "uploads", "claude-attachments")

# Reap materialized attachments older than this on each turn so the folder can't
# grow without bound. 0 disables the sweep.
_ATTACH_RETENTION_SECS = int(
    os.environ.get("CLAUDE_CODE_ATTACH_RETENTION_HOURS", "48")
) * 3600

# Env vars that mark a *nested* Claude-Code/SDK child session. They only appear if
# WebAgent itself was launched from within a Claude session; left in the child env
# they confuse a freshly-spawned `claude` (and can hijack its auth). Dropped by
# explicit NAME — never a prefix match, so real config like CLAUDE_CODE_USE_BEDROCK
# / CLAUDE_CODE_USE_VERTEX (and all ANTHROPIC_* auth) is preserved untouched.
_CHILD_ENV_DROP = {
    "CLAUDECODE", "CLAUDE_CODE_ENTRYPOINT", "CLAUDE_CODE_CHILD_SESSION",
    "CLAUDE_CODE_SESSION_ID", "CLAUDE_CODE_SDK_HAS_HOST_AUTH_REFRESH",
    "CLAUDE_CODE_SDK_HAS_OAUTH_REFRESH", "CLAUDE_CODE_OAUTH_SCOPES",
    "CLAUDE_AGENT_SDK_VERSION", "CLAUDE_CODE_EXECPATH",
}


# ── Config + message helpers ────────────────────────────────────────────────

def _read_cfg(agent_rec: Optional[Dict[str, Any]]) -> Dict[str, Any]:
    """Pull the claude_code config block out of the agent's metadata, with
    sane defaults. Metadata may arrive as a dict or a JSON string."""
    meta: Dict[str, Any] = {}
    if agent_rec:
        raw = agent_rec.get("metadata")
        if isinstance(raw, str):
            try:
                meta = json.loads(raw or "{}")
            except (TypeError, ValueError):
                meta = {}
        elif isinstance(raw, dict):
            meta = raw
    cc = meta.get("claude_code") if isinstance(meta.get("claude_code"), dict) else {}
    return {
        "folder": str(cc.get("folder") or "").strip(),
        "extra_flags": str(cc.get("extra_flags") or "").strip(),
        "act_freely": bool(cc.get("act_freely", True)),
        "append_persona": bool(cc.get("append_persona", False)),
        "model": str(cc.get("model") or "").strip(),
        # Reasoning-effort level for the local CLI: claude --effort (low/medium/
        # high/xhigh/max) or codex model_reasoning_effort (minimal/low/medium/high).
        # Empty ⇒ the CLI's own default. Picked from the footer model selector.
        "effort": str(cc.get("effort") or "").strip(),
        # Per-agent "Default chat mode" (Ask/Plan/Auto), shared with normal agents
        # — lives at metadata.default_execution_mode (NOT inside claude_code). Empty
        # ⇒ the admin never opted in, so the legacy act_freely toggle still rules
        # (see _resolve_permission_mode).
        "default_mode": str(meta.get("default_execution_mode") or "").strip().lower(),
        # MCP bridge — expose WebAgent's tool registry to the local CLI.
        "mcp_enabled": bool(cc.get("mcp_enabled", False)),
        "mcp_tool_allowlist": str(cc.get("mcp_tool_allowlist") or "").strip(),
    }


# Claude's `--permission-mode` only goes near the CLI; this maps the WebAgent
# Ask/Plan/Auto vocabulary onto it. Auto keeps the historical --dangerously-skip-
# permissions flag (identical to the old act_freely=True) rather than the
# equivalent --permission-mode bypassPermissions, so nothing changes for trusted
# agents. Ask → default (Claude won't edit/run without approval); Plan → plan
# (read-only, proposes a plan).
_MODE_LEGACY_ALIASES = {"read": "plan", "write": "ask"}


def _resolve_permission_mode(cfg: Dict[str, Any], execution_mode: Any) -> str:
    """Decide the effective chat mode ('ask' | 'plan' | 'auto') for this run.

    Backward compatible: an agent that has never chosen a Default chat mode
    (cfg['default_mode'] blank) keeps the legacy binary behaviour and IGNORES the
    chat pill — existing Claude agents and their automations are untouched
    (act_freely=True ⇒ 'auto', False ⇒ 'ask'). Once an admin picks a mode on the
    Config tab, the agent becomes mode-driven: the per-session pill
    (execution_mode) wins, falling back to the configured default when absent.
    """
    default_mode = cfg.get("default_mode") or ""
    if default_mode not in ("ask", "plan", "auto"):
        return "auto" if cfg.get("act_freely", True) else "ask"
    m = str(execution_mode or "").strip().lower()
    m = _MODE_LEGACY_ALIASES.get(m, m)
    return m if m in ("ask", "plan", "auto") else default_mode


def _coerce_text(user_message: Any) -> str:
    """Reduce the chat message (str, or a list of content blocks, or a dict) to
    the plain text we hand `claude`."""
    if isinstance(user_message, str):
        return user_message
    if isinstance(user_message, list):
        parts: List[str] = []
        for block in user_message:
            if isinstance(block, dict) and block.get("type") == "text":
                parts.append(str(block.get("text") or ""))
            elif isinstance(block, str):
                parts.append(block)
        return "\n".join(p for p in parts if p)
    if isinstance(user_message, dict):
        return str(user_message.get("text") or user_message.get("content") or "")
    return str(user_message or "")


# ── Attachments (pasted images + attached files) → local file paths ─────────
# The default loop inlines images into the model message as base64. The local
# `claude` can't be handed base64 on stdin, but it CAN read any file off disk
# with its own Read tool (images included). So we write each attachment to a real
# file under data/uploads/claude-attachments/ and reference the absolute paths in
# the prompt — giving the agent true vision + true file access, no describe step.

def _safe_filename(name: str) -> str:
    """Strip a user-supplied attachment name down to a safe basename, keeping its
    extension so `claude`'s Read tool recognises the type (.png, .pdf, …)."""
    base = os.path.basename(name or "").strip() or "attachment"
    base = re.sub(r"[^A-Za-z0-9._-]+", "_", base).strip("._") or "attachment"
    return base[:120]


def _prune_old_attachments(dest_dir: str) -> None:
    """Best-effort: delete materialized attachments past the retention window."""
    if _ATTACH_RETENTION_SECS <= 0:
        return
    try:
        cutoff = time.time() - _ATTACH_RETENTION_SECS
        for nm in os.listdir(dest_dir):
            fp = os.path.join(dest_dir, nm)
            try:
                if os.path.isfile(fp) and os.path.getmtime(fp) < cutoff:
                    os.remove(fp)
            except OSError:
                pass
    except OSError:
        pass


async def _materialize_attachments(
    attachment_docs: Optional[List[Dict[str, Any]]]
) -> Tuple[List[Tuple[str, str, str]], List[Tuple[str, str]]]:
    """Write each attachment's bytes to a local file `claude` can read.

    Returns (readable, unreadable):
      • readable   — (abspath, original_name, mime) we wrote to disk.
      • unreadable — (original_name, reason) we couldn't fetch (e.g. the bytes
                     live in the user's browser, not on the server).
    """
    readable: List[Tuple[str, str, str]] = []
    unreadable: List[Tuple[str, str]] = []
    if not attachment_docs:
        return readable, unreadable

    dest_dir = _attach_dir()
    try:
        os.makedirs(dest_dir, exist_ok=True)
    except OSError as e:
        logger.warning("claude_code: could not prepare attachment dir: %s", e)
        for att in attachment_docs:
            unreadable.append((att.get("original_name") or "attachment", "no local storage"))
        return readable, unreadable
    _prune_old_attachments(dest_dir)

    from app.db.attachments import read_file
    for att in attachment_docs:
        name = att.get("original_name") or "attachment"
        mime = (att.get("mime_type") or "").lower()
        provider = att.get("storage_provider") or "local"
        if provider == "browser":
            # Bytes live in the visitor's IndexedDB — the server can't read them.
            unreadable.append((name, "stored in your browser, not on this machine"))
            continue
        storage_path = att.get("storage_path")
        if not storage_path:
            unreadable.append((name, "no stored file"))
            continue
        try:
            data = await read_file(storage_path, storage_provider=provider)
        except Exception as e:
            logger.warning("claude_code: read_file failed for %s: %s", att.get("id"), e)
            data = None
        if not data:
            unreadable.append((name, "could not be read"))
            continue
        abspath = os.path.abspath(
            os.path.join(dest_dir, uuid.uuid4().hex + "-" + _safe_filename(name)))
        try:
            with open(abspath, "wb") as fh:
                fh.write(data)
        except OSError as e:
            logger.warning("claude_code: could not save attachment %s: %s", name, e)
            unreadable.append((name, "could not be saved"))
            continue
        readable.append((abspath, name, mime))
    return readable, unreadable


def _attachment_prompt_suffix(
    readable: List[Tuple[str, str, str]], unreadable: List[Tuple[str, str]]
) -> str:
    """Build the text appended to the prompt telling `claude` where the user's
    attachments live on disk (so it can Read them) and which ones got lost."""
    if not readable and not unreadable:
        return ""
    lines: List[str] = ["", "---", "The user attached the following to this message."]
    if readable:
        lines.append("Read them from these local paths with your Read tool "
                     "(images open as images):")
        for (abspath, name, mime) in readable:
            tag = f" ({mime})" if mime else ""
            lines.append(f'  - "{name}"{tag} → {abspath}')
    if unreadable:
        lines.append("These attachments could not be made available; tell the user "
                     "rather than guessing their contents:")
        for (name, reason) in unreadable:
            lines.append(f'  - "{name}" — {reason}')
    return "\n".join(lines)


def _blocks(message: Any) -> List[Dict[str, Any]]:
    """Content blocks of a stream-json assistant/user message."""
    if isinstance(message, dict):
        content = message.get("content")
        if isinstance(content, list):
            return [b for b in content if isinstance(b, dict)]
        if isinstance(content, str):
            return [{"type": "text", "text": content}]
    return []


def _text_of(blocks: List[Dict[str, Any]]) -> str:
    return "".join(str(b.get("text") or "") for b in blocks if b.get("type") == "text")


def _tool_uses(blocks: List[Dict[str, Any]]) -> List[Tuple[str, str, Dict[str, Any]]]:
    """(tool_use_id, name, input) for each tool_use block."""
    out: List[Tuple[str, str, Dict[str, Any]]] = []
    for b in blocks:
        if b.get("type") == "tool_use":
            args = b.get("input")
            out.append((str(b.get("id") or ""), str(b.get("name") or "tool"),
                        args if isinstance(args, dict) else {}))
    return out


def _result_str(content: Any) -> str:
    """Flatten a tool_result block's content (str or list of text blocks)."""
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        return "".join(str(b.get("text") or "") for b in content
                       if isinstance(b, dict) and b.get("type") == "text")
    return str(content or "")


def _openai_tool_calls(uses: List[Tuple[str, str, Dict[str, Any]]]) -> List[Dict[str, Any]]:
    """Shape tool_use blocks like the OpenAI tool_calls the reload renderer
    expects in an assistant row's output_data."""
    return [{
        "id": tid, "type": "function",
        "function": {"name": name, "arguments": json.dumps(args)},
    } for (tid, name, args) in uses]


# ── Subprocess launch (worker thread → queue) ───────────────────────────────

def _spawn_reader(cmd: List[str], cwd: str, env: Dict[str, str],
                  loop: asyncio.AbstractEventLoop, q: "asyncio.Queue",
                  proc_holder: Dict[str, Any], prompt: str = "") -> threading.Thread:
    """Run `claude` on a daemon thread, pushing ('line', str) for each stdout
    line and finally ('exit', (returncode, stderr)) — or ('fail', message) if it
    couldn't be launched. The user's prompt is fed on STDIN (never as an argv, so
    the Windows .CMD shim can't mangle/inject on special chars). Never uses asyncio
    subprocess (Windows-safe)."""
    def _drain_stderr(proc: "subprocess.Popen", sink: List[str]) -> None:
        try:
            for line in proc.stderr:  # type: ignore[union-attr]
                sink.append(line)
        except Exception:
            pass

    def _worker() -> None:
        try:
            proc = subprocess.Popen(
                cmd, cwd=cwd, env=env,
                stdin=subprocess.PIPE,
                stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                text=True, encoding="utf-8", errors="replace", bufsize=1,
            )
        except FileNotFoundError:
            loop.call_soon_threadsafe(
                q.put_nowait,
                ("fail", "Claude not accessible on this device — the `claude` "
                         "command isn't installed (or not on PATH) on the machine "
                         "running this turn. Install Claude Code there, or set this "
                         "agent's target device to one that has it."))
            return
        except Exception as e:
            loop.call_soon_threadsafe(q.put_nowait, ("fail", f"Could not launch claude: {e}"))
            return
        proc_holder["proc"] = proc
        try:
            if proc.stdin:
                proc.stdin.write(prompt)
                proc.stdin.close()
        except Exception:
            pass
        err_sink: List[str] = []
        t_err = threading.Thread(target=_drain_stderr, args=(proc, err_sink), daemon=True)
        t_err.start()
        try:
            for line in proc.stdout:  # type: ignore[union-attr]
                loop.call_soon_threadsafe(q.put_nowait, ("line", line))
        except Exception as e:
            loop.call_soon_threadsafe(q.put_nowait, ("fail", f"Error reading claude output: {e}"))
            return
        rc = proc.wait()
        t_err.join(timeout=2.0)
        loop.call_soon_threadsafe(q.put_nowait, ("exit", (rc, "".join(err_sink))))

    t = threading.Thread(target=_worker, daemon=True)
    t.start()
    return t


# ── CLI resume-history index ─────────────────────────────────────────────────
# `claude -p` writes a full, resumable transcript per run, but it does NOT record
# the prompt in ~/.claude/history.jsonl — the index the interactive `claude
# --resume` PICKER lists sessions from. So a headless WebAgent session is
# resumable by exact id yet never appears in the menu. We append one entry per
# turn in the CLI's own schema (display / pastedContents / project / sessionId /
# timestamp), so the session shows up in the picker named after its first prompt
# and sorted by recency. Best-effort — any failure is swallowed so it can never
# break a chat turn. Honours CLAUDE_CONFIG_DIR (the same relocation the CLI
# respects); disable entirely with WEBAGENT_CLAUDE_CLI_HISTORY=0. Append-only on
# the index — the transcript files the CLI owns are never modified.
def _claude_config_dir() -> str:
    return (os.environ.get("CLAUDE_CONFIG_DIR")
            or os.path.join(os.path.expanduser("~"), ".claude"))


def _record_cli_resume_history(folder: str, claude_id: str, display: str) -> None:
    """Append one entry to Claude's resume-history index so this headless session
    surfaces in the interactive `claude --resume` picker. No-op on failure."""
    if str(os.environ.get("WEBAGENT_CLAUDE_CLI_HISTORY", "1")).strip().lower() in (
            "0", "false", "no", "off"):
        return
    if not (folder and claude_id):
        return
    try:
        path = os.path.join(_claude_config_dir(), "history.jsonl")
        entry = {
            "display": (display or "(attachment)")[:2000],
            "pastedContents": {},
            "timestamp": int(time.time() * 1000),
            "project": folder,
            "sessionId": claude_id,
        }
        # Write the whole line in one append so a concurrent interactive `claude`
        # appending to the same index can't interleave with ours.
        with open(path, "a", encoding="utf-8") as fh:
            fh.write(json.dumps(entry, ensure_ascii=False) + "\n")
    except Exception as e:
        logger.debug("claude_code: resume-history append failed: %s", e)


# ── /compact "compact & restart" (one-shot, user-driven) ─────────────────────
# A Claude Code agent keeps its memory inside the `claude` CLI's OWN session
# (resumed each turn via --resume), which only ever grows — WebAgent can't trim it.
# The /compact command breaks that chain: WebAgent's Context Control folds THIS
# chat's transcript into a compact recap, we drop the stored Claude id, and the
# user's NEXT message starts a brand-new `claude` session seeded with the recap.
# Claude's runaway context is reset while WebAgent keeps the full transcript
# (nothing is lost). One-shot — the engine resumes the fresh, lighter session
# normally until /compact is run again. compact_restart() is the engine hook the
# core /compact handler calls (app/api/chat.py:_handle_compact_command); the reseed
# branch in stream() below consumes the armed recap.

# The recap builder is shared with the Codex engine (both CLIs keep their own
# session/thread outside WebAgent) — lives in app/agent/session_history.py.
from app.agent.session_history import build_reseed_context  # noqa: E402


async def compact_restart(
    db: Any, user_id: str, session_id: str, agent_id: str,
    info: Optional[Dict[str, Any]] = None,
) -> Dict[str, Any]:
    """Engine /compact override for a Local Claude Code agent (compact & restart).

    Context Control has just folded the older turns (``info`` is its result, or None
    when the chat was short enough that nothing needed folding). We build a compact
    recap of the WHOLE conversation so far (summary cars + verbatim tail), arm it as
    a one-shot reseed, and forget the current Claude id — so the user's NEXT message
    starts a fresh, lighter `claude` session seeded with the recap. Returns the
    user-facing chat reply. Failure-safe: any error returns a plain message and
    leaves the existing session untouched."""
    try:
        from app.agent.session_history import build_openai_history_from_session
        # No agent_id on purpose: Context Control's write side already ran via the
        # forced compaction above; here we only want the passive read (cars + tail).
        msgs = await build_openai_history_from_session(db, user_id, session_id)
        seed = build_reseed_context(msgs)
        if not seed:
            return {
                "status": "noop",
                "message": (
                    "Nothing to carry over yet — this conversation is empty, so there is "
                    "no context to compact. Send a message first, then run /compact."
                ),
            }
        await db.set_session_claude_reseed(session_id, seed)
        if info:
            metrics = info
        else:
            from app.agent.context_control import estimate_tokens
            history_before = estimate_tokens(msgs)
            history_after = estimate_tokens([{"role": "system", "content": seed}])
            metrics = {
                "tokens_before": history_before,
                "tokens_after": min(history_before, history_after),
                "summarised_rows": 0, "new_cars": 0, "segments": 0,
            }
    except Exception as e:
        logger.warning("claude_code compact_restart failed for %s: %s", session_id, e)
        return {"status": "error", "message": f"Couldn't prepare a fresh Claude session — {e}"}
    folded = (info or {}).get("summarised_rows") or 0
    head = (
        f"✅ **Compacted — ready to restart.** Folded {folded} older message(s) into "
        "a summary and prepared a recap of this conversation."
        if folded else
        "✅ **Ready to restart with a clean session.** This conversation was short "
        "enough to keep in full, and a recap is prepared."
    )
    message = (
        f"{head} Your **next message** starts a **fresh Claude session** seeded with "
        "that recap — Claude's own runaway context is reset, while WebAgent keeps the "
        "full transcript (nothing is lost). It then resumes that lighter session "
        "normally until you run `/compact` again."
    )
    return {"status": "success", "message": message, "metrics": metrics}


# ── The engine ──────────────────────────────────────────────────────────────

async def stream(
    *,
    user_id: str,
    session_id: str,
    agent_id: str,
    user_message: Any,
    agent_rec: Optional[Dict[str, Any]] = None,
    db: Any = None,
    system_prompt: str = "",
    channel: Optional[str] = None,
    parent_interaction_id: Optional[str] = None,
    interrupt_event: Optional[asyncio.Event] = None,
    attachment_docs: Optional[List[Dict[str, Any]]] = None,
    execution_mode: str = 'ask',
    persona_prompt: Optional[str] = None,
    **_ignored: Any,
) -> AsyncGenerator[Dict[str, Any], None]:
    """Run one chat turn through the local `claude` CLI, yielding WebAgent chat
    events and persisting the transcript. See module docstring."""

    def _meta(role: str, cost: Optional[float] = None, usage: Optional[Dict] = None,
              model: str = "", message_phase: str = "pending",
              engine_msg_id: Optional[str] = None) -> str:
        m: Dict[str, Any] = {"provider": "claude_code", "model": model, "role": role,
                             "engine": "claude_code", "message_phase": message_phase}
        if engine_msg_id:
            m["engine_message_id"] = engine_msg_id
        if isinstance(usage, dict):
            m["input_tokens"] = usage.get("input_tokens")
            m["output_tokens"] = usage.get("output_tokens")
        if cost is not None:
            m["cost"] = cost
        return json.dumps(m)

    async def _say(msg: str) -> Optional[str]:
        """Persist a clean assistant message so a pre-flight/early outcome shows as a
        normal chat bubble (visible live + on reload), NOT a silent run-error that
        leaves the turn looking stuck/pending."""
        try:
            return await db.insert_interaction(
                user_id, session_id, role="assistant", content=msg,
                parent_id=parent_interaction_id, channel=channel,
                metadata=_meta("assistant", message_phase="main"),
                output_data=json.dumps({"role": "assistant", "content": msg, "tool_calls": []}),
                sender_id=agent_id, receiver_id=user_id)
        except Exception:
            return None

    # 1. Admin gate — same line as every other admin power (a signed-in admin,
    #    including over a tunnel; non-admins and guests refused).
    try:
        is_admin = await db.is_user_admin(user_id)
    except Exception:
        is_admin = False
    if not is_admin:
        _m = "**Local Claude Code** agents are admin-only, and you are not signed in as an admin."
        yield {"type": "response", "level": "agent", "content": _m, "asst_id": await _say(_m)}
        return

    # 2. Resolve config + message.
    cfg = _read_cfg(agent_rec)
    text = _coerce_text(user_message).strip()
    # Clean user prompt captured up-front (before the attachment suffix / reseed
    # recap wrappers mutate `text` below) — used as the display label when we index
    # this session in Claude's CLI resume-history in the finally.
    user_display = text

    # 2b. Materialize any pasted images / attached files to local paths and tell
    #     `claude` where to read them. Images that the default loop would have
    #     inlined as base64 are dropped by _coerce_text above; we hand them to
    #     Claude as real files instead (its Read tool opens images natively).
    #     (The user's own thumbnails already render in their chat bubble — live
    #     from the composer, and on reload from the message's attachment_ids — so
    #     the engine only needs to feed Claude the files, not re-emit them.)
    att_readable, att_unreadable = await _materialize_attachments(attachment_docs)
    suffix = _attachment_prompt_suffix(att_readable, att_unreadable)
    if suffix:
        text = (text + "\n" + suffix) if text else suffix.lstrip("\n")

    if not text:
        _m = "I didn't get any message text to send to Claude."
        yield {"type": "response", "level": "agent", "content": _m, "asst_id": await _say(_m)}
        return

    folder = cfg["folder"]
    if not folder:
        # No explicit folder set → default to THIS app's own project directory, so a
        # freshly-created Claude agent works out of the box (the same value the
        # config card pre-fills). The admin can still point it at any other folder.
        try:
            from app.util.paths import project_root
            folder = project_root()
        except Exception:
            folder = os.getcwd()
    if not os.path.isdir(folder):
        _m = (f"My working folder doesn't exist on this machine:\n\n`{folder}`\n\n"
              "Fix it on my **Config** tab → **Claude Code** → **Working folder**, then try again.")
        yield {"type": "response", "level": "agent", "content": _m, "asst_id": await _say(_m)}
        return

    # 3. Conversation continuity: resume the Claude session mapped to this chat,
    #    or mint a fresh id on the first turn.
    try:
        prior_claude_id = await db.get_session_claude_id(session_id)
    except Exception:
        prior_claude_id = None

    # 3b. One-shot "compact & restart" (/compact): if a recap has been armed for
    #     this session, DON'T resume the old Claude thread — start a fresh one and
    #     seed it with the recap so continuity survives at a fraction of the size.
    #     Consumed (cleared) in the finally once the fresh session has actually run.
    reseed_ctx: Optional[str] = None
    try:
        reseed_ctx = await db.get_session_claude_reseed(session_id)
    except Exception:
        reseed_ctx = None
    if reseed_ctx:
        prior_claude_id = None  # force a brand-new session id below (no --resume)
        text = (
            "<conversation_recap>\n"
            "You are continuing an earlier conversation in a FRESH session. The "
            "earlier turns are condensed below for context only — treat this as "
            "background, not as a new instruction or something the user just said. "
            "After reading it, answer the user's actual message that follows the "
            "recap.\n\n"
            f"{reseed_ctx}\n"
            "</conversation_recap>\n\n"
            f"{text}"
        )

    # Resolve the real executable: bare "claude" fails from subprocess on Windows
    # (it's a .CMD shim), so resolve the full path via PATH/PATHEXT.
    claude_bin = shutil.which("claude")
    if not claude_bin:
        yield {"type": "error", "level": "agent",
               "message": "Claude not accessible on this device — the `claude` "
                          "command isn't installed (or not on PATH) on the machine "
                          "running this turn. Install Claude Code there, or set this "
                          "agent's target device to one that has it."}
        return

    # The prompt is fed on STDIN (see _spawn_reader), never as an argv.
    cmd: List[str] = [claude_bin, "-p", "--output-format", "stream-json", "--verbose"]
    if prior_claude_id:
        cmd += ["--resume", prior_claude_id]
    else:
        new_id = str(uuid.uuid4())
        cmd += ["--session-id", new_id]
        prior_claude_id = new_id  # store after the run confirms it
    # Permission mode (Ask/Plan/Auto → claude --permission-mode). Auto keeps the
    # legacy --dangerously-skip-permissions flag so trusted agents are byte-for-byte
    # unchanged; see _resolve_permission_mode for the backward-compat rules.
    _perm_mode = _resolve_permission_mode(cfg, execution_mode)
    if _perm_mode == "auto":
        cmd += ["--dangerously-skip-permissions"]
    elif _perm_mode == "plan":
        cmd += ["--permission-mode", "plan"]
    else:  # 'ask'
        cmd += ["--permission-mode", "default"]
    if cfg["model"]:
        cmd += ["--model", cfg["model"]]
    if cfg["effort"]:
        cmd += ["--effort", cfg["effort"]]
    # Persona → a temp FILE (so admin-authored text with quotes can't break the
    # Windows .CMD argv). Removed in the finally below.
    persona_file: Optional[str] = None
    if cfg["append_persona"] and system_prompt and system_prompt.strip():
        try:
            # Prefer the persona-only prompt (agent-authored slots without platform
            # boilerplate/tool schemas) when available; fall back to the full prompt.
            _pers = (persona_prompt or "").strip() or system_prompt
            fd, persona_file = tempfile.mkstemp(prefix="wa_persona_", suffix=".txt", text=True)
            with os.fdopen(fd, "w", encoding="utf-8") as fh:
                fh.write(_pers)
            cmd += ["--append-system-prompt-file", persona_file]
        except Exception:
            persona_file = None
    if cfg["extra_flags"]:
        try:
            cmd += shlex.split(cfg["extra_flags"], posix=False)
        except ValueError:
            cmd += cfg["extra_flags"].split()

    # ── MCP bridge (exposes WebAgent tools to the local CLI) ───────────────────
    mcp_proc: Optional[subprocess.Popen] = None
    mcp_config_file: Optional[str] = None
    if cfg["mcp_enabled"]:
        import sys as _sys
        # Invoke the server as a SCRIPT (not `-m`) so it starts from any cwd the
        # CLI spawns it in; server.py bootstraps the project root onto sys.path.
        _mcp_server = os.path.join(os.path.dirname(os.path.dirname(os.path.dirname(
            os.path.dirname(os.path.abspath(__file__))))), "app", "mcp", "server.py")
        _mcp_args = [
            _sys.executable, _mcp_server,
            "--user-id", user_id,
            "--agent-id", agent_id,
            "--session-id", session_id,
            "--mode", _perm_mode,
        ]
        if cfg["mcp_tool_allowlist"]:
            _mcp_args += ["--allowed-tools", cfg["mcp_tool_allowlist"]]
        # Agent template id so MCP tools/list can gate orchestration tools.
        if agent_rec and agent_rec.get("template_id"):
            _mcp_args += ["--agent-template-id", str(agent_rec["template_id"])]

        _mcp_flags = getattr(subprocess, "CREATE_NO_WINDOW", 0) if os.name == "nt" else 0
        try:
            mcp_proc = subprocess.Popen(
                _mcp_args,
                stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                stderr=subprocess.DEVNULL,
                text=True, encoding="utf-8", errors="replace",
                creationflags=_mcp_flags,
            )
        except Exception as _me:
            logger.warning("claude_code: MCP server failed to start: %s", _me)
            mcp_proc = None

        if mcp_proc is not None:
            # Write a temporary .mcp.json Claude Code reads. We use the
            # mcpServers format; the server speaks over the stdio pipes
            # we already opened above.
            try:
                _mcp_fd, mcp_config_file = tempfile.mkstemp(
                    prefix="wa_mcp_", suffix=".json")
                _mcp_cfg = {
                    "mcpServers": {
                        "webagent": {
                            "command": _sys.executable,
                            "args": _mcp_args,
                            "env": {},
                        }
                    }
                }
                with os.fdopen(_mcp_fd, "w", encoding="utf-8") as _fh:
                    json.dump(_mcp_cfg, _fh, ensure_ascii=False)
                cmd += ["--mcp-config", mcp_config_file]
            except Exception as _mce:
                logger.warning("claude_code: MCP config write failed: %s", _mce)
                if mcp_config_file:
                    try: os.unlink(mcp_config_file)
                    except Exception: pass
                    mcp_config_file = None
                # Don't kill the server — it'll exit when stdin closes.
                mcp_proc = None  # disable MCP for this turn

    # Clean child env: drop the Claude-Code nested-session re-entrancy markers so a
    # spawned claude behaves as a top-level session (only matters if WebAgent itself
    # was launched from a Claude session). Auth/login env is left intact — claude
    # uses the SAME login it uses in your terminal.
    env = {k: v for k, v in os.environ.items() if k not in _CHILD_ENV_DROP}
    # If the admin signed in from the Claude card (Config → Sign in to Claude), use
    # that saved LONG-LIVED token: inject it as CLAUDE_CODE_OAUTH_TOKEN so claude
    # authenticates with it regardless of the keychain's daily-expiring OAuth (the
    # recurring 401 cause). No saved token → fall back to claude's own login.
    try:
        from .claude_code_login import get_stored_oauth_token
        _oauth = await get_stored_oauth_token()
        if _oauth:
            env["CLAUDE_CODE_OAUTH_TOKEN"] = _oauth
    except Exception:
        pass

    loop = asyncio.get_event_loop()
    q: "asyncio.Queue" = asyncio.Queue()
    proc_holder: Dict[str, Any] = {}
    _spawn_reader(cmd, folder, env, loop, q, proc_holder, prompt=text)

    # ── Per-turn state ──
    captured_id: Optional[str] = None      # session id claude reports
    last_msg_id: Optional[str] = None      # claude message id (msg_…) of the newest assistant record
    last_text: str = ""                    # text of the latest no-tool assistant msg
    responded = False                      # have we emitted the final `response`?
    final_cost: Optional[float] = None
    final_usage: Optional[Dict[str, Any]] = None
    final_model: str = cfg["model"]
    # tool_use_id → (assistant_row_id, tool_name, input) so the tool result row
    # parents to the right step and the tool_result event names the right tool.
    pending_tools: Dict[str, Tuple[str, str, Dict[str, Any]]] = {}

    async def _persist_final(content: str) -> str:
        return await db.insert_interaction(
            user_id, session_id, role="assistant", content=content,
            parent_id=parent_interaction_id, channel=channel,
            metadata=_meta("assistant", final_cost, final_usage, final_model,
                           message_phase="main", engine_msg_id=last_msg_id),
            output_data=json.dumps({"role": "assistant", "content": content, "tool_calls": []}),
            sender_id=agent_id, receiver_id=user_id,
        )

    async def _flush_held():
        """Emit any held intermediate text (a plain no-tool assistant message that
        is NOT the final answer) as its own chat bubble — persisted so reload shows
        it and streamed so it appears live. Called when the next message arrives, so
        only the *last* held text survives to become the authoritative final reply.
        Without this, Claude's running commentary between actions was overwritten and
        only the final message was ever shown."""
        nonlocal last_text
        txt = last_text
        last_text = ""
        if not (txt and txt.strip()):
            return
        rid = await db.insert_interaction(
            user_id, session_id, role="assistant", content=txt,
            parent_id=parent_interaction_id, channel=channel,
            metadata=_meta("assistant", model=final_model, message_phase="progress",
                           engine_msg_id=last_msg_id),
            output_data=json.dumps({"role": "assistant", "content": txt, "tool_calls": []}),
            sender_id=agent_id, receiver_id=user_id,
        )
        yield {"type": "db", "level": "db", "op": "insert_interaction", "role": "assistant", "id": rid}
        yield {"type": "stream", "level": "agent", "content": txt, "asst_id": rid}
        yield {"type": "agent_step_end", "level": "agent", "message_phase": "progress",
               "asst_id": rid, "content": txt}

    # Background heartbeat keeps the liveness watchdog alive during the
    # long-running child-process wait. See plugins/engines/_heartbeat.py.
    _hb = start_heartbeat(db, session_id)

    try:
        while True:
            # Stop button → kill the child and bail.
            if interrupt_event is not None and interrupt_event.is_set():
                p = proc_holder.get("proc")
                if p and p.poll() is None:
                    try:
                        p.terminate()
                    except Exception:
                        pass
                yield {"type": "interrupted", "level": "agent",
                       "message": "Stopped by user."}
                responded = True
                break

            try:
                kind, payload = await asyncio.wait_for(q.get(), timeout=_POLL_TIMEOUT)
            except asyncio.TimeoutError:
                continue

            if kind == "fail":
                # Couldn't launch claude (missing / unrunnable) — surface as a
                # readable reply, not a silent error.
                yield {"type": "response", "level": "agent",
                       "content": str(payload), "asst_id": await _say(str(payload))}
                responded = True
                break

            if kind == "exit":
                rc, err = payload
                if not responded:
                    if last_text:
                        asst_id = await _persist_final(last_text)
                        yield {"type": "db", "level": "db", "op": "insert_interaction", "role": "assistant", "id": asst_id}
                        yield {"type": "response", "level": "agent",
                               "content": last_text, "asst_id": asst_id}
                    else:
                        msg = (("Claude couldn't complete the request:\n\n" + err.strip()[:1500])
                               if err and err.strip()
                               else f"Claude Code exited without a reply (code {rc}).")
                        yield {"type": "response", "level": "agent",
                               "content": msg, "asst_id": await _say(msg)}
                    responded = True
                break

            if kind != "line":
                continue

            line = (payload or "").strip()
            if not line:
                continue
            try:
                rec = json.loads(line)
            except (TypeError, ValueError):
                continue  # non-JSON noise (shouldn't happen in stream-json)
            if not isinstance(rec, dict):
                continue

            rtype = rec.get("type")
            if rec.get("session_id"):
                captured_id = rec.get("session_id")

            # ── system / init: capture tools + session id ──
            if rtype == "system":
                tools = rec.get("tools")
                if isinstance(tools, list) and tools:
                    yield {"type": "pipeline", "level": "pipeline",
                           "step": "load_tools", "count": len(tools),
                           "names": [str(t) for t in tools]}
                if rec.get("model"):
                    final_model = str(rec.get("model"))
                continue

            # ── assistant: text steps + tool calls ──
            if rtype == "assistant":
                msg = rec.get("message") or {}
                _mid = str(msg.get("id") or "") or None  # claude msg_… id
                if msg.get("model"):
                    final_model = str(msg.get("model"))
                blocks = _blocks(msg)
                text_part = _text_of(blocks)
                uses = _tool_uses(blocks)

                if uses:
                    # A tool round begins — flush any earlier narration text that was
                    # being held, so it shows as its own bubble before the tool cards.
                    async for ev in _flush_held():
                        yield ev
                    # Intermediate step: persist the assistant row (with its tool
                    # calls) so reload pairs results to it, then surface tool cards.
                    outp = json.dumps({"role": "assistant", "content": text_part,
                                       "tool_calls": _openai_tool_calls(uses)})
                    asst_id = await db.insert_interaction(
                        user_id, session_id, role="assistant", content=text_part,
                        parent_id=parent_interaction_id, channel=channel,
                        metadata=_meta("assistant", model=final_model,
                                       message_phase="progress", engine_msg_id=_mid),
                        output_data=outp, sender_id=agent_id, receiver_id=user_id,
                    )
                    if _mid:
                        last_msg_id = _mid
                    yield {"type": "db", "level": "db", "op": "insert_interaction", "role": "assistant", "id": asst_id}
                    if text_part.strip():
                        yield {"type": "stream", "level": "agent",
                               "content": text_part, "asst_id": asst_id}
                        yield {"type": "agent_step_end", "level": "agent",
                               "message_phase": "progress",
                               "asst_id": asst_id, "content": text_part}
                    for (tid, name, args) in uses:
                        pending_tools[tid] = (asst_id, name, args)
                        yield {"type": "tool_call", "level": "agent",
                               "tool": name, "args": args, "tool_call_id": tid}
                else:
                    # No tools → this is (probably) the final answer. Hold it; the
                    # `result` record provides the authoritative final text. But if a
                    # PRIOR text was already held, that one was just intermediate
                    # narration — flush it as its own bubble before holding this one.
                    if text_part:
                        async for ev in _flush_held():
                            yield ev
                        last_text = text_part
                        if _mid:
                            last_msg_id = _mid
                continue

            # ── user: tool results coming back ──
            if rtype == "user":
                msg = rec.get("message") or {}
                for b in _blocks(msg):
                    if b.get("type") != "tool_result":
                        continue
                    tid = str(b.get("tool_use_id") or "")
                    is_err = bool(b.get("is_error"))
                    result_text = _result_str(b.get("content"))
                    asst_id, name, args = pending_tools.get(tid, (parent_interaction_id, "tool", {}))
                    try:
                        await db.insert_interaction(
                            user_id, session_id, role="tool", content=result_text,
                            parent_id=asst_id, tool_call_id=tid or None,
                            tool_name=name, channel=channel,
                            metadata=json.dumps({"success": not is_err, "duration_ms": 0,
                                                 "input_params": args,
                                                 "error_message": "tool error" if is_err else None}),
                            output_data=json.dumps({"role": "tool", "content": result_text,
                                                    "tool_call_id": tid, "name": name,
                                                    "success": not is_err}),
                            sender_id=agent_id, receiver_id=agent_id,
                        )
                    except Exception as _pe:
                        logger.debug("claude_code tool-row persist failed: %s", _pe)
                    yield {"type": "tool_result", "level": "agent", "tool": name,
                           "result": result_text[:4000], "duration_ms": 0,
                           "error": is_err, "tool_call_id": tid}
                continue

            # ── result: final answer + informational cost ──
            if rtype == "result":
                cost = rec.get("total_cost_usd")
                final_cost = float(cost) if isinstance(cost, (int, float)) else None
                u = rec.get("usage")
                final_usage = u if isinstance(u, dict) else None
                final_text = rec.get("result")
                if not isinstance(final_text, str) or not final_text.strip():
                    # No authoritative result text → the held narration IS the reply.
                    final_text = last_text
                elif last_text.strip() and final_text.strip() != last_text.strip():
                    # Claude printed running narration (held in last_text) and then a
                    # DIFFERENT final summary — show that narration as its own bubble
                    # first so no intermediate message is lost.
                    async for ev in _flush_held():
                        yield ev
                last_text = ""  # consumed as the final reply; never re-emit at exit
                # Turn claude's raw auth failure into actionable guidance. The
                # local `claude` login (its stored Claude.ai OAuth token) is
                # expired/invalid for a headless run, so it 401s before doing
                # anything. WebAgent can't fix that — the user must re-sign-in the
                # CLI itself. (The interactive Claude Code app authenticates
                # separately, so it won't refresh this token.)
                _auth_err = bool(rec.get("is_error")) and (
                    rec.get("api_error_status") == 401
                    or "authenticat" in (final_text or "").lower()
                    or "401" in (final_text or ""))
                if _auth_err:
                    final_text = (
                        "**Claude Code couldn't sign in on this machine.** The saved "
                        "Claude login looks expired or invalid, so it returned a 401 "
                        "before it could run.\n\n"
                        "Fix it right here: open my **Config** and click **Sign in to "
                        "Claude**, then authorize in your browser and paste the code "
                        "back. That saves a long-lived login and I'll work again — no "
                        "restart needed.")
                asst_id = await _persist_final(final_text or "")
                yield {"type": "db", "level": "db", "op": "insert_interaction", "role": "assistant", "id": asst_id}
                # Real context size for the footer's ctx chip: the whole input side
                # this turn — fresh input + both cache tiers — which is what claude
                # actually had in context, not WebAgent's chars/4 estimate. This is
                # the exact number the ctx pill should show, so it rides llm_call_end
                # (the pill's single source of truth) AND is persisted to the usage
                # ledger below so a reload shows the same figure.
                _u = final_usage or {}
                _ctx_tokens = ((_u.get("input_tokens") or 0)
                               + (_u.get("cache_read_input_tokens") or 0)
                               + (_u.get("cache_creation_input_tokens") or 0))
                # Informational cost/usage chip — NOT charged to WebAgent credits.
                yield {"type": "pipeline", "level": "pipeline", "step": "llm_call_end",
                       "model": final_model,
                       "input_tokens": _ctx_tokens,
                       "output_tokens": _u.get("output_tokens"),
                       "cost_usd": final_cost,
                       "duration_ms": rec.get("duration_ms")}
                # Persist the exact tokens to the usage ledger (source='chat', zero
                # cost) so the ctx pill + IN/OUT counters survive a reload. The
                # engine is billed by the user's own CLI account, never WebAgent
                # credits, so this must NOT go through the charge path.
                await record_engine_usage(
                    model=final_model or "",
                    input_tokens=_ctx_tokens,
                    output_tokens=int(_u.get("output_tokens") or 0),
                    session_id=session_id, user_id=user_id, agent_id=agent_id,
                )
                if _ctx_tokens:
                    yield {"type": "pipeline", "level": "pipeline",
                           "step": "context_status", "tokens": _ctx_tokens}
                yield {"type": "response", "level": "agent", "message_phase": "main",
                       "content": final_text or "", "asst_id": asst_id}
                responded = True
                # keep reading until exit so we capture the session id cleanly
                continue

    except Exception as e:
        logger.exception("claude_code engine error")
        if not responded:
            yield {"type": "error", "level": "agent",
                   "message": f"Local Claude Code engine error: {e}"}
        responded = True
    finally:
        _hb.cancel()

        # Persist the Claude conversation id for this chat so the next turn resumes.
        store_id = captured_id or prior_claude_id
        if store_id:
            try:
                await db.set_session_claude_id(session_id, store_id, folder)
            except Exception as _se:
                logger.debug("claude_code session-id store failed: %s", _se)
        # Index this turn in the CLI's resume-history so the session shows up in the
        # `claude --resume` picker. Gated on captured_id: only when `claude` actually
        # ran and emitted a session id (⇒ a transcript exists to resume) — never for
        # a failed launch, whose minted id has no transcript behind it.
        if captured_id:
            _record_cli_resume_history(folder, captured_id, user_display)
        # Consume a one-shot reseed only once the fresh session actually produced an
        # id — so a failed restart retries the recap next turn instead of losing it.
        if reseed_ctx and store_id:
            try:
                await db.clear_session_claude_reseed(session_id)
            except Exception as _ce:
                logger.debug("claude_code reseed clear failed: %s", _ce)
        if persona_file:
            try:
                os.unlink(persona_file)
            except Exception:
                pass
        if mcp_config_file:
            try:
                os.unlink(mcp_config_file)
            except Exception:
                pass
        if mcp_proc is not None and mcp_proc.poll() is None:
            try:
                mcp_proc.terminate()
            except Exception:
                pass

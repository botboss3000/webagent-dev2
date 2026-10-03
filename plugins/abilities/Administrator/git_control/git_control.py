"""Git Control ability - SELF-CONTAINED drop-in.

Structured git operations only (no shell / file / python access) - the entire
surface is repository_access, git_tool, resolve_conflict and commit_and_push.
Discovery lives in the sibling repository_access module; operation handlers are
defined HERE, in this ability file (`_inject_git_tools` below): Git Control is
the only thing that uses them, so they are NOT kept in the shared admin library.
Only genuinely shared, multi-use infra is reused from `plugins/admin`: the
`adapter.extract_injected` bridge (used by every admin ability) and the low-level
path/subprocess helpers (`_safe_path` / `_write_file_direct` / `_run_subprocess`,
which the file & shell tools use too). `extract_injected` runs the injector
against a throwaway dict and adapts its ToolInfo objects into the (handlers,
schemas, destructive) triple the generic drop-in loader contract wants, so
schemas/flags never drift.

Discovered generically by core (see app/tools/loader.py "Self-contained ability
tools"): build_tools() returns its handlers and the loader reads the
module-level TOOL_SCHEMAS / DESTRUCTIVE populated below AFTER the call.

Background watcher (the "Notify me about repo changes" setting):
  When an agent has Git Control enabled AND its ``notify_repo_changes`` setting
  turned on, this ability's ``start_background`` service quietly polls the repo
  on a cadence and, when the working tree newly diverges from the latest commit
  (uncommitted edits) or local commits sit unpushed, drops a PASSIVE heads-up
  message into that agent's most-recent chat session. No agent turn runs — it's
  an FYI, persisted so it shows when the user returns to that chat and broadcast
  live (``repo_change_notice``) so an open tab renders it immediately. Started/
  stopped by the leader-gated "ability-background" service in app/main.py via the
  generic ``background_service_hooks`` discovery — no core wiring. See
  ui/shared/js/agentWs.js (case 'repo_change_notice') for the front-end render.
"""

from __future__ import annotations

import asyncio
import json
import logging

logger = logging.getLogger(__name__)

# Populated inside build_tools() from the injected ToolInfo objects (the loader
# reads these AFTER calling build_tools, so populating them there keeps them
# from drifting).
TOOL_SCHEMAS: dict = {}
DESTRUCTIVE: set = set()


def build_tools(*, user_id: str = "", session_id: str = "", agent_id: str = "",
                agent_template_id: str = "", enabled_providers=None, **_ctx):
    """Return {tool_name: handler} for the structured git tools.

    Imports stay LAZY so merely importing this module stays cheap.
    """
    from plugins.admin.adapter import extract_injected

    handlers, schemas, destructive = extract_injected(_inject_git_tools, user_id)

    # Discovery belongs to Git Control; shared credentials stay server-side.
    from plugins.abilities.Administrator.git_control.repository_access import (
        repository_access, SCHEMA as repository_schema,
    )

    async def _repository_access(verify_remote: bool = True):
        return json.dumps(await repository_access(
            verify_remote=verify_remote, user_id=user_id, agent_id=agent_id,
        ))

    handlers["repository_access"] = _repository_access
    schemas["repository_access"] = dict(repository_schema)

    TOOL_SCHEMAS.clear()
    TOOL_SCHEMAS.update(schemas)
    DESTRUCTIVE.clear()
    DESTRUCTIVE.update(destructive)
    return handlers


def _inject_git_tools(tools: dict, user_id: str) -> None:
    """Build the structured git tools into ``tools`` as ToolInfo objects - the
    whole surface of the Git Control ability (no shell / file / python access).

    The git-specific code (operation allow-lists, the conflict-marker resolver,
    the one-shot commit+push wrapper) lives right here. Only the low-level path
    and subprocess helpers are reused from the shared admin library, because the
    file & shell tools use them too. Imports stay LAZY so importing this module
    for discovery pulls no heavy deps."""
    import re
    from app.tools.loader import ToolInfo
    from plugins.admin.source_tools import (
        _safe_path, _write_file_direct, _run_subprocess,
    )

    # ── git_tool: structured git operations ──
    def _checkout_context():
        from app.tools.execution_context import current_tool_context
        return current_tool_context()

    async def _git_preflight(operation: str, checkout_paths):
        ctx = _checkout_context()
        worktree_ops = {
            "pull", "reset", "checkout", "restore", "switch", "merge", "rebase",
            "cherry-pick", "revert", "stash apply", "stash pop", "clean", "mv", "rm", "apply",
        }
        if (not ctx or not ctx.require_file_checkout or not ctx.write_capable_mode
                or operation not in worktree_ops):
            return None, []
        declared = list(dict.fromkeys(checkout_paths or []))
        if not declared:
            raise RuntimeError(
                f"git {operation} may mutate repository files; declare checkout_paths first."
            )
        from app.agent.file_checkout import preflight_write
        from app.agent.session_changes import capture_tool_state_async
        await preflight_write(ctx.session_id, declared)
        return await capture_tool_state_async("git_tool", {}), declared

    async def _git_audit(before, declared):
        if before is None:
            return
        ctx = _checkout_context()
        from app.agent.file_checkout import approve_write_result, mark_violation, normalize_path
        from app.agent.session_changes import capture_tool_state_async
        after = await capture_tool_state_async("git_tool", {}) or {}
        changed = (
            {path for path, value in after.items() if before.get(path) != value}
            | {path for path in before if path not in after}
        )
        allowed = {normalize_path(path)[0] for path in declared}
        undeclared = sorted(changed - allowed)
        if undeclared:
            await mark_violation(
                ctx.session_id, undeclared,
                "Git changed undeclared repository paths; reread and explicitly reconcile."
            )
        approved = sorted(changed & allowed)
        if approved:
            await approve_write_result(ctx.session_id, approved)

    async def _git_tool(operation: str, args: str = "", checkout_paths=None) -> str:
        """Run a structured git operation. Read-only by default; mutating needs confirmation."""
        safe_ops = {"status", "log", "diff", "show", "branch", "stash", "remote",
                    "config", "ls-files", "rev-parse", "blame", "describe",
                    "for-each-ref", "reflog", "cat-file", "fsck", "shortlog",
                    "name-rev", "tag", "worktree"}
        mutating_ops = {"add", "commit", "push", "pull", "fetch", "reset",
                        "checkout", "restore", "switch", "merge", "rebase",
                        "cherry-pick", "revert", "stash apply", "stash pop",
                        "stash drop", "clean", "mv", "rm", "apply"}

        op = operation.lower().strip()
        if op not in safe_ops and op not in mutating_ops:
            known = ', '.join(sorted(safe_ops | mutating_ops))
            return f"Error: unknown git operation '{operation}'. Known: {known}"

        before, declared = await _git_preflight(op, checkout_paths)
        git_args = op.split()  # supports "stash apply", "stash pop", etc.
        if args:
            import shlex
            git_args.extend(shlex.split(args))

        # Run through the same helper the Source Control UI's commit/push buttons
        # use, so the agent authenticates identically (GitHub token injected for
        # network ops). `_pin_to_project_root()` forces git back to THIS app's repo
        # + shared key, so an agent never follows whatever other repo the user has
        # selected on the Source Control page. Fall back to a plain subprocess if
        # the github module can't be imported.
        try:
            from app.api.github import _run_git, _pin_to_project_root
            _pin_to_project_root()
            stdout, stderr, exit_code = _run_git(git_args, timeout=60)
        except Exception:
            result = _run_subprocess(["git"] + git_args, timeout=60)
            stdout, stderr, exit_code = result["stdout"], result["stderr"], result["exit_code"]

        await _git_audit(before, declared)
        output = stdout or stderr
        if exit_code != 0 and not output:
            output = f"Command failed (exit {exit_code})"

        return f"[Git {op}] " + (output.strip() or "(no output)")

    tools["git_tool"] = ToolInfo(
        name="git_tool",
        handler=_git_tool,
        parameters={
            "type": "object",
            "properties": {
                "operation": {
                    "type": "string",
                    "description": (
                        "Git operation. Read-only (run without a confirmation pause): "
                        "status, log, diff, show, ls-files, rev-parse, blame, reflog, "
                        "describe, for-each-ref, cat-file, shortlog, name-rev. "
                        "Confirmation-gated (pause for the user in Ask/Plan mode): add, "
                        "commit, push, pull, fetch, reset, checkout, restore, switch, "
                        "merge, rebase, cherry-pick, revert, stash, clean — plus branch, "
                        "tag, remote, config, worktree (these can mutate with flags, so "
                        "they confirm too). "
                        "For continue/abort on conflicts pass via args (e.g. operation='cherry-pick', args='--continue')."
                    ),
                },
                "args": {"type": "string", "description": "Additional arguments (e.g. '--oneline -5' for log, '--continue' for cherry-pick)", "default": ""},
                "checkout_paths": {"type": "array", "items": {"type": "string"},
                                   "description": "Checked-out paths a worktree-mutating Git operation may change."},
            },
            "required": ["operation"],
        },
    )

    # ── resolve_conflict: strip merge markers and keep chosen side ──
    _CONFLICT_RE = re.compile(
        r"^<{7} .*?\n(.*?)^={7}\n(.*?)^>{7} .*?\n",
        re.DOTALL | re.MULTILINE,
    )

    async def _resolve_conflict(path: str, choice: str = "ours") -> str:
        """Resolve all conflict markers in a file by keeping one side.

        choice: 'ours' (keep <<<<<<< side), 'theirs' (keep >>>>>>> side),
        or 'both' (keep both sides concatenated, markers removed)."""
        ctx = _checkout_context()
        if ctx and ctx.require_file_checkout and ctx.write_capable_mode:
            from app.agent.file_checkout import preflight_write
            await preflight_write(ctx.session_id, [path])
        safe = _safe_path(path)
        if not safe:
            return "Error: path is outside the project root"
        if not safe.exists():
            return f"Error: file not found: {path}"
        text = safe.read_text(encoding="utf-8")

        choice = choice.lower().strip()
        if choice not in {"ours", "theirs", "both"}:
            return "Error: choice must be 'ours', 'theirs', or 'both'"

        count = 0

        def _sub(match):
            nonlocal count
            count += 1
            ours, theirs = match.group(1), match.group(2)
            if choice == "ours":
                return ours
            if choice == "theirs":
                return theirs
            return ours + theirs

        new_text = _CONFLICT_RE.sub(_sub, text)
        if count == 0:
            return f"No conflict markers found in {path}."
        _write_file_direct(safe, new_text)
        if ctx and ctx.require_file_checkout and ctx.write_capable_mode:
            from app.agent.file_checkout import approve_write_result
            await approve_write_result(ctx.session_id, [path])
        return f"Resolved {count} conflict block(s) in {path} — kept '{choice}'."

    tools["resolve_conflict"] = ToolInfo(
        name="resolve_conflict",
        handler=_resolve_conflict,
        parameters={
            "type": "object",
            "properties": {
                "path": {"type": "string", "description": "Path to the conflicted file"},
                "choice": {
                    "type": "string",
                    "enum": ["ours", "theirs", "both"],
                    "description": "Which side of every conflict block to keep. 'ours'=HEAD/current, 'theirs'=incoming, 'both'=concatenate.",
                    "default": "ours",
                },
            },
            "required": ["path"],
        },
    )

    # ── commit_and_push: one-shot stage → commit → push ──
    # Thin wrapper over the shared core (app/api/github.commit_and_push_repo), so
    # this agent tool and the Source-Control ⭐ button share one implementation of
    # the secret scan, auto-message style, and commit/push flow — they can't drift.
    async def _commit_and_push(message: str = "", skip_push: bool = False, include_untracked: bool = True) -> str:
        """Stage all changes, auto-write a commit message from the diff (unless one
        is given), commit, and push — all in one tool call."""
        try:
            from app.api.github import commit_and_push_repo, _pin_to_project_root
        except Exception as e:
            return f"Error: one-shot commit+push is unavailable: {e}"

        # Pin to THIS app's repo + shared key so the agent's commit+push never lands
        # in whatever other repo the user has selected on the Source Control page.
        _pin_to_project_root()
        r = await commit_and_push_repo(
            message or "", skip_push=skip_push, include_untracked=include_untracked,
        )
        status = r.get("status")
        if status == "nothing_to_commit":
            return "Nothing to commit — working tree is clean."
        if status == "blocked":
            return "❌ **Commit BLOCKED by safety checks**\n\n" + r.get("message", "")
        if status == "error":
            return "Error: " + r.get("message", "git operation failed")

        push = r.get("push") or {}
        if not push.get("attempted"):
            push_result = "⏭️  Skipped (skip_push=True)"
        elif push.get("ok"):
            push_result = "✅ Push successful"
        else:
            push_result = f"❌ Push FAILED: {push.get('detail', '')}"

        return (
            f"## ✅ Commit + Push\n\n"
            f"**Message:** `{r.get('title', '')}`\n"
            f"**Hash:**   `{r.get('hash', '')}`\n"
            f"**Push:**   {push_result}\n"
            f"**Files:**  {r.get('file_count', 0)} file(s) affected\n\n"
            f"```\n{r.get('stat', '')}\n```"
        )

    tools["commit_and_push"] = ToolInfo(
        name="commit_and_push",
        handler=_commit_and_push,
        parameters={
            "type": "object",
            "properties": {
                "message": {
                    "type": "string",
                    "description": "Optional commit message. Auto-generated from diff via lightweight LLM if empty.",
                    "default": "",
                },
                "skip_push": {
                    "type": "boolean",
                    "description": "If true, commit locally but skip push.",
                    "default": False,
                },
                "include_untracked": {
                    "type": "boolean",
                    "description": "If true, include untracked files (git add -A). If false, modified only (git add -u).",
                    "default": True,
                },
            },
            "required": [],
        },
    )

    # Confirm-gate all three git tools: they write to the working tree or the
    # remote, so the agent loop pauses for the user in Ask/Plan mode. (git_tool
    # is dual-use - the loop exempts its read-only operations at runtime via
    # _is_safe_git_operation, keying off the tool name regardless of ability.)
    for _name in ("git_tool", "resolve_conflict", "commit_and_push"):
        _ti = tools.get(_name)
        if _ti is not None:
            _ti.destructive = True
            _ti.requires_confirmation = True


# ── Repo-change watcher (background service) ───────────────────────────────
#
# A single leader-gated poll loop shared by every watching agent. The repo is
# global (one working tree), so we read its divergence ONCE per tick and compare
# it against a per-agent baseline — that way a brand-new opt-in is seeded
# silently and only ever pinged on a NEW change, and a server restart with an
# already-dirty tree doesn't spam (the first observation just sets the baseline).

POLL_INTERVAL_SECONDS = 60   # how often to re-check the repo
_WARMUP_SECONDS = 8          # let app startup settle before the first tick

_watch_task = None                  # asyncio.Task | None — the running loop
_last_seen: dict = {}               # agent_id -> "<uncommitted>:<ahead>" fingerprint


def _coerce_bool(v) -> bool:
    """The settings panel stores every value as a STRING, so naive ``bool('false')``
    is True — exactly backwards. Treat the common false-ish strings as False."""
    if isinstance(v, bool):
        return v
    if isinstance(v, (int, float)):
        return bool(v)
    if isinstance(v, str):
        return v.strip().lower() in ("1", "true", "yes", "on", "enabled")
    return bool(v)


def _setting_on(config_raw) -> bool:
    """Read ``ability_settings.notify_repo_changes`` from a git_control connection
    row's ``config`` (a JSON string or dict)."""
    try:
        cfg = json.loads(config_raw) if isinstance(config_raw, str) else (config_raw or {})
        if not isinstance(cfg, dict):
            return False
        settings = cfg.get("ability_settings") or {}
        return _coerce_bool(settings.get("notify_repo_changes"))
    except Exception:
        return False


def _repo_divergence():
    """Return ``(uncommitted_count, unpushed_ahead_count)`` for the project repo,
    or ``None`` if git can't be read. Reuses app/api/github.py's ``_run_git`` so
    it honours the same project root + token env as the Source Control page."""
    try:
        from app.api.github import _run_git
    except Exception:
        return None
    status_out, _, rc = _run_git(["status", "--porcelain"])
    if rc != 0:
        return None
    uncommitted = sum(1 for line in status_out.splitlines() if line.strip())
    # Commits on the current branch not yet on its upstream. No upstream → 0.
    ahead = 0
    ab_out, _, ab_rc = _run_git(["rev-list", "--count", "@{upstream}..HEAD"])
    if ab_rc == 0:
        try:
            ahead = int(ab_out.strip() or "0")
        except ValueError:
            ahead = 0
    return uncommitted, ahead


async def _watching_agents(db) -> list:
    """All agent_ids whose git_control ability is enabled AND have the
    notify_repo_changes setting turned on."""
    out = []
    try:
        raw = db.get_raw_client()
        rows = (raw.table("agent_connections")
                   .select("agent_id,enabled,config")
                   .eq("connection_type", "git_control")
                   .execute()).data or []
        for r in rows:
            enabled = r.get("enabled")
            enabled = bool(enabled) if not isinstance(enabled, str) else enabled.strip().lower() in ("1", "true")
            if enabled and _setting_on(r.get("config")):
                out.append(r.get("agent_id"))
    except Exception as e:
        logger.debug("git_control watcher: could not list watching agents: %s", e)
    return [a for a in out if a]


async def _latest_session(db, agent_id: str):
    """Return ``(session_id, user_id)`` for the agent's most-recently-active live
    session, or ``None`` if it has no chat to post into."""
    try:
        raw = db.get_raw_client()
        rows = (raw.table("sessions")
                   .select("id,user_id,updated_at,created_at")
                   .eq("agent_id", agent_id)
                   .eq("status", "active")
                   .execute()).data or []
        if not rows:
            return None
        rows.sort(key=lambda s: (s.get("updated_at") or s.get("created_at") or ""), reverse=True)
        top = rows[0]
        sid, uid = top.get("id"), top.get("user_id")
        return (sid, uid) if sid and uid else None
    except Exception as e:
        logger.debug("git_control watcher: latest-session lookup failed for %s: %s", agent_id, e)
        return None


def _notice_text(uncommitted: int, ahead: int) -> str:
    parts = []
    if uncommitted:
        parts.append(f"{uncommitted} uncommitted file change{'s' if uncommitted != 1 else ''}")
    if ahead:
        parts.append(f"{ahead} local commit{'s' if ahead != 1 else ''} not pushed")
    detail = " and ".join(parts) if parts else "changes"
    return (f"🔔 Repo watch: the project has diverged from the last commit — {detail}. "
            "This is just a heads-up; tell me if you'd like me to review or commit them.")


async def _deliver_notice(db, agent_id: str, uncommitted: int, ahead: int) -> None:
    """Persist a passive heads-up into the agent's latest session and broadcast it
    live. No agent turn runs."""
    target = await _latest_session(db, agent_id)
    if not target:
        return   # nobody is chatting with this agent — nowhere natural to post
    session_id, user_id = target
    msg = _notice_text(uncommitted, ahead)
    try:
        await db.insert_interaction(
            user_id, session_id, role="assistant", content=msg,
            channel="git_watch", source="git_watch",
            metadata=json.dumps({"kind": "repo_change_notice",
                                  "uncommitted": uncommitted, "ahead": ahead}),
        )
    except Exception as e:
        logger.debug("git_control watcher: could not persist notice for %s: %s", agent_id, e)
    try:
        from app.events.delivery import notify_user
        await notify_user(user_id, {
            "type": "repo_change_notice",
            "session_id": session_id,
            "agent_id": agent_id,
            "message": msg,
            "uncommitted": uncommitted,
            "ahead": ahead,
        })
    except Exception as e:
        logger.debug("git_control watcher: live broadcast failed for %s: %s", agent_id, e)


async def _watch_tick() -> None:
    from app.db import get_db
    db = get_db()

    agents = await _watching_agents(db)
    if not agents:
        _last_seen.clear()   # nobody watching — drop stale baselines
        return

    state = _repo_divergence()
    if state is None:
        return   # transient git error — try again next tick
    uncommitted, ahead = state
    fp = f"{uncommitted}:{ahead}"

    watching = set(agents)
    for agent_id in agents:
        prev = _last_seen.get(agent_id)
        _last_seen[agent_id] = fp
        if prev is None:
            continue                       # first observation — seed baseline, no ping
        if fp == prev:
            continue                       # unchanged since last tick
        if uncommitted == 0 and ahead == 0:
            continue                       # returned to clean — update baseline only
        await _deliver_notice(db, agent_id, uncommitted, ahead)

    # Forget agents that turned the watch off so a later re-opt-in re-seeds.
    for stale in [a for a in _last_seen if a not in watching]:
        _last_seen.pop(stale, None)


async def _watch_loop() -> None:
    await asyncio.sleep(_WARMUP_SECONDS)
    while True:
        try:
            await _watch_tick()
        except asyncio.CancelledError:
            raise
        except Exception as e:
            logger.exception("git_control watcher tick failed: %s", e)
        try:
            await asyncio.sleep(POLL_INTERVAL_SECONDS)
        except asyncio.CancelledError:
            raise


async def start_background() -> None:
    """Leader-gated background service (discovered by background_service_hooks)."""
    global _watch_task
    if _watch_task and not _watch_task.done():
        return
    _last_seen.clear()
    _watch_task = asyncio.create_task(_watch_loop(), name="git_control_repo_watch")
    logger.info("git_control repo-change watcher started (every %ss)", POLL_INTERVAL_SECONDS)


async def stop_background() -> None:
    global _watch_task
    if _watch_task:
        _watch_task.cancel()
        try:
            await _watch_task
        except (asyncio.CancelledError, Exception):
            pass
        _watch_task = None
    _last_seen.clear()

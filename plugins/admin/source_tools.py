"""
Built-in source management tools injected by loader.py.

Gives the agent full filesystem access: read, write, edit, delete, shell commands,
Python execution, browser testing, and git operations.

Delete this file (along with source.py) to lock down the agent.
"""

import asyncio
import difflib
import json
import logging
import os
import re
import subprocess
from pathlib import Path
from typing import Any, Dict, List, Optional

from app.tools.loader import ToolInfo
# In-process source operations. The agent's tools call these DIRECTLY rather
# than POSTing to the (now admin-gated) /admin/source/* HTTP endpoints, so they
# never need to send a token to localhost — the HTTP surface stays locked down
# while the server-side agent path keeps working. See plugins/admin/source.py.
from plugins.admin.source import _do_read, _do_write, _do_delete, _do_exec

logger = logging.getLogger(__name__)

BASE = "http://localhost:8080"


def _err(e: Exception) -> str:
    """Human-readable message from an exception — HTTPException.detail if present."""
    detail = getattr(e, "detail", None)
    return str(detail) if detail is not None else str(e)

# ── Write/exec tools that must pause for the user's confirmation ───────────────
# THE single hard-coded source of truth for which admin tools are "destructive"
# (they write, delete, or execute with side-effects). The admin abilities that
# inject these tools — Codebase Admin (inject_source_tools) and UI Admin
# (inject_ui_tools) — build off the ToolInfo objects created here, so they
# inherit the same gating with no per-ability list to keep in sync. (Git Control
# defines its OWN git tools in its ability file and stamps them confirm-gated
# there.) Setting both flags makes the agent loop treat them as confirmation-
# gated: it pauses for the user's go-ahead in BOTH Ask and Plan execution modes
# (the read-only tools left out below — read_source, search_*, read_directory —
# keep running without a pause). One tool in the set is DUAL-USE — run_command
# both reads and writes — and the agent loop exempts its read-only invocations at
# runtime (safe shell commands like `git status`, `ls`, … via
# _is_safe_shell_command) so a broad set doesn't gate routine reads.
_CONFIRM_TOOLS = frozenset({
    "write_source", "edit_source", "patch_source", "delete_source",
    "run_command", "run_python",
})
# NOTE: restart_server is deliberately NOT confirm-gated. Restarting is how the
# agent validates a change that only takes effect in a fresh process, so it runs
# free in every execution mode — no user permission required. Do NOT re-add it to
# this set; see _restart_server's docstring for the full rationale.


def _apply_confirm_flags(tools: dict) -> None:
    """Mark every write/exec tool in ``tools`` confirmation-gated (see
    ``_CONFIRM_TOOLS``). Mutates the ToolInfo objects in place so the flags flow
    through ``adapter.extract_injected`` into each ability's DESTRUCTIVE set."""
    for _name in _CONFIRM_TOOLS:
        _ti = tools.get(_name)
        if _ti is not None:
            _ti.destructive = True
            _ti.requires_confirmation = True


# ── Helpers ───────────────────────────────────────────────────────────────────

_PROJECT_ROOT = Path(__file__).resolve().parent.parent.parent  # app/../

try:
    from plugins.admin.guardrails import check_path as _guard_check_path  # type: ignore
except Exception:  # pragma: no cover
    async def _guard_check_path(path: str, action: str = "read") -> None:  # type: ignore
        return None


def _safe_path(path: str) -> Optional[Path]:
    """Resolve a path relative to the project root. Return None if outside.

    Uses ``Path.is_relative_to`` (a true prefix test) rather than a string
    ``startswith`` so a sibling dir like ``…/webagent-dev-attacker`` can't
    slip past the project-root containment check.
    """
    p = Path(path)
    if not p.is_absolute():
        p = _PROJECT_ROOT / p
    p = p.resolve()
    try:
        if p != _PROJECT_ROOT and not p.is_relative_to(_PROJECT_ROOT):
            return None  # outside project root
    except AttributeError:  # pragma: no cover - Python < 3.9
        if not str(p).startswith(str(_PROJECT_ROOT) + os.sep):
            return None
    return p


def _mini_diff(before: str, after: str, path: str, context: int = 2) -> str:
    """Return a compact unified diff snippet showing only the changed hunks."""
    before_lines = before.splitlines(keepends=False)
    after_lines = after.splitlines(keepends=False)
    diff = difflib.unified_diff(
        before_lines, after_lines,
        fromfile=path, tofile=path, n=context, lineterm="",
    )
    out = list(diff)
    if not out:
        return "(no textual change)"
    text = "\n".join(out)
    if len(text) > 2000:
        text = text[:2000] + "\n... (diff truncated)"
    return text


def _run_subprocess(cmd: List[str], timeout: int = 30) -> dict:
    """Run a subprocess and return stdout, stderr, exit_code."""
    try:
        r = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout)
        return {"stdout": r.stdout, "stderr": r.stderr, "exit_code": r.returncode}
    except subprocess.TimeoutExpired:
        return {"stdout": "", "stderr": f"Timed out after {timeout}s", "exit_code": -1}
    except FileNotFoundError:
        return {"stdout": "", "stderr": f"Command not found: {cmd[0]}", "exit_code": -1}
    except Exception as e:
        return {"stdout": "", "stderr": str(e), "exit_code": -1}


def _leading_width(line: str, tabsize: int = 4) -> int:
    """Visual column width of a line's leading whitespace (tabs expanded)."""
    w = 0
    for ch in line:
        if ch == " ":
            w += 1
        elif ch == "\t":
            w += tabsize
        else:
            break
    return w


def _leading_ws(line: str) -> str:
    """The literal leading-whitespace prefix of a line."""
    return line[: len(line) - len(line.lstrip(" \t"))]


def _file_indent_uses_tabs(text: str) -> bool:
    """True if the file predominantly indents with tabs rather than spaces."""
    tabs = spaces = 0
    for line in text.split("\n"):
        if not line[:1].strip(" \t") and line.strip():  # has leading ws + content
            if line[0] == "\t":
                tabs += 1
            elif line[0] == " ":
                spaces += 1
    return tabs > spaces


def _rebase_block_indent(block: str, base_indent: str, use_tabs: bool) -> str:
    """Re-indent a replacement block so its outermost level sits at
    ``base_indent`` and inner levels follow the file's indent style.

    The model frequently supplies replacement text with tab/space-mismatched
    or wrongly-based indentation (e.g. tabs pasted into a spaces file). When a
    patch is located by IGNORING whitespace we must not splice that text in
    verbatim — this rewrites only the LEADING whitespace of every line so it
    matches the surrounding file; the content of each line is untouched. For a
    block that is already correctly indented this is a no-op."""
    lines = block.split("\n")
    nonblank = [l for l in lines if l.strip()]
    if not nonblank:
        return block
    min_w = min(_leading_width(l) for l in nonblank)
    out = []
    for line in lines:
        if not line.strip():
            out.append("")
            continue
        rel = _leading_width(line) - min_w  # extra columns beyond the block base
        if use_tabs:
            indent = base_indent + "\t" * (rel // 4)
        else:
            indent = base_indent + " " * rel
        out.append(indent + line.lstrip(" \t"))
    return "\n".join(out)


def _py_parses(text: str) -> bool:
    """True if ``text`` compiles as Python (or isn't broken by a SyntaxError)."""
    try:
        compile(text, "<patch>", "exec")
        return True
    except SyntaxError:
        return False
    except Exception:
        return True  # non-syntax errors aren't this tool's concern


def _has_mixed_indent(text: str) -> bool:
    """True if any line's leading whitespace mixes both tabs and spaces."""
    for line in text.split("\n"):
        lead = _leading_ws(line)
        if "\t" in lead and " " in lead:
            return True
    return False


def _patch_sanity_warning(path: Path, before: str, after: str) -> str:
    """Return a prominent warning if a patch likely broke the file: a Python
    file that parsed before but not after, or newly-introduced mixed
    tab/space indentation. Empty string when the result looks clean. This is
    the safety net for the fuzzy match strategies — a patch must never report
    a bare 'success' when it has silently corrupted the file."""
    warns = []
    if path.suffix == ".py" and _py_parses(before) and not _py_parses(after):
        warns.append("the file no longer parses as valid Python")
    if _has_mixed_indent(after) and not _has_mixed_indent(before):
        warns.append("mixed tabs/spaces indentation was introduced")
    if not warns:
        return ""
    return (
        "\n\n[WARNING] " + "; ".join(warns) + ". The match was not exact, so the "
        "replacement may have landed with the wrong whitespace. Re-read the region "
        "(read_source with offset/limit) and fix it — or write_source the whole "
        "file — before moving on. Do NOT assume this patch was clean."
    )


def _write_file_direct(path: Path, content: str) -> None:
    """Write content to a file directly (no HTTP proxy). Creates backup.

    Write failures must propagate to the tool executor. Returning an error
    string here lets ``patch_source`` continue and falsely report a successful
    patch even though the file was unchanged.
    """
    backup = path.with_suffix(path.suffix + ".bak")
    try:
        if path.exists():
            import shutil
            shutil.copy2(str(path), str(backup))
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(content, encoding="utf-8")
    except Exception as e:
        raise OSError(f"Error writing {path}: {e}") from e


# ── HTTP helpers (existing tools) ─────────────────────────────────────────────

async def _api_get(path: str, params: dict = None):
    import httpx
    async with httpx.AsyncClient(base_url=BASE, timeout=15) as c:
        r = await c.get(path, params=params or {})
        r.raise_for_status()
        return r.json()


async def _api_post(path: str, json_data: dict):
    import httpx
    async with httpx.AsyncClient(base_url=BASE, timeout=30) as c:
        r = await c.post(path, json=json_data)
        return r


# ── Main injection entry point ────────────────────────────────────────────────

def inject_source_tools(tools: dict, user_id: str) -> None:
    """Inject filesystem tools into the tools dict."""

    def _checkout_context():
        from app.tools.execution_context import current_tool_context
        return current_tool_context()

    async def _observe(paths):
        ctx = _checkout_context()
        if not ctx or not ctx.session_id or not ctx.require_file_checkout:
            return
        from app.agent.file_checkout import mark_validated, observe_files
        await observe_files(ctx.session_id, paths)
        await mark_validated(ctx.session_id, paths, "Successful post-edit source read")

    async def _checkout_preflight(paths):
        ctx = _checkout_context()
        if not ctx or not ctx.require_file_checkout or not ctx.write_capable_mode:
            return
        from app.agent.file_checkout import preflight_write
        await preflight_write(ctx.session_id, paths)

    async def _checkout_approved(paths):
        ctx = _checkout_context()
        if not ctx or not ctx.require_file_checkout or not ctx.write_capable_mode:
            return
        from app.agent.file_checkout import approve_write_result
        await approve_write_result(ctx.session_id, paths)

    async def _checkout_tools_context():
        ctx = _checkout_context()
        if not ctx or not ctx.session_id:
            raise RuntimeError("File checkout tools require an active chat session.")
        return ctx

    async def _observe_search_output(output: str):
        paths = []
        for line in str(output or "").splitlines():
            match = re.match(r"^(.+?):[0-9]+:", line)
            if match and (safe := _safe_path(match.group(1))) and safe.is_file():
                paths.append(str(safe))
        if paths:
            await _observe(list(dict.fromkeys(paths)))

    async def _broad_preflight(name: str, args: dict, checkout_paths: Optional[List[str]]):
        ctx = _checkout_context()
        if not ctx or not ctx.require_file_checkout or not ctx.write_capable_mode:
            return None, []
        from app.agent.contracts import is_verify_tool
        declared = list(dict.fromkeys(checkout_paths or []))
        if is_verify_tool(name, args):
            return None, declared
        if not declared:
            raise RuntimeError(
                f"{name} may mutate repository files; declare checkout_paths and acquire them first."
            )
        await _checkout_preflight(declared)
        from app.agent.session_changes import capture_tool_state_async
        return await capture_tool_state_async(name, args), declared

    async def _broad_audit(name: str, args: dict, before, declared: List[str]):
        if before is None:
            return {"declared": declared, "changed": [], "undeclared": []}
        from app.agent.session_changes import capture_tool_state_async
        from app.agent.file_checkout import normalize_path
        after = await capture_tool_state_async(name, args) or {}
        changed = sorted(
            {path for path, value in after.items() if before.get(path) != value}
            | {path for path in before if path not in after}
        )
        normalized_declared = {
            normalize_path(path)[0]
            for path in declared
        }
        undeclared = [path for path in changed if path not in normalized_declared]
        ctx = _checkout_context()
        if undeclared:
            from app.agent.file_checkout import mark_violation
            await mark_violation(
                ctx.session_id, undeclared,
                f"{name} changed undeclared repository paths; reread and explicitly reconcile."
            )
        approved = [path for path in changed if path in normalized_declared]
        if approved:
            await _checkout_approved(approved)
        return {"declared": sorted(normalized_declared), "changed": changed,
                "undeclared": undeclared}

    # ═══════════════════════════════════════════════════════════════════════
    # FILE OPERATIONS
    # ═══════════════════════════════════════════════════════════════════════

    # ── read_source: direct disk read, offset/limit support ──
    async def _read_source(path: str, offset: int = 1, limit: int = 2000) -> str:
        """Read file contents from disk. Supports line-range slicing for large files.

        offset is 1-indexed (line numbers shown in cat -n format). NOT named 'start'.
        Default limit is 2000 lines; the response includes a truncation hint
        when the file extends beyond the returned slice."""
        safe = _safe_path(path)
        if not safe:
            return "Error: path is outside the project root"
        try:
            await _guard_check_path(str(safe), "read")
        except PermissionError as e:
            return f"Error: {e}"
        if not safe.exists():
            return f"Error: file not found: {path}"
        if not safe.is_file():
            return f"Error: not a file: {path}"

        try:
            text = safe.read_text(encoding="utf-8")
        except UnicodeDecodeError:
            return f"Error: {path} is not UTF-8 text"
        await _observe([path])

        lines = text.split("\n")
        total = len(lines)
        if offset < 1:
            offset = 1
        if limit <= 0:
            limit = 2000
        start = offset - 1
        end = min(start + limit, total)
        sliced = lines[start:end]
        # Render with line numbers so the agent can re-target patches precisely
        width = len(str(end if end > 0 else 1))
        body = "\n".join(f"{str(i + 1).rjust(width)}\t{ln}"
                         for i, ln in enumerate(sliced, start=start))
        header = f"{path} ({total} lines, showing {offset}-{end})"
        suffix = ""
        if end < total:
            suffix = (f"\n... ({total - end} more lines — call read_source("
                     f"path={path!r}, offset={end + 1}, limit={limit}) to continue)")
        return f"{header}\n{body}{suffix}"

    tools["read_source"] = ToolInfo(
        name="read_source",
        handler=_read_source,
        parameters={
            "type": "object",
            "properties": {
                "path": {"type": "string", "description": "Path to the file (relative to project root or absolute)"},
                "offset": {"type": "integer", "description": "1-indexed line to start from. Use 'offset' NOT 'start'. Default 1.", "default": 1},
                "limit": {"type": "integer", "description": "Max lines to return. Default 2000.", "default": 2000},
            },
            "required": ["path"],
        },
    )

    # ── write_source: no permission needed (just write) ──
    async def _checkout_files(paths: List[str]) -> dict:
        ctx = await _checkout_tools_context()
        from app.agent.file_checkout import checkout_files
        files = await checkout_files(ctx.session_id, ctx.user_id, ctx.agent_id, paths)
        return {"status": "checked_out", "files": files}

    tools["checkout_files"] = ToolInfo(
        name="checkout_files", handler=_checkout_files,
        parameters={"type": "object", "properties": {
            "paths": {"type": "array", "items": {"type": "string"},
                      "description": "Repository-relative file paths to acquire atomically."}},
                    "required": ["paths"]},
    )

    async def _checkin_files(paths: List[str], validation_note: str = "") -> dict:
        ctx = await _checkout_tools_context()
        from app.agent.file_checkout import checkin_files
        files = await checkin_files(ctx.session_id, paths, validation_note)
        return {"status": "checked_in", "files": files}

    tools["checkin_files"] = ToolInfo(
        name="checkin_files", handler=_checkin_files,
        parameters={"type": "object", "properties": {
            "paths": {"type": "array", "items": {"type": "string"}},
            "validation_note": {"type": "string",
                                "description": "Optional summary of post-edit validation."}},
                    "required": ["paths"]},
    )

    async def _abandon_file_checkouts(paths: List[str], reason: str = "") -> dict:
        ctx = await _checkout_tools_context()
        from app.agent.file_checkout import abandon_file_checkouts
        files = await abandon_file_checkouts(ctx.session_id, paths, reason)
        return {"status": "abandoned", "files": files}

    tools["abandon_file_checkouts"] = ToolInfo(
        name="abandon_file_checkouts", handler=_abandon_file_checkouts,
        parameters={"type": "object", "properties": {
            "paths": {"type": "array", "items": {"type": "string"}},
            "reason": {"type": "string", "description": "Why the lease is being released."}},
                    "required": ["paths"]},
    )

    async def _file_checkout_status(paths: Optional[List[str]] = None) -> dict:
        ctx = await _checkout_tools_context()
        from app.agent.file_checkout import file_checkout_status
        files = await file_checkout_status(ctx.session_id, paths)
        return {"status": "ok", "files": files,
                "active_count": sum(1 for item in files if item.get("active"))}

    tools["file_checkout_status"] = ToolInfo(
        name="file_checkout_status", handler=_file_checkout_status,
        parameters={"type": "object", "properties": {
            "paths": {"type": "array", "items": {"type": "string"},
                      "description": "Optional path filter."}}, "required": []},
    )

    async def _write_source(path: str, content: str) -> str:
        """Overwrite a file with new content. Backup created automatically."""
        await _checkout_preflight([path])
        try:
            result = await _do_write(path, content, True)
        except Exception as e:
            return f"Error: {_err(e)}"
        await _checkout_approved([path])
        return result.message

    tools["write_source"] = ToolInfo(
        name="write_source",
        handler=_write_source,
        parameters={
            "type": "object",
            "properties": {
                "path": {"type": "string", "description": "Path to the file (relative to project root or absolute)"},
                "content": {"type": "string", "description": "Full content to write to the file"},
            },
            "required": ["path", "content"],
        },
    )

    # ── edit_source: exact find-and-replace (kept for backward compat) ──
    async def _edit_source(path: str, old_text: str, new_text: str) -> str:
        """Edit a file by replacing exact text. For fuzzy matching, use patch_source."""
        await _checkout_preflight([path])
        try:
            data = await _do_read(path)
            current = data.content
        except Exception:
            return f"Error: could not read {path}. Does it exist?"

        if old_text not in current:
            return f"Error: old_text not found in {path}"

        new_content = current.replace(old_text, new_text, 1)
        if new_content == current:
            return f"Error: replacement produced no change"

        try:
            result = await _do_write(path, new_content, True)
        except Exception as e:
            return f"Error: {_err(e)}"
        await _checkout_approved([path])
        return f"{result.message}. Backup: {result.backup_path or 'none'}"

    tools["edit_source"] = ToolInfo(
        name="edit_source",
        handler=_edit_source,
        parameters={
            "type": "object",
            "properties": {
                "path": {"type": "string", "description": "Path to the file"},
                "old_text": {"type": "string", "description": "Exact text to find and replace (must match exactly)"},
                "new_text": {"type": "string", "description": "Replacement text"},
            },
            "required": ["path", "old_text", "new_text"],
        },
    )

    # ── patch_source: FUZZY find-and-replace (preferred over edit_source) ──
    async def _patch_source(path: str, old_string: str, new_string: str) -> str:
        """Edit a file by replacing text. Uses fuzzy matching — handles
        whitespace differences, indentation changes, and partial matches.

        Tries up to 9 matching strategies:
          1. Exact match
          2. Whitespace-normalized match
          3. Truncated old_string match
          4. Unique-line match (single unique line from old_string)
          5. Context-anchored match (first + last lines)
          6-9. Progressive character truncation from the end

        When a match is found by IGNORING whitespace (strategy 2+), the
        replacement text's own indentation is untrusted: it is rebased onto the
        matched region's indent and the file's tab/space style rather than
        spliced in verbatim. Any patch that still leaves the file unparseable or
        with mixed tabs/spaces returns a loud warning instead of a bare success.
        """
        await _checkout_preflight([path])
        safe = _safe_path(path)
        if not safe:
            return "Error: path is outside the project root"
        if not safe.exists():
            return f"Error: file not found: {path}"
        current = safe.read_text(encoding="utf-8")
        old_stripped = old_string.strip()
        new_stripped = new_string.strip()
        use_tabs = _file_indent_uses_tabs(current)

        async def _finish(result: str, label: str) -> str:
            """Write the result and report, appending a loud warning if the
            (non-exact) match left the file broken. Every success path goes
            through here so a fuzzy match can never report a bare 'success'."""
            _write_file_direct(safe, result)
            await _checkout_approved([path])
            warn = _patch_sanity_warning(safe, current, result)
            return f"Patched ({label}) — {path}\n\n{_mini_diff(current, result, path)}{warn}"

        # Strategy 1: exact match (replacement text is taken verbatim — the
        # caller matched exactly, so its whitespace is trusted as-is)
        if old_string in current:
            result = current.replace(old_string, new_string, 1)
            return await _finish(result, "exact match")

        # Strategy 2: whitespace-normalized match
        def _norm(s):
            return re.sub(r'\s+', ' ', s).strip()

        norm_old = _norm(old_string)
        norm_current = _norm(current)
        if norm_old in norm_current:
            idx = norm_current.index(norm_old)
            # Reconstruct approximate span
            lines = current.split('\n')
            char_count = 0
            start_line = 0
            for i, line in enumerate(lines):
                next_count = char_count + len(line) + 1
                stripped_line = ' '.join(line.split())
                if idx >= char_count and idx < next_count and stripped_line == norm_old.split('\n')[0] if '\n' in old_string else stripped_line == norm_old:
                    start_line = i
                    break
                char_count = next_count
            result = current.replace(old_string, new_string, 1)
            # If whitespace match didn't replace, try reconstructing
            if result == current:
                old_lines = old_string.split('\n')
                found = False
                for i in range(len(lines) - len(old_lines) + 1):
                    chunk = '\n'.join(lines[i:i + len(old_lines)])
                    if _norm(chunk) == norm_old:
                        leading = '\n'.join(lines[:i])
                        trailing = '\n'.join(lines[i + len(old_lines):])
                        sep = '\n' if leading and trailing else ''
                        # The match ignored whitespace, so new_string's own
                        # indentation is untrusted — rebase it onto the matched
                        # region's indent + the file's tab/space style instead
                        # of splicing it in verbatim (the old bug).
                        base_indent = _leading_ws(lines[i])
                        rebased = _rebase_block_indent(new_string, base_indent, use_tabs)
                        result = leading + sep + rebased + sep + trailing
                        found = True
                        break
                if found:
                    return await _finish(result, "fuzzy whitespace match")

        # Strategy 3: truncated old_string (remove trailing context)
        for truncate_to in range(len(old_string) - 1, len(old_string) // 2, -1):
            truncated = old_string[:truncate_to]
            if truncated in current:
                # Replace the first occurrence of the truncated string
                result = current.replace(truncated, new_string, 1)
                if result != current:
                    return await _finish(result, f"truncated match to {truncate_to} chars")

        # Strategy 4: unique-line match
        old_lines = old_string.strip().split('\n')
        unique_lines = [l.strip() for l in old_lines if len(l.strip()) > 10]
        for ul in unique_lines:
            if ul in current:
                result = current.replace(ul, new_string, 1)
                if result != current:
                    return await _finish(result, f"unique-line match: '{ul[:50]}...'")

        return (f"Error: could not find a match for old_string in {path}. "
                "Two-strike rule: do NOT retry patch_source with another variant. "
                "Instead, read_source the exact region (with offset/limit), then either "
                "patch_source with the exact text or write_source the whole file.")

    tools["patch_source"] = ToolInfo(
        name="patch_source",
        handler=_patch_source,
        parameters={
            "type": "object",
            "properties": {
                "path": {"type": "string", "description": "Path to the file to edit"},
                "old_string": {"type": "string", "description": "Text to find and replace (fuzzy matching — handles whitespace/differs)"},
                "new_string": {"type": "string", "description": "Replacement text"},
            },
            "required": ["path", "old_string", "new_string"],
        },
    )

    # ── delete_source: user commands → do it; own initiative → ask ──
    async def _delete_source(path: str, recursive: bool = False) -> str:
        """Delete a file or directory. Call directly when user commands it; ask first when deciding on your own."""
        await _checkout_preflight([path])
        try:
            result = await _do_delete(path, recursive)
        except Exception as e:
            return f"Error: {_err(e)}"
        await _checkout_approved([path])
        return result.message

    tools["delete_source"] = ToolInfo(
        name="delete_source",
        handler=_delete_source,
        parameters={
            "type": "object",
            "properties": {
                "path": {"type": "string", "description": "Path to the file or directory to delete"},
                "recursive": {"type": "boolean", "description": "Set true to delete directories", "default": False},
            },
            "required": ["path"],
        },
    )

    # ═══════════════════════════════════════════════════════════════════════
    # SHELL / EXECUTION
    # ═══════════════════════════════════════════════════════════════════════

    # ── run_command: read-only safe, mutating needs permission ──
    async def _run_command(
        command: str, timeout: int = 30, checkout_paths: Optional[List[str]] = None
    ) -> dict:
        """Execute a shell command. Mutations require declared checked-out paths."""
        before, declared = await _broad_preflight(
            "run_command", {"command": command, "timeout": timeout}, checkout_paths
        )
        res = await _do_exec(command, timeout)
        audit = await _broad_audit(
            "run_command", {"command": command, "timeout": timeout}, before, declared
        )
        if res.exit_code == 0 and declared and before is None:
            ctx = _checkout_context()
            from app.agent.file_checkout import mark_validated
            await mark_validated(ctx.session_id, declared, "Validation via run_command")
        return {
            "exit_code": res.exit_code,
            "stdout": res.stdout,
            "stderr": res.stderr,
            "timed_out": res.timed_out,
            "checkout_audit": audit,
        }

    tools["run_command"] = ToolInfo(
        name="run_command",
        handler=_run_command,
        parameters={
            "type": "object",
            "properties": {
                "checkout_paths": {"type": "array", "items": {"type": "string"},
                                   "description": "Checked-out repository paths this command may mutate."},
                "command": {"type": "string", "description": "Shell command to execute. Commands run via cmd.exe on Windows (not bash). Important: '%' is a special character in cmd — for curl -w format strings, double every '%' to '%%' (e.g. -w '%%{http_code}'). For complex commands, use powershell -Command \"...\" instead."},
                "timeout": {"type": "integer", "description": "Timeout in seconds (default 30)", "default": 30},
            },
            "required": ["command"],
        },
    )

    # ── run_python: execute Python code directly ──
    async def _run_python(
        code: str, timeout: int = 30, checkout_paths: Optional[List[str]] = None
    ) -> str:
        """Execute Python code and return stdout. The code runs as a subprocess
        in the project root directory. Use for: testing logic, validating changes,
        computing values, transforming data.

        Import standard library modules freely. External packages may not be
        available if not installed.
        """
        if not code.strip():
            return "Error: no code provided"
        before, declared = await _broad_preflight(
            "run_python", {"code": code, "timeout": timeout}, checkout_paths
        )
        cmd = [sys_executable := "python", "-c", code] if os.name != 'nt' else ["python", "-c", code]
        # Use the same python that's running the server if possible
        try:
            import sys
            cmd[0] = sys.executable
        except Exception:
            pass

        result = _run_subprocess(cmd, timeout=timeout)
        audit = await _broad_audit(
            "run_python", {"code": code, "timeout": timeout}, before, declared
        )
        if result["exit_code"] == 0 and declared and before is None:
            ctx = _checkout_context()
            from app.agent.file_checkout import mark_validated
            await mark_validated(ctx.session_id, declared, "Validation via run_python")
        output_parts = []
        if result["stdout"]:
            output_parts.append(result["stdout"].strip())
        if result["stderr"]:
            output_parts.append(f"[stderr]\n{result['stderr'].strip()}")
        if not output_parts:
            if result["exit_code"] == 0:
                output_parts.append("(no output)")
            else:
                output_parts.append(f"Process exited {result['exit_code']} (no output)")
        full = "\n".join(output_parts)
        if audit.get("changed") or audit.get("declared"):
            full += "\n[checkout_audit] " + json.dumps(audit, sort_keys=True)
        if len(full) > 10000:
            full = full[:10000] + f"\n... (truncated, {len(full)} total)"
        return full

    tools["run_python"] = ToolInfo(
        name="run_python",
        handler=_run_python,
        parameters={
            "type": "object",
            "properties": {
                "checkout_paths": {"type": "array", "items": {"type": "string"},
                                   "description": "Checked-out repository paths this code may mutate."},
                "code": {"type": "string", "description": "Python code to execute"},
                "timeout": {"type": "integer", "description": "Timeout in seconds (default 30)", "default": 30},
            },
            "required": ["code"],
        },
    )

    # ── restart_server: free to call — never confirmation-gated ──
    async def _restart_server() -> str:
        """Restart the web agent server to validate a change that needs a fresh process. Call it freely — no user confirmation required.

        Use this whenever you have edited source, config, or plugin code whose
        effect only appears in a restarted server, then verify the change. It is
        the only correct route: do not POST ``/api/v1/restart`` and never kill
        the process (see below).

        Delegates to ``app.relauncher.trigger_restart`` IN-PROCESS — the same
        call the admin UI paths make (see ``app/admin/storage.py``). It picks
        the topology: Uvicorn's reload parent, a multiprocess supervisor
        (SIGHUP), or the detached fallback that revives a server launched
        directly with run.py.

        Expect this session's own turn to be cut off when the workers roll; the
        run resumes in a fresh worker.

        Do NOT "restore" this to a POST of ``/api/v1/restart``: that route is
        gated by ``require_platform_admin``, and the agent path has no token to
        present from localhost, so every call came back 401 and the restart
        silently never happened. Same rationale as the in-process source
        operations imported at the top of this module.
        """
        from app.relauncher import trigger_restart

        try:
            result = trigger_restart()
        except Exception as e:
            return f"Could not request server restart: {e}"
        if not result.get("auto_restart"):
            return "Server restart unavailable: " + str(
                result.get("reason") or "no restart launcher is available"
            )
        logger.warning("Restart requested via the restart_server tool — mode=%s",
                       result.get("mode", "standalone"))
        return f"Server restarting: {result.get('message', 'ok')}"

    tools["restart_server"] = ToolInfo(
        name="restart_server",
        handler=_restart_server,
        parameters={"type": "object", "properties": {}, "required": []},
    )

    # ═══════════════════════════════════════════════════════════════════════
    # SEARCH / DISCOVERY
    # ═══════════════════════════════════════════════════════════════════════

    # ── search_source: structured grep ──
    async def _search_source(pattern: str, path: str = ".", file_pattern: str = "") -> str:
        """Search file contents for a regex pattern. Returns matching lines with line numbers."""
        search_path = _safe_path(path)
        if not search_path:
            return "Error: path is outside the project root"
        if not search_path.exists():
            return f"Error: path does not exist: {path}"

        import shutil
        if shutil.which("rg"):
            cmd = ["rg", "-n", "--no-heading"]
            # A project-root search is source discovery, not a scan of live
            # databases, generated indexes, deployment stages, or logs.  Those
            # trees can be hundreds of MB and include binary strings that look
            # like source matches.  Callers can still search one explicitly by
            # naming it as ``path``.
            if search_path == _PROJECT_ROOT:
                for excluded in (
                    ".ast-index/**", ".tmp/**", "data/**", "logs/**",
                    "node_modules/**", ".venv/**", ".git/**",
                    "*.db", "*.db-*", "*.sqlite", "*.sqlite-*",
                ):
                    cmd.extend(["-g", f"!{excluded}"])
            cmd.extend([pattern, str(search_path)])
            if file_pattern:
                cmd.extend(["-g", file_pattern])
            # Tool handlers execute on the server loop.  Never make Stop,
            # WebSocket heartbeats, contract timers, or /health wait behind rg.
            result = await asyncio.to_thread(_run_subprocess, cmd, timeout=30)
            if result["exit_code"] == 0:
                lines = result["stdout"].strip().split("\n")
                if len(lines) > 200:
                    lines = lines[:200]
                    lines.append(f"... ({len(lines)} total, showing first 200)")
                output = "\n".join(lines)
                await _observe_search_output(output)
                return output
            elif result["exit_code"] == 1:
                return "No matches found."
            else:
                return f"Error: {result['stderr']}"

        try:
            compiled = re.compile(pattern)
        except re.error as e:
            return f"Error: invalid regex pattern: {e}"

        def _fallback_search() -> str:
            matches: List[str] = []
            if search_path.is_file():
                files_to_search = [search_path]
            else:
                # Exclude large/irrelevant dirs to avoid hangs. Runtime trees
                # are excluded only from a root search; an explicit path remains
                # inspectable.
                exclude_dirs = {"__pycache__", "node_modules", ".git", ".venv",
                                "venv", "env", ".source-backups", "temp",
                                ".mypy_cache", ".pytest_cache", ".cache"}
                if search_path == _PROJECT_ROOT:
                    exclude_dirs.update({".ast-index", ".tmp", "data", "logs"})
                all_files = []
                for f in search_path.rglob("*"):
                    if any(part in exclude_dirs for part in f.parts):
                        continue
                    all_files.append(f)
                    if len(all_files) > 50000:
                        return "Error: too many files to search (50,000+). Narrow your search path or file pattern."
                files_to_search = all_files
                if file_pattern:
                    files_to_search = [f for f in files_to_search
                                       if f.is_file() and f.match(file_pattern)]

            for fpath in files_to_search:
                if not fpath.is_file() or fpath.suffix.lower() in {
                    ".db", ".sqlite", ".sqlite3", ".pyc", ".pyd", ".dll", ".exe",
                }:
                    continue
                try:
                    text = fpath.read_text(encoding="utf-8", errors="replace")
                    for i, line in enumerate(text.split("\n"), 1):
                        if compiled.search(line):
                            rel = fpath.relative_to(_PROJECT_ROOT)
                            matches.append(f"{rel}:{i}:{line.rstrip()[:200]}")
                            if len(matches) >= 200:
                                matches.append(f"... ({len(matches)} total, showing first 200)")
                                return "\n".join(matches)
                except (OSError, UnicodeDecodeError):
                    continue
            return "\n".join(matches) if matches else "No matches found."

        output = await asyncio.to_thread(_fallback_search)
        await _observe_search_output(output)
        return output

    tools["search_source"] = ToolInfo(
        name="search_source",
        handler=_search_source,
        parameters={
            "type": "object",
            "properties": {
                "pattern": {"type": "string", "description": "Regex pattern to search for"},
                "path": {"type": "string", "description": "Directory or file to search (default: project root)", "default": "."},
                "file_pattern": {"type": "string", "description": "Optional file glob filter (e.g. '*.py', '*.json')", "default": ""},
            },
            "required": ["pattern"],
        },
    )

    # ── search_comments: comment-only search + single-file comment outline ──
    # A "map, not territory" tool: comments are this repo's table of contents.
    # Two modes:
    #   • pattern given            → grep ONLY comment lines across the tree, so
    #     consistency markers (SISTER-PANEL, TODO …) surface
    #     without code-line noise.
    #   • no pattern + a single file → that file's comments in order, as a quick
    #     outline of what it does and where each part lives.
    # Heuristic, NOT a parser: a line counts as a comment only when its STRIPPED
    # form STARTS WITH a comment marker for that file type. This deliberately
    # ignores trailing inline comments and '#'-inside-strings (e.g. hex colours
    # like "#bb9af7") to stay low-noise. When a hit matters, open the file and
    # read the full comment + surrounding code — the real instruction lives there.
    _COMMENT_MARKERS = {
        # extension (no dot) → stripped-line-start markers that begin a comment
        "py": ("#",), "pyi": ("#",), "sh": ("#",), "bash": ("#",),
        "yaml": ("#",), "yml": ("#",), "toml": ("#",), "ini": ("#",),
        "cfg": ("#",), "conf": ("#",), "rb": ("#",), "pl": ("#",),
        "js": ("//", "/*", "*"), "mjs": ("//", "/*", "*"), "ts": ("//", "/*", "*"),
        "jsx": ("//", "/*", "*"), "tsx": ("//", "/*", "*"),
        "css": ("/*", "*"), "scss": ("//", "/*", "*"),
        "java": ("//", "/*", "*"), "c": ("//", "/*", "*"), "h": ("//", "/*", "*"),
        "cpp": ("//", "/*", "*"), "go": ("//", "/*", "*"), "rs": ("//", "/*", "*"),
        "html": ("<!--", "*"), "htm": ("<!--", "*"), "xml": ("<!--", "*"),
        "vue": ("<!--", "//", "/*", "*"), "svelte": ("<!--", "//", "/*", "*"),
        "md": ("<!--",), "markdown": ("<!--",),
    }

    def _markers_for(fpath: Path):
        return _COMMENT_MARKERS.get(fpath.suffix.lstrip(".").lower())

    def _comment_lines(text: str, markers) -> List[tuple]:
        """Return [(lineno, stripped_text), …] for a file's comment lines."""
        out: List[tuple] = []
        for i, line in enumerate(text.split("\n"), 1):
            s = line.strip()
            if s and s.startswith(markers):  # str.startswith accepts a tuple
                out.append((i, s[:200]))
        return out

    async def _search_comments(pattern: str = "", path: str = ".", file_pattern: str = "") -> str:
        """Search ONLY code comments, or outline one file's comments.

        With `pattern`: matching comment lines across the tree (great for finding
        consistency markers like SISTER-PANEL or TODO). Without
        `pattern` on a single file: that file's comments in order, as a quick
        outline. Heuristic (stripped-line-start markers) — when a hit matters,
        read the file to see the full comment + surrounding code."""
        search_path = _safe_path(path)
        if not search_path:
            return "Error: path is outside the project root"
        if not search_path.exists():
            return f"Error: path does not exist: {path}"

        compiled = None
        if pattern:
            try:
                compiled = re.compile(pattern)
            except re.error as e:
                return f"Error: invalid regex pattern: {e}"

        # ── Outline mode: a single file, no pattern → all its comment lines ──
        if search_path.is_file() and not compiled:
            markers = _markers_for(search_path)
            if not markers:
                return f"No comment syntax known for {search_path.name}; nothing to outline."
            try:
                text = search_path.read_text(encoding="utf-8", errors="replace")
            except OSError as e:
                return f"Error reading {path}: {e}"
            cl = _comment_lines(text, markers)
            if not cl:
                return "(no comments found in this file)"
            await _observe([search_path])
            rel = search_path.relative_to(_PROJECT_ROOT)
            head = f"{rel} — {len(cl)} comment lines (outline):\n"
            return head + "\n".join(f"{n}: {t}" for n, t in cl)

        # ── Search mode: pattern required (single file or whole tree) ──
        if search_path.is_file():
            files_to_search = [search_path]
        else:
            if not compiled:
                return ("Error: searching a directory needs a `pattern`. To outline "
                        "one file's comments, pass that file's `path` with no pattern.")
            _EXCLUDE_DIRS = {"__pycache__", "node_modules", ".git", ".venv",
                             "venv", "env", ".source-backups", "temp",
                             ".mypy_cache", ".pytest_cache", ".cache"}
            files_to_search = []
            for f in search_path.rglob("*"):
                if any(part in _EXCLUDE_DIRS for part in f.parts):
                    continue
                if not f.is_file() or not _markers_for(f):
                    continue
                if file_pattern and not f.match(file_pattern):
                    continue
                files_to_search.append(f)
                if len(files_to_search) > 50000:
                    return "Error: too many files to search. Narrow the path or file_pattern."

        matches: List[str] = []
        for fpath in files_to_search:
            markers = _markers_for(fpath)
            if not markers:
                continue
            try:
                text = fpath.read_text(encoding="utf-8", errors="replace")
            except (OSError, UnicodeDecodeError):
                continue
            rel = fpath.relative_to(_PROJECT_ROOT)
            for n, t in _comment_lines(text, markers):
                if compiled.search(t):
                    matches.append(f"{rel}:{n}: {t}")
                    if len(matches) >= 300:
                        matches.append("... (showing first 300 matches; narrow your search)")
                        output = "\n".join(matches)
                        await _observe_search_output(output)
                        return output

        if not matches:
            return "No matching comments found."
        output = "\n".join(matches)
        await _observe_search_output(output)
        return output

    tools["search_comments"] = ToolInfo(
        name="search_comments",
        handler=_search_comments,
        parameters={
            "type": "object",
            "properties": {
                "pattern": {"type": "string", "description": "Regex matched within comment text only (e.g. 'SISTER-PANEL', 'TODO'). Leave empty to outline a single file's comments.", "default": ""},
                "path": {"type": "string", "description": "A file to outline, or a directory to search (default: project root).", "default": "."},
                "file_pattern": {"type": "string", "description": "Optional file glob for directory search (e.g. '*.py', '*.css').", "default": ""},
            },
            "required": [],
        },
    )

    # ── read_directory: list files with metadata ──
    async def _read_directory(path: str = ".", depth: int = 1) -> str:
        """List files and directories with size and modification time."""
        search_path = _safe_path(path)
        if not search_path:
            return "Error: path is outside the project root"
        if not search_path.exists():
            return f"Error: path does not exist: {path}"
        if not search_path.is_dir():
            return "Error: path is not a directory"

        lines: List[str] = []

        _EXCLUDE_DIRS = {"__pycache__", "node_modules", ".git", ".venv",
                         "venv", "env", ".source-backups", "temp",
                         ".mypy_cache", ".pytest_cache", ".cache"}

        def _walk(p: Path, current_depth: int):
            if current_depth > depth:
                return
            try:
                entries = sorted(p.iterdir(), key=lambda x: (not x.is_dir(), x.name))
            except PermissionError:
                return
            for entry in entries:
                if entry.name.startswith("."):
                    continue
                if entry.is_dir() and entry.name in _EXCLUDE_DIRS:
                    continue
                indent = "  " * current_depth
                if entry.is_dir():
                    lines.append(f"{indent}{entry.name}/")
                    _walk(entry, current_depth + 1)
                else:
                    try:
                        size = entry.stat().st_size
                        if size < 1024:
                            size_str = f"{size}B"
                        elif size < 1024 * 1024:
                            size_str = f"{size / 1024:.0f}K"
                        else:
                            size_str = f"{size / 1024 / 1024:.1f}M"
                        lines.append(f"{indent}{entry.name}  ({size_str})")
                    except OSError:
                        lines.append(f"{indent}{entry.name}")

        _walk(search_path, 0)

        if not lines:
            return "(empty directory)"
        summary = f"{search_path} — {len(lines)} entries\n"
        return summary + "\n".join(lines)

    tools["read_directory"] = ToolInfo(
        name="read_directory",
        handler=_read_directory,
        parameters={
            "type": "object",
            "properties": {
                "path": {"type": "string", "description": "Directory path (default: project root)", "default": "."},
                "depth": {"type": "integer", "description": "How deep to recurse (default: 1, use 0 for flat, 5 for deep)", "default": 1},
            },
            "required": [],
        },
    )

    # Flag the write/exec tools confirmation-gated (single source of truth above).
    _apply_confirm_flags(tools)

    logger.info(
        "Injected admin tools: read/write/edit/patch/delete/run/run_python/restart/search/dir (%d tools)",
        len(tools),
    )

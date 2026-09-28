"""
Catalog gate mechanics enforced inside the main agent loop.

Modes define access and their own prompt injection. Independently enabled gate
instances are assigned to modes and are the only source of mechanical behavior.
They are enforced synchronously in ``loop.py`` with ZERO extra LLM calls: every
rule is a flag or counter the loop already keeps.

Supported rules (all optional):

- ``require_pre_tool_progress_message`` (bool) — in AUTO mode the first write/edit tool
  is blocked until the user's latest message reads as approval/continuation
  sent AFTER the original request. Plan/Ask modes pass automatically — the
  guardrails gate already enforces confirmation there. This is the machine
  analogue of "verify the plan before it edits", with no LLM involved.
- ``require_verify_after_edit`` (bool) — after a write/edit tool executes, the
  NEXT tool call must be a read/verify tool (read_source, git diff, a safe
  run_command, …). Anything else is blocked with an actionable message telling
  the agent to verify its change first. Prevents edit-edit-edit without ever
  checking the result.
- ``max_tool_errors`` (int) — consecutive failed tool results beyond this count
  trip a ONE-TIME contract strike (a nudge via a tool_result, not a hard stop;
  the stall guard owns the hard stop). 0 = disabled.
- ``require_browser_journey_for_ui_change`` (bool) — successful UI-facing
  edits invalidate earlier browser proof. Completion requires a post-change
  browser journey that navigates to the interface and captures a user-visible
  assertion, on either browser surface (``browser_action`` or the development
  browser's ``dev_browser_action``). The service-worker cache bump does
  not arm a new journey revision. Failed backend switches remain scoped history; only a fresh failure on the active browser journey may be retained as a blocker.
- ``require_assertions`` (bool) — every run opens by DECLARING the user's
  concrete requirements as a checklist (``present_chat_component`` todo_list,
  which must be the run's first tool call) and cannot finish until the agent has
  ANSWERED for each declared assertion (met, or why not). Not edit-scoped and
  with no trivial-message filter: a research turn or a plain question declares an
  assertion too. Spatial/layout assertions are one KIND — machine-checked through
  the existing ``webagent_layout_assertion`` payload — rather than a separate
  gate. The standing instruction ships in the ``assertions`` gate descriptor and
  the resolution is enforced by the loop
  (``app/agent/assertions.missing_resolutions``).
- ``require_final_service_worker_cache_bump`` (bool) -- any repository change
  arms a completion requirement. The last source-edit event must include
  ``sw.js``, whose numeric ``CACHE`` generation is incremented. A later
  source edit invalidates that completion evidence and requires another bump.

- ``max_tool_calls_per_turn`` (int) — a global cap on executed tool calls per
  turn (per-tool budgets already exist separately). 0 = disabled.

The active rules are injected into the system prompt as a ``## Contract`` block
so the agent KNOWS its contract — the same visibility pattern the execution-mode
prompt uses (``app/agent/loop.py``). Mechanical violations return an actionable
``tool_result`` error (``error_type="contract_blocked"``) exactly like the
guardrail/validation paths, so the agent sees why and adjusts.

The loop node is ``contract_chk`` (see ``app/agent/loop_executor.py``) — it can
be disabled per-agent via loop_logic like any other node. Enforcement is
fail-open: a missing/broken contract config simply disables the rules.
"""

from __future__ import annotations

import ast
import json
import re
from typing import Any, Dict, List, Optional

# ── Canonical tool sets ──────────────────────────────────────────────────────
# Write/edit tools that change the repository (mirror of the _PATH_TOOLS set in
# app/agent/session_changes.py, plus commit). run_command/run_python/git_tool
# are deliberately NOT auto-classified as edits: whether they mutate depends on
# their arguments, so the contract only treats the unambiguous file writers as
# edits. (A mutating run_command still trips the mode gate in plan/ask.)
EDIT_TOOLS: frozenset = frozenset({
    "write_source", "edit_source", "patch_source", "delete_source",
    "resolve_conflict",
})

COMMIT_TOOLS: frozenset = frozenset({"commit_and_push"})

# Tools that inspect rather than mutate — satisfying ``require_verify_after_edit``.
# run_command / git_tool only count when their arguments are read-only
# (mirrors the guardrail per-arg exemptions in loop.py).
_VERIFY_TOOL_NAMES: frozenset = frozenset({
    "read_source", "read_directory", "search_source", "search_comments",
    "get_genui_data", "get_genui_logs", "list_genui", "session_search",
    "list_my_agents", "list_agent_templates", "list_webhooks", "list_skills",
})

# Read-only git_tool operations (mirror of SAFE_GIT_OPERATIONS in loop.py).
_SAFE_GIT_OPERATIONS: frozenset = frozenset({
    "status", "log", "diff", "show", "ls-files", "rev-parse", "blame",
    "describe", "for-each-ref", "reflog", "cat-file", "shortlog", "name-rev",
})

# Read-only shell prefixes (mirror of SAFE_RUN_COMMAND_PREFIXES in loop.py).
_SAFE_SHELL_PREFIXES: tuple = (
    "cd",
    "git status", "git log", "git diff", "git show", "git branch",
    "git ls-files", "git rev-parse", "git remote", "git stash list",
    "git config --get", "git config --list",
    "ls", "dir", "pwd", "tree", "stat", "file",
    "cat", "head", "tail", "type", "more",
    "find", "grep", "rg", "where", "which",
    "wc", "du", "df",
    "whoami", "hostname", "date", "uname", "id", "groups", "uptime",
    "ps", "env", "printenv", "echo",
    "python --version", "python -V", "python3 --version", "python3 -V",
    "pip list", "pip show", "pip --version",
    "node --version", "node -v",
    "npm list", "npm ls", "npm --version", "npm -v",
)
_HARD_UNSAFE_TOKENS: tuple = (">", "<", "`", "$(", "|&")
_CHAIN_SEPARATOR_RE = re.compile(r"\s*(?:&&|\|\||;|\|)\s*")

# Approval keywords — the user's latest message must read as explicit
# approval/continuation (word-boundary matched, so "ok" can't match inside
# "looking at"). Mirror of the confirm set in loop._check_user_confirmed.
_CONFIRM_KEYWORDS: tuple = (
    "yes", "go ahead", "proceed", "approved", "ok", "okay", "sure", "do it",
    "confirm", "go for it", "please do", "continue", "looks good",
    "sounds good", "that's fine", "approved",
)

# Default contract prompt block template — renders only the ACTIVE rules so the
# agent is told exactly what is being enforced.
_CONTRACT_PROMPT_TEMPLATE = (
    "You are working under a run contract that the loop enforces:\n{bullets}"
)

# NOTE: the spatial-relation regex that used to live here
# (``_LAYOUT_RELATION_RE`` / ``extract_layout_requirements``) is RETIRED. It
# scraped "after/before/… <anchor>" out of the user's prose, which turned phrases
# like "read before you write" into phantom acceptance contracts. Spatial
# requirements are now asserted like any other: the agent DECLARES them as
# checklist items under the ``assertions`` gate, and a layout-kind declaration is
# machine-checked through the existing ``webagent_layout_assertion`` payload
# (see ``app/agent/assertions.py``).


# ── Parsing ──────────────────────────────────────────────────────────────────

# The boolean rules `parse_contract` coerces, in canonical order.
_BOOL_RULES = (
    "require_pre_tool_progress_message", "require_verify_after_edit",
    "require_browser_journey_for_ui_change",
    "require_assertions",
    "require_final_service_worker_cache_bump",
    "require_user_named_reference_implementation",
    "require_file_checkout",
    "require_message_visibility",
)

# Rules whose DEFAULT is ON. A contract that omits the key — or an agent with no
# contract block at all — still gets the rule, so "on by default" is a real
# default rather than a UI-only tick. Opting out is explicit: `false` in
# metadata['contract'], which the Modes UI always writes when the rule is
# unticked. Every other rule defaults OFF, so for those a missing key and an
# unticked box stay distinguishable.
_RULE_DEFAULTS: Dict[str, bool] = {
    "require_file_checkout": True,
}


def parse_contract(
    agent_rec: Optional[Dict[str, Any]], mode: Optional[str] = None
) -> Dict[str, Any]:
    """Resolve gate-owned enforcement for one active mode.

    ``metadata['contract']`` and mode checklists are retired.  The catalog is
    intentionally fail-closed: missing, disabled, malformed, or out-of-scope
    gate instances project every known rule to OFF/zero.
    """
    try:
        from app.agent.gate_registry import gate_view_for_agent
        return gate_view_for_agent(agent_rec, mode=mode) if agent_rec else {}
    except Exception:  # noqa: BLE001 — discovery must never fail a run
        return {}


def active_rules(contract: Dict[str, Any]) -> List[str]:
    """Names of the rules that are actually on for this contract."""
    rules: List[str] = []
    if contract.get("require_pre_tool_progress_message"):
        rules.append("require_pre_tool_progress_message")
    if contract.get("require_verify_after_edit"):
        rules.append("require_verify_after_edit")
    if contract.get("require_browser_journey_for_ui_change"):
        rules.append("require_browser_journey_for_ui_change")
    if contract.get("require_assertions"):
        rules.append("require_assertions")
    if contract.get("require_user_named_reference_implementation"):
        rules.append("require_user_named_reference_implementation")
    if contract.get("require_file_checkout"):
        rules.append("require_file_checkout")
    if contract.get("require_message_visibility"):
        rules.append("require_message_visibility")
    if (contract.get("max_tool_errors") or 0) > 0:
        rules.append("max_tool_errors")
    if contract.get("require_final_service_worker_cache_bump"):
        rules.append("require_final_service_worker_cache_bump")
    if (contract.get("max_tool_calls_per_turn") or 0) > 0:
        rules.append("max_tool_calls_per_turn")
    return rules


def contract_prompt_block(
    contract: Dict[str, Any], agent_rec: Optional[Dict[str, Any]] = None,
    mode: Optional[str] = None,
) -> str:
    """The ``## Contract`` system-prompt block for the active rules.

    Data-driven gates own their instruction: an enabled instance's custom
    ``message`` wins, followed by the gate descriptor's default instruction.
    Legacy rule text remains only as a fallback for rules without a descriptor.
    """
    rules = active_rules(contract)
    try:
        from app.agent.gate_registry import prompt_instructions_for_agent

        gate_instructions = prompt_instructions_for_agent(agent_rec, mode=mode)
    except Exception:  # noqa: BLE001 — prompt guidance must never break a run
        gate_instructions = {}

    rendered_rules = []
    for rule in rules:
        instruction = gate_instructions.get(rule, "")
        instruction = str(instruction).strip()
        if not instruction:
            continue
        rendered_rules.append(
            instruction if instruction.startswith("-") else f"- {instruction}"
        )

    items = contract.get("checklist") or []
    bullets = "\n".join(rendered_rules)
    if items:
        extra = "\n".join(
            f"- {item.get('label')}" for item in items if item.get("label")
        )
        bullets = f"{bullets}\n{extra}" if bullets else extra
    if not bullets:
        return ""
    return _CONTRACT_PROMPT_TEMPLATE.format(bullets=bullets)

# ── Tool classification ──────────────────────────────────────────────────────

def is_edit_tool(name: str) -> bool:
    return name in EDIT_TOOLS

_UI_PATH_PREFIXES: tuple[str, ...] = (
    "ui/", "static/", "frontend/", "client/", "public/", "web/",
)
_UI_ROOT_FILES: frozenset[str] = frozenset({
    "index.html", "sw.js", "manifest.json", "site.webmanifest",
})


def is_ui_path(path: Any) -> bool:
    """Return whether a changed path can alter a user-facing browser interface."""
    normalized = str(path or "").strip().replace("\\", "/").lstrip("./").lower()
    if not normalized:
        return False
    if normalized in _UI_ROOT_FILES:
        return True
    if normalized.startswith(_UI_PATH_PREFIXES):
        return True
    return any(marker in f"/{normalized}" for marker in ("/templates/", "/static/"))


# The service-worker cache bump is repository bookkeeping, not an interface
# change: it exists to invalidate the offline cache, and the loop separately
# requires it to be the final source edit. It must therefore never arm a new
# browser-journey revision nor wipe proof the run already captured.
_CACHE_BUMP_FILES: frozenset[str] = frozenset({"sw.js"})


def is_cache_bump_path(path: Any) -> bool:
    """True for the service-worker cache-generation file (bookkeeping only)."""
    normalized = str(path or "").strip().replace("\\", "/").lstrip("./").lower()
    if not normalized:
        return False
    return normalized in _CACHE_BUMP_FILES or normalized.endswith("/sw.js")


def journey_ui_paths(changed_paths: Optional[List[str]] = None) -> List[str]:
    """UI paths that require a browser journey — real interfaces, not the
    service-worker cache bump."""
    return [
        str(path) for path in (changed_paths or [])
        if is_ui_path(path) and not is_cache_bump_path(path)
    ]


_BROWSER_ASSERT_ACTIONS = frozenset({
    "get_text", "get_html", "screenshot", "title", "url",
})
_BROWSER_INTERACTION_ACTIONS = frozenset({
    "click", "type", "scroll", "hover", "drag", "select", "press", "key",
})

# Tools that can carry browser-journey evidence. Two browser surfaces exist: the
# generic Playwright pair (``browser_action`` / ``browser_backend``) and the
# authenticated Diagnostics development browser (``dev_browser_action``) that
# most WebAgent sessions are actually handed. Both are real journeys, so both
# must be credited — otherwise a successful journey is recorded as no journey
# at all and the run can never satisfy ``require_browser_journey_for_ui_change``.
JOURNEY_TOOL_NAMES: frozenset = frozenset({
    "browser_action", "browser_backend", "dev_browser_action",
})


def is_journey_tool(name: str) -> bool:
    """True when a tool call may contribute browser-journey evidence."""
    return name in JOURNEY_TOOL_NAMES


def is_commit_tool(name: str) -> bool:
    return name in COMMIT_TOOLS


def is_verify_tool(name: str, tool_args: Any = None) -> bool:
    """True when the call reads/verifies rather than mutates — satisfies
    ``require_verify_after_edit``. run_command / git_tool only count with
    read-only arguments (mirrors the guardrail per-arg exemptions)."""
    if name in _VERIFY_TOOL_NAMES:
        return True
    args = tool_args if isinstance(tool_args, dict) else {}
    if name == "git_tool":
        op = str(args.get("operation", "")).strip().lower()
        return op in _SAFE_GIT_OPERATIONS
    if name == "run_command":
        command = args.get("command", "")
        if _is_safe_shell_command(command):
            return True
        from app.agent.verification_evidence import semantic_verification_kind
        return bool(semantic_verification_kind(command))
    if name == "http_request":
        return str(args.get("method", "GET")).strip().lower() in ("get", "head", "options")
    if name in ("browser_action", "dev_browser_action"):
        return str(args.get("action", "")).strip().lower() in (
            "open", "navigate", "get_text", "get_html", "screenshot", "wait", "title", "url", "close",
        )
    return False


def _is_safe_shell_command(command: Any) -> bool:
    if not isinstance(command, str):
        return False
    stripped = command.strip()
    if not stripped:
        return False
    if any(tok in stripped for tok in _HARD_UNSAFE_TOKENS):
        return False
    for piece in _CHAIN_SEPARATOR_RE.split(stripped):
        piece = piece.strip()
        if not piece:
            return False
        low = piece.lower()
        if not any(low == p or low.startswith(p + " ") for p in _SAFE_SHELL_PREFIXES):
            return False
    return True


def add_manual_resume_approval(
    messages: List[Dict[str, Any]], resume_reason: Optional[str],
) -> List[Dict[str, Any]]:
    """Add the one-click Continue approval to a manually resumed model history.

    Manual resume deliberately continues the interrupted turn instead of
    creating a second durable user turn. The click is still an explicit user
    approval, so expose the same canonical "Continue" text that the typed path
    sends. Automatic watchdog/boot resumes must never gain this authorization.
    """
    if str(resume_reason or "").lower() != "manual":
        return messages
    out = list(messages)
    out.append({
        "role": "user",
        "content": "Continue",
    })
    return out


# ── Approval detection ───────────────────────────────────────────────────────

def user_approved_plan(messages: List[Dict[str, Any]]) -> bool:
    """True when the user's LATEST message is an explicit approval/continuation
    sent AFTER the original request.

    The first user message is never approval (the plan-mode rule: the original
    request predates any plan). Only a newer user message that reads as
    approval counts. Word-boundary matching so "ok" can't match inside
    "looking at" — same discipline as loop._check_user_confirmed.
    """
    last_user_idx: Optional[int] = None
    first_user_idx: Optional[int] = None
    for i, msg in enumerate(messages):
        if msg.get("role") != "user":
            continue
        if first_user_idx is None:
            first_user_idx = i
        last_user_idx = i
    if last_user_idx is None or first_user_idx is None:
        return False
    if last_user_idx == first_user_idx:
        return False  # the original request alone is never approval
    text = (messages[last_user_idx].get("content") or "").lower()
    return any(
        re.search(r"\b" + re.escape(kw) + r"\b", text)
        for kw in _CONFIRM_KEYWORDS
    )


# ── Mechanical checks (read state, return a violation message or None) ──────

def check_pre_tool_progress_message(
    contract: Dict[str, Any],
    state: Dict[str, Any],
    tool_name: str,
    execution_mode: str,
    announcement: str = "",
    turn: Optional[int] = None,
    edit_call: Optional[bool] = None,
) -> Optional[str]:
    """Hold AUTO-mode edits until a pre-edit progress message has been sent.

    Plan/Ask modes pass automatically because their confirmation guardrail owns
    the boundary. In Auto, every edit stays blocked until the agent sends the
    requested progress message as a separate, tool-free assistant response. No
    approval is requested or awaited; publishing that message lets the run carry
    on automatically.

    The gate is deliberately strict. Sibling edits in the intercepted batch and
    edits retried in later turns remain blocked until the standalone message is
    published. That guarantees the user-visible message contains no tool calls
    from the work it announces.

    ``turn`` remains an accepted compatibility argument for callers that record
    the assistant turn, but no longer changes this strict boundary.
    """
    if not contract.get("require_pre_tool_progress_message"):
        return None
    if edit_call is None:
        if not is_edit_tool(tool_name):
            return None
    elif not (edit_call or is_edit_tool(tool_name)):
        return None
    if execution_mode in ("plan", "ask"):
        return None
    if state.get("plan_approved") or state.get("edit_executed"):
        return None
    if state.get("pre_tool_progress_message_done"):
        return None

    state["pre_tool_progress_message_pending"] = True
    state["pre_tool_progress_message_turn"] = turn
    text = str(announcement or "").strip()
    if text:
        return text
    return (
        "Contract: this edit was not run. You are in AUTO mode with the "
        "'Pre-edit progress message' gate enabled — send the user one deliberate, "
        "tool-free message describing the work you are about to do before your first "
        "edit, then carry on automatically. Do not request approval."
    )


def check_verify_after_edit(
    contract: Dict[str, Any],
    state: Dict[str, Any],
    tool_name: str,
    tool_args: Any,
) -> Optional[str]:
    """Block non-verify tools after an edit until a verify tool runs."""
    if not contract.get("require_verify_after_edit"):
        return None
    if not state.get("awaiting_verify"):
        return None
    if is_verify_tool(tool_name, tool_args):
        return None
    return (
        "Contract: you edited a file but have not verified the change. Run a "
        "verify step first — read the file back, run a test, or check a diff "
        "(read_source / run_command with a read-only command / git_tool diff). "
        "I did not run this tool."
    )


def _ui_browser_blocker_is_current(journey: Dict[str, Any]) -> bool:
    """A blocker is usable only for the active scope/generation and while fresh."""
    blocker = journey.get("blocker")
    if not isinstance(blocker, dict) or not blocker.get("negative_capability"):
        return False
    current = journey.get("backend_evidence")
    if isinstance(current, dict):
        for key in ("resource_scope", "resource_id", "environment_generation"):
            if current.get(key) and current.get(key) != blocker.get(key):
                return False
    from app.agent.fresh_evidence import evidence_staleness
    stale, _reason, _generation = evidence_staleness(
        blocker,
        current_scope=str(blocker.get("resource_scope") or "") or None,
        current_resource_id=str(blocker.get("resource_id") or "") or None,
    )
    return not stale


def ui_journey_requirement(
    contract: Dict[str, Any],
    state: Dict[str, Any],
) -> Optional[str]:
    """Return the outstanding UI-journey requirement, or None when satisfied.

    Spatial/layout acceptance is no longer enforced here or by a sibling gate:
    it is one KIND of assertion under the ``assertions`` gate, matched against
    the agent's declared items through the ``webagent_layout_assertion`` payload
    (app/agent/assertions.py). Enabling a browser journey therefore never arms —
    or advertises — a spatial-DOM requirement.
    """
    if not contract.get("require_browser_journey_for_ui_change"):
        return None
    paths = list(state.get("ui_changed_paths") or [])
    if not paths:
        return None
    journey = state.get("ui_journey") or {}
    if journey.get("complete"):
        return None
    blocker = journey.get("blocker")
    if isinstance(blocker, dict) and blocker.get("negative_capability"):
        if not _ui_browser_blocker_is_current(journey):
            journey["blocker"] = None
            blocker = None
    if isinstance(blocker, dict) and blocker.get("negative_capability"):
        return (
            "UI browser journey remains unverified because a fresh browser "
            f"capability probe failed for {blocker.get('resource_scope') or 'the selected backend'} "
            f"in environment generation {blocker.get('environment_generation') or 'unknown'}. "
            "Report this as a blocker; do not claim the UI change is verified."
        )
    return (
        "UI browser journey required after the latest UI change. Navigate to the "
        "affected interface, exercise the changed workflow or state, then capture "
        "a user-visible assertion with browser_action or dev_browser_action "
        "get_text, get_html, or screenshot. Source inspection and unit tests are "
        "insufficient."
    )




def check_assertions_declared(
    contract: Dict[str, Any],
    state: Dict[str, Any],
    tool_name: str,
    tool_args: Any,
    declaration: str = "",
) -> Optional[str]:
    """Hold the run's FIRST tool call until it opens the assertions contract.

    The declaration beat is mechanical: the first tool call must be a
    ``present_chat_component`` TODO LIST, and the items are captured from that
    call's own arguments. Every other first call — including a read — is blocked
    with the assembled declaration instruction, exactly like the pre-tool progress-message gate's
    interception. Nothing is checked about the wording: the only pass/fail is
    that a checklist was declared and captured.

    This is deliberately NOT edit-scoped and has NO trivial-message filter. A
    conversational turn declares the question itself; a research turn declares
    the research requirement. The contract is opened on every run the gate
    governs, so the closing beat always has something to answer for.

    ``declaration`` is the assembled instruction (floor + app-level + per-agent
    guidance); the caller passes the composed text in rather than this module
    reaching for config.
    """
    if not contract.get("require_assertions"):
        return None
    if state.get("assertions_declared"):
        return None
    if tool_name == "present_chat_component":
        from app.agent.assertions import PENDING_KEY, items_from_tool_args

        items = items_from_tool_args(tool_args)
        if items:
            state["assertion_items"] = items
            state["assertions_declared"] = True
            # Open the opening-message window. The prose the declaration beat
            # asks for alongside the checklist IS the run's introduction, and the
            # loop publishes it as the run's promoted opening bubble — which is
            # what makes this gate also the introductory gate. Mirrors
            # ``pre_tool_progress_message_pending``.
            state[PENDING_KEY] = True
            return None
    text = str(declaration or "").strip()
    if text:
        return text
    return (
        "Contract: this tool call was not run. Before your first tool call, "
        "declare the user's concrete requirements as a checklist by calling "
        "present_chat_component with type 'todo_list' — shape: "
        '{"type": "todo_list", "data": {"items": [{"label": "..."}]}} — one item '
        "per requirement, each with a non-empty label, then carry straight on "
        "with the work. I did not run this tool."
    )


def assertions_requirement(
    contract: Dict[str, Any],
    state: Dict[str, Any],
) -> bool:
    """Whether this run still owes an answer for a declared assertion.

    Reads the labels the loop stamped at the completion path
    (``assertions_unresolved``), so the closing-message gate can forbid a clean
    ``complete`` without re-running the envelope parser. The assertions
    resolution itself is enforced by the loop
    (``app/agent/assertions.missing_resolutions``).
    """
    if not contract.get("require_assertions"):
        return False
    return bool(state.get("assertions_unresolved"))


def service_worker_cache_requirement(
    contract: Dict[str, Any],
    state: Dict[str, Any],
) -> Optional[str]:
    """Return the outstanding final-cache-bump requirement, if any."""
    if not contract.get("require_final_service_worker_cache_bump"):
        return None
    if not state.get("repository_change_revision"):
        return None
    if not state.get("service_worker_cache_bump_pending"):
        return None
    return (
        "Repository changes require a final sw.js cache-generation bump. "
        "Increment the numeric CACHE suffix in sw.js as the final source edit; "
        "if any source file changes afterward, bump sw.js again."
    )
def check_tool_cap(
    contract: Dict[str, Any],
    state: Dict[str, Any],
) -> Optional[str]:
    """Global per-turn tool-call cap (0 = disabled)."""
    cap = int(contract.get("max_tool_calls_per_turn") or 0)
    if cap <= 0:
        return None
    if int(state.get("tool_calls_this_turn") or 0) >= cap:
        return (
            f"Contract: you have reached the per-turn tool-call budget of "
            f"{cap}. Stop calling tools this turn — synthesize what you have, "
            f"or give the user your best answer. I did not run this tool."
        )
    return None


def error_strike(
    contract: Dict[str, Any],
    state: Dict[str, Any],
) -> Optional[str]:
    """ONE-TIME nudge when consecutive tool errors cross the threshold.

    Returns a message only once per run (guarded by ``error_strike_sent``,
    set here when the strike fires); the stall guard owns the hard stop, this
    is a soft course-correction.
    """
    threshold = int(contract.get("max_tool_errors") or 0)
    if threshold <= 0:
        return None
    if state.get("error_strike_sent"):
        return None
    if int(state.get("consecutive_errors") or 0) >= threshold:
        state["error_strike_sent"] = True
        return (
            f"Contract: {state.get('consecutive_errors')} consecutive tool "
            f"calls have failed (limit {threshold}). Stop retrying the same "
            f"approach — change tactics, ask a clarifying question, or report "
            f"the blocker to the user."
        )
    return None


def mechanical_check(
    contract: Dict[str, Any],
    state: Dict[str, Any],
    *,
    tool_name: str,
    tool_args: Any = None,
    execution_mode: str = "ask",
    pre_tool_progress_message: str = "",
    assertions_declaration: str = "",
    turn: Optional[int] = None,
    edit_call: Optional[bool] = None,
) -> Optional[str]:
    """Run every mechanical rule for one tool call, in order.

    Returns the first violation message (the loop blocks the call and feeds
    the message back as an actionable tool_result) or None when the call may
    proceed. Pure and synchronous — no LLM, no I/O.

    ``pre_tool_progress_message`` and ``assertions_declaration`` are the two opening-beat
    instructions. Those rules are mechanical, but their wording is configurable
    guidance, so the caller passes the composed text in rather than this module
    reaching for config. ``turn`` is the assistant turn this call belongs to — the
    plan window uses it to tell a sibling edit in the same batch from the agent
    genuinely moving on (see ``check_pre_tool_progress_message``); omit it and the rule
    falls back to call-order closing. ``edit_call`` is the caller's own verdict on
    whether this call counts as an edit — the loop derives it from the run's
    resolved ask-permission set so the plan rule can follow tool permissions
    rather than a name list; omit it and the name list decides.
    """
    msg = check_pre_tool_progress_message(
        contract, state, tool_name, execution_mode,
        announcement=pre_tool_progress_message, turn=turn, edit_call=edit_call,
    )
    if msg:
        return msg
    msg = check_assertions_declared(
        contract, state, tool_name, tool_args,
        declaration=assertions_declaration,
    )
    if msg:
        return msg
    msg = check_verify_after_edit(contract, state, tool_name, tool_args)
    if msg:
        return msg
    msg = check_tool_cap(contract, state)
    if msg:
        return msg
    return error_strike(contract, state)


# ── State mutations (called by the loop at execution time) ──────────────────

def new_contract_state(
    user_messages: Optional[List[Dict[str, Any]]] = None,
) -> Dict[str, Any]:
    """Fresh per-run contract state (dies with the run — the 'short state').

    ``user_messages`` is kept for signature compatibility; requirements are no
    longer scraped from the user's prose (the spatial regex is retired under the
    ``assertions`` gate) — the agent declares its own assertions instead.
    """
    return {
        "plan_approved": False,       # user approved the plan (or an edit ran)
        "edit_executed": False,       # at least one edit has run
        # Pre-tool progress-message window (see app/agent/pre_tool_progress.py). ``pending``
        # is set when the announcement is delivered and cleared when the window
        # closes; ``done`` records that the pre-tool progress-message gate is finished with this run,
        # so an ignored announcement is never re-delivered.
        "pre_tool_progress_message_pending": False,
        "pre_tool_progress_message_done": False,
        # Assertions contract (see app/agent/assertions.py). ``assertion_items``
        # holds the declared checklist captured from the run's first tool call;
        # ``assertions_unresolved`` is stamped at the completion path so the
        # closing gate can forbid a clean ``complete``.
        "assertions_declared": False,
        "assertion_items": [],
        # Opening-message window (see app/agent/assertions.py). ``pending`` is set
        # when the declaration is captured and cleared when the window closes;
        # ``done`` records that the introductory gate is finished with this run,
        # so a partial or resumed run never publishes a second introduction.
        "assertion_announcement_pending": False,
        "assertion_announcement_done": False,
        "assertions_unresolved": [],
        "assertions_completion_retries": 0,
        "awaiting_verify": False,     # an edit ran; verify required before next tool
        "consecutive_errors": 0,      # consecutive failed tool results
        "error_strike_sent": False,   # one-time soft strike delivered
        "tool_calls_this_turn": 0,    # executed tool calls this turn
        "ui_changed_paths": [],
        "ui_change_revision": 0,
        "ui_layout_assertions": [],
        "ui_journey": _new_ui_journey_state(0),
        "ui_journey_completion_retries": 0,
        "repository_change_revision": 0,
        "service_worker_cache_bump_pending": False,
        "service_worker_cache_bump_revision": 0,
        "service_worker_cache_completion_retries": 0,
    }


def _new_ui_journey_state(revision: int) -> Dict[str, Any]:
    """Create revision-scoped journey evidence for one UI edit generation."""
    return {"revision": revision, "navigated": False, "interacted": False,
            "asserted": False, "complete": False, "events": [], "attempts": [],
            "outcome": "pending", "blocker": None, "backend_evidence": None,
            "capability_failures": []}


def mark_tool_scheduled(state: Dict[str, Any]) -> None:
    """Record a tool call that passed validation and guardrails.

    The pre-edit progress-message gate is satisfied only by its dedicated,
    tool-free publication path; scheduling unrelated tools must never release
    that boundary.
    """
    state["tool_calls_this_turn"] = int(state.get("tool_calls_this_turn") or 0) + 1


def mark_tool_outcome(state: Dict[str, Any], success: bool) -> None:
    """Called after a tool executes — tracks the consecutive-error counter."""
    if success:
        state["consecutive_errors"] = 0
    else:
        state["consecutive_errors"] = int(state.get("consecutive_errors") or 0) + 1


def mark_edit_executed(
    state: Dict[str, Any],
    changed_paths: Optional[List[str]] = None,
) -> None:
    """Called after an edit succeeds; UI edits invalidate prior journey proof."""
    state["edit_executed"] = True
    state["plan_approved"] = True
    state["awaiting_verify"] = True
    ui_paths = journey_ui_paths(changed_paths)
    if ui_paths:
        # Accumulate across the run: a later edit must not drop the interface
        # files an earlier edit changed, or the journey requirement would be
        # scoped to whichever file happened to be edited last.
        state["ui_changed_paths"] = sorted(
            set(state.get("ui_changed_paths") or []) | set(ui_paths)
        )
        state["ui_change_revision"] = int(state.get("ui_change_revision") or 0) + 1
        state["ui_layout_assertions"] = []
        state["ui_journey"] = _new_ui_journey_state(state["ui_change_revision"])
        state["ui_journey_completion_retries"] = 0


def begin_ui_journey_recovery_attempt(state: Dict[str, Any]) -> Dict[str, Any]:
    """Persist a bounded self-healing attempt before asking the model to retry."""
    journey = state.setdefault("ui_journey", _new_ui_journey_state(0))
    attempts = journey.setdefault("attempts", [])
    blocker = journey.get("blocker") if isinstance(journey.get("blocker"), dict) else {}
    scope = str(blocker.get("resource_scope") or "")
    strategy = "same_backend"
    if blocker.get("negative_capability"):
        strategy = "switch_to_server" if "headless" in scope else "switch_to_headless"
    attempt = {"number": len(attempts) + 1, "revision": int(state.get("ui_change_revision") or 0),
               "strategy": strategy, "from_scope": scope or None, "status": "requested"}
    attempts.append(attempt); journey["outcome"] = "retrying"
    return attempt


def ui_journey_recovery_instruction(state: Dict[str, Any], attempt: Dict[str, Any]) -> str:
    """Give the model a precise, safe recovery instruction for the current revision."""
    journey = state.get("ui_journey") or {}; missing = []
    if not journey.get("navigated"): missing.append("navigate")
    if not journey.get("interacted"): missing.append("exercise the changed control")
    if not journey.get("asserted"): missing.append("capture visible evidence with get_text, get_html, or screenshot")
    action = "; then ".join(missing) or "capture fresh visible evidence"; backend = ""
    if attempt.get("strategy") == "switch_to_server": backend = " First call browser_backend(mode=server), then run the journey there."
    elif attempt.get("strategy") == "switch_to_headless": backend = " First call browser_backend(mode=headless), then run the journey there."
    paths = ", ".join(str(path) for path in (state.get("ui_changed_paths") or []))
    return (f"UI JOURNEY RECOVERY attempt {attempt.get('number')} for UI revision "
            f"{state.get('ui_change_revision')}, changed paths: {paths or 'unknown'}. "
            f"{action}.{backend} The development browser (`dev_browser_action`) is a valid "
            "journey surface too — use it when "
            "that is the browser this session actually has. Do not close the browser "
            "until the visible assertion has succeeded. Report the route, interaction, "
            "assertion, and backend scope.")


def mark_repository_changes(
    state: Dict[str, Any],
    changed_paths: Optional[List[str]] = None,
) -> None:
    """Track whether the latest repository edit event includes sw.js."""
    paths = [
        str(path).replace("\\", "/").strip().lstrip("./").lower()
        for path in (changed_paths or [])
        if str(path).strip()
    ]
    if not paths:
        return
    revision = int(state.get("repository_change_revision") or 0) + 1
    state["repository_change_revision"] = revision
    bumped = any(path == "sw.js" or path.endswith("/sw.js") for path in paths)
    state["service_worker_cache_bump_pending"] = not bumped
    if bumped:
        state["service_worker_cache_bump_revision"] = revision
    state["service_worker_cache_completion_retries"] = 0


# Observations that assert nothing: a JS probe that returned no state at all, a
# falsy boolean, or an empty container. They prove neither presence nor absence
# of the required UI state, so they must not satisfy the assertion step.
_EMPTY_OBSERVATIONS = frozenset({
    "", "null", "none", "undefined", "false", "{}", "[]", "nan",
})


def _observation_is_substantive(observation: Any) -> bool:
    """True when an ``evaluate`` result carries real observed interface state."""
    raw = str(observation or "").strip()
    if len(raw) < 2:
        return False
    return raw.lower() not in _EMPTY_OBSERVATIONS


def mark_browser_journey_event(
    state: Dict[str, Any],
    *,
    tool_name: str,
    tool_args: Any,
    success: bool,
    evidence: Any,
    observation: Any = None,
) -> None:
    """Record post-UI-mutation browser steps and complete a real journey."""
    if not state.get("ui_changed_paths"):
        return
    if not is_journey_tool(tool_name):
        return
    ev = dict(evidence) if isinstance(evidence, dict) else {}
    journey = state.setdefault("ui_journey", {})
    args = tool_args if isinstance(tool_args, dict) else {}
    if not success:
        if ev.get("negative_capability"):
            failure = {
                **ev,
                "tool": tool_name,
                "requested_mode": str(args.get("mode") or ""),
            }
            failures = journey.setdefault("capability_failures", [])
            failures.append(failure)
            del failures[:-20]
            # A failed backend-selection attempt proves only that requested
            # scope unavailable. It cannot waive a journey on the backend that
            # remains selected. A failed browser action probes the active scope.
            if tool_name != "browser_backend":
                journey["backend_evidence"] = ev
                journey["blocker"] = ev
        return
    journey["backend_evidence"] = ev
    journey["blocker"] = None
    if tool_name == "browser_backend":
        return
    action = str(args.get("action") or "").strip().lower()
    if action == "open" and tool_name == "dev_browser_action":
        # Opening the dev browser loads (and authenticates) a route: a navigation.
        action = "navigate"
    if action == "evaluate" and journey.get("navigated"):
        payload: Dict[str, Any] = {}
        raw = str(observation or "").strip()
        for loader in (json.loads, ast.literal_eval):
            try:
                candidate = loader(raw)
            except Exception:
                continue
            if isinstance(candidate, dict):
                payload = candidate
                break
        for key in ("result", "value", "data"):
            if isinstance(payload.get(key), dict):
                payload = dict(payload[key])
                break
        if payload.get("webagent_layout_assertion") is True:
            relation = re.sub(r"\s+", " ", str(payload.get("relation") or "").lower()).strip()
            anchor = re.sub(r"\s+", " ", str(payload.get("anchor") or "").lower()).strip()
            passed = payload.get("passed") is True
            # Record any passing spatial assertion. The assertions gate matches
            # these against the agent's DECLARED layout items by relation+anchor
            # (app/agent/assertions._layout_covers); there is no pre-extracted
            # requirement list any more — the spatial regex is retired.
            if passed:
                assertion = {"relation": relation, "anchor": anchor, "passed": True}
                assertions = state.setdefault("ui_layout_assertions", [])
                if assertion not in assertions:
                    assertions.append(assertion)
                journey["asserted"] = True
                journey["complete"] = bool(journey.get("interacted"))
        elif _observation_is_substantive(observation):
            # The development browser's documented assertion idiom is `evaluate`
            # returning the observed state — a chip's class list and box shadow,
            # a tooltip's text, an element rect. A JS assertion that returns real
            # UI state is user-visible evidence just like get_text, so credit it.
            # Otherwise a journey proven entirely through evaluate never counts
            # and the agent's strongest evidence is silently discarded.
            journey["asserted"] = True
            journey["complete"] = bool(journey.get("interacted"))
    event = {
        "action": action,
        "url": str(args.get("url") or args.get("path") or "")[:500],
        "evidence": ev,
    }
    attempts = journey.get("attempts") or []
    if attempts:
        event["attempt"] = attempts[-1].get("number")
        attempts[-1]["status"] = "in_progress"
    events = journey.setdefault("events", [])
    events.append(event)
    del events[:-20]
    # An interaction resets the assertion: a click changes interface state, so
    # evidence captured before it no longer proves the current state, and the
    # journey needs a fresh POST-interaction assertion. A navigation likewise
    # invalidates the assertion (the page state changed) but leaves the recorded
    # interaction standing, so re-verifying from a clean page can complete the
    # journey again with one fresh assertion.
    if action == "navigate":
        journey["navigated"] = True
        journey["asserted"] = False
        journey["complete"] = False
    elif action in _BROWSER_INTERACTION_ACTIONS and journey.get("navigated"):
        journey["interacted"] = True
        journey["asserted"] = False
        journey["complete"] = False
    elif action in _BROWSER_ASSERT_ACTIONS and journey.get("navigated"):
        journey["asserted"] = True
        journey["complete"] = bool(journey.get("interacted"))
    if journey.get("complete"):
        journey["outcome"] = "verified"
        if attempts:
            attempts[-1]["status"] = "verified"



def mark_verify_executed(state: Dict[str, Any]) -> None:
    """Called after a verify tool succeeds — clears the verify-await flag."""
    state["awaiting_verify"] = False

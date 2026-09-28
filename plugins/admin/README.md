# `plugins/admin/` — Privileged filesystem & source-control tools

**WARNING: The files here grant the agent broad filesystem read/write/delete and shell-command execution. These are debug/development tools, NOT normal user-facing features.**

This folder holds the **implementation** behind the privileged developer abilities. It is the shared library that three drop-in abilities build on — each ability (in `plugins/abilities/`) declares *which* tools it gates; the handlers live here.

| Ability (`plugins/abilities/`) | Gets (gated in `app/tools/loader.py`) | Built from |
|---|---|---|
| **Codebase Admin** (`codebase_admin`) | full filesystem + shell + `search_comments` (no git — version control is the separate Git Control ability; shell `git …` still works via `run_command`) | `inject_source_tools` |
| **Git Control** (`git_control`) | `git_tool` / `resolve_conflict` / `commit_and_push` only | `_inject_git_tools` **defined in its own ability file** (not in this shared library) |
| **UI Admin** (`ui_admin`) | file edits confined to `ui/` (`.css`/`.html`) | `inject_ui_tools` (wraps the source tools behind `ui_guardrails`) |

> **Tied to the ability — no ability, no tools.** The loader only imports and injects these handlers when the matching ability is enabled (admin-level **and** per-agent). Disable or delete the ability and the agent never receives the tools. The HTTP router below is the one surface mounted independently of the ability.

> **Git Control — optional repo-change watcher.** Beyond the gated tools, the Git Control ability carries a per-agent **"Notify me about repo changes"** setting (off by default, in its `git_control.json` `config.settings`). When on, the ability's `start_background` service (leader-gated, discovered via `background_service_hooks`) polls the working tree every ~60s and, when it **newly** diverges from the last commit (uncommitted edits) or has unpushed local commits, drops a **passive** heads-up message into that agent's most-recent chat session — no agent turn runs. It's persisted (shows on session open) and broadcast live as a `repo_change_notice` WebSocket event (rendered in `ui/shared/js/agentWs.js`). All logic lives in `plugins/abilities/Administrator/git_control/git_control.py`.

## What's here

| File | Purpose |
|------|---------|
| **`source_tools.py`** | Agent tool wrappers. `inject_source_tools` builds the filesystem + shell suite (`read_source`, `write_source`, `edit_source`, `patch_source`, `delete_source`, `search_source`, **`search_comments`**, `read_directory`, `run_command`, `run_python`, `restart_server`) — **no git**. It also exports the low-level helpers (`_safe_path`, `_write_file_direct`, `_run_subprocess`) and the `_CONFIRM_TOOLS` gating that the git tools reuse. The **git** tools themselves live in the Git Control ability file (`plugins/abilities/Administrator/git_control/git_control.py`'s `_inject_git_tools`), since nothing else uses them. |
| **`source.py`** | FastAPI router (`/admin/source/`) — REST endpoints for reading, writing, deleting files and running shell commands. Backs up overwritten files to `.source-backups/`. Mounted in `app/main.py` behind an `ImportError` guard. |
| **`guardrails.py`** | Optional deny-list. Blocks `.env`, `.env.*`, shell-history, `.gitconfig`, `.ssh/*`, and dangerous commands (`rm -rf /`, fork bombs). **Delete this file to remove all path/command restrictions.** |
| **`ui_tools.py`** | `inject_ui_tools` — re-exposes only the file tools (no shell / python / git / restart), each wrapped with a `ui_guardrails` pre-check. Backs the **UI Admin** ability. |
| **`ui_guardrails.py`** | UI-only boundary: confines paths to `ui/`, restricts writes to `.css`/`.html`, marks sensitive pages read-only. |

## How injection works

These three abilities are **self-contained drop-ins**. Each ability file in `plugins/abilities/` (`codebase_admin.py`, `git_control.py`, `ui_admin.py`) exposes a `build_tools(...)` hook that the core loader discovers and calls generically for every enabled ability — there is **no** per-ability `if/elif` in `app/tools/loader.py` anymore. `build_tools` returns `{tool_name: handler}`, and the loader then reads each module's `TOOL_SCHEMAS` / `DESTRUCTIVE` (populated *during* the call) to register the tools.

The handlers live in `inject_*` functions that mutate a tools dict with `ToolInfo` objects (the historical shape). The file/shell injectors (`inject_source_tools`, `inject_ui_tools`) live here in the shared library; the **git** injector (`_inject_git_tools`) lives in the Git Control ability file because it is its only user. The bridge in all cases is **`adapter.extract_injected`** (shared infra, used by every admin ability):

- Each ability's `build_tools` calls `extract_injected(<its injector>, user_id)` — `inject_source_tools` for Codebase Admin, `_inject_git_tools` for Git Control, `inject_ui_tools` for UI Admin.
- `extract_injected` runs the injector against a *throwaway* dict, then pulls `handler` and `parameters` off every `ToolInfo` and folds `destructive` / `requires_confirmation` into a destructive set. It returns the `(handlers, schemas, destructive)` triple the loader contract wants — so the schemas and guardrail flags come straight from the `ToolInfo` objects and **never drift**.

> **Which tools require confirmation.** `source_tools.py` owns `_CONFIRM_TOOLS`, the source of truth for the file/shell **write/exec** tools that must pause for the user's go-ahead (`write_source`, `edit_source`, `patch_source`, `delete_source`, `run_command`, `run_python`, `restart_server`). `inject_source_tools` stamps `destructive = requires_confirmation = True` on each (via `_apply_confirm_flags`), so the flags flow through `extract_injected` into Codebase Admin's and UI Admin's `DESTRUCTIVE` sets. The **git** tools (`git_tool`, `commit_and_push`, `resolve_conflict`) are confirm-gated the same way, but stamped in the **Git Control ability file itself** (`_inject_git_tools` sets the flags on its three `ToolInfo` objects) — since the git tools now live there. Read-only tools (`read_source`, `search_source`, `search_comments`, `read_directory`) are deliberately left out. At run time the agent loop turns this into a confirmation pause in **Ask** and **Plan** execution modes; **Auto** mode runs everything. Two tools are **dual-use** (read *and* write), so the loop exempts their read-only invocations at run time so a broad set doesn't gate routine reads: `run_command` skips safe read-only shell commands (`_is_safe_shell_command`), and `git_tool` skips read-only git operations — `status` / `log` / `diff` / `show` / … (`_is_safe_git_operation`) — while its mutating ops (`commit` / `push` / `reset` / `checkout` / …) still pause. The loop keys these exemptions off the tool **name**, so they apply wherever the tool is defined.

Per-ability specifics:

| Ability | `build_tools` does |
|---|---|
| `codebase_admin` | `extract_injected(inject_source_tools, user_id)` for the files + shell suite (no git), **plus** a `db_query` handler defined in the ability file itself (`plugins/abilities/Administrator/codebase_admin.py`'s `_db_query_core`, merged into handlers + schemas; `db_query` confirm-gates — its writes edit the user's agent prompt slots — so the ability adds it to its `DESTRUCTIVE` set). |
| `git_control` | `extract_injected(_inject_git_tools, user_id)` — and `_inject_git_tools` is defined **in the Git Control ability file**, not in this library. It builds `git_tool` / `resolve_conflict` / `commit_and_push` (reusing this library's shared `_safe_path` / `_write_file_direct` / `_run_subprocess` helpers) and stamps the three confirm-gated. |
| `ui_admin` | `extract_injected(inject_ui_tools, user_id)`, but returns `{}` when `enabled_providers` contains `codebase_admin` (the unrestricted superset wins). It accepts `enabled_providers=None` and guards defensively, since the generic loader call does not yet pass `enabled_providers` into `build_tools`. |

Imports stay **lazy** (inside `build_tools`) so scanning the `FEATURE` descriptor is cheap. If these files don't exist, the agent simply gets no privileged tools — no errors, no side effects. `app/main.py` mounts the `/admin/source` router the same guarded way.

## Disabling / editions

**To remove all privileged filesystem and shell access, delete this directory:**

```bash
rm -rf plugins/admin/
```

No code edits, no config changes — the loader's `try/except ImportError` and the main-app router guard make it vanish cleanly. A production edition that excludes the Codebase Admin / Git Control / UI Admin abilities can drop this folder.

## `create_tool` lockout (still required)

Deleting this folder is **not enough on its own**: the agent's built-in `create_tool` could otherwise re-create `read_source` / `run_command` by writing raw Python. Two protections in **core** prevent that and must stay:

| Layer | File | What it does |
|-------|------|-------------|
| Code scanner | `app/tools/registry.py` | Blocks dangerous imports/patterns (`os`, `subprocess`, `open(`, `exec(`, …) before tool code is saved. |
| Restricted namespace | `app/tools/loader.py` | `_compile_tool` strips `open`/`exec`/`eval`/`compile`/`__import__` from builtins before executing tool code. |

## Production note

In production or user-facing deployments, **delete this directory** (and keep the `create_tool` lockout). Normal users should never have filesystem read/write/command-execution tools.

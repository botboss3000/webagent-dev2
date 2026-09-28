# `app/admin/` — Platform administration & management

This directory holds the **admin/management surfaces** of the app: provider settings, integration/OAuth configuration, database mode, users, and the admin-config endpoints for the various subsystems. These are core management plumbing the rest of the app calls into — not droppable per-edition capabilities.

> **Moved out:** the **privileged filesystem / source-control / UI-edit tools** (the code-editing, shell, git, and UI-Admin tools) now live in **[`plugins/admin/`](../../plugins/admin/README.md)**, gated by the **Codebase Admin**, **Git Control**, and **UI Admin** abilities. If you're looking for `source_tools.py`, `source.py`, `guardrails.py`, `ui_tools.py`, or `ui_guardrails.py`, they are there.

**Removable by design.** Boot does not hard-depend on this folder — `app/main.py` imports the startup provider-config applier from `app/provider_boot.py`, and every admin router is registered behind an `ImportError` guard. Delete a file (or the folder) and the matching `/admin/*` endpoints simply disappear.

## What's here

| File | Purpose |
|------|---------|
| **`settings.py`** | Provider configuration — AI provider / API key / model selection, metadata-logging toggle, provider presets, model fetching. Also the **usage/cost endpoints** that sum `usage_events.cost_usd`: `model-usage` (per model; `scope=global` is admin-wide incl. background), `session-cost` (per session), `agent-usage` (this user's spend with one agent — scoped to the (user, agent) pair, total + per-model; its `/reset` clears only the caller's own rows), and `session-model-usage` (per model, this session). |
| **`integrations.py`** | Admin enable/disable of integrations + abilities, OAuth credential config and redirect handling, `gather_enabled_providers` inputs, default-ability seeding. |
| **`db_mode.py`** | Database mode status and agent prompt template lifecycle (`/admin/db/`). |
| **`users.py`** | User administration endpoints. |
| **`storage.py`** | Storage backend admin. |
| **`communications.py`** | Enable/disable communication plugins (Telegram, WhatsApp) and set webhook URLs. |
| **`events_admin.py`** | Admin config for the events subsystem. |
| **`scheduler_config.py`** | Admin config for the scheduler providers. |
| **`webhooks_admin.py`** | Webhook administration endpoints. |
| **`remote_access.py`** | Remote-access / tunnel configuration. |
| **`optimizer.py`** | Prompt-optimizer admin pipeline support. |
| **`tasks.py`** | **Task grouping** — folds a session's flat turns into inferred "tasks" (one request + its plan/execution + any approval/feedback turns). Like `optimizer.py`'s runs dashboard it is **derived at read time** (no `task_id` column, no migration): it reconstructs turns from `interactions` and runs a boundary-inference layer over message wording (timing is NOT used). **Synthetic `role='user'` rows are excluded** (`_is_synthetic_user` — `[ORCHESTRATION EVENT]`/event/automation/optimizer-trigger injections): they attach to the open turn instead of opening one, so a background wake-up never splits a session. **Two passes:** (1) `_decide_boundary` — a fast text-only keyword rule (short reaction/approval/feedback or explicit "also…" continuation ⇒ same task; everything else ⇒ new task); (2) when that rule would open a NEW task, a **fast single-shot LLM tie-breaker** (`_llm_decide_related`) sees the last few turns — the user's messages **and** the agent's replies to them (never tool output) — plus the new message and decides SAME/NEW, gluing over-split follow-ups ("make it bigger") back onto the current task. Its purpose is **context-pool economy**: SAME keeps the task's tool results in the agent's context; NEW closes the task and its results degrade to a placeholder in the model payload (off-task hiding in `app/agent/session_history.py`, which reuses the memoised verdicts synchronously via `cached_llm_verdict` / `refresh_task_grouping_verdicts`). The LLM prompt lives in `app/defaults/app-prompts.json` (`app_level_prompts.task_grouping_classifier`); the call is bounded (no retries) and memoised, and degrades to the keyword verdict on any failure / no-credentials / `TASK_GROUPING_LLM=0`. `GET /admin/settings/tasks/session/{id}` groups one session **with** the LLM tie-breaker; `GET /admin/settings/tasks/overview?user_id=` lists sessions with task counts and stays **text-only** (avoids an LLM-call storm across many sessions). |
| **`review.py`** | List/view/deprecate DB-defined tools (`/admin/tools`). |

## See also

- **[`plugins/admin/`](../../plugins/admin/README.md)** — the privileged filesystem/source/git/UI-edit tools (moved here from this folder).
- The `create_tool` lockout (in `app/tools/registry.py` + `app/tools/loader.py`) — still required to prevent privileged tools being re-created at runtime; documented in `plugins/admin/README.md`.

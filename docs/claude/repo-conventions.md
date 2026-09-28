# Repo conventions (docs, files, git)

How to keep docs in sync, where to put scratch files, and how to push. Read this when your change adds/removes/renames paths, touches config, or before committing.

## Wiki upkeep (user-facing docs live in `data/wiki.db`)

The product's user-facing documentation is the **in-app Wiki** (`data/wiki.db`, its own SQLite file — see `app/wiki/db.py`), and README is being **trimmed** as docs move there. So:

- When you add or change **user-facing behaviour** (a new ability, a new tool, a workflow, a setting), **update or add the relevant Wiki article(s)** in the same work — articles are keyed by `slug`, written as Markdown, linked with `[[Title]]`, and `status='published'` to be public. Upsert via `app/wiki/db.py`'s `get_wiki_store().upsert(...)`, the `wiki_*` agent tools, or the Wiki tab.
- Keep the matching **README** entry **short** and point to the `[[Wiki Article]]` rather than duplicating prose there.
- Developer/architecture detail still belongs in README + these `docs/claude/*.md` files; the Wiki is for how users (and agents) *use* the feature.

## README upkeep (required with code changes)

After **any** change that affects how the repo is structured, configured, or used, **update `README.md` in the same work** so it stays the single accurate guide. Do not leave README stale while landing features or refactors.

**Always review and adjust when your edit touches:**

| Area of change | Update in `README.md` |
|----------------|----------------------|
| New / removed / renamed **directories** or notable **root files** | **Directory tree** (abbreviated block) and any prose that lists those paths. |
| New / removed **`app/`** modules, routers, endpoints, WebSockets, mounts | **Architecture → Backend** table; **Features** bullets if behavior is user-visible; **Useful URLs** if routes or ports change. |
| **`ui/`** entry points, major tabs, or default DB / API strings | **Frontend** paragraph; tree; **Quick test** if flows change. |
| **`.env.example`**, config knobs, default model, DB mode / paths | **Environment variables** table and related sentences (e.g. local vs cloud, `local.db`, `db_mode.json`). |
| **Install / run** commands, Docker, scripts, ports | **Installation**, **Deployment**, tree (`scripts/`, `Dockerfile`, `WebAgent.bat`), and **Useful URLs**. |
| **Cloud Run compatibility** — adding new Python deps, persistent storage assumptions, WebSocket origin checks, file write persistence | **`README.md` → Google Cloud Run checklist** — ensure new deps are in `requirements.txt`, no local-filesystem assumptions for persistent state, no loopback guards on WS endpoints. |
| **Migrations**, schema location, seeding / context behavior | Sections on **Installation** (Supabase / `migrations/`), **Context defaults**, **Adding custom context** as applicable. |

**Process:** Before you consider the task done, re-read the sections above and patch whatever is now wrong or missing. If the change is tiny (e.g. typo in a comment only) and **nothing** in the table applies, you may skip README—but default to **brief README sync** whenever behavior, paths, or layout moved.

## Folder .md files: read before act, update after change

**Before** modifying, analyzing, or creating files in any folder:

1. **Read every `.md` file** in that folder first. They contain context, instructions, config, and docs you need to follow.
2. **Apply what you learn** — if a `.md` file specifies an approach, use it.
3. **Recurse** — same rule for subdirectories you enter (skip `.git/`, `node_modules/`, `.venv*`, `__pycache__`, `.source-backups/`, `temp/`).

**After** completing work that touches any folder:

1. **Identify** all `.md` files in the affected folder(s) that describe the changed area.
2. **Re-read** them (content may now be stale vs your changes).
3. **Update** any stale descriptions, tables, paths, config keys, env vars, usage examples, or architecture notes.
4. **If a root `.md` file** (like `README.md` or `CLAUDE.md`) references the changed area, update it too.

## Temporary files (non-Markdown)

- Put **all temporary non-Markdown** artifacts here: scratch exports, one-off data dumps, downloaded samples, throwaway scripts output, ad-hoc logs, etc. — under **`temp/`**, unless the user explicitly authorizes a different path.
- **Do not read by default:** Do not treat **`temp/`** as reliable context for the codebase. Only read or search there if the user clearly asks you to.

## Markdown files (drafts, notes, scratch)

- **Where to write:** Put **new** Markdown you create in this workspace (analysis drafts, scratch specs, ad-hoc notes, roadmap drafts, etc.) under **`temp/`** (for example `temp/my-notes.md`), unless the user explicitly tells you to use a different directory or path.
- **Do not read by default:** **Do not** open, search, grep, or treat **`temp/`** as a source of truth for how the product works. It may contain obsolete, incorrect, or conflicting material that would confuse reasoning about the real codebase. **Only** read files there if the user clearly asks you to (e.g. "read my notes in `temp/…`").

## Misc directions

- **Console logs:** If adding console logs to investigate an issue, remove the logging after the issue is resolved.

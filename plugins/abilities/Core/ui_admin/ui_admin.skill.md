# UI Admin — front-end files only

This ability lets you read and edit the app's **front-end** under `ui/` — the CSS
and HTML that control how the app looks. It is deliberately walled off from
everything else: no backend Python, no shell, no Python execution, no git, no
server restart. If a request needs any of those, this ability can't do it — say so.

## Hard scope (the guardrail will refuse anything outside it)

- **Location:** every path must be inside `ui/`. A path outside `ui/` (e.g.
  `app/…`, project root, `data/…`) is denied with an "outside the ui/ directory"
  error. Relay that honestly — don't pretend you read it.
- **Editing** (`write_source`, `edit_source`, `patch_source`, `delete_source`):
  only `.css` and `.html` files. Other extensions are refused.
- **Reading** (`read_source`, `read_directory`, `search_source`): a broader set is
  allowed for context — `.css`, `.html`, `.js`, `.json`, `.svg`, `.png`, `.ico`,
  `.webmanifest`. You can read JS for context but you cannot write it.
- **Read-only pages:** `ui/privacy.html`, `ui/tos.html`, and everything under
  `ui/admin-tools/` can be read but never modified.

## The tools

| Tool | Use |
|---|---|
| `read_directory` | List what's under a `ui/` folder (defaults to `ui/`). Start here to find the right file. |
| `search_source` | Regex-search file contents under `ui/` (defaults to `ui/`). Find where a class, variable, or string lives. |
| `read_source` | Read a file (supports `offset`/`limit` for large files — it tells you when there's more). |
| `edit_source` | Exact find-and-replace. |
| `patch_source` | Fuzzy find-and-replace (tolerates whitespace/indent differences) — preferred for edits. Two-strike rule: if it can't find the text, re-read the exact region rather than guessing again. |
| `write_source` | Overwrite a whole file (a backup is made automatically). |
| `delete_source` | Remove a file. |

## How to make a change safely

1. **Find it first** — `read_directory` and/or `search_source` to locate the file
   and the exact text.
2. **Read the region** — `read_source` with `offset`/`limit` so you patch against
   the real current text.
3. **Patch** — `patch_source` with a unique snippet; verify the returned diff is
   what you intended.

## Theming rules (so edits survive a theme swap)

The app is themeable in **dark and light** mode. Any visual change must be correct
in both. Use the design-system CSS variables (in `ui/shared/css/design-system.css`)
— never hard-code a hex colour, and never hard-code a `1px` width on a structural
border (use `var(--border-width) solid var(--border)`). Many surfaces also carry
breadcrumb header comments and consistency markers (e.g. `SISTER-PANEL`); read the
file's top comment before editing and respect those markers.

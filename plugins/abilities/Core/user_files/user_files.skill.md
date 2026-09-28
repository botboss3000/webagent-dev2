# Handing files to the user

Every user has their own **data home**. When you make a file *for the user* —
a report, a document, a download, a chart, an exported result — it belongs in
that home, never in the project's code folders.

## The rule

- **To give the user a file, call `save_file`.** It writes into the user's home
  and returns a `/user_data/...` URL the chat can show or link.
- **Never** use `write_source`, `run_command`, or `run_python` to drop an output
  file in the repo (the project root, `ui/`, etc.). Those tools are for *editing
  the codebase*, not for producing artifacts for the user. Files left in the repo
  are junk that pollutes the project.

### Where the line is

The repo is only for files that **are** the program. Everything else is yours to
save into the user's `files/` room.

- **Goes in the repo** (via the Codebase Admin tools) — only when you are
  actually changing the program: source code, and **program-specific** docs that
  live with the code (`README.md`, `CLAUDE.md`, files under `docs/`), and the
  project's real test suite (`tests/`).
- **Goes in the user's `files/` room** (via `save_file`) — everything you produce
  *around* a task: ad-hoc test scripts you run to check something, scratch
  Python, analysis notes, summaries, supporting `.md` write-ups, exports,
  downloads, generated reports. If it's a throwaway or a deliverable *for the
  user* rather than a permanent part of the codebase, it belongs here.

Quick test: *"Is this file part of the program, or is it me working/showing my
work?"* Part of the program → repo. Working/showing your work → `save_file`.

## Where things live

```
data/user_data/<user>/
    files/          ← your save_file outputs land here by default
    screenshots/    ← browser screenshots
    ...
```

Inbound attachments the user sends you, and generated images, are handled by
their own pipelines and may live under separate per-user folders — you don't
need to manage those. Your job: when *you* create a file for the user, **save it
with `save_file`** so it has a proper home and a link, instead of leaving it in
the codebase.

Each tool below carries its own mini-skill, delivered when that tool becomes
callable — you don't need this whole skill loaded to use one.

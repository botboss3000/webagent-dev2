# Driving interactive terminal programs

This skill is attached to the **Terminal Control** ability. It loads on demand —
the agent sees the one-line "when to use" in its `# [SKILLS]` list and pulls this
full body with `load_skill` only when a task actually needs to drive a terminal.

## When to use

Use this when you must operate a program that takes over the terminal and reacts
to keystrokes — a coding agent (Claude Code, Gemini CLI, Aider), a REPL
(`python`, `node`), an SSH session, a TUI installer, or any prompt-driven CLI.
For one-shot, non-interactive commands prefer `run_command` (under Codebase
Admin) instead — it's simpler and returns output directly.

## The loop

The terminal tools are low-level and program-agnostic. The pattern is always:
**open → read → send → wait → read**, repeating until the task is done, then
**close**.

1. **`terminal_open`** — launch the program (e.g. `claude`, `python3`, `ssh host`).
   You get back a session id. The session also shows up in the user's sidebar
   badged `AGENT`, so a human can watch.
2. **`terminal_read`** — read the current screen text before you type anything.
   Never type blind; read first so you know what state the program is in.
3. **`terminal_send`** — type input. Send literal text, or named keys
   (`enter`, `esc`, `up`, `down`, `tab`, `ctrl+c`, `y`, `n`). Send the keypress
   the program is actually waiting for — many TUIs need a bare `enter` to submit.
4. **`terminal_wait`** — wait until the program reacts and the screen settles
   before reading again. Don't hammer `terminal_read` in a tight loop.
5. **`terminal_close`** — end the session when finished.

## Rules of thumb

- **Read before every send.** The screen tells you what's expected (a menu, a
  yes/no prompt, a password field, a finished result).
- **One step at a time.** Send a key, wait, read, decide the next key. Don't
  queue a long script of keystrokes hoping the program keeps up.
- **Respect the human.** If a person takes over the session (the **Pause**
  toggle), `terminal_send` is refused — you and the human never share the
  keyboard. Read-only watching is always fine.
- **Confirmations.** `terminal_open` / `terminal_send` / `terminal_close` are
  destructive (they can run arbitrary programs), so the write-mode guardrail may
  ask the user to confirm. That's expected.
- **Stuck?** If the screen stops changing, read it: you're probably at a prompt
  waiting for a specific key (often `enter`, `y`, or `q` to quit a pager).

## Example: drive Claude Code

1. `terminal_open` with command `claude`.
2. `terminal_read` → wait for the prompt.
3. `terminal_send` your instruction text, then `terminal_send` `enter`.
4. `terminal_wait`, then `terminal_read` to see what it did / what it's asking.
5. Answer prompts (`y`/`n`/`enter`) as they appear; repeat 3–4 until done.
6. `terminal_close`.

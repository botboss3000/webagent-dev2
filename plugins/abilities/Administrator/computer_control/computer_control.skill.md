# Computer Control

Use this ability to work through the host computer's native desktop UI. Treat
the screen as sensitive: screenshots can contain passwords, private messages,
notifications, customer data, or authentication codes.

Each tool below carries its own mini-skill, delivered when that tool becomes
callable. The operating loop and the coordinate/focus rules apply to all of them.

## Current capability

Phase 3 exposes screenshots plus Windows pointer movement, clicking, scrolling,
dragging, literal Unicode text entry, and key/chord input. macOS and Linux can
capture screenshots, but their input tools return `unsupported_platform` until
native backends are added.

## Operating loop

Always use this loop — it applies to every pointer and keyboard tool below:

1. Call `computer_screenshot` and inspect the current state.
2. Choose one small action whose target is unambiguous.
3. Perform exactly one action.
4. Call `computer_screenshot` again and verify the expected state change.
5. Stop if the screen differs from what you expected. Re-observe instead of
   sending blind follow-up input.

The runtime enforces steps 1 and 4 for pointer and keyboard input: it rejects
action-before-screenshot, stale observations, changed display layouts,
out-of-bounds coordinates, and a second action before verification.

Ask before actions that submit forms, send messages, purchase, publish, delete,
install system-wide, change security settings, expose secrets, or otherwise
create an external or hard-to-reverse effect. Never type passwords, API keys,
recovery codes, or payment details unless the user explicitly supplies and
authorizes that exact use.

## Coordinates and focus

All coordinates are integer pixels in the most recent screenshot, measured from
its top-left `(0, 0)`. Never reuse coordinates after a screenshot reports a new
size, origin, or monitor arrangement.

Keyboard input goes to the control that currently owns focus. Before typing or
pressing a key, click the intended control, take a new screenshot, and verify
its focus indicator or caret. Never assume focus survived a dialog, window
switch, notification, or prior shortcut.

## Platform notes

- Prefer the modifier exposed by the platform: `CTRL` on Windows/Linux and
  `CMD` on macOS.
- UI scale, Retina/HiDPI, mixed-DPI monitors, display rotation, and monitor
  rearrangement can invalidate old coordinates. Take a fresh screenshot after
  any display change and before every consequential action.
- Use semantic browser tools for web pages when available; reach for Computer
  Control when the task genuinely depends on the native desktop.

## Later tools

- Clipboard, file-drop, accessibility-tree, and application/window-selection
  tools should remain separate capabilities rather than being simulated with
  undocumented key sequences.

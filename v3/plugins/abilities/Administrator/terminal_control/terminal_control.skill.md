# Interactive coding terminals in V3

Terminal Control starts real interactive processes in /live-checkout when that mount is present. Approved commands run as the development checkout owner and can reach the mounted live checkout at /live-checkout, including v3 and v2 source. Coding CLI binaries and their credentials are not implicitly installed or supplied. Use Codebase Admin `shell` for one-shot commands.

Use `terminal_open` → `terminal_read` → `terminal_send` → `terminal_wait` → `terminal_read`, then `terminal_close`. Each send requires the exact `sequence` from a fresh read or wait. New output or an earlier send invalidates that observation; read again after a stale-observation error. Do not replay input after an uncertain result.

Terminal IDs belong to the current workspace. Output is a bounded byte ring, preserving raw terminal escape sequences; this is not a rendered screen. `cursor` advances in bytes, and `truncated: true` means older output was discarded. Terminal processes are capped at four per workspace, expire after fifteen minutes without tool activity, and have a one-hour absolute lifetime. A worker restart closes them and never replays keystrokes.

Opening, sending and closing require the live Rust execution policy and any required approval. Read, wait and list are observations; observing never grants permission to execute. Respect the configured caution guidance. There is no V3 terminal sidebar, attached computer access, or human-takeover toggle in this adapter. If the user asks to stop, close the terminal through its authorized control path.

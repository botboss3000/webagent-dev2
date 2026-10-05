# terminal_send

Provide `terminal_id`, `expected_sequence` from a fresh read or wait, and exactly one of `text` or `keys`. Text is at most 8192 UTF-8 bytes. Keys is a list of 1–32 names: enter, esc, tab, backspace, up, down, left, right, ctrl+c, ctrl+d, ctrl+z. Sending invalidates the observation even if output has not arrived. Read again before another send. Stale, closed, or uncertain input must not be retried blindly. Requires execution permission and any required approval.

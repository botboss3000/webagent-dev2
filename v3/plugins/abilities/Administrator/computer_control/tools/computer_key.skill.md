`computer_key(keys)` — send one key or one chord. Examples: `["ENTER"]`,
`["CTRL", "L"]`, `["ALT", "F4"]`, or `["CTRL", "SHIFT", "S"]`.

- Supply an array of 1–4 unique names. A chord may contain modifiers plus one
  ordinary key. Modifiers are normalized and pressed before the ordinary key,
  then released safely in reverse order.
- Supported modifiers are `CTRL`, `SHIFT`, `ALT`, and `WIN`. Supported ordinary
  keys include `A`–`Z`, `0`–`9`, `F1`–`F24`, `ENTER`, `TAB`, `ESCAPE`,
  `BACKSPACE`, `DELETE`, `INSERT`, `SPACE`, arrows, `HOME`, `END`, `PAGEUP`,
  `PAGEDOWN`, `PRINTSCREEN`, lock keys, and `SEMICOLON`, `EQUALS`, `COMMA`,
  `MINUS`, `PERIOD`, `SLASH`, `BACKTICK`, `BRACKET_LEFT`, `BACKSLASH`,
  `BRACKET_RIGHT`, and `QUOTE`.
- Common aliases such as `CONTROL`, `RETURN`, `ESC`, `DEL`, `PGUP`, `PGDN`,
  `WINDOWS`, and `META` are normalized. Use Windows names while Windows is the
  only input backend.
- Use exactly one chord per call. Never encode a multi-step macro as one call.
- Treat `ENTER`, `DELETE`, `ALT+F4`, `CTRL+W`, `CTRL+S`, `CTRL+P`, and similar
  keys as potentially consequential. Verify the focused UI and obtain approval
  when they could submit, close, save, print, delete, publish, or send.

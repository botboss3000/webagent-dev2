`computer_type(text, interval_ms=0)` — enter literal Unicode text without using
the clipboard. Use it for ordinary text only, not shortcuts or special keys.

- `text` must contain 1–4000 Unicode characters. Newline, tab, escape, delete,
  and all other control characters are rejected; send those deliberately with
  `computer_key`.
- Use `interval_ms=0` normally. Use a small delay only for an application that
  visibly drops fast input; the maximum is 1000 ms per character.
- The result reports character counts but never repeats the entered text.
- Treat all text entry as consequential. Re-read the user's exact requested
  text before calling, then screenshot and verify it afterward.
- Never enter a password, API key, recovery code, authentication code, payment
  detail, or other secret unless the user explicitly supplied it and authorized
  that exact destination.

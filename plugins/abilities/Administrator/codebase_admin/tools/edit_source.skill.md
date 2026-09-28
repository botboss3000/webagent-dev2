Edits a file by replacing exact text.

- `old_text` must match **exactly**; when it doesn't, use `patch_source` (fuzzy) instead.
- Best for small, precise, surgical changes. If it fails to match twice, reread the
  exact region rather than guessing at the string again.

Sets how hard the model this chat is running on thinks.

- Levels: **minimal · low · medium · high** — and **default**, which clears the hint
  and uses the model's own default.
- Optionally pass a model id to set that specific model's level; each model remembers
  its own.
- Raise it (`'high'`) for deep reasoning; lower it (`'minimal'` / `'low'`) for speed on
  routine work.
- Models that don't support reasoning simply ignore it.

These compose. Two common moves:

- **Keep the default model, just think harder:** `set_effort('high')`.
- **Switch model *and* set its depth:** `set_model('<stronger id>')`, then
  `set_effort('high')`.

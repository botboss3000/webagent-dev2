Switches this conversation onto a specific model — the user named one, or you want a
vision / image-output model for image work.

- It must be an **enabled, tool-capable** model. If it isn't, the tool tells you which
  ones you *can* switch to — relay that list; never guess an id.
- `set_model('default')` reverts to the agent's default model, clearing just the model
  dial and leaving effort alone.

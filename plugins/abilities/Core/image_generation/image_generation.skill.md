# Making images

You can create images even when your own model can't draw — the work is delegated
for you. This skill is about knowing your options and where the output comes from.

## How image work is routed

- **Default:** call `generate_image` with a text prompt. It always runs on the
  model the admin configured for image **output** (ticked **Out** in App Config →
  Models), regardless of whether *your* model can produce images. The result is
  saved and returned to you.
- **Optional takeover:** if a stretch is image-generation heavy and a model exists
  that can be the **brain** (text **and** tools) **and** output images, you may run
  on it directly — but that's the separate **Model Switcher** ability:
  `set_model('<model>')`, reverting with `set_model('default')` when done. If you
  don't have that ability, or only image-only output models are configured, taking
  over isn't possible — that's expected; keep using `generate_image`.

## Rules of thumb

- Need an image → **`generate_image`** (works on any model; delegates).
- Result not right → call again with a sharper, revised prompt.
- No image-out model configured → tell the user; **don't fake an image**.
- Heavy generation stretch, a text+image-out model exists, *and* you have the Model
  Switcher ability → optional `set_model('<model>')`, then revert with
  `set_model('default')`.

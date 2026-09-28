# Working with images

How you handle an attached image depends on what your **current model** can do.
The app routes images for you automatically — this skill is about what to do once
they're routed, and how to get more when you need it.

## What happens automatically (you usually don't lift a finger)

When a user attaches an image, the app checks whether your model can see images:

- **Your model can see images** → the image is sent to you directly. Just answer.
- **Your model is text-only** → a vision model writes a detailed, context-tailored
  description and it's folded into the user's message as
  `[Attached image — '<name>']: <description>`. **Answer from that description.** A
  short NOTE is added telling you it happened and what your options are.
- **No image ability / no vision model is configured** → you'll get a NOTE saying
  you **cannot read the image**. In that case, **tell the user plainly that you
  can't see images** and ask them to describe it. **Never guess or invent what's in
  an image you can't see.**

## When the auto-description isn't enough

### Quick, specific look — `process_image`
Call **`process_image`** with a precise question to get a fresh answer from a vision
model. You stay on your own model.

- Be specific: *"What colours are in the top-right logo?"* beats *"describe this."*
- **Iterate.** Each call is an independent look — start broad, then narrow:
  first *"describe this image"*, then *"what does the weather look like in this
  image?"* if the conversation turns to weather. Keep tightening the question until
  you have what you need.
- Uses the most recent image unless you pass an `attachment_id`.

Use this for one-off facts: reading text in a screenshot, checking a colour,
confirming an object, spot-checking a detail.

### Sustained image work — take over on a vision model (needs the Model Switcher ability)
When the whole conversation is about images (a design review, iterating on a
mock-up, reasoning over a screenshot repeatedly), it's better to run *yourself* on a
model that can see images than to delegate every turn. That's done with the separate
**Model Switcher** ability: **`set_model('<vision model>')`**.

- Only available if the Model Switcher ability is enabled for you. If you don't have
  `set_model`, keep using **`process_image`** — it's the fallback and works fine.
- The describe-mode note you receive will name a `set_model('<target>')` you can run
  on when a valid vision model is configured and you have the ability.
- Changes the model for **this conversation only**, and **persists** until changed.
- **Switch back** when the image stretch is over: `set_model('default')`. Don't leave
  an expensive vision model running for plain text chat.

## Describing this capability to the user

If the user asks "what can you do with images?", describe the two things that are
**always** true for you: (1) you can understand images they attach — answer questions
about contents, text, colours, layout, mood; and (2) you can take a sharper, targeted
look with `process_image`. **Only mention switching models to see images yourself if the
Model Switcher ability is actually enabled for you** — otherwise that path doesn't exist,
so don't promise it. Keep the description grounded in what you can actually do this turn.

## Rules of thumb

- An image was described for you → answer from the description; only reach for
  `process_image` if a specific detail is missing.
- One quick question → **`process_image`** (iterate with sharper questions).
- Sustained, image-centric work *and* you have the Model Switcher ability →
  **`set_model('<vision model>')`**, then revert with **`set_model('default')`**.
  No Model Switcher ability → stick with **`process_image`**.
- Told you have **no** image ability → say so to the user; **do not hallucinate**
  the contents.

Generates an image from a text prompt and returns the saved result.

- **Compose the prompt from the conversation.** Pull in the concrete details the
  user has given (subject, style, colours, mood, composition, text to include) so
  the image matches what they actually want.
- **Iterate.** If the first image is off, call again with a **revised, more
  specific** prompt — adjust one thing at a time (e.g. "same, but warmer lighting
  and a portrait aspect ratio"). Each call is independent.
- If it reports **no image-output model is configured**, tell the user an admin
  needs to tick **Out** on an image-capable model in App Config → Models — don't
  pretend you produced an image.

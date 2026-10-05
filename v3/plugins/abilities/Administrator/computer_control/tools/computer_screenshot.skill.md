Capture the entire virtual desktop, including every connected display, and save
the PNG as a conversation attachment.

- Set `question` to the exact visual fact needed, such as `"Which dialog is
  open and what buttons does it show?"`. Leave it empty for a complete screen
  description.
- Keep `analyze=true` when you need the configured vision model to inspect the
  image. Set it to `false` only when capture proof is enough or another
  image-capable tool/model will inspect the attachment.
- Read `dimensions` and `coordinate_space` from the result. Screenshot
  coordinates start at `(0, 0)` in the PNG's top-left, even when the operating
  system has a monitor positioned to the left of its primary display.
- A successful capture can include a `warning` instead of `description` when no
  vision model is configured. The screenshot is still valid; do not invent what
  it shows.
- On Windows, this uses the interactive desktop. A locked session, secure
  desktop/UAC prompt, service session, or DRM-protected surface may be blank or
  unavailable. Treat that as a `local_device` observation for the reported
  environment generation, not as a fact about headless or browser backends. If
  its evidence is stale, retry one safe screenshot/status probe on the current
  device generation before reporting the limitation.
- Every successful screenshot authorizes exactly one input action. It expires
  after five minutes and is invalidated by any Computer Control action from
  another session.

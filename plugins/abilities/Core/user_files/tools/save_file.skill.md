Writes a file into the user's own data home and returns a link to it.

- `filename` — what to call it, e.g. `summary.md`, `invoice.pdf`, `chart.png`.
  It's sanitised to a safe name; if that name is taken, a short suffix is added
  so you never clobber an earlier file.
- `content` — the text of the file. For **binary** files (images, PDFs), set
  `is_base64: true` and pass base64-encoded bytes.
- `room` — which bucket under the home. Use **`files`** (the default) for
  documents and downloads. Other rooms exist for specific kinds (e.g.
  `screenshots`); only use a different room when it clearly fits.

It returns the saved `filename`, the `room`, the `url`, and the byte size. Show
the user the URL (or embed it) so they can open what you made.

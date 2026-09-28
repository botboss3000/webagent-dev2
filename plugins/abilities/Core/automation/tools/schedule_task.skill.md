Creates a job that runs a **prompt** later — one-shot or recurring.

- `prompt` (required) — what to do when it fires. Write it as an instruction to
  your future self: that run has **no memory of this chat**, so make it
  self-contained — say what to produce, and where the input came from.
- Exactly one timing:
  - `in_minutes` — fire once, this many minutes from now.
  - `at` — fire once, at an ISO timestamp (e.g. `2026-06-10T15:00:00`). Naive
    timestamps are treated as UTC.
  - `schedule_cron` — repeat on a cron pattern (e.g. `0 9 * * 1-5` = weekdays at
    09:00). Set `timezone` (IANA, e.g. `America/New_York`) so the user's "9am"
    means *their* 9am.
- `delivery` — where the result goes. Decide this with the user **before** you
  create the job (see **Delivery** above).
- `run_mode` — `inline` by default; the clone modes are gated (see **Run mode**).
- The recurring guardrails and the per-agent limits both apply here.

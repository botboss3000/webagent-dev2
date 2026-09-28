"""Background sync engine for the hybrid local-first backend (Phase 4).

The hybrid backend writes Synced-tier rows to the LOCAL SQLite hot store first
(fast, resilient) and records the change in a local OUTBOX. This package drains
that outbox to the remote Postgres authority in batches (the PUSHER) and pulls
rows other devices wrote back into the local replica (the PULLER).

  * :mod:`app.db.sync.outbox`  — the local pending-change queue.
  * :mod:`app.db.sync.engine`  — the pusher/puller + the per-table sync specs.

Money and coordination tables NEVER go through here — the hybrid writes those to
Postgres synchronously (see app/db/hybrid.py). This engine only moves the Synced
tier (shared content/config), where last-writer-wins on ``updated_at`` and a
one-turn freshness gap are acceptable.
"""

from app.db.sync.outbox import Outbox
from app.db.sync.engine import SyncEngine, SYNCED_SPECS, SyncedSpec

__all__ = ["Outbox", "SyncEngine", "SYNCED_SPECS", "SyncedSpec"]

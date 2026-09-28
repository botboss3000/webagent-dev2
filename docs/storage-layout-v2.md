# Storage layout v2

WebAgent has three logical authority planes and two specialized physical
planes.  Table ownership is declared in `app/db/schema/ownership.py`; generated
schemas and migrations must use that registry rather than maintain independent
table lists.

| Plane | SQLite location | Purpose |
| --- | --- | --- |
| app | `data/db/app.db` | accounts, ACL/catalog projections, templates, billing, devices, coordination |
| user | `data/user_data/<user_id>/<user_id>.db` | sessions, interactions, memories, files, user-created content and automations |
| agent | `data/agent_data/<agent_id>/<agent_id>.db` | agent authority, prompts, abilities, connections, policies and bindings |
| secrets | existing scoped vault files | secret values; domain databases store references only |
| telemetry | `logs.db` and `recordings.db` | rolling per-machine diagnostics and recordings |

The wiki and plugin-owned databases remain independent because they have their
own schema/lifecycle. Queues, caches, reservations, and optimizer databases are
runtime stores, not authority planes.

## Per-user database size limit

**Settings → App Settings → App Functions → User database size limit** provides a per-user
cap for the user SQLite authority file. The expanded row contains an editable
numeric **Maximum database size (MB)** field, prefilled with the default **100
MB**. The setting is enabled by default and accepts 10 MB through 10,240 MB.

The limit is enforced after a user moves a session to the recycle bin and by a
leader-only maintenance sweep five minutes after startup, then at the configured
interval (24 hours by default). SQLite is checkpointed and compacted first. If the
database is still over the cap,
WebAgent permanently removes the oldest recycled session families first. Once
the bin has no older sessions left, it removes the oldest active session
families. The session just moved to the bin (and its child-session family) is
preserved. Each permanent cleanup removes its transcript, summaries, run state,
and spawned session data, and participates in normal hybrid-sync deletion.

## Invariants

1. A core table has exactly one `TablePolicy`.
2. Cross-plane IDs are not physical foreign keys. Updates spanning files use
   projections, revisions, and idempotent reconciliation.
3. The per-agent database is the read authority after activation. `agent_catalog`
   in app.db is a minimal discovery/ACL projection.
4. A file's existence never activates a layout. `app.db.storage_layout` must
   contain the current version, all three planes must be verified, every
   migration record must be verified, and the manifest must be explicitly
   activated.
5. Runtime routing is a hard cutover. No authority read or write falls back to
   `local.db`, `global.db`, or an `_mig_*` table.

## Rollout

Run the audit while the application is running; it opens SQLite sources with
`immutable=1` and does not touch WAL sidecars:

```powershell
python -m app.db.migrate_storage_layout
```

Before applying, stop every WebAgent process so all WAL files are checkpointed.
Then run:

```powershell
python -m app.db.migrate_storage_layout --apply
```

For an installation that has not yet cut over, the apply pass:

- creates the generated app schema;
- merges the newest account/profile compatibility copies into app.db;
- creates the minimal agent catalog projection;
- fills missing user-owned rows into each user database;
- refreshes complete per-agent authority bundles;
- records per-source counts and content hashes in `storage_migrations`;
- verifies every copied authority before activation.

Activation is deliberately a separate operation and must happen only after the
report has no warnings and backups have been checked. After activation, stop
the application, checkpoint the source WAL files, rerun verification, and
remove the live legacy files. Keep backups outside the runtime filenames.

If the legacy databases have already been retired but the activation row is
missing, stop WebAgent, back up the split databases, and run the guarded recovery:

```powershell
python -m app.db.migrate_storage_layout --activate-existing
```

This mode refuses to run while `local.db` or `global.db` exists. It performs a
SQLite integrity check, verifies every required app/user/agent table, records
verified recovery entries in `storage_migrations`, and only then activates the
layout. It never treats file existence alone as proof of a completed migration.

The current development layout has completed that hard cutover. The only
authorities are `app.db`, per-user databases, and per-agent databases. The old
`migrate_to_split` command redirects to this v2 migrator and no runtime path
creates or opens `local.db` or `global.db`.

## Adding a table

Add its canonical `Table` definition, then add one ownership policy. Tests fail
if either side is missing. Use `render_plane()` for physical DDL. A plugin table
does not belong in the core registry: the plugin owns its schema and lifecycle.

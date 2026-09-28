"""Pending-reset boot handler + self-destruct spawner (the Danger Zone engine).

CORE-SAFE, STDLIB-FIRST. Imported once very early in ``app/main.py`` (right after
the provider config is applied, *before* the DB / stores are opened) so a reset
can delete database + vault + attachment + genui + log files **while nothing has
them open** — the same "stop → wipe → start" shape the TUI's reset tool uses
(``TUI/tui_app/tools/reset.py``), but driven from inside the app across a
self-restart instead of from an external supervisor.

Flow (the in-app Danger Zone at the bottom of Data Settings):

  1. The admin ticks reset groups + confirms → ``POST /admin/storage/reset``
     (app/admin/storage.py) calls :func:`write_marker` and then triggers a
     self-restart (app/relauncher.trigger_restart).
  2. The old process exits; the relauncher/supervisor starts a fresh one.
  3. On the new boot, ``main.py`` calls :func:`run_pending_reset`, which reads the
     marker, moves the selected file groups into a timestamped backup under
     ``temp/`` (so a wipe is reversible), drops + recreates the Postgres schema
     when the active DB backend is Postgres (irreversible — not backed up),
     records the outcome to ``data/config/last-reset.json`` (the UI shows it after
     reload), and deletes the marker so it runs exactly once.

Self-destruct ("Delete entire installation") is separate: :func:`spawn_self_destruct`
writes a tiny stdlib script to the SYSTEM temp dir (outside the repo, so it can't
delete itself mid-run), spawns it detached, and exits the server so the folder is
unlocked. That helper waits for ``/health`` to go down, then removes the whole
install folder.

Group → what it clears (all paths relative to the repo root):

  * ``db``          the active app database — SQLite files (moved to backup) OR the
                    live Postgres schema (dropped + recreated). Reseeds on boot.
  * ``secrets``     the credentials vault (``data/db/vault.db``) — API keys, OAuth
                    tokens, encryption keys. (OS-keyring secrets are not reachable
                    here; a note is recorded.)
  * ``attachments`` uploaded images / voice / dropped files (``data/uploads`` +
                    every ``data/user_data/<uid>/uploads``).
  * ``genui``       generated Gen UI pages (``data/user_data/<uid>/genui`` + the
                    legacy ``canvas`` / ``visuals/users`` layouts).
  * ``logs``        diagnostics / recordings DBs (``data/db/logs.db`` /
                    ``recordings.db`` / ``instance_id.txt``).
"""

from __future__ import annotations

import glob
import json
import logging
import os
import shutil
import subprocess
import sys
import time
from pathlib import Path

logger = logging.getLogger(__name__)

# app/util/reset_boot.py → app/util → app → repo root
_REPO_ROOT = Path(__file__).resolve().parent.parent.parent

# The marker the endpoint writes and this handler consumes (deleted after one run).
MARKER_PATH = _REPO_ROOT / "data" / "config" / "pending-reset.json"
# The outcome of the last reset — the Danger Zone UI fetches this after reload.
RESULT_PATH = _REPO_ROOT / "data" / "config" / "last-reset.json"

# Every group the Danger Zone can select. Kept here so the endpoint validates
# against one list.
GROUPS = ("db", "secrets", "attachments", "genui", "logs")

# Postgres-family providers — these use the live Postgres backend, not SQLite, so
# a DB reset drops the schema instead of removing files (mirrors the TUI tool).
# Keep in sync with connection_config.is_postgres_provider() — the single source of truth.


def repo_root() -> Path:
    """Absolute path to the install folder (the repo the server runs in)."""
    return _REPO_ROOT


# ── Group → concrete file/dir lists ──────────────────────────────────────────
# Static per-group paths (relative to the repo root). Per-user dirs that need a
# glob (attachments / genui under data/user_data/*) are expanded in _paths_for().

_DB_SQLITE_FILES = [
    "data/db/app.db", "data/db/app.db-journal", "data/db/app.db-wal",
    "data/db/app.db-shm",
]
_SECRETS_FILES = [
    "data/db/vault.db", "data/db/vault.db-journal", "data/db/vault.db-wal", "data/db/vault.db-shm",
    "app/db/vault.db", "app/db/vault.db-journal", "app/db/vault.db-wal", "app/db/vault.db-shm",
]
_ATTACH_STATIC = ["data/uploads"]
_GENUI_STATIC = ["visuals/users"]
_LOGS_FILES = [
    "data/db/logs.db", "data/db/logs.db-journal", "data/db/logs.db-wal", "data/db/logs.db-shm",
    "data/db/recordings.db", "data/db/recordings.db-journal", "data/db/recordings.db-wal", "data/db/recordings.db-shm",
    "data/db/instance_id.txt",
    "app/db/logs.db", "app/db/logs.db-journal", "app/db/logs.db-wal", "app/db/logs.db-shm",
    "app/db/recordings.db", "app/db/recordings.db-journal", "app/db/recordings.db-wal", "app/db/recordings.db-shm",
    "app/db/instance_id.txt",
]


def _user_data_subdirs(root: Path, *names: str) -> list[str]:
    """Every ``data/user_data/<uid>/<name>`` dir, as repo-relative paths."""
    out: list[str] = []
    base = root / "data" / "user_data"
    if not base.is_dir():
        return out
    for name in names:
        for p in glob.glob(str(base / "*" / name)):
            try:
                out.append(str(Path(p).relative_to(root)).replace("\\", "/"))
            except ValueError:
                out.append(p)
    return out


def _paths_for(root: Path, groups: set[str]) -> list[str]:
    """Concrete repo-relative file/dir list for the selected groups (no PG — that
    is handled separately by _wipe_postgres). ``db`` files are added only when the
    active backend is SQLite (Postgres has no local DB file to move)."""
    from app.db.connection_config import is_postgres_provider
    rels: list[str] = []
    if "db" in groups and not is_postgres_provider(_active_provider(root)):
        rels += _DB_SQLITE_FILES
    if "secrets" in groups:
        rels += _SECRETS_FILES
    if "attachments" in groups:
        rels += _ATTACH_STATIC + _user_data_subdirs(root, "uploads")
    if "genui" in groups:
        rels += _GENUI_STATIC + _user_data_subdirs(root, "genui", "canvas")
    if "logs" in groups:
        rels += _LOGS_FILES
    return rels


# ── Active-backend probe (mirrors app.db.connection_config just enough) ───────

def _active_provider(root: Path) -> str:
    """Which DB backend the app will use, without importing the heavy app.db
    package. Env-locked config wins; else read the saved connection config."""
    if os.environ.get("WEBAGENT_CONFIG_SOURCE", "").lower() == "env":
        return (os.environ.get("WEBAGENT_DB_PROVIDER") or "sqlite").lower()
    for rel in ("data/config/db_connection.json", "app/db_connection.json"):
        try:
            data = json.loads((root / rel).read_text(encoding="utf-8"))
            prov = str(data.get("provider") or "").lower()
            if prov:
                return prov
        except (OSError, json.JSONDecodeError, ValueError):
            continue
    return "sqlite"


# ── The boot handler ─────────────────────────────────────────────────────────

def _move_or_delete(root: Path, rel: str, backup_dir: Path,
                    results: dict[str, list[str]]) -> None:
    """Move ``root/rel`` into the backup dir (reversible). Records the outcome."""
    src = root / rel
    if not src.exists():
        return
    try:
        dst = backup_dir / rel
        dst.parent.mkdir(parents=True, exist_ok=True)
        shutil.move(str(src), str(dst))
        results["ok"].append(rel)
    except OSError as e:
        results["failed"].append(f"{rel} ({e})")


def _wipe_postgres(root: Path) -> tuple[bool, str]:
    """Drop + recreate the active Postgres schema (irreversible — not backed up).
    Runs in-process at early boot, before the app opens its own pool."""
    try:
        from app.db.connection_config import load_config
        from app.db import _resolve_pg_password
        from app.db.postgres_backend import make_conninfo
        import psycopg
    except Exception as e:  # pragma: no cover - optional dep / layout drift
        return False, f"could not load Postgres support: {e!r}"
    try:
        cfg = load_config()
    except Exception as e:
        return False, f"could not load DB config: {e!r}"
    if not is_postgres_provider(getattr(cfg, "provider", "sqlite")):
        return False, "active backend is not Postgres"
    schema = getattr(cfg, "schema", None) or "public"
    loc = f"{schema}@{cfg.host}:{cfg.port}/{cfg.database}"
    try:
        conninfo = make_conninfo(cfg, _resolve_pg_password(cfg))
        with psycopg.connect(conninfo, autocommit=True) as conn:
            try:
                conn.execute(f'DROP SCHEMA IF EXISTS "{schema}" CASCADE')
                conn.execute(f'CREATE SCHEMA "{schema}"')
                try:
                    conn.execute(f'GRANT ALL ON SCHEMA "{schema}" TO CURRENT_USER')
                    conn.execute(f'GRANT ALL ON SCHEMA "{schema}" TO public')
                except Exception:
                    pass
                return True, f"schema dropped + recreated ({loc})"
            except Exception:
                # Managed Postgres may forbid dropping the schema itself — drop
                # every table instead (the app recreates them on next start).
                rows = conn.execute(
                    "SELECT tablename FROM pg_tables WHERE schemaname = %s", (schema,)
                ).fetchall()
                for (t,) in rows:
                    conn.execute(f'DROP TABLE IF EXISTS "{schema}"."{t}" CASCADE')
                return True, f"{len(rows)} table(s) dropped ({loc})"
    except Exception as e:
        return False, f"Postgres wipe failed: {e!r}"


def run_pending_reset() -> None:
    """Consume the pending-reset marker if present. Safe to call unconditionally at
    boot — a missing/invalid marker is a no-op and any error is swallowed so a bad
    marker can never brick startup."""
    try:
        if not MARKER_PATH.is_file():
            return
        marker = json.loads(MARKER_PATH.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError, ValueError):
        try:
            MARKER_PATH.unlink()
        except OSError:
            pass
        return

    groups = {g for g in (marker.get("groups") or []) if g in GROUPS}
    root = _REPO_ROOT
    ts = time.strftime("%Y%m%d-%H%M%S")           # compact — backup folder name
    at_iso = time.strftime("%Y-%m-%dT%H:%M:%S")   # ISO — parseable by the UI clock
    results: dict[str, list[str]] = {"ok": [], "failed": []}

    logger.warning("[reset] pending reset detected — groups=%s", sorted(groups))

    # 1) Live Postgres schema drop (only when resetting the DB on a PG backend).
    pg_ok: bool | None = None
    pg_detail = ""
    from app.db.connection_config import is_postgres_provider
    if "db" in groups and is_postgres_provider(_active_provider(root)):
        pg_ok, pg_detail = _wipe_postgres(root)
        logger.warning("[reset] postgres wipe ok=%s (%s)", pg_ok, pg_detail)

    # 2) Move the selected file groups into a timestamped backup (reversible).
    backup_dir = root / "temp" / f"reset-backup-{ts}"
    rels = _paths_for(root, groups)
    if rels:
        try:
            backup_dir.mkdir(parents=True, exist_ok=True)
        except OSError:
            pass
        for rel in rels:
            _move_or_delete(root, rel, backup_dir, results)

    # 3) Record the outcome for the UI, then delete the marker so it runs once.
    note = None
    if "secrets" in groups:
        note = ("OS-keyring secrets, if any, were not cleared automatically — "
                "re-key them from the Secrets Vault row if needed.")
    result = {
        "at": at_iso,
        "groups": sorted(groups),
        "ok": results["ok"],
        "failed": results["failed"],
        "postgres": None if pg_ok is None else {"ok": pg_ok, "detail": pg_detail},
        "backup": str(backup_dir) if results["ok"] else None,
        "note": note,
    }
    try:
        RESULT_PATH.parent.mkdir(parents=True, exist_ok=True)
        RESULT_PATH.write_text(json.dumps(result, indent=2), encoding="utf-8")
    except OSError:
        pass
    try:
        MARKER_PATH.unlink()
    except OSError:
        pass
    logger.warning("[reset] done — moved=%d failed=%d backup=%s",
                   len(results["ok"]), len(results["failed"]), result["backup"])


def write_marker(groups: list[str], requested_by: str = "") -> list[str]:
    """Write the pending-reset marker. Returns the validated group list. Called by
    the reset endpoint just before it triggers the self-restart."""
    valid = [g for g in groups if g in GROUPS]
    MARKER_PATH.parent.mkdir(parents=True, exist_ok=True)
    MARKER_PATH.write_text(json.dumps({
        "groups": valid,
        "requested_by": requested_by,
        "requested_at": time.strftime("%Y-%m-%dT%H:%M:%S"),
    }, indent=2), encoding="utf-8")
    return valid


# ── Self-destruct (delete the entire installation) ───────────────────────────

# A tiny stdlib script written to the SYSTEM temp dir (never inside the repo, so
# it can't delete itself mid-run). It waits for the server to stop answering
# /health, then removes the whole install folder, retrying briefly for files a
# slow shutdown may still hold open (Windows).
_SELF_DESTRUCT_SCRIPT = r"""
import os, shutil, sys, time
import urllib.request, urllib.error

port = int(sys.argv[1]); root = sys.argv[2]

def health_up():
    try:
        with urllib.request.urlopen("http://127.0.0.1:%d/health" % port, timeout=2) as r:
            return r.status < 500
    except urllib.error.HTTPError as e:
        return e.code < 500
    except Exception:
        return False

# 1) Wait up to ~30s for the old server to go down (it frees its file handles).
deadline = time.time() + 30
while time.time() < deadline and health_up():
    time.sleep(0.7)

# 2) Remove the whole folder, retrying for locked files.
def onerr(func, path, exc):
    try:
        os.chmod(path, 0o777); func(path)
    except Exception:
        pass

for _ in range(20):
    shutil.rmtree(root, onerror=onerr)
    if not os.path.isdir(root):
        break
    time.sleep(1.0)
"""


def spawn_self_destruct(port: int | None = None) -> dict:
    """Write the self-destruct helper to a temp file OUTSIDE the repo, spawn it
    detached, and schedule this server to exit so the folder unlocks. Returns a
    status dict; the caller flushes its HTTP response before the process dies."""
    import tempfile
    import threading

    if port is None:
        try:
            port = int(os.environ.get("WEBAGENT_PORT") or 8080)
        except ValueError:
            port = 8080

    try:
        fd, script_path = tempfile.mkstemp(prefix="webagent-selfdestruct-", suffix=".py")
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            fh.write(_SELF_DESTRUCT_SCRIPT)
    except OSError as e:
        return {"status": "error", "reason": f"Could not stage the delete helper: {e}"}

    args = [sys.executable, script_path, str(port), str(_REPO_ROOT)]
    kwargs: dict = dict(stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
                        stderr=subprocess.DEVNULL, close_fds=True)
    # Run from a NEUTRAL cwd (system temp), not the repo — we're about to delete it.
    kwargs["cwd"] = tempfile.gettempdir()
    if sys.platform == "win32":
        kwargs["creationflags"] = 0x08000000 | 0x00000200  # NO_WINDOW | NEW_GROUP
    else:
        kwargs["start_new_session"] = True
    try:
        subprocess.Popen(args, **kwargs)
    except OSError as e:
        return {"status": "error", "reason": f"Could not spawn the delete helper: {e}"}

    def _die() -> None:
        time.sleep(0.7)
        os._exit(0)

    threading.Thread(target=_die, daemon=True).start()
    return {"status": "deleting",
            "message": "Server is shutting down and the installation is being deleted."}

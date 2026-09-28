"""Database-backed file observations and exclusive checkout leases."""

from __future__ import annotations

import hashlib
import json
import os
import stat
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Iterable, Optional

from app.db import get_db

PROJECT_ROOT = Path(__file__).resolve().parents[2]
LEASE_SECONDS = 1800
_EXCLUDED_PREFIXES = (
    "data/agent_data/", "data/user_data/", "data/db/", "data/uploads/",
    "data/engine_state/", "logs/", ".ast-index/", ".git/",
)
_EXCLUDED_SUFFIXES = (
    ".db", ".db-wal", ".db-shm", ".db-journal",
    ".sqlite", ".sqlite-wal", ".sqlite-shm", ".sqlite-journal",
)
_SCHEMA = """
CREATE TABLE IF NOT EXISTS file_checkout_leases (
 path TEXT PRIMARY KEY, session_id TEXT NOT NULL, user_id TEXT NOT NULL,
 agent_id TEXT, expected_fingerprint TEXT NOT NULL, checked_out_at TEXT NOT NULL,
 heartbeat_at TEXT NOT NULL, expires_at TEXT NOT NULL, modified INTEGER NOT NULL DEFAULT 0,
 validation_note TEXT, validated_at TEXT, violation TEXT);
CREATE INDEX IF NOT EXISTS idx_file_checkout_leases_session ON file_checkout_leases(session_id);
CREATE TABLE IF NOT EXISTS file_checkout_activity (
 session_id TEXT NOT NULL, path TEXT NOT NULL, observed_fingerprint TEXT,
 expected_fingerprint TEXT, state TEXT NOT NULL DEFAULT 'observed',
 modified INTEGER NOT NULL DEFAULT 0, validation_note TEXT, validated_at TEXT,
 violation TEXT, checked_in_at TEXT, updated_at TEXT NOT NULL,
 PRIMARY KEY (session_id, path));
CREATE INDEX IF NOT EXISTS idx_file_checkout_activity_session ON file_checkout_activity(session_id);
"""


class CheckoutError(RuntimeError):
    def __init__(self, message: str, files: Optional[list[dict]] = None):
        super().__init__(message)
        self.files = files or []

    def __str__(self) -> str:
        return json.dumps(
            {"error": "file_checkout", "message": super().__str__(), "files": self.files},
            sort_keys=True,
        )


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _iso(value: Optional[datetime] = None) -> str:
    return (value or _now()).isoformat(timespec="microseconds")


def _dump(value: Optional[dict]) -> Optional[str]:
    return json.dumps(value, sort_keys=True, separators=(",", ":")) if value is not None else None


def _load(value: Any) -> Optional[dict]:
    try:
        decoded = json.loads(value) if isinstance(value, str) else value
        return decoded if isinstance(decoded, dict) else None
    except (TypeError, ValueError):
        return None


def _same(left: Optional[dict], right: Optional[dict]) -> bool:
    return _dump(left) == _dump(right)


def _summary(value: Optional[dict]) -> Optional[dict]:
    if not value:
        return None
    return {key: value.get(key) for key in
            ("exists", "type", "size", "mtime_ns", "mode", "sha256")}


def normalize_path(raw: str, *, allow_missing: bool = True) -> tuple[str, Path]:
    if not isinstance(raw, str) or not raw.strip():
        raise CheckoutError("A non-empty repository file path is required.")
    supplied = Path(raw.strip())
    candidate = supplied if supplied.is_absolute() else PROJECT_ROOT / supplied
    try:
        lexical = Path(os.path.abspath(candidate))
        parts = lexical.relative_to(PROJECT_ROOT).parts
    except (OSError, ValueError):
        raise CheckoutError(f"Path is outside the project root: {raw}")
    cursor = PROJECT_ROOT
    for part in parts:
        cursor = cursor / part
        if cursor.is_symlink():
            raise CheckoutError(f"Symlink paths cannot be checked out: {raw}")
    relative = lexical.relative_to(PROJECT_ROOT).as_posix()
    lower = relative.lower()
    if not relative or relative == ".":
        raise CheckoutError("The repository root is not a file path.")
    if lower.startswith(_EXCLUDED_PREFIXES) or lower.endswith(_EXCLUDED_SUFFIXES):
        raise CheckoutError(f"Runtime or generated data is excluded from checkout: {relative}")
    if lexical.exists() and lexical.is_dir():
        raise CheckoutError(f"Directories cannot be checked out: {relative}")
    if lexical.exists() and not lexical.is_file():
        raise CheckoutError(f"Only regular files can be checked out: {relative}")
    if not lexical.exists() and not allow_missing:
        raise CheckoutError(f"File does not exist: {relative}")
    return relative, lexical


def fingerprint_path(raw: str | Path) -> dict:
    relative, path = normalize_path(str(raw))
    try:
        info = path.lstat()
    except FileNotFoundError:
        return {"path": relative, "exists": False, "type": "missing", "size": 0,
                "mtime_ns": None, "mode": None, "sha256": None}
    if stat.S_ISLNK(info.st_mode):
        raise CheckoutError(f"Symlink paths cannot be fingerprinted: {relative}")
    if not stat.S_ISREG(info.st_mode):
        raise CheckoutError(f"Only regular files can be fingerprinted: {relative}")
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return {"path": relative, "exists": True, "type": "file", "size": info.st_size,
            "mtime_ns": info.st_mtime_ns, "mode": stat.S_IMODE(info.st_mode),
            "sha256": digest.hexdigest()}


def _backend() -> Any:
    current, seen = get_db(), set()
    while id(current) not in seen:
        seen.add(id(current))
        if hasattr(current, "_inner"):
            current = current._inner
        elif hasattr(current, "_remote"):
            current = current._remote
        else:
            break
    return current


def _schema(conn: Any) -> None:
    for statement in (part.strip() for part in _SCHEMA.split(";") if part.strip()):
        conn.execute(statement)


async def _store(operation):
    backend = _backend()
    if not hasattr(backend, "_get_conn") or not hasattr(backend, "_write_lock"):
        raise CheckoutError("The active database backend cannot store file checkouts.")
    async with backend._write_lock:
        conn = backend._get_conn()
        try:
            _schema(conn)
            result = operation(conn)
            conn.commit()
            return result
        except CheckoutError:
            conn.commit()
            raise
        except Exception:
            try:
                conn.rollback()
            except Exception:
                pass
            raise
        finally:
            conn.close()


def _expire(conn: Any) -> None:
    conn.execute("DELETE FROM file_checkout_leases WHERE expires_at <= ?", (_iso(),))


def _activity(conn: Any, session_id: str, path: str) -> dict:
    row = conn.execute("SELECT * FROM file_checkout_activity WHERE session_id=? AND path=?",
                       (session_id, path)).fetchone()
    return dict(row) if row is not None else {}


def _put_activity(conn: Any, session_id: str, path: str, **updates: Any) -> None:
    old = _activity(conn, session_id, path)
    data = {
        "observed_fingerprint": old.get("observed_fingerprint"),
        "expected_fingerprint": old.get("expected_fingerprint"),
        "state": old.get("state") or "observed", "modified": int(old.get("modified") or 0),
        "validation_note": old.get("validation_note"), "validated_at": old.get("validated_at"),
        "violation": old.get("violation"), "checked_in_at": old.get("checked_in_at"),
    }
    data.update(updates)
    values = (data["observed_fingerprint"], data["expected_fingerprint"], data["state"],
              int(data["modified"] or 0), data["validation_note"], data["validated_at"],
              data["violation"], data["checked_in_at"], _iso(), session_id, path)
    if old:
        conn.execute(
            "UPDATE file_checkout_activity SET observed_fingerprint=?,expected_fingerprint=?,"
            "state=?,modified=?,validation_note=?,validated_at=?,violation=?,checked_in_at=?,"
            "updated_at=? WHERE session_id=? AND path=?", values)
    else:
        conn.execute(
            "INSERT INTO file_checkout_activity "
            "(observed_fingerprint,expected_fingerprint,state,modified,validation_note,"
            "validated_at,violation,checked_in_at,updated_at,session_id,path) "
            "VALUES (?,?,?,?,?,?,?,?,?,?,?)", values)


async def observe_files(session_id: str, paths: Iterable[str]) -> list[dict]:
    fingerprints = [fingerprint_path(path) for path in dict.fromkeys(paths)]
    if not session_id or not fingerprints:
        return fingerprints
    def op(conn):
        _expire(conn)
        for fp in fingerprints:
            path = fp["path"]
            lease = conn.execute("SELECT * FROM file_checkout_leases WHERE path=?", (path,)).fetchone()
            state, violation = "observed", None
            if lease and lease["session_id"] == session_id:
                if not _same(_load(lease["expected_fingerprint"]), fp):
                    state = "violation"
                    violation = ("File changed outside an approved checked-out write; "
                                 "reread and call checkout_files to reconcile.")
                    conn.execute("UPDATE file_checkout_leases SET violation=? WHERE path=?",
                                 (violation, path))
                else:
                    state = ("validated" if lease["validated_at"] else
                             "changed" if lease["modified"] else "checked_out")
            _put_activity(conn, session_id, path, observed_fingerprint=_dump(fp),
                          state=state, violation=violation)
        return fingerprints
    return await _store(op)


async def checkout_files(session_id: str, user_id: str, agent_id: Optional[str],
                         paths: Iterable[str]) -> list[dict]:
    fingerprints = [fingerprint_path(path) for path in dict.fromkeys(paths)]
    if not fingerprints:
        raise CheckoutError("Select at least one file to check out.")
    def op(conn):
        _expire(conn)
        now = _now()
        expires = _iso(now + timedelta(seconds=LEASE_SECONDS))
        errors = []
        for fp in fingerprints:
            path = fp["path"]
            lease = conn.execute("SELECT * FROM file_checkout_leases WHERE path=?", (path,)).fetchone()
            if lease and lease["session_id"] != session_id:
                errors.append({"path": path, "status": "conflict", "owner": lease["session_id"],
                               "expires_at": lease["expires_at"],
                               "error": "File is checked out by another session."})
                continue
            activity = _activity(conn, session_id, path)
            observed = _load(activity.get("observed_fingerprint"))
            if activity.get("state") == "recheck_required":
                errors.append({"path": path, "status": "recheck_required",
                               "error": "This checkout was manually released. Reread the file before checkout."})
            elif lease and lease["session_id"] == session_id:
                expected = _load(lease["expected_fingerprint"])
                if lease["violation"] and not _same(observed, fp):
                    errors.append({"path": path, "status": "violation",
                                   "error": "Reread the file before reconciling this checkout."})
                elif not lease["violation"] and not _same(expected, fp):
                    errors.append({
                        "path": path, "status": "stale",
                        "expected": _summary(expected), "current": _summary(fp),
                        "error": "The checked-out file changed outside an approved write. Read it again.",
                    })
            elif observed is not None and not _same(observed, fp):
                errors.append({"path": path, "status": "stale", "observed": _summary(observed),
                               "current": _summary(fp),
                               "error": "The file changed after it was read. Read it again before checkout."})
        if errors:
            for error in errors:
                _put_activity(conn, session_id, error["path"], state=error["status"],
                              violation=error["error"])
            raise CheckoutError("Checkout failed; no files were acquired.", errors)
        results = []
        for fp in fingerprints:
            path = fp["path"]
            lease = conn.execute("SELECT path FROM file_checkout_leases WHERE path=?", (path,)).fetchone()
            if lease:
                conn.execute(
                    "UPDATE file_checkout_leases SET user_id=?,agent_id=?,expected_fingerprint=?,"
                    "heartbeat_at=?,expires_at=?,violation=NULL WHERE path=? AND session_id=?",
                    (user_id, agent_id, _dump(fp), _iso(now), expires, path, session_id))
            else:
                conn.execute(
                    "INSERT INTO file_checkout_leases "
                    "(path,session_id,user_id,agent_id,expected_fingerprint,checked_out_at,heartbeat_at,expires_at) "
                    "VALUES (?,?,?,?,?,?,?,?)",
                    (path, session_id, user_id, agent_id, _dump(fp), _iso(now), _iso(now), expires))
            _put_activity(conn, session_id, path, observed_fingerprint=_dump(fp),
                          expected_fingerprint=_dump(fp), state="checked_out", violation=None)
            results.append({"path": path, "status": "checked_out", "owner": session_id,
                            "expires_at": expires, "fingerprint": _summary(fp)})
        return results
    return await _store(op)


async def preflight_write(session_id: str, paths: Iterable[str]) -> list[dict]:
    fingerprints = [fingerprint_path(path) for path in dict.fromkeys(paths)]
    if not fingerprints:
        raise CheckoutError("Declare at least one checkout path for this write.")
    def op(conn):
        _expire(conn)
        errors = []
        for fp in fingerprints:
            row = conn.execute("SELECT * FROM file_checkout_leases WHERE path=?",
                               (fp["path"],)).fetchone()
            if not row or row["session_id"] != session_id:
                errors.append({"path": fp["path"], "status": "conflict",
                               "error": "File is not checked out by this session."})
                continue
            expected = _load(row["expected_fingerprint"])
            if row["violation"] or not _same(expected, fp):
                message = row["violation"] or "File changed outside an approved checked-out write."
                conn.execute("UPDATE file_checkout_leases SET violation=? WHERE path=?",
                             (message, fp["path"]))
                _put_activity(conn, session_id, fp["path"], state="violation", violation=message)
                errors.append({"path": fp["path"], "status": "violation",
                               "expected": _summary(expected), "current": _summary(fp), "error": message})
        if errors:
            raise CheckoutError("Write preflight failed.", errors)
        return fingerprints
    return await _store(op)


async def approve_write_result(session_id: str, paths: Iterable[str]) -> list[dict]:
    fingerprints = [fingerprint_path(path) for path in dict.fromkeys(paths)]
    def op(conn):
        now, expires, output = _iso(), _iso(_now() + timedelta(seconds=LEASE_SECONDS)), []
        for fp in fingerprints:
            row = conn.execute("SELECT path FROM file_checkout_leases WHERE path=? AND session_id=?",
                               (fp["path"], session_id)).fetchone()
            if not row:
                continue
            conn.execute(
                "UPDATE file_checkout_leases SET expected_fingerprint=?,modified=1,"
                "validation_note=NULL,validated_at=NULL,violation=NULL,heartbeat_at=?,expires_at=? "
                "WHERE path=? AND session_id=?",
                (_dump(fp), now, expires, fp["path"], session_id))
            _put_activity(conn, session_id, fp["path"], expected_fingerprint=_dump(fp),
                          state="changed", modified=1, validation_note=None,
                          validated_at=None, violation=None)
            output.append(fp)
        return output
    return await _store(op)


async def mark_violation(session_id: str, paths: Iterable[str], message: str) -> None:
    normalized = [normalize_path(path)[0] for path in dict.fromkeys(paths)]
    def op(conn):
        for path in normalized:
            conn.execute("UPDATE file_checkout_leases SET violation=? WHERE path=? AND session_id=?",
                         (message, path, session_id))
            _put_activity(conn, session_id, path, state="violation", violation=message)
    await _store(op)


async def mark_validated(session_id: str, paths: Iterable[str], note: str) -> int:
    normalized = [normalize_path(path)[0] for path in dict.fromkeys(paths)]
    def op(conn):
        count, now = 0, _iso()
        for path in normalized:
            row = conn.execute(
                "SELECT path,modified FROM file_checkout_leases WHERE path=? AND session_id=?",
                (path, session_id),
            ).fetchone()
            if row and row["modified"]:
                conn.execute("UPDATE file_checkout_leases SET validation_note=?,validated_at=? "
                             "WHERE path=? AND session_id=?", (note or "Validated", now, path, session_id))
                _put_activity(conn, session_id, path, state="validated",
                              validation_note=note or "Validated", validated_at=now)
                count += 1
        return count
    return await _store(op)


async def checkin_files(session_id: str, paths: Iterable[str],
                        validation_note: Optional[str] = None) -> list[dict]:
    fingerprints = [fingerprint_path(path) for path in dict.fromkeys(paths)]
    if not fingerprints:
        raise CheckoutError("Select at least one file to check in.")
    def op(conn):
        _expire(conn)
        errors, leases = [], {}
        for fp in fingerprints:
            row = conn.execute("SELECT * FROM file_checkout_leases WHERE path=?",
                               (fp["path"],)).fetchone()
            leases[fp["path"]] = row
            if not row or row["session_id"] != session_id:
                errors.append({"path": fp["path"], "status": "conflict",
                               "error": "File is not checked out by this session."})
            elif row["violation"] or not _same(_load(row["expected_fingerprint"]), fp):
                message = row["violation"] or "File changed outside an approved checked-out write."
                conn.execute("UPDATE file_checkout_leases SET violation=? WHERE path=?",
                             (message, fp["path"]))
                _put_activity(conn, session_id, fp["path"], state="violation", violation=message)
                errors.append({"path": fp["path"], "status": "violation", "error": message})
        if errors:
            raise CheckoutError("Check-in failed; no files were released.", errors)
        output, now = [], _iso()
        for fp in fingerprints:
            row, path = leases[fp["path"]], fp["path"]
            note = (validation_note or "").strip() or row["validation_note"]
            conn.execute("DELETE FROM file_checkout_leases WHERE path=? AND session_id=?",
                         (path, session_id))
            _put_activity(conn, session_id, path, expected_fingerprint=_dump(fp),
                          state="checked_in", validation_note=note,
                          validated_at=(now if note else row["validated_at"]),
                          violation=None, checked_in_at=now)
            output.append({"path": path, "status": "checked_in", "owner": session_id,
                           "fingerprint": _summary(fp), "validation_note": note})
        return output
    return await _store(op)


async def abandon_file_checkouts(session_id: str, paths: Iterable[str],
                                  reason: Optional[str] = None) -> list[dict]:
    normalized = [normalize_path(path)[0] for path in dict.fromkeys(paths)]
    if not normalized:
        raise CheckoutError("Select at least one checkout to abandon.")
    def op(conn):
        for path in normalized:
            row = conn.execute("SELECT session_id FROM file_checkout_leases WHERE path=?",
                               (path,)).fetchone()
            if not row or row["session_id"] != session_id:
                raise CheckoutError(f"File is not checked out by this session: {path}")
        for path in normalized:
            conn.execute("DELETE FROM file_checkout_leases WHERE path=? AND session_id=?",
                         (path, session_id))
            _put_activity(conn, session_id, path, state="abandoned",
                          violation=(reason or "").strip() or None)
        return [{"path": path, "status": "abandoned", "reason": (reason or "").strip()}
                for path in normalized]
    return await _store(op)


async def manual_checkin_files(requester_session_id: str, user_id: str,
                               paths: Iterable[str]) -> list[dict]:
    """Release another same-user lease and require its owner to reread before reuse."""
    normalized = [normalize_path(path)[0] for path in dict.fromkeys(paths)]
    if not normalized:
        raise CheckoutError("Select at least one checked-out file to check in.")
    def op(conn):
        _expire(conn)
        errors, leases = [], {}
        for path in normalized:
            row = conn.execute("SELECT * FROM file_checkout_leases WHERE path=?", (path,)).fetchone()
            leases[path] = row
            if not row:
                errors.append({"path": path, "status": "missing",
                               "error": "File is no longer checked out."})
            elif row["user_id"] != user_id:
                errors.append({"path": path, "status": "conflict",
                               "error": "Only the lease owner's user may manually check it in."})
            elif row["session_id"] == requester_session_id:
                errors.append({"path": path, "status": "owner",
                               "error": "Use ordinary check-in for this session's own lease."})
        if errors:
            raise CheckoutError("Manual check-in failed; no files were released.", errors)
        now, output = _iso(), []
        for path, row in leases.items():
            fp = fingerprint_path(path)
            message = ("Lease manually checked in from this session. "
                       "Reread the file before checking it out again.")
            conn.execute("DELETE FROM file_checkout_leases WHERE path=?", (path,))
            _put_activity(conn, row["session_id"], path, observed_fingerprint=None,
                          expected_fingerprint=_dump(fp), state="recheck_required",
                          violation=message)
            _put_activity(conn, requester_session_id, path, expected_fingerprint=_dump(fp),
                          state="checked_in", violation=None, checked_in_at=now)
            output.append({"path": path, "status": "checked_in", "owner": row["session_id"],
                           "manual": True, "fingerprint": _summary(fp)})
        return output
    return await _store(op)


async def heartbeat_file_checkouts(session_id: str) -> int:
    if not session_id:
        return 0
    def op(conn):
        now = _now()
        _expire(conn)
        cur = conn.execute("UPDATE file_checkout_leases SET heartbeat_at=?,expires_at=? "
                           "WHERE session_id=?",
                           (_iso(now), _iso(now + timedelta(seconds=LEASE_SECONDS)), session_id))
        return int(cur.rowcount or 0)
    return await _store(op)


async def release_session_checkouts(session_id: str, reason: str = "Session cleanup") -> int:
    def op(conn):
        rows = conn.execute("SELECT path FROM file_checkout_leases WHERE session_id=?",
                            (session_id,)).fetchall()
        for row in rows:
            _put_activity(conn, session_id, row["path"], state="abandoned", violation=reason)
        conn.execute("DELETE FROM file_checkout_leases WHERE session_id=?", (session_id,))
        return len(rows)
    return await _store(op)


async def file_checkout_status(session_id: str,
                               paths: Optional[Iterable[str]] = None,
                               *, include_user_leases: bool = False,
                               user_id: Optional[str] = None) -> list[dict]:
    wanted = None if paths is None else [normalize_path(path)[0] for path in dict.fromkeys(paths)]
    def op(conn):
        _expire(conn)
        params: list[Any] = [session_id]
        where = "WHERE a.session_id=?"
        if wanted:
            where += " AND a.path IN (%s)" % ",".join("?" for _ in wanted)
            params.extend(wanted)
        rows = conn.execute(
            "SELECT a.*,l.session_id AS lease_owner,l.user_id AS lease_user_id,"
            "l.agent_id AS lease_agent_id,l.expires_at,l.modified AS lease_modified,"
            "l.validation_note AS lease_validation_note,l.validated_at AS lease_validated_at,"
            "l.violation AS lease_violation FROM file_checkout_activity a "
            "LEFT JOIN file_checkout_leases l ON l.path=a.path " + where + " ORDER BY a.path",
            tuple(params)).fetchall()
        output = []
        for raw in rows:
            row = dict(raw)
            owner, status_name = row.get("lease_owner"), row.get("state") or "observed"
            if owner == session_id:
                status_name = ("violation" if row.get("lease_violation") else
                               "validated" if row.get("lease_validated_at") else
                               "changed" if row.get("lease_modified") else "checked_out")
            output.append({
                "path": row["path"], "status": status_name, "active": owner == session_id,
                "owner": owner, "owner_user_id": row.get("lease_user_id"),
                "owner_agent_id": row.get("lease_agent_id"), "expires_at": row.get("expires_at"),
                "manual_checkin_allowed": False,
                "observed_fingerprint": _summary(_load(row.get("observed_fingerprint"))),
                "expected_fingerprint": _summary(_load(row.get("expected_fingerprint"))),
                "modified": bool(row.get("lease_modified") or row.get("modified")),
                "validated": bool(row.get("lease_validated_at") or row.get("validated_at")),
                "validation_note": row.get("lease_validation_note") or row.get("validation_note"),
                "violation": row.get("lease_violation") or row.get("violation"),
                "checked_in_at": row.get("checked_in_at"),
            })
        if include_user_leases and user_id:
            known_paths = {row["path"] for row in output}
            extra_rows = conn.execute(
                "SELECT * FROM file_checkout_leases WHERE user_id=? AND session_id<>? ORDER BY path",
                (user_id, session_id),
            ).fetchall()
            for raw in extra_rows:
                row = dict(raw)
                if row["path"] in known_paths:
                    continue
                output.append({
                    "path": row["path"], "status": "checked_out_elsewhere", "active": False,
                    "owner": row["session_id"], "owner_user_id": row["user_id"],
                    "owner_agent_id": row.get("agent_id"), "expires_at": row.get("expires_at"),
                    "manual_checkin_allowed": True, "observed_fingerprint": None,
                    "expected_fingerprint": _summary(_load(row.get("expected_fingerprint"))),
                    "modified": bool(row.get("modified")), "validated": bool(row.get("validated_at")),
                    "validation_note": row.get("validation_note"), "violation": row.get("violation"),
                    "checked_in_at": None,
                })
            output.sort(key=lambda row: row["path"])
        return output
    return await _store(op)


async def active_checkout_paths(session_id: str) -> list[str]:
    return [row["path"] for row in await file_checkout_status(session_id) if row.get("active")]


def snapshot_repository_tree() -> dict[str, str]:
    """Capture exact fingerprints for repository source without spawning Git."""
    ignored_dirs = {
        ".git", "data", "logs", "uploads", "node_modules", "__pycache__",
        ".venv", "venv", "env", ".source-backups", "temp", ".pytest_cache",
        ".mypy_cache", ".cache",
    }
    ignored_suffixes = (
        ".pyc", ".pyo", ".bak", ".db", ".sqlite", ".db-wal", ".db-shm",
        ".sqlite-wal", ".sqlite-shm",
    )
    output: dict[str, str] = {}
    for root, dirs, files in os.walk(PROJECT_ROOT, followlinks=False):
        root_path = Path(root)
        dirs[:] = [
            name for name in dirs
            if name not in ignored_dirs and not (root_path / name).is_symlink()
        ]
        for name in files:
            if name.endswith(ignored_suffixes):
                continue
            target = root_path / name
            if target.is_symlink():
                continue
            try:
                relative = target.relative_to(PROJECT_ROOT).as_posix()
                output[relative] = _dump(_summary(fingerprint_path(relative))) or ""
            except (CheckoutError, OSError):
                continue
    return output


async def audit_external_tree_delta(
    session_id: str, before: dict[str, str], after: dict[str, str]
) -> list[str]:
    """Flag native/shell changes not reconciled through checkout-aware writes."""
    changed = sorted(
        {path for path, value in after.items() if before.get(path) != value}
        | {path for path in before if path not in after}
    )
    if not changed:
        return []
    statuses = {row["path"]: row for row in await file_checkout_status(session_id, changed)}
    violations = []
    for path in changed:
        record = statuses.get(path)
        current = _summary(fingerprint_path(path))
        if not record or not _same(record.get("expected_fingerprint"), current):
            violations.append(path)
    if violations:
        await mark_violation(
            session_id, violations,
            "Native editor or shell changed the file outside an approved checkout-aware write; "
            "reread and call checkout_files to reconcile."
        )
    return violations

"""Prepare and verify the app/user/agent storage layout without deleting data.

Dry-run is the default.  ``--apply`` creates/populates ``data/db/app.db``, fills
missing rows in per-user stores, and refreshes every per-agent authority bundle.
Legacy files and ``_mig_*`` tables remain untouched for rollback.
"""

from __future__ import annotations

import argparse
import asyncio
import hashlib
import json
import logging
import sqlite3
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Iterable

from app.db.schema.ownership import StoragePlane, tables_for_plane
from app.db.schema import render_plane
from app.db.storage_layout import (
    APP_DB_PATH,
    LEGACY_GLOBAL_DB_PATH,
    activate_layout,
    begin_layout,
    get_app_store,
    mark_plane_status,
    read_layout,
)

logger = logging.getLogger(__name__)


def _q(identifier: str) -> str:
    if not identifier.replace("_", "").isalnum():
        raise ValueError(f"Unsafe SQL identifier: {identifier!r}")
    return f'"{identifier}"'


def _open_readonly(path: Path) -> sqlite3.Connection:
    # immutable=1 guarantees an audit never creates/touches WAL sidecars.  Apply
    # should be run with the server stopped so every committed row is in the
    # main database file.
    conn = sqlite3.connect(f"file:{path.as_posix()}?mode=ro&immutable=1", uri=True)
    conn.row_factory = sqlite3.Row
    return conn


def _tables(conn: sqlite3.Connection) -> set[str]:
    return {
        row[0]
        for row in conn.execute("SELECT name FROM sqlite_master WHERE type='table'").fetchall()
    }


def _columns(conn: sqlite3.Connection, table: str) -> list[str]:
    return [row[1] for row in conn.execute(f"PRAGMA table_info({_q(table)})").fetchall()]


def _primary_key(conn: sqlite3.Connection, table: str) -> list[str]:
    rows = conn.execute(f"PRAGMA table_info({_q(table)})").fetchall()
    return [row[1] for row in sorted((row for row in rows if row[5]), key=lambda row: row[5])]


def _stable_identity_columns(
    conn: sqlite3.Connection,
    table: str,
    rows: list[sqlite3.Row],
    available: list[str],
) -> list[str]:
    """Choose a non-null key suitable for additive migration verification.

    Some legacy SQLite tables declared ``TEXT PRIMARY KEY`` without ``NOT
    NULL`` and contain NULL keys (notably ``channel_identities``).  Prefer the
    declared primary key when it is populated, otherwise use the first usable
    unique index.  Verification intentionally compares identities rather than
    complete row values: an already-populated per-user authority may contain a
    newer version of a row than the stale compatibility copy in local.db.
    """
    candidates: list[list[str]] = []
    primary = [column for column in _primary_key(conn, table) if column in available]
    if primary:
        candidates.append(primary)
    for index in conn.execute(f"PRAGMA index_list({_q(table)})").fetchall():
        if not index[2] or index[4]:  # non-unique or partial
            continue
        columns = [
            row[2]
            for row in conn.execute(f"PRAGMA index_info({_q(index[1])})").fetchall()
            if row[2] in available
        ]
        if columns:
            candidates.append(columns)
    for candidate in candidates:
        if all(all(row[column] is not None for column in candidate) for row in rows):
            return candidate
    return available


def _json_value(value: Any) -> Any:
    if isinstance(value, (bytes, bytearray, memoryview)):
        return {"sha256": hashlib.sha256(bytes(value)).hexdigest(), "bytes": len(value)}
    return value


def _digest(rows: Iterable[sqlite3.Row], columns: list[str]) -> str:
    hasher = hashlib.sha256()
    normalized = []
    for row in rows:
        normalized.append([_json_value(row[column]) for column in columns])
    normalized.sort(key=lambda value: json.dumps(value, sort_keys=True, default=str))
    for value in normalized:
        hasher.update(json.dumps(value, sort_keys=True, default=str, separators=(",", ":")).encode())
        hasher.update(b"\n")
    return hasher.hexdigest()


@dataclass
class TableResult:
    plane: str
    table: str
    source: str
    source_count: int
    copied: int = 0
    verified: bool = False
    detail: str = ""


@dataclass
class MigrationReport:
    dry_run: bool
    app_db: str
    tables: list[TableResult] = field(default_factory=list)
    users: int = 0
    agents: int = 0
    warnings: list[str] = field(default_factory=list)

    def as_dict(self) -> dict:
        return {
            "dry_run": self.dry_run,
            "app_db": self.app_db,
            "tables": [vars(result) for result in self.tables],
            "users": self.users,
            "agents": self.agents,
            "warnings": self.warnings,
        }


def _source_table(conn: sqlite3.Connection, logical_name: str) -> str | None:
    existing = _tables(conn)
    if logical_name in existing:
        return logical_name
    migrated = f"_mig_{logical_name}"
    return migrated if migrated in existing else None


def _copy_rows(
    source: sqlite3.Connection,
    source_table: str,
    target: sqlite3.Connection,
    target_table: str,
    *,
    where: str = "",
    params: tuple[Any, ...] = (),
    replace_existing: bool = False,
) -> tuple[int, int, bool, str, str]:
    source_cols = _columns(source, source_table)
    target_cols = _columns(target, target_table)
    columns = [column for column in target_cols if column in source_cols]
    if not columns:
        return 0, 0, False, "", ""
    sql = f"SELECT {', '.join(_q(c) for c in columns)} FROM {_q(source_table)}"
    if where:
        sql += f" WHERE {where}"
    rows = source.execute(sql, params).fetchall()
    before = target.total_changes
    if rows:
        conflict = "REPLACE" if replace_existing else "IGNORE"
        target.executemany(
            f"INSERT OR {conflict} INTO {_q(target_table)} ({', '.join(_q(c) for c in columns)}) "
            f"VALUES ({', '.join('?' for _ in columns)})",
            [tuple(row[column] for column in columns) for row in rows],
        )
    copied = target.total_changes - before

    identity = _stable_identity_columns(target, target_table, rows, columns)
    target_rows = []
    for row in rows:
        predicate = " AND ".join(f"{_q(column)} IS ?" for column in identity)
        found = target.execute(
            f"SELECT {', '.join(_q(c) for c in identity)} FROM {_q(target_table)} WHERE {predicate}",
            tuple(row[column] for column in identity),
        ).fetchone()
        if found is not None:
            target_rows.append(found)
    source_hash = _digest(rows, identity)
    target_hash = _digest(target_rows, identity)
    return len(rows), copied, len(target_rows) == len(rows) and source_hash == target_hash, source_hash, target_hash


def _record(
    conn: sqlite3.Connection,
    *,
    plane: str,
    source: str,
    target: str,
    table: str,
    source_count: int,
    verified: bool,
    source_hash: str,
    target_hash: str,
) -> None:
    migration_id = hashlib.sha256(f"{plane}|{source}|{target}|{table}".encode()).hexdigest()[:32]
    conn.execute(
        """INSERT INTO storage_migrations
           (migration_id,source_ref,target_ref,table_name,state,source_count,target_count,
            source_hash,target_hash,detail,updated_at)
           VALUES (?,?,?,?,?,?,?,?,?,'{}',datetime('now'))
           ON CONFLICT(migration_id) DO UPDATE SET
             state=excluded.state, source_count=excluded.source_count,
             target_count=excluded.target_count, source_hash=excluded.source_hash,
             target_hash=excluded.target_hash, updated_at=excluded.updated_at""",
        (
            migration_id,
            source,
            target,
            table,
            "verified" if verified else "failed",
            source_count,
            source_count if verified else 0,
            source_hash,
            target_hash,
        ),
    )


class StorageLayoutMigrator:
    def __init__(self, project_root: Path, *, app_db: Path | None = None):
        self.root = project_root.resolve()
        self.app_db = (app_db or (self.root / "data" / "db" / "app.db")).resolve()
        self.local_db = (self.root / "data" / "db" / "local.db").resolve()
        self.global_db = (self.root / "data" / "db" / "global.db").resolve()
        self.user_dir = (self.root / "data" / "user_data").resolve()

    def _sources(self) -> dict[str, sqlite3.Connection]:
        result = {}
        for name, path in (("local", self.local_db), ("global", self.global_db)):
            if path.exists() and path.stat().st_size:
                result[name] = _open_readonly(path)
        return result

    def _verify_authority_schema(
        self,
        path: Path,
        plane: StoragePlane,
    ) -> tuple[int, str]:
        if not path.exists() or not path.stat().st_size:
            raise RuntimeError(f"{plane.value} authority database is missing: {path}")
        conn = _open_readonly(path)
        try:
            integrity = str(conn.execute("PRAGMA quick_check").fetchone()[0])
            if integrity != "ok":
                raise RuntimeError(
                    f"{plane.value} authority database failed quick_check: {path}: {integrity}"
                )
            expected = set(tables_for_plane(plane))
            missing = sorted(expected - _tables(conn))
            if missing:
                raise RuntimeError(
                    f"{plane.value} authority database {path} is missing tables: "
                    + ", ".join(missing)
                )
            digest = hashlib.sha256("\n".join(sorted(expected)).encode()).hexdigest()
            return len(expected), digest
        finally:
            conn.close()

    def _existing_user_paths(self) -> list[Path]:
        if not self.user_dir.exists():
            return []
        paths: list[Path] = []
        for directory in sorted(self.user_dir.iterdir()):
            if not directory.is_dir():
                continue
            candidate = directory / f"{directory.name}.db"
            if candidate.exists():
                paths.append(candidate.resolve())
        return paths

    def _expected_agent_paths(self) -> list[Path]:
        conn = _open_readonly(self.app_db)
        try:
            agents = [
                (str(row[0]), str(row[1] or ""))
                for row in conn.execute("SELECT agent_id, storage_ref FROM agent_catalog")
            ]
        finally:
            conn.close()
        root = (self.root / "data" / "agent_data").resolve()
        paths: list[Path] = []
        for agent_id, storage_ref in sorted(agents):
            if storage_ref:
                relative = Path(storage_ref)
                if relative.parts and relative.parts[0] == "agent_data":
                    candidate = (self.root / "data" / relative).resolve()
                else:
                    candidate = (self.root / relative).resolve()
            else:
                candidate = (root / agent_id / f"{agent_id}.db").resolve()
            try:
                candidate.relative_to(root)
            except ValueError as exc:
                raise RuntimeError(
                    f"Agent storage_ref escapes authority root for {agent_id!r}: {candidate}"
                ) from exc
            paths.append(candidate)
        return paths

    def _verify_plane_paths(
        self,
        plane: StoragePlane,
        paths: list[Path],
    ) -> tuple[int, str]:
        digests: list[str] = []
        for path in paths:
            _, digest = self._verify_authority_schema(path, plane)
            digests.append(f"{path.name}:{digest}")
        if not digests:
            digests.append(f"empty:{plane.value}")
        return len(paths), hashlib.sha256("\n".join(digests).encode()).hexdigest()

    def activate_existing(self) -> dict:
        """Verify and activate an already-split installation with no legacy sources.

        This recovery is intentionally explicit. It never infers activation from
        file existence, and it refuses to bypass the additive migrator while a
        legacy authority database is still present.
        """
        current = read_layout(path=self.app_db)
        if current and current.get("state") == "active":
            return current

        legacy = [
            path
            for path in (self.local_db, self.global_db)
            if path.exists() and path.stat().st_size
        ]
        if legacy:
            raise RuntimeError(
                "Legacy authority databases are present; run the additive --apply "
                "migration instead of --activate-existing"
            )

        plane_paths = {
            StoragePlane.APP: [self.app_db],
            StoragePlane.USER: self._existing_user_paths(),
            StoragePlane.AGENT: self._expected_agent_paths(),
        }
        verified = {
            plane: self._verify_plane_paths(plane, paths)
            for plane, paths in plane_paths.items()
        }

        conn = _open_readonly(self.app_db)
        try:
            failed = conn.execute(
                "SELECT COUNT(*) FROM storage_migrations WHERE state != 'verified'"
            ).fetchone()[0]
        finally:
            conn.close()
        if failed:
            raise RuntimeError(
                f"Cannot activate existing layout: {failed} migration records are not verified"
            )

        begin_layout(
            path=self.app_db,
            manifest={
                "plane_status": {"app": "pending", "user": "pending", "agent": "pending"},
                "legacy_files_retained": False,
                "activation_mode": "verified-existing-split",
            },
        )
        store = get_app_store(path=self.app_db, initialize=True)
        migration_conn = store._connect()
        try:
            for plane, paths in plane_paths.items():
                count, digest = verified[plane]
                target = ",".join(str(path) for path in paths) or f"{plane.value}:empty"
                _record(
                    migration_conn,
                    plane=plane.value,
                    source=f"existing-split:{plane.value}",
                    target=target,
                    table="__schema__",
                    source_count=count,
                    verified=True,
                    source_hash=digest,
                    target_hash=digest,
                )
            migration_conn.commit()
        finally:
            migration_conn.close()

        for plane, paths in plane_paths.items():
            mark_plane_status(
                plane.value,
                "verified",
                path=self.app_db,
                summary={"databases": len(paths)},
            )
        return activate_layout(
            path=self.app_db,
            manifest={"activation_mode": "verified-existing-split"},
        )

    def _app_source(self, table: str, sources: dict[str, sqlite3.Connection]):
        # global.db is authoritative for the shared template/catalog subset;
        # everything else still lives in local.db during the compatibility era.
        preferred = ("global", "local") if table in {
            "agent_templates", "agent_prompt_templates", "app_meta", "tools"
        } else ("local", "global")
        for name in preferred:
            conn = sources.get(name)
            if conn is None:
                continue
            physical = _source_table(conn, table)
            if physical:
                return name, conn, physical
        return None

    def _identity_rows(
        self,
        table: str,
        sources: dict[str, sqlite3.Connection],
    ) -> list[dict]:
        """Merge compatibility copies, choosing the newest row per user."""
        candidates: list[tuple[int, sqlite3.Connection, str]] = []
        # The current split writes central identity through the admin-attached
        # user DB, so inspect per-user files before legacy local/global copies.
        user_paths = sorted(self.user_dir.glob("*/*.db"), key=lambda p: (p.parent.name != "admin", str(p)))
        opened: list[sqlite3.Connection] = []
        try:
            for path in user_paths:
                try:
                    conn = _open_readonly(path)
                except Exception:
                    continue
                opened.append(conn)
                physical = _source_table(conn, table)
                if physical:
                    candidates.append((2 if path.parent.name == "admin" else 1, conn, physical))
            for name in ("local", "global"):
                conn = sources.get(name)
                if conn is None:
                    continue
                physical = _source_table(conn, table)
                if physical:
                    candidates.append((0, conn, physical))

            selected: dict[str, tuple[tuple[str, int], dict]] = {}
            for priority, conn, physical in candidates:
                for row in conn.execute(f"SELECT * FROM {_q(physical)}").fetchall():
                    payload = dict(row)
                    user_id = str(payload.get("user_id") or "")
                    if not user_id:
                        continue
                    score = (str(payload.get("updated_at") or payload.get("last_login_at") or ""), priority)
                    current = selected.get(user_id)
                    if current is None or score > current[0]:
                        selected[user_id] = (score, payload)
            return [entry[1] for entry in selected.values()]
        finally:
            for conn in opened:
                conn.close()

    def _copy_identity_rows(
        self,
        table: str,
        rows: list[dict],
        target: sqlite3.Connection,
    ) -> tuple[int, int, bool, str, str]:
        target_cols = _columns(target, table)
        before = target.total_changes
        normalized_source: list[dict] = []
        normalized_target: list[dict] = []
        for payload in rows:
            cols = [column for column in target_cols if column in payload]
            target.execute(
                f"INSERT OR REPLACE INTO {_q(table)} ({', '.join(_q(c) for c in cols)}) "
                f"VALUES ({', '.join('?' for _ in cols)})",
                tuple(payload[column] for column in cols),
            )
            normalized_source.append({column: payload[column] for column in cols})
            found = target.execute(
                f"SELECT {', '.join(_q(c) for c in target_cols)} FROM {_q(table)} WHERE user_id=?",
                (payload["user_id"],),
            ).fetchone()
            if found is not None:
                normalized_target.append({column: found[column] for column in cols})
        def digest_objects(values: list[dict]) -> str:
            encoded = json.dumps(
                sorted(values, key=lambda item: str(item.get("user_id") or "")),
                sort_keys=True,
                default=str,
                separators=(",", ":"),
            ).encode()
            return hashlib.sha256(encoded).hexdigest()

        source_hash = digest_objects(normalized_source)
        target_hash = digest_objects(normalized_target)
        verified = len(normalized_target) == len(rows) and source_hash == target_hash
        return len(rows), target.total_changes - before, verified, source_hash, target_hash

    def run(self, *, apply: bool = False) -> MigrationReport:
        report = MigrationReport(dry_run=not apply, app_db=str(self.app_db))
        sources = self._sources()
        if not sources:
            report.warnings.append("No legacy local.db/global.db source was found")
            return report

        target_store = None
        target = None
        if apply:
            begin_layout(
                path=self.app_db,
                manifest={
                    "plane_status": {"app": "preparing", "user": "pending", "agent": "pending"},
                    "legacy_files_retained": True,
                },
            )
            target_store = get_app_store(path=self.app_db, initialize=True)
            target = target_store._connect()

        try:
            for table in tables_for_plane(StoragePlane.APP):
                if table in {"agent_catalog", "storage_layout", "storage_migrations"}:
                    continue
                if table in {"user_profiles", "user_accounts"}:
                    rows = self._identity_rows(table, sources)
                    result = TableResult("app", table, "identity-merge", len(rows))
                    if apply and target is not None:
                        count, copied, verified, source_hash, target_hash = self._copy_identity_rows(
                            table, rows, target
                        )
                        result.copied = copied
                        result.verified = verified
                        _record(
                            target,
                            plane="app",
                            source="identity-merge",
                            target=str(self.app_db),
                            table=table,
                            source_count=count,
                            verified=verified,
                            source_hash=source_hash,
                            target_hash=target_hash,
                        )
                    report.tables.append(result)
                    continue
                located = self._app_source(table, sources)
                if located is None:
                    report.warnings.append(f"No source table found for app.{table}")
                    continue
                source_name, source, physical = located
                count = source.execute(f"SELECT COUNT(*) FROM {_q(physical)}").fetchone()[0]
                result = TableResult("app", table, source_name, count)
                if apply and target is not None:
                    count, copied, verified, source_hash, target_hash = _copy_rows(
                        source, physical, target, table, replace_existing=True
                    )
                    result.copied = copied
                    result.verified = verified
                    _record(
                        target,
                        plane="app",
                        source=source_name,
                        target=str(self.app_db),
                        table=table,
                        source_count=count,
                        verified=verified,
                        source_hash=source_hash,
                        target_hash=target_hash,
                    )
                report.tables.append(result)

            # Build the minimal lookup/ACL projection from the current agent
            # authority.  Full configuration remains in each agent bundle.
            global_conn = sources.get("global")
            if global_conn is not None and "agents" in _tables(global_conn):
                rows = global_conn.execute("SELECT * FROM agents").fetchall()
                result = TableResult("app", "agent_catalog", "global", len(rows))
                if apply and target is not None:
                    before = target.total_changes
                    for row in rows:
                        item = dict(row)
                        target.execute(
                            """INSERT INTO agent_catalog
                               (agent_id,name,icon,status,template_id,admin_users,member_users,
                                authorized_users,storage_ref,authority_revision,created_at,updated_at)
                               VALUES (?,?,?,?,?,?,?,?,?,1,?,?)
                               ON CONFLICT(agent_id) DO UPDATE SET
                                 name=excluded.name, icon=excluded.icon, status=excluded.status,
                                 template_id=excluded.template_id, admin_users=excluded.admin_users,
                                 member_users=excluded.member_users,
                                 authorized_users=excluded.authorized_users,
                                 storage_ref=excluded.storage_ref,
                                 authority_revision=agent_catalog.authority_revision+1,
                                 updated_at=excluded.updated_at""",
                            (
                                item.get("id"), item.get("name") or "", item.get("icon"),
                                item.get("status") or "active", item.get("template_id"),
                                item.get("admin_users") or "[]", item.get("member_users") or "[]",
                                item.get("authorized_users") or "[]",
                                f"agent_data/{item.get('id')}/{item.get('id')}.db",
                                item.get("created_at"), item.get("updated_at"),
                            ),
                        )
                    result.copied = target.total_changes - before
                    result.verified = target.execute("SELECT COUNT(*) FROM agent_catalog").fetchone()[0] >= len(rows)
                    _record(
                        target,
                        plane="app",
                        source="global",
                        target=str(self.app_db),
                        table="agent_catalog",
                        source_count=len(rows),
                        verified=result.verified,
                        source_hash="projection",
                        target_hash="projection" if result.verified else "",
                    )
                report.tables.append(result)

            if apply and target is not None:
                target.commit()
        finally:
            if target is not None:
                target.close()
            for source in sources.values():
                source.close()

        # User and agent files are audited/refreshed in separate passes.  They
        # are additive and never delete legacy rows.
        report.users = len(list(self.user_dir.glob("*/*.db"))) if self.user_dir.exists() else 0
        agent_paths = list((self.root / "data" / "agent_data").glob("*/*.db"))
        report.agents = len(agent_paths)
        if apply:
            app_results = [item for item in report.tables if item.plane == "app"]
            mark_plane_status(
                "app",
                "verified" if app_results and all(item.verified for item in app_results) else "failed",
                path=self.app_db,
                summary={"tables": len(app_results), "rows": sum(item.source_count for item in app_results)},
            )
            self._migrate_user_plane(report)
            asyncio.run(self._refresh_agent_bundles(report))
        return report

    def _discover_user_ids(self, source: sqlite3.Connection | None) -> set[str]:
        user_ids = {path.parent.name for path in self.user_dir.glob("*/*.db")}
        if source is None:
            return user_ids
        existing = _tables(source)
        for logical in tables_for_plane(StoragePlane.USER):
            physical = logical if logical in existing else f"_mig_{logical}"
            if physical not in existing:
                continue
            cols = set(_columns(source, physical))
            owner = "user_id" if "user_id" in cols else "owner_user_id" if "owner_user_id" in cols else None
            if owner:
                rows = source.execute(
                    f"SELECT DISTINCT {_q(owner)} FROM {_q(physical)} "
                    f"WHERE {_q(owner)} IS NOT NULL AND {_q(owner)} != ''"
                ).fetchall()
                user_ids.update(str(row[0]) for row in rows)
        return user_ids

    def _user_filter(
        self,
        source: sqlite3.Connection,
        physical: str,
        logical: str,
    ) -> tuple[str, tuple[Any, ...]] | None:
        cols = set(_columns(source, physical))
        if "user_id" in cols:
            return f"{_q('user_id')} = ?", ()
        if "owner_user_id" in cols:
            return f"{_q('owner_user_id')} = ?", ()

        existing = _tables(source)
        sessions = _source_table(source, "sessions")
        if "session_id" in cols and sessions:
            return (
                f"{_q('session_id')} IN (SELECT id FROM {_q(sessions)} WHERE user_id = ?)",
                (),
            )
        if logical == "memory_chunks":
            memories = _source_table(source, "memories")
            if memories:
                return (
                    f"memory_id IN (SELECT id FROM {_q(memories)} WHERE user_id = ?)",
                    (),
                )
        if logical == "doc_chunks":
            data_sources = _source_table(source, "data_sources")
            if data_sources:
                return (
                    f"data_source_id IN (SELECT id FROM {_q(data_sources)} WHERE user_id = ?)",
                    (),
                )
        if logical == "webhook_event_log":
            registrations = _source_table(source, "webhook_registrations")
            if registrations:
                return (
                    f"webhook_id IN (SELECT id FROM {_q(registrations)} WHERE user_id = ?)",
                    (),
                )
        return None

    def _migrate_user_plane(self, report: MigrationReport) -> None:
        local = _open_readonly(self.local_db) if self.local_db.exists() and self.local_db.stat().st_size else None
        global_conn = _open_readonly(self.global_db) if self.global_db.exists() and self.global_db.stat().st_size else None
        app_store = get_app_store(path=self.app_db, initialize=True)
        migration_conn = app_store._connect()
        all_verified = True
        users = self._discover_user_ids(local)
        report.users = len(users)
        try:
            for user_id in sorted(users):
                target_path = self.user_dir / user_id / f"{user_id}.db"
                target_path.parent.mkdir(parents=True, exist_ok=True)
                target = sqlite3.connect(target_path)
                target.row_factory = sqlite3.Row
                try:
                    target.execute("PRAGMA foreign_keys=ON")
                    target.execute("PRAGMA journal_mode=WAL")
                    target.executescript(render_plane("user", "sqlite"))
                    for logical in tables_for_plane(StoragePlane.USER):
                        located = None
                        for source_name, source in (("local", local), ("global", global_conn)):
                            if source is None:
                                continue
                            physical = _source_table(source, logical)
                            if physical:
                                located = (source_name, source, physical)
                                break
                        if located is None:
                            continue
                        source_name, source, physical = located
                        selected = self._user_filter(source, physical, logical)
                        if selected is None:
                            # A user-plane table without a derivable owner is a
                            # migration error, never an invitation to copy all rows.
                            all_verified = False
                            report.warnings.append(f"Cannot derive owner for user.{logical}")
                            continue
                        where, _ = selected
                        count, copied, verified, source_hash, target_hash = _copy_rows(
                            source, physical, target, logical, where=where, params=(user_id,)
                        )
                        if count == 0:
                            continue
                        all_verified = all_verified and verified
                        result = TableResult(
                            "user", logical, f"{source_name}:{user_id}", count, copied, verified
                        )
                        report.tables.append(result)
                        _record(
                            migration_conn,
                            plane="user",
                            source=f"{source_name}:{user_id}",
                            target=str(target_path),
                            table=logical,
                            source_count=count,
                            verified=verified,
                            source_hash=source_hash,
                            target_hash=target_hash,
                        )
                    target.commit()
                finally:
                    target.close()
            migration_conn.commit()
        finally:
            migration_conn.close()
            if local is not None:
                local.close()
            if global_conn is not None:
                global_conn.close()
        mark_plane_status(
            "user",
            "verified" if all_verified else "failed",
            path=self.app_db,
            summary={"users": len(users)},
        )

    async def _refresh_agent_bundles(self, report: MigrationReport) -> None:
        from app.db.agent_store import migrate_all_agent_stores

        outcome = await migrate_all_agent_stores()
        migrated = int(outcome.get("migrated") or 0)
        failed = int(outcome.get("failed") or 0)
        expected = int(outcome.get("expected_non_clone") or 0)
        if failed or migrated < expected:
            report.warnings.append(
                f"Agent refresh synced {migrated}/{expected} authority stores ({failed} failed)"
            )
        mark_plane_status(
            "agent",
            "verified" if not failed and migrated == expected else "failed",
            path=self.app_db,
            summary={"expected": expected, "migrated": migrated, "failed": failed},
        )


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    mode = parser.add_mutually_exclusive_group()
    mode.add_argument("--apply", action="store_true", help="write additive target data")
    mode.add_argument(
        "--activate-existing",
        action="store_true",
        help="verify and activate existing split stores when legacy sources are absent",
    )
    parser.add_argument("--project-root", type=Path, default=Path(__file__).resolve().parent.parent.parent)
    parser.add_argument("--app-db", type=Path)
    args = parser.parse_args(argv)
    logging.basicConfig(level=logging.INFO, format="%(levelname)s: %(message)s")
    migrator = StorageLayoutMigrator(args.project_root, app_db=args.app_db)
    if args.activate_existing:
        activated = migrator.activate_existing()
        print(json.dumps(activated, indent=2, sort_keys=True))
        return 0

    report = migrator.run(apply=args.apply)
    print(json.dumps(report.as_dict(), indent=2, sort_keys=True))
    return 0 if not report.warnings else 2


if __name__ == "__main__":
    raise SystemExit(main())

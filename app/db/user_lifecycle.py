"""Backend-portable user export/delete primitives.

The functions accept an already-scoped SQL connection so they work for local
SQLite, split per-user SQLite, and the PgPortableConnection used by Postgres.
Every statement is intentionally best-effort across older schemas, but the
account row is deleted last and only after the known data planes were visited.
"""

from __future__ import annotations

import logging
import base64
from datetime import datetime, timezone
from typing import Any

logger = logging.getLogger(__name__)

_EXPORT_SECRET_FIELDS = {
    "password_hash",
    "remember_token",
    "fire_token",
}


async def _delete_attachment_bytes(rows: list[Any]) -> int:
    from app.db.attachments import delete_file

    deleted = 0
    for row in rows:
        storage_path = str(row[0] or "")
        provider = str(row[1] or "local")
        if not storage_path or provider == "browser":
            continue
        try:
            if await delete_file(storage_path, storage_provider=provider):
                deleted += 1
        except Exception as exc:
            # Do not silently claim a complete erasure if object storage failed.
            raise RuntimeError(
                f"Could not delete attachment bytes from {provider}"
            ) from exc
    return deleted


def _existing_tables(connection: Any) -> set[str]:
    try:
        return {
            str(row[0])
            for row in connection.execute(
                "SELECT name FROM sqlite_master WHERE type='table'"
            ).fetchall()
        }
    except Exception:
        return set()


def _execute_delete(connection: Any, sql: str, params: tuple[Any, ...]) -> int:
    try:
        cursor = connection.execute(sql, params)
        return max(0, int(cursor.rowcount or 0))
    except Exception as exc:
        raise RuntimeError(f"User lifecycle deletion failed: {sql}") from exc


async def erase_user_data(
    connection: Any,
    user_id: str,
    *,
    include_account: bool = True,
) -> dict[str, int]:
    """Erase known user-owned data and return per-plane deletion counts."""
    counts: dict[str, int] = {}
    existing = _existing_tables(connection)

    try:
        attachment_rows = connection.execute(
            "SELECT storage_path,storage_provider FROM attachments WHERE user_id=?",
            (user_id,),
        ).fetchall()
    except Exception:
        attachment_rows = []
    counts["attachment_objects"] = await _delete_attachment_bytes(attachment_rows)

    # Children reached through session ownership.
    session_children = (
        "interactions",
        "session_manifests",
        "session_runs",
        "session_interrupts",
        "session_summary_segments",
        "session_summaries",
        "attachments",
        "pipeline_events",
        "run_contract_checks",
        "run_contract_state",
        "messages",
    )
    for table in session_children:
        if table not in existing:
            continue
        counts[table] = _execute_delete(
            connection,
            f"DELETE FROM {table} WHERE session_id IN "
            "(SELECT id FROM sessions WHERE user_id=?)",
            (user_id,),
        )

    # Children whose parent is another user-owned object.
    linked_deletes = (
        (
            "memory_chunks",
            "DELETE FROM memory_chunks WHERE memory_id IN "
            "(SELECT id FROM memories WHERE user_id=?)",
        ),
        (
            "doc_chunks",
            "DELETE FROM doc_chunks WHERE data_source_id IN "
            "(SELECT id FROM data_sources WHERE user_id=?)",
        ),
        (
            "agent_data_sources",
            "DELETE FROM agent_data_sources WHERE data_source_id IN "
            "(SELECT id FROM data_sources WHERE user_id=?)",
        ),
        (
            "webhook_event_log",
            "DELETE FROM webhook_event_log WHERE webhook_id IN "
            "(SELECT id FROM webhook_registrations WHERE user_id=?)",
        ),
        (
            "wallet_transactions",
            "DELETE FROM wallet_transactions WHERE wallet_id IN "
            "(SELECT id FROM wallets WHERE owner_type='user' AND owner_id=?)",
        ),
    )
    for table, sql in linked_deletes:
        if table not in existing:
            continue
        counts[table] = _execute_delete(connection, sql, (user_id,))

    direct_user_columns = (
        ("render_recordings", "user_id"),
        ("workspace_artifacts", "user_id"),
        ("diagnostics", "user_id"),
        ("memories", "user_id"),
        ("skills", "user_id"),
        ("skill_executions", "user_id"),
        ("skill_feedback", "user_id"),
        ("data_sources", "user_id"),
        ("webhook_registrations", "user_id"),
        ("session_notifications", "user_id"),
        ("browser_sessions", "user_id"),
        ("browser_sync_receipts", "user_id"),
        ("channel_identities", "user_id"),
        ("anonymous_guest_credentials", "user_id"),
        ("usage_events", "user_id"),
        ("subscriptions", "user_id"),
        ("trials", "user_id"),
        ("payments", "user_id"),
        ("billing_exemptions", "user_id"),
        ("user_tier_assignments", "user_id"),
        ("tenant_key_meta", "user_id"),
        ("user_profiles", "user_id"),
        ("genui", "user_id"),
        ("agent_automations", "owner_user_id"),
        ("agent_event_subscriptions", "owner_user_id"),
        ("automation_runs", "owner_user_id"),
        ("event_deliveries", "owner_user_id"),
        ("device_jobs", "owner_user_id"),
    )
    for table, column in direct_user_columns:
        if table not in existing:
            continue
        counts[table] = _execute_delete(
            connection,
            f"DELETE FROM {table} WHERE {column}=?",
            (user_id,),
        )

    # Entitlement audit evidence follows the installation's audit retention
    # lifecycle, not the account lifecycle. Remove direct identifiers while
    # retaining the authorization change record.
    if "entitlement_audit_events" in existing:
        counts["entitlement_audit_events_pseudonymized"] = _execute_delete(
            connection,
            "UPDATE entitlement_audit_events SET "
            "subject_user_id=CASE WHEN subject_user_id=? THEN NULL ELSE subject_user_id END, "
            "actor_user_id=CASE WHEN actor_user_id=? THEN NULL ELSE actor_user_id END "
            "WHERE subject_user_id=? OR actor_user_id=?",
            (user_id, user_id, user_id, user_id),
        )

    if "wallets" in existing:
        counts["wallets"] = _execute_delete(
            connection,
            "DELETE FROM wallets WHERE owner_type='user' AND owner_id=?",
            (user_id,),
        )
    if "sessions" in existing:
        counts["sessions"] = _execute_delete(
            connection, "DELETE FROM sessions WHERE user_id=?", (user_id,)
        )
    if include_account and "user_accounts" in existing:
        counts["user_accounts"] = _execute_delete(
            connection, "DELETE FROM user_accounts WHERE user_id=?", (user_id,)
        )

    # Vault cleanup — auth_elements rows for this user in vault_agent and vault_user.
    # vault_app rows (admin infra) are never user-scoped and should not be cleaned.
    for vault_schema in ("vault_agent", "vault_user"):
        try:
            n = _execute_delete(
                connection,
                f"DELETE FROM {vault_schema}.auth_elements WHERE user_id = ?",
                (user_id,),
            )
            counts[f"{vault_schema}_auth_elements"] = n
        except Exception:
            pass

    connection.commit()
    return counts


def _select_dicts(
    connection: Any,
    sql: str,
    params: tuple[Any, ...],
) -> list[dict]:
    try:
        rows = [
            {key: _json_safe(value) for key, value in dict(row).items()}
            for row in connection.execute(sql, params).fetchall()
        ]
    except Exception as exc:
        raise RuntimeError(f"User lifecycle export failed: {sql}") from exc
    for row in rows:
        for field in _EXPORT_SECRET_FIELDS:
            row.pop(field, None)
    return rows


def _json_safe(value: Any) -> Any:
    if isinstance(value, (bytes, bytearray, memoryview)):
        return {
            "__webagent_bytes__": True,
            "base64": base64.b64encode(bytes(value)).decode("ascii"),
        }
    if value is None or isinstance(value, (str, int, float, bool, list, dict)):
        return value
    if hasattr(value, "isoformat"):
        try:
            return value.isoformat()
        except Exception:
            pass
    return str(value)


async def export_user_data(connection: Any, user_id: str) -> dict:
    """Export the server-authority planes for one user, including attachment bytes."""
    existing = _existing_tables(connection)
    sessions = _select_dicts(
        connection, "SELECT * FROM sessions WHERE user_id=?", (user_id,)
    ) if "sessions" in existing else []
    tables: dict[str, list[dict]] = {"sessions": sessions}

    session_tables = (
        "interactions",
        "session_manifests",
        "session_runs",
        "session_interrupts",
        "session_summary_segments",
        "session_summaries",
        "attachments",
        "pipeline_events",
        "messages",
    )
    for table in session_tables:
        if table not in existing:
            continue
        tables[table] = _select_dicts(
            connection,
            f"SELECT * FROM {table} WHERE session_id IN "
            "(SELECT id FROM sessions WHERE user_id=?)",
            (user_id,),
        )

    direct_tables = (
        ("workspace_artifacts", "user_id"),
        ("memories", "user_id"),
        ("skills", "user_id"),
        ("skill_executions", "user_id"),
        ("skill_feedback", "user_id"),
        ("data_sources", "user_id"),
        ("webhook_registrations", "user_id"),
        ("browser_sessions", "user_id"),
        ("browser_sync_receipts", "user_id"),
        ("channel_identities", "user_id"),
        ("anonymous_guest_credentials", "user_id"),
        ("user_profiles", "user_id"),
        ("genui", "user_id"),
        ("agent_automations", "owner_user_id"),
        ("agent_event_subscriptions", "owner_user_id"),
        ("automation_runs", "owner_user_id"),
        ("event_deliveries", "owner_user_id"),
        ("device_jobs", "owner_user_id"),
        ("usage_events", "user_id"),
        ("subscriptions", "user_id"),
        ("trials", "user_id"),
        ("payments", "user_id"),
        ("billing_exemptions", "user_id"),
        ("user_tier_assignments", "user_id"),
    )
    for table, column in direct_tables:
        if table not in existing:
            continue
        tables[table] = _select_dicts(
            connection, f"SELECT * FROM {table} WHERE {column}=?", (user_id,)
        )

    if "entitlement_audit_events" in existing:
        tables["entitlement_audit_events"] = _select_dicts(
            connection,
            "SELECT * FROM entitlement_audit_events "
            "WHERE subject_user_id=? OR actor_user_id=?",
            (user_id, user_id),
        )

    if {"memory_chunks", "memories"} <= existing:
        tables["memory_chunks"] = _select_dicts(
            connection,
            "SELECT * FROM memory_chunks WHERE memory_id IN "
            "(SELECT id FROM memories WHERE user_id=?)",
            (user_id,),
        )
    if {"doc_chunks", "data_sources"} <= existing:
        tables["doc_chunks"] = _select_dicts(
            connection,
            "SELECT * FROM doc_chunks WHERE data_source_id IN "
            "(SELECT id FROM data_sources WHERE user_id=?)",
            (user_id,),
        )
    if {"agent_data_sources", "data_sources"} <= existing:
        tables["agent_data_sources"] = _select_dicts(
            connection,
            "SELECT * FROM agent_data_sources WHERE data_source_id IN "
            "(SELECT id FROM data_sources WHERE user_id=?)",
            (user_id,),
        )
    if {"webhook_event_log", "webhook_registrations"} <= existing:
        tables["webhook_event_log"] = _select_dicts(
            connection,
            "SELECT * FROM webhook_event_log WHERE webhook_id IN "
            "(SELECT id FROM webhook_registrations WHERE user_id=?)",
            (user_id,),
        )
    if "wallets" in existing:
        tables["wallets"] = _select_dicts(
            connection,
            "SELECT * FROM wallets WHERE owner_type='user' AND owner_id=?",
            (user_id,),
        )
    if {"wallet_transactions", "wallets"} <= existing:
        tables["wallet_transactions"] = _select_dicts(
            connection,
            "SELECT * FROM wallet_transactions WHERE wallet_id IN "
            "(SELECT id FROM wallets WHERE owner_type='user' AND owner_id=?)",
            (user_id,),
        )

    account_rows = _select_dicts(
        connection, "SELECT * FROM user_accounts WHERE user_id=?", (user_id,)
    ) if "user_accounts" in existing else []
    tables["user_accounts"] = account_rows

    from app.db.attachments import read_file

    attachment_blobs: list[dict] = []
    for attachment in tables.get("attachments", []):
        path = str(attachment.get("storage_path") or "")
        provider = str(attachment.get("storage_provider") or "local")
        blob = None
        if path and provider != "browser":
            try:
                blob = await read_file(path, storage_provider=provider)
            except Exception as exc:
                raise RuntimeError(
                    f"Could not export attachment bytes from {provider}"
                ) from exc
        attachment_blobs.append({
            "attachment_id": attachment.get("id"),
            "storage_provider": provider,
            "mime_type": attachment.get("mime_type"),
            "base64": base64.b64encode(blob).decode("ascii") if blob is not None else None,
            "browser_local": provider == "browser",
        })

    return {
        "format": "webagent-user-export",
        "version": 1,
        "exported_at": datetime.now(timezone.utc).isoformat(),
        "user_id": user_id,
        "server_authority": tables,
        "attachment_blobs": attachment_blobs,
        "browser_authority_included": False,
    }

"""Opaque, authenticated grants for workspace-owned content."""

from __future__ import annotations

import base64
import hashlib
import json
import re
import time
import uuid
from pathlib import Path
from typing import Optional

from cryptography.fernet import Fernet, InvalidToken
from fastapi import HTTPException, Request

CONTENT_TTL_SECONDS = 600
URL_PREFIX = "/api/v1/content"
_CONTENT_URL_RE = re.compile(r"/api/v1/content/([A-Za-z0-9_-]+={0,2})")


def _fernet() -> Fernet:
    # Domain separation prevents a content token from being interpreted as a
    # login token even though both keys converge across sibling app workers.
    from app.auth.jwt import get_secret
    digest = hashlib.sha256(
        b"webagent-content-grant-v1\x00" + get_secret().encode("utf-8")
    ).digest()
    return Fernet(base64.urlsafe_b64encode(digest))


def issue_content_grant(
    *, user_id: str, artifact_id: str, source_type: str = "attachment",
    ttl_seconds: int = CONTENT_TTL_SECONDS,
) -> str:
    now = int(time.time())
    payload = json.dumps({
        "v": 1,
        "purpose": "workspace-content",
        "user_id": user_id,
        "artifact_id": artifact_id,
        "source_type": source_type,
        "exp": now + max(1, min(int(ttl_seconds), CONTENT_TTL_SECONDS)),
    }, separators=(",", ":")).encode("utf-8")
    token = _fernet().encrypt(payload).decode("ascii")
    return f"{URL_PREFIX}/{token}"


def issue_path_grant(
    *, user_id: str, storage_path: str, original_name: str = "",
    mime_type: str = "application/octet-stream", storage_provider: str = "local",
    ttl_seconds: int = CONTENT_TTL_SECONDS,
) -> str:
    now = int(time.time())
    payload = json.dumps({
        "v": 1,
        "purpose": "workspace-content",
        "user_id": user_id,
        "source_type": "path",
        "storage_path": storage_path,
        "storage_provider": storage_provider,
        "original_name": original_name,
        "mime_type": mime_type,
        "exp": now + max(1, min(int(ttl_seconds), CONTENT_TTL_SECONDS)),
    }, separators=(",", ":")).encode("utf-8")
    return f"{URL_PREFIX}/{_fernet().encrypt(payload).decode('ascii')}"


def _ensure_artifact_table(db) -> None:
    factory = getattr(db, "_get_conn", None)
    if not callable(factory):
        return
    conn = factory()
    try:
        conn.executescript("""
            CREATE TABLE IF NOT EXISTS workspace_artifacts (
                id TEXT PRIMARY KEY, user_id TEXT NOT NULL, storage_path TEXT NOT NULL,
                storage_provider TEXT NOT NULL DEFAULT 'local', original_name TEXT NOT NULL DEFAULT '',
                mime_type TEXT NOT NULL DEFAULT 'application/octet-stream', size_bytes INTEGER NOT NULL DEFAULT 0,
                source_type TEXT NOT NULL DEFAULT 'file', source_id TEXT, created_by TEXT,
                created_at TEXT NOT NULL DEFAULT (datetime('now')), deleted_at TEXT
            );
            CREATE INDEX IF NOT EXISTS idx_workspace_artifacts_user
                ON workspace_artifacts(user_id, created_at);
            CREATE INDEX IF NOT EXISTS idx_workspace_artifacts_source
                ON workspace_artifacts(source_type, source_id);
        """)
        conn.commit()
    finally:
        conn.close()


def register_path_artifact(
    *, user_id: str, storage_path: str, original_name: str = "",
    mime_type: str = "application/octet-stream", storage_provider: str = "local",
    created_by: Optional[str] = None,
) -> str:
    """Idempotently register a workspace path and return its opaque stable id."""
    from app.db import get_data_db

    artifact_id = str(uuid.uuid5(
        uuid.NAMESPACE_URL, f"webagent:{user_id}:{storage_provider}:{storage_path}"
    ))
    db = get_data_db(user_id)
    _ensure_artifact_table(db)
    table = db.get_raw_client().table("workspace_artifacts")
    existing = table.select("id").eq("id", artifact_id).limit(1).execute()
    if not (getattr(existing, "data", None) or []):
        table.insert({
            "id": artifact_id, "user_id": user_id, "storage_path": storage_path,
            "storage_provider": storage_provider, "original_name": original_name,
            "mime_type": mime_type, "size_bytes": 0, "source_type": "file",
            "source_id": storage_path, "created_by": created_by or user_id,
        }).execute()
    return artifact_id


def artifact_url_for_path(**kwargs) -> str:
    """Register a path when possible, with an encrypted-path upgrade fallback."""
    try:
        artifact_id = register_path_artifact(**kwargs)
        return issue_content_grant(
            user_id=str(kwargs["user_id"]), artifact_id=artifact_id, source_type="artifact"
        )
    except Exception:
        # Upgrade compatibility: content remains protected even if an older
        # remote schema has not created the artifact registry yet.
        fallback = {key: value for key, value in kwargs.items() if key != "created_by"}
        return issue_path_grant(**fallback)


def decode_content_grant(token: str) -> dict:
    try:
        raw = _fernet().decrypt(token.encode("ascii"), ttl=CONTENT_TTL_SECONDS + 5)
        payload = json.loads(raw)
    except (InvalidToken, ValueError, TypeError, json.JSONDecodeError, UnicodeError):
        raise HTTPException(status_code=404, detail="Content not found")
    if payload.get("purpose") != "workspace-content" or int(payload.get("exp") or 0) < int(time.time()):
        raise HTTPException(status_code=404, detail="Content not found")
    return payload


def refresh_content_urls_in_text(value: object, user_id: str) -> object:
    """Refresh persisted grants during serialization without exposing their path."""
    if not isinstance(value, str) or URL_PREFIX not in value:
        return value

    def replace(match: re.Match) -> str:
        try:
            payload = json.loads(_fernet().decrypt(match.group(1).encode("ascii")))
            if payload.get("purpose") != "workspace-content" or payload.get("user_id") != user_id:
                return match.group(0)
            payload["exp"] = int(time.time()) + CONTENT_TTL_SECONDS
            token = _fernet().encrypt(
                json.dumps(payload, separators=(",", ":")).encode("utf-8")
            ).decode("ascii")
            return f"{URL_PREFIX}/{token}"
        except Exception:
            return match.group(0)

    return _CONTENT_URL_RE.sub(replace, value)


async def resolve_content(request: Request, token: str) -> dict:
    from app.auth.workspace import require_user_workspace
    from app.db import get_data_db

    grant = decode_content_grant(token)
    user_id = str(grant.get("user_id") or "")
    context = await require_user_workspace(request, user_id)
    context.require("user:read")
    db = get_data_db(user_id)
    source_type = str(grant.get("source_type") or "attachment")
    artifact_id = str(grant.get("artifact_id") or "")
    if source_type == "path":
        row = {
            "user_id": user_id,
            "storage_path": str(grant.get("storage_path") or ""),
            "storage_provider": str(grant.get("storage_provider") or "local"),
            "original_name": str(grant.get("original_name") or "download"),
            "mime_type": str(grant.get("mime_type") or "application/octet-stream"),
        }
    elif source_type == "attachment":
        row = await db.get_attachment(artifact_id)
    else:
        result = (
            db.get_raw_client().table("workspace_artifacts").select("*")
            .eq("id", artifact_id).eq("user_id", user_id).limit(1).execute()
        )
        rows = list(getattr(result, "data", None) or [])
        row = dict(rows[0]) if rows else None
    if not row or row.get("user_id") != user_id or row.get("deleted_at"):
        raise HTTPException(status_code=404, detail="Content not found")
    return dict(row)


def validate_local_storage_path(user_id: str, storage_path: str) -> None:
    """Reject provider records that point outside their declared user home."""
    if storage_path.startswith("indexeddb://"):
        return
    from app import user_workspace as ws
    root = ws.base_dir().resolve()
    candidate = (root / storage_path).resolve()
    expected = ws.user_home(user_id).resolve()
    try:
        candidate.relative_to(expected)
    except ValueError as exc:
        raise HTTPException(status_code=404, detail="Content not found") from exc

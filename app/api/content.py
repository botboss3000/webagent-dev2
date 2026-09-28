"""Authenticated streaming for user workspace artifacts."""

from __future__ import annotations

import re

from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import Response
from pydantic import BaseModel

from app.content_access import issue_content_grant, resolve_content, validate_local_storage_path
from app.db.attachments import read_file

router = APIRouter(prefix="/api/v1/content", tags=["content"])

_INLINE_PREFIXES = ("image/", "audio/", "video/")
_INLINE_EXACT = {"application/pdf", "text/plain"}


class ContentGrantRequest(BaseModel):
    artifact_id: str
    source_type: str = "attachment"
    user_id: str | None = None


@router.post("/grants")
async def create_content_grant(request: Request, body: ContentGrantRequest):
    from app.auth.workspace import require_user_workspace
    from app.db import get_data_db

    context = await require_user_workspace(request, body.user_id)
    context.require("user:read")
    if body.source_type not in {"attachment", "artifact"}:
        raise HTTPException(status_code=400, detail="Unsupported artifact type")
    db = get_data_db(context.subject_id)
    if body.source_type == "attachment":
        row = await db.get_attachment(body.artifact_id)
    else:
        result = (
            db.get_raw_client().table("workspace_artifacts").select("id,user_id,deleted_at")
            .eq("id", body.artifact_id).eq("user_id", context.subject_id).limit(1).execute()
        )
        rows = list(getattr(result, "data", None) or [])
        row = rows[0] if rows else None
    if not row or row.get("user_id") != context.subject_id or row.get("deleted_at"):
        raise HTTPException(status_code=404, detail="Content not found")
    return {
        "artifact_id": body.artifact_id,
        "url": issue_content_grant(
            user_id=context.subject_id, artifact_id=body.artifact_id,
            source_type=body.source_type,
        ),
        "expires_in": 600,
    }


@router.get("/{token}")
async def get_workspace_content(request: Request, token: str):
    row = await resolve_content(request, token)
    provider = str(row.get("storage_provider") or "local")
    storage_path = str(row.get("storage_path") or "")
    if provider == "browser" or storage_path.startswith("indexeddb://"):
        raise HTTPException(status_code=404, detail="Content is stored on the user's device")
    contents = None
    if provider == "local":
        user_id = str(row.get("user_id") or "")
        validate_local_storage_path(user_id, storage_path)
        # Files saved through the User Files ability live in the encrypted
        # workspace root, while ordinary uploads use the attachment store's
        # configured upload root. Prefer an existing validated workspace file
        # and retain the attachment-store fallback for normal uploads.
        from app import user_workspace as ws
        from app.encryption.workspace_files import read_encrypted_file
        workspace_path = (ws.base_dir() / storage_path).resolve()
        if workspace_path.is_file():
            try:
                contents = await read_encrypted_file(user_id, workspace_path)
            except (OSError, ValueError):
                contents = None
    if contents is None:
        contents = await read_file(storage_path, storage_provider=provider)
    if contents is None:
        raise HTTPException(status_code=404, detail="Content not found")
    mime = str(row.get("mime_type") or "application/octet-stream").lower().strip()
    if not re.fullmatch(r"[a-z0-9!#$&^_.+-]+/[a-z0-9!#$&^_.+-]+", mime):
        mime = "application/octet-stream"
    inline = mime in _INLINE_EXACT or mime.startswith(_INLINE_PREFIXES)
    filename = str(row.get("original_name") or "download").replace("\\", "/").split("/")[-1]
    filename = re.sub(r"[\x00-\x1f\x7f\"]", "", filename).strip() or "download"
    return Response(
        content=contents,
        media_type=mime,
        headers={
            "Cache-Control": "private, no-store",
            "X-Content-Type-Options": "nosniff",
            "Content-Disposition": f'{"inline" if inline else "attachment"}; filename="{filename}"',
        },
    )

"""
Tool for reading attachment content (images, text files, PDFs).

Registered dynamically so the agent can call read_attachment(attachment_id)
to inspect files the user uploaded.

File bytes are fetched via app/db/attachments/ (local filesystem or remote storage).
"""

import json
import logging
from typing import Optional

from app.db import get_db
from app.db.attachments import read_file

logger = logging.getLogger(__name__)


TOOL_DEFINITION = {
    "name": "read_attachment",
    "description": (
        "Read the contents of a file the user attached to the conversation. "
        "For text files, returns the text content. For images, returns a "
        "description (via OCR placeholder). For audio, returns metadata about the recording. "
        "Returns the attachment info including original filename, type, size, and available content."
    ),
    "parameters": {
        "type": "object",
        "properties": {
            "attachment_id": {
                "type": "string",
                "description": "The attachment ID (e.g., from the USER ATTACHMENTS section in the system prompt)."
            }
        },
        "required": ["attachment_id"]
    },
}


async def read_attachment(attachment_id: str) -> str:
    """
    Read content from an uploaded attachment.

    Args:
        attachment_id: The UUID of the attachment to read.

    Returns:
        A JSON string with the file's content or metadata description.
    """
    db = get_db()
    att = await db.get_attachment(attachment_id)
    if not att:
        return json.dumps({
            "status": "error",
            "message": f"Attachment '{attachment_id}' not found.",
        })

    mime = att["mime_type"]
    storage_path = att["storage_path"]
    storage_provider = att.get("storage_provider") or "local"
    size_str = _format_size(att["size_bytes"])

    # Browser backend keeps bytes in the user's IndexedDB; the server has nothing to read.
    if storage_provider == "browser":
        return json.dumps({
            "status": "error",
            "message": (
                f"Attachment '{att['original_name']}' is stored in the user's browser "
                "(IndexedDB backend). Server-side tools cannot read its bytes."
            ),
        })

    # Fetch bytes via the row's backend (not just the active one).
    file_bytes = await read_file(storage_path, storage_provider=storage_provider)
    if file_bytes is None:
        return json.dumps({
            "status": "error",
            "message": f"File not found on storage ({storage_provider}): {storage_path}",
        })

    from app.content_access import issue_content_grant
    public_url = issue_content_grant(
        user_id=str(att.get("user_id") or ""), artifact_id=attachment_id
    )

    # ── Text files ──
    if mime in ("text/plain", "text/markdown", "text/csv", "text/html", "application/json"):
        try:
            text = file_bytes.decode("utf-8", errors="replace")
            max_chars = 10000
            if len(text) > max_chars:
                text = text[:max_chars] + f"\n\n... [truncated at {max_chars} chars]"
            return json.dumps({
                "status": "ok",
                "attachment_id": attachment_id,
                "original_name": att["original_name"],
                "mime_type": mime,
                "size": size_str,
                "content": text,
            }, indent=2)
        except Exception as e:
            return json.dumps({
                "status": "error",
                "message": f"Failed to decode text file: {e}",
            })

    # ── Images ──
    # The chat pipeline inlines images attached to the current user message as
    # vision content parts (see app/agent/prompts.build_user_message_content),
    # so the model can already see them directly. This tool is now mainly a
    # metadata lookup / re-inspection hint.
    elif mime.startswith("image/"):
        return json.dumps({
            "status": "ok",
            "attachment_id": attachment_id,
            "original_name": att["original_name"],
            "mime_type": mime,
            "size": size_str,
            "content": (
                f"[Image file: {att['original_name']} ({mime}, {size_str})]\n"
                f"The image is at: {public_url}\n"
                "This tool returns metadata only — it does NOT let you see the image. "
                "If your model can see images, an image attached to the latest message "
                "is already provided to you as a vision part. If your model cannot see "
                "images, look for a folded-in '[Attached image — ...]: <description>' "
                "in the user message, or call process_image (Image Vision ability) to "
                "ask a vision model about it. Do NOT guess the image's contents from "
                "this metadata."
            ),
        }, indent=2)

    # ── Audio ──
    elif mime.startswith("audio/"):
        meta = {}
        try:
            meta = json.loads(att.get("metadata", "{}"))
        except json.JSONDecodeError:
            pass
        duration = meta.get("duration_seconds", "unknown")
        return json.dumps({
            "status": "ok",
            "attachment_id": attachment_id,
            "original_name": att["original_name"],
            "mime_type": mime,
            "size": size_str,
            "duration_seconds": duration,
            "content": (
                f"[Audio file: {att['original_name']} ({mime}, {size_str})]\n"
                f"Playable at: {public_url}\n"
                "Transcription not yet available. Summarize based on filename."
            ),
        }, indent=2)

    # ── PDF / others ──
    elif mime == "application/pdf":
        return json.dumps({
            "status": "ok",
            "attachment_id": attachment_id,
            "original_name": att["original_name"],
            "mime_type": mime,
            "size": size_str,
            "content": (
                f"[PDF file: {att['original_name']} ({size_str})]\n"
                f"Downloadable at: {public_url}\n"
                "PDF text extraction not yet available."
            ),
        }, indent=2)

    # ── Fallback ──
    else:
        return json.dumps({
            "status": "ok",
            "attachment_id": attachment_id,
            "original_name": att["original_name"],
            "mime_type": mime,
            "size": size_str,
            "content": f"[File: {att['original_name']} ({mime}, {size_str})]",
        }, indent=2)


def _format_size(bytes_val: int) -> str:
    if bytes_val < 1024:
        return f"{bytes_val}B"
    elif bytes_val < 1024 * 1024:
        return f"{bytes_val / 1024:.1f}KB"
    else:
        return f"{bytes_val / (1024 * 1024):.1f}MB"

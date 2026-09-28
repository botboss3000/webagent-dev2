"""File-bytes facade for attachments (images, voice, docs).

Delegates to the configured backend in `app/attachments_store/`. The active
backend can be one of: local, browser (IndexedDB), s3, gcs.

Usage:
    from app.db.attachments import store_file, read_file, delete_file

    result = await store_file(user_id, session_id, file_bytes, filename, mime_type)
    # → { storage_path, public_url, storage_provider }

    contents = await read_file(storage_path, storage_provider="s3")
    # → bytes | None

    await delete_file(storage_path, storage_provider="s3")
"""

from .file_store import (
    active_backend_name,
    delete_file,
    public_url_for,
    read_file,
    store_file,
)

__all__ = [
    "active_backend_name",
    "delete_file",
    "public_url_for",
    "read_file",
    "store_file",
]

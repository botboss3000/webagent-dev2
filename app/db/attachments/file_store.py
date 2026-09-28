"""
Bytes-persistence dispatcher for attachment uploads.

Thin facade over `app/attachments_store/`. The active backend handles new
stores; read/delete prefer a per-row `storage_provider` so files written
under a previous backend remain accessible after an admin switch.
"""

import logging
from typing import Optional

from app.attachments_store import (
    get_attachment_store,
    get_store_for_provider,
    get_mode as get_active_backend,
)

logger = logging.getLogger(__name__)


async def store_file(
    user_id: str,
    session_id: str,
    file_bytes: bytes,
    filename: str,
    mime_type: str,
) -> dict:
    """
    Store a file's bytes using the active backend.

    Returns dict with keys: storage_path, public_url, storage_provider.
    """
    store = get_attachment_store()
    result = await store.store(
        user_id=user_id,
        file_bytes=file_bytes,
        filename=filename,
        mime_type=mime_type,
    )
    result["storage_provider"] = store.name
    return result


async def read_file(storage_path: str, storage_provider: Optional[str] = None) -> Optional[bytes]:
    """
    Read a stored file's bytes. If `storage_provider` is given it overrides
    the active backend — needed for files written before a backend switch.
    """
    if storage_provider:
        store = get_store_for_provider(storage_provider)
    else:
        store = get_attachment_store()
    return await store.read(storage_path)


async def delete_file(storage_path: str, storage_provider: Optional[str] = None) -> bool:
    """Delete a stored file. Honors per-row provider when given."""
    if storage_provider:
        store = get_store_for_provider(storage_provider)
    else:
        store = get_attachment_store()
    return await store.delete(storage_path)


def public_url_for(storage_path: str, storage_provider: Optional[str] = None) -> str:
    """Build the URL the frontend uses to display/download a file."""
    if storage_provider:
        store = get_store_for_provider(storage_provider)
    else:
        store = get_attachment_store()
    return store.public_url(storage_path)


def active_backend_name() -> str:
    return get_active_backend()

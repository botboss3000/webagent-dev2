"""Google Cloud Storage attachment backend.

Configuration:
  - GCS_BUCKET                   (required)
  - GCS_PREFIX                   (optional; keys: `{prefix}/{user_id}/...`)
  - GOOGLE_APPLICATION_CREDENTIALS  path to a service-account JSON, OR run
                                  in an environment with default ADC creds

URLs are signed V4 GETs valid for `GCS_URL_TTL` seconds (default 7 days).
"""

import datetime
import logging
import os
import uuid
from typing import Optional

from app.attachments_store.interface import AttachmentStore

logger = logging.getLogger(__name__)


def _make_object_name(filename: str) -> str:
    ext = ""
    if "." in filename:
        ext = f".{filename.rsplit('.', 1)[1].lower()}"
    return f"{uuid.uuid4().hex}{ext}"


class GcsAttachmentStore(AttachmentStore):
    name = "gcs"

    def __init__(
        self,
        bucket: Optional[str] = None,
        prefix: Optional[str] = None,
        url_ttl_seconds: int = 60 * 60 * 24 * 7,
    ):
        self._bucket_name = bucket or os.environ.get("GCS_BUCKET")
        self._prefix = (prefix or os.environ.get("GCS_PREFIX", "")).strip("/")
        self._ttl = url_ttl_seconds
        self._client = None
        self._bucket = None

    def _key(self, user_id: str, object_name: str) -> str:
        parts = [p for p in (self._prefix, user_id, object_name) if p]
        return "/".join(parts)

    def _get_bucket(self):
        if self._bucket is not None:
            return self._bucket
        try:
            from google.cloud import storage  # type: ignore
        except ImportError as e:
            raise RuntimeError(
                "google-cloud-storage is not installed. Add it to requirements.txt to use GCS."
            ) from e
        if not self._bucket_name:
            raise RuntimeError("GCS_BUCKET is not configured.")
        self._client = storage.Client()
        self._bucket = self._client.bucket(self._bucket_name)
        return self._bucket

    async def store(self, user_id: str, file_bytes: bytes, filename: str, mime_type: str) -> dict:
        bucket = self._get_bucket()
        key = self._key(user_id, _make_object_name(filename))
        blob = bucket.blob(key)
        try:
            blob.upload_from_string(file_bytes, content_type=mime_type)
        except Exception as e:
            logger.error("GCS upload failed: %s", e)
            raise IOError(f"GCS upload failed: {e}") from e
        return {"storage_path": key, "public_url": self.public_url(key)}

    async def read(self, storage_path: str) -> Optional[bytes]:
        bucket = self._get_bucket()
        blob = bucket.blob(storage_path)
        try:
            return blob.download_as_bytes()
        except Exception as e:
            logger.warning("GCS download failed for %s: %s", storage_path, e)
            return None

    async def delete(self, storage_path: str) -> bool:
        bucket = self._get_bucket()
        blob = bucket.blob(storage_path)
        try:
            blob.delete()
            return True
        except Exception as e:
            logger.warning("GCS delete failed for %s: %s", storage_path, e)
            return False

    def public_url(self, storage_path: str) -> str:
        try:
            bucket = self._get_bucket()
            blob = bucket.blob(storage_path)
            return blob.generate_signed_url(
                version="v4",
                expiration=datetime.timedelta(seconds=self._ttl),
                method="GET",
            )
        except Exception as e:
            logger.warning("GCS sign failed for %s: %s", storage_path, e)
            return ""

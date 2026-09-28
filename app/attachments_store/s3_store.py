"""AWS S3 attachment backend.

Configuration (env or saved provider config):
  - S3_BUCKET            (required)
  - AWS_REGION           (optional; defaults to boto3 default)
  - S3_PREFIX            (optional; keys are stored under `{prefix}/{user_id}/...`)
  - AWS_ACCESS_KEY_ID    (optional if running with IAM roles)
  - AWS_SECRET_ACCESS_KEY
  - S3_ENDPOINT_URL      (optional; for S3-compatible providers like MinIO, R2)

URLs returned are pre-signed GETs valid for `S3_URL_TTL` seconds (default
7 days). Bytes are *not* served via /uploads — the URL points at S3.
"""

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


class S3AttachmentStore(AttachmentStore):
    name = "s3"

    def __init__(
        self,
        bucket: Optional[str] = None,
        region: Optional[str] = None,
        prefix: Optional[str] = None,
        endpoint_url: Optional[str] = None,
        url_ttl_seconds: int = 60 * 60 * 24 * 7,
    ):
        self._bucket = bucket or os.environ.get("S3_BUCKET")
        self._region = region or os.environ.get("AWS_REGION")
        self._prefix = (prefix or os.environ.get("S3_PREFIX", "")).strip("/")
        self._endpoint = endpoint_url or os.environ.get("S3_ENDPOINT_URL")
        self._ttl = url_ttl_seconds
        self._client = None

    def _key(self, user_id: str, object_name: str) -> str:
        parts = [p for p in (self._prefix, user_id, object_name) if p]
        return "/".join(parts)

    def _get_client(self):
        if self._client is not None:
            return self._client
        try:
            import boto3  # type: ignore
        except ImportError as e:
            raise RuntimeError(
                "boto3 is not installed. Add `boto3` to requirements.txt to use the S3 backend."
            ) from e
        if not self._bucket:
            raise RuntimeError("S3_BUCKET is not configured.")
        kwargs = {}
        if self._region:
            kwargs["region_name"] = self._region
        if self._endpoint:
            kwargs["endpoint_url"] = self._endpoint
        self._client = boto3.client("s3", **kwargs)
        return self._client

    async def store(self, user_id: str, file_bytes: bytes, filename: str, mime_type: str) -> dict:
        client = self._get_client()
        key = self._key(user_id, _make_object_name(filename))
        try:
            client.put_object(
                Bucket=self._bucket,
                Key=key,
                Body=file_bytes,
                ContentType=mime_type,
            )
        except Exception as e:
            logger.error("S3 put_object failed: %s", e)
            raise IOError(f"S3 upload failed: {e}") from e
        url = self.public_url(key)
        return {"storage_path": key, "public_url": url}

    async def read(self, storage_path: str) -> Optional[bytes]:
        client = self._get_client()
        try:
            obj = client.get_object(Bucket=self._bucket, Key=storage_path)
            return obj["Body"].read()
        except Exception as e:
            logger.warning("S3 get_object failed for %s: %s", storage_path, e)
            return None

    async def delete(self, storage_path: str) -> bool:
        client = self._get_client()
        try:
            client.delete_object(Bucket=self._bucket, Key=storage_path)
            return True
        except Exception as e:
            logger.warning("S3 delete_object failed for %s: %s", storage_path, e)
            return False

    def public_url(self, storage_path: str) -> str:
        try:
            client = self._get_client()
            return client.generate_presigned_url(
                "get_object",
                Params={"Bucket": self._bucket, "Key": storage_path},
                ExpiresIn=self._ttl,
            )
        except Exception as e:
            logger.warning("S3 presign failed for %s: %s", storage_path, e)
            return ""

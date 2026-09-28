"""
AWS Secrets Manager backend.

Requires `boto3`. Region defaults to AWS_REGION env var.
"""

import os
from typing import List, Optional

from app.secrets.interface import SecretsBackend

FEATURE = {
    "id": "aws_secrets_manager",
    "display_name": "AWS Secrets Manager",
    "category": "secrets",
    "status": "experimental",
    "summary": "Store secrets in AWS Secrets Manager.",
    "requires": ["boto3", "AWS credentials + region"],
}


class AWSSecretsManager(SecretsBackend):
    name = "aws_secrets_manager"

    def __init__(self, region: Optional[str] = None):
        # Resolution order: explicit arg → saved UI config / token → env var.
        from app.secrets.provider_config import get_provider_config, get_provider_token
        cfg = get_provider_config(self.name)
        self._region = (
            region
            or cfg.get("region")
            or os.environ.get("AWS_REGION")
            or os.environ.get("AWS_DEFAULT_REGION")
        )
        # Explicit IAM key pair is OPTIONAL — when blank, boto3's default
        # credential chain (instance/ECS/Lambda role, ~/.aws, env) is used.
        self._access_key_id = cfg.get("access_key_id") or os.environ.get("AWS_ACCESS_KEY_ID")
        self._secret_access_key = get_provider_token(self.name) or os.environ.get("AWS_SECRET_ACCESS_KEY")
        try:
            import boto3  # noqa: F401
        except ImportError as e:
            raise RuntimeError(
                "AWSSecretsManager requires `boto3`. Install: pip install boto3"
            ) from e

    def _client(self):
        import boto3
        kwargs = {}
        if self._region:
            kwargs["region_name"] = self._region
        if self._access_key_id and self._secret_access_key:
            kwargs["aws_access_key_id"] = self._access_key_id
            kwargs["aws_secret_access_key"] = self._secret_access_key
        return boto3.client("secretsmanager", **kwargs)

    async def get(self, key: str) -> Optional[str]:
        client = self._client()
        try:
            resp = client.get_secret_value(SecretId=key)
            return resp.get("SecretString")
        except client.exceptions.ResourceNotFoundException:
            return None
        except Exception:
            return None

    async def set(self, key: str, value: str) -> None:
        client = self._client()
        try:
            client.create_secret(Name=key, SecretString=value)
        except client.exceptions.ResourceExistsException:
            client.put_secret_value(SecretId=key, SecretString=value)

    async def delete(self, key: str) -> bool:
        client = self._client()
        try:
            client.delete_secret(SecretId=key, ForceDeleteWithoutRecovery=True)
            return True
        except Exception:
            return False

    async def list_keys(self, prefix: str = "") -> List[str]:
        client = self._client()
        keys = []
        paginator = client.get_paginator("list_secrets")
        for page in paginator.paginate():
            for s in page.get("SecretList", []):
                name = s.get("Name", "")
                if name.startswith(prefix):
                    keys.append(name)
        return sorted(keys)

    async def test_connection(self) -> dict:
        # Real probe: a minimal authenticated list call in the region.
        try:
            client = self._client()
            client.list_secrets(MaxResults=1)
        except Exception as e:
            return {"ok": False, "message": f"Could not reach AWS Secrets Manager ({self._region or 'default region'}): {type(e).__name__}: {e}"}
        return {"ok": True, "message": f"AWS Secrets Manager reachable in region {self._region or '(default)'}."}

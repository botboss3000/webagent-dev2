"""
Google Cloud Secret Manager backend.

Requires `google-cloud-secret-manager`. Project ID must be configured via env
(`GCP_PROJECT` / `GOOGLE_CLOUD_PROJECT`) — bootstrap chicken-and-egg avoided.
"""

import os
from typing import List, Optional

from app.secrets.interface import SecretsBackend

FEATURE = {
    "id": "gcp_secret_manager",
    "display_name": "GCP Secret Manager",
    "category": "secrets",
    "status": "experimental",
    "summary": "Store secrets in Google Cloud Secret Manager.",
    "requires": ["google-cloud-secret-manager", "GCP_PROJECT env var"],
}


class GCPSecretManager(SecretsBackend):
    name = "gcp_secret_manager"

    def __init__(self, project_id: Optional[str] = None):
        # Resolution order: explicit arg → saved UI config / token → env var.
        from app.secrets.provider_config import get_provider_config, get_provider_token
        cfg = get_provider_config(self.name)
        self._project_id = (
            project_id
            or cfg.get("project")
            or os.environ.get("GCP_PROJECT")
            or os.environ.get("GOOGLE_CLOUD_PROJECT")
        )
        # The "token" for GCP is a service-account key JSON (the whole file's
        # contents). Optional — when blank, Application Default Credentials are
        # used (a GOOGLE_APPLICATION_CREDENTIALS file, workload identity, etc.).
        self._sa_json = get_provider_token(self.name)
        if not self._project_id:
            raise RuntimeError(
                "GCPSecretManager requires GCP_PROJECT env var or explicit project_id."
            )
        try:
            from google.cloud import secretmanager  # noqa: F401
        except ImportError as e:
            raise RuntimeError(
                "GCPSecretManager requires `google-cloud-secret-manager`. "
                "Install: pip install google-cloud-secret-manager"
            ) from e

    def _client(self):
        from google.cloud import secretmanager
        if self._sa_json:
            import json as _json
            from google.oauth2 import service_account
            info = _json.loads(self._sa_json)
            creds = service_account.Credentials.from_service_account_info(info)
            return secretmanager.SecretManagerServiceClient(credentials=creds)
        return secretmanager.SecretManagerServiceClient()

    def _secret_name(self, key: str) -> str:
        # Secret IDs allow only [A-Za-z0-9_-], length 1-255.
        safe = key.replace(".", "_").replace("/", "_")
        return f"projects/{self._project_id}/secrets/{safe}"

    async def get(self, key: str) -> Optional[str]:
        client = self._client()
        try:
            resp = client.access_secret_version(
                request={"name": f"{self._secret_name(key)}/versions/latest"}
            )
            return resp.payload.data.decode("utf-8")
        except Exception:
            return None

    async def set(self, key: str, value: str) -> None:
        client = self._client()
        secret_path = self._secret_name(key)
        parent = f"projects/{self._project_id}"
        # Ensure secret resource exists.
        try:
            client.get_secret(request={"name": secret_path})
        except Exception:
            safe = secret_path.rsplit("/", 1)[-1]
            client.create_secret(
                request={
                    "parent": parent,
                    "secret_id": safe,
                    "secret": {"replication": {"automatic": {}}},
                }
            )
        client.add_secret_version(
            request={"parent": secret_path, "payload": {"data": value.encode("utf-8")}}
        )

    async def delete(self, key: str) -> bool:
        client = self._client()
        try:
            client.delete_secret(request={"name": self._secret_name(key)})
            return True
        except Exception:
            return False

    async def list_keys(self, prefix: str = "") -> List[str]:
        client = self._client()
        parent = f"projects/{self._project_id}"
        keys = []
        for sec in client.list_secrets(request={"parent": parent}):
            name = sec.name.rsplit("/", 1)[-1]
            if name.startswith(prefix):
                keys.append(name)
        return sorted(keys)

    async def test_connection(self) -> dict:
        # Real probe: a minimal authenticated list call against the project.
        try:
            client = self._client()
            parent = f"projects/{self._project_id}"
            next(iter(client.list_secrets(request={"parent": parent})), None)
        except Exception as e:
            return {"ok": False, "message": f"Could not reach GCP project {self._project_id}: {type(e).__name__}: {e}"}
        return {"ok": True, "message": f"GCP Secret Manager reachable for project {self._project_id}."}

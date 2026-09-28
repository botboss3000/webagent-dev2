"""
HashiCorp Vault backend (KV v2 secrets engine).

Cloud-agnostic alternative to the GCP/AWS managed vaults: works against Vault
you self-host, run in a container, or consume as HCP Vault. Connection details
come from env only (bootstrap chicken-and-egg avoided):
  - VAULT_ADDR    — server URL, e.g. https://vault.example.com:8200  (required)
  - VAULT_TOKEN   — a token with read/write on the KV path                (required)
  - VAULT_KV_MOUNT  — KV v2 mount point        (optional, default "secret")
  - VAULT_KV_PREFIX — path prefix for our keys (optional, default "webagent")

Requires `hvac`. Imported lazily so this module always loads for discovery even
when the SDK is absent (construction then fails with a clear message).
"""

import os
from typing import List, Optional

from app.secrets.interface import SecretsBackend

FEATURE = {
    "id": "hashicorp_vault",
    "display_name": "HashiCorp Vault",
    "category": "secrets",
    "status": "experimental",
    "summary": "Store secrets in a HashiCorp Vault KV v2 engine (self-hosted or HCP).",
    "requires": ["hvac", "VAULT_ADDR + VAULT_TOKEN env vars"],
}


class HashiCorpVaultSecrets(SecretsBackend):
    name = "hashicorp_vault"

    def __init__(self, addr: Optional[str] = None, token: Optional[str] = None):
        # Resolution order: explicit arg → saved UI config / token → env var.
        from app.secrets.provider_config import get_provider_config, get_provider_token
        cfg = get_provider_config(self.name)
        self._addr = addr or cfg.get("address") or os.environ.get("VAULT_ADDR")
        self._token = (
            token or get_provider_token(self.name) or os.environ.get("VAULT_TOKEN")
        )
        self._mount = cfg.get("kv_mount") or os.environ.get("VAULT_KV_MOUNT") or "secret"
        self._prefix = (
            cfg.get("kv_prefix") or os.environ.get("VAULT_KV_PREFIX") or "webagent"
        ).strip("/")
        if not self._addr or not self._token:
            raise RuntimeError(
                "HashiCorpVaultSecrets requires VAULT_ADDR and VAULT_TOKEN env vars."
            )
        try:
            import hvac  # noqa: F401
        except ImportError as e:
            raise RuntimeError(
                "HashiCorpVaultSecrets requires `hvac`. Install: pip install hvac"
            ) from e

    def _client(self):
        import hvac
        return hvac.Client(url=self._addr, token=self._token)

    def _path(self, key: str) -> str:
        # Vault path segments are slash-delimited; keep our keys flat by turning
        # any embedded slash into an underscore. `:` and `_` are path-safe.
        safe = key.replace("/", "_")
        return f"{self._prefix}/{safe}"

    async def get(self, key: str) -> Optional[str]:
        client = self._client()
        try:
            resp = client.secrets.kv.v2.read_secret_version(
                path=self._path(key),
                mount_point=self._mount,
                raise_on_deleted_version=True,
            )
            return resp["data"]["data"].get("value")
        except Exception:
            return None

    async def set(self, key: str, value: str) -> None:
        client = self._client()
        client.secrets.kv.v2.create_or_update_secret(
            path=self._path(key),
            secret={"value": value},
            mount_point=self._mount,
        )

    async def delete(self, key: str) -> bool:
        client = self._client()
        try:
            client.secrets.kv.v2.delete_metadata_and_all_versions(
                path=self._path(key),
                mount_point=self._mount,
            )
            return True
        except Exception:
            return False

    async def list_keys(self, prefix: str = "") -> List[str]:
        client = self._client()
        try:
            resp = client.secrets.kv.v2.list_secrets(
                path=self._prefix, mount_point=self._mount
            )
            keys = [k for k in resp["data"]["keys"] if k.startswith(prefix)]
            return sorted(keys)
        except Exception:
            return []

    async def test_connection(self) -> dict:
        # Real probe (list_keys above swallows errors, so the default check would
        # falsely pass): reach the server and confirm the token authenticates.
        try:
            client = self._client()
            ok = client.is_authenticated()
        except Exception as e:
            return {"ok": False, "message": f"Could not reach Vault at {self._addr}: {type(e).__name__}: {e}"}
        if not ok:
            return {"ok": False, "message": f"Reached {self._addr} but the token was not accepted."}
        return {"ok": True, "message": f"HashiCorp Vault reachable and authenticated at {self._addr} (mount '{self._mount}')."}

"""
Azure Key Vault backend.

Completes the big-three cloud secret managers (alongside GCP and AWS). Vault
location comes from env (bootstrap chicken-and-egg avoided); authentication uses
the standard Azure credential chain (env service-principal vars, Managed
Identity, Azure CLI login, etc.) via `DefaultAzureCredential`:
  - AZURE_KEY_VAULT_URL — e.g. https://my-vault.vault.azure.net   (required)
  - AZURE_CLIENT_ID / AZURE_CLIENT_SECRET / AZURE_TENANT_ID  (one auth option)
    or a Managed Identity / `az login` session on the host.

Requires `azure-keyvault-secrets` + `azure-identity`. Imported lazily so this
module always loads for discovery even when the SDKs are absent.
"""

import os
from typing import List, Optional

from app.secrets.interface import SecretsBackend

FEATURE = {
    "id": "azure_key_vault",
    "display_name": "Azure Key Vault",
    "category": "secrets",
    "status": "experimental",
    "summary": "Store secrets in Azure Key Vault.",
    "requires": ["azure-keyvault-secrets", "azure-identity", "AZURE_KEY_VAULT_URL env var"],
}


class AzureKeyVaultSecrets(SecretsBackend):
    name = "azure_key_vault"

    def __init__(self, vault_url: Optional[str] = None):
        # Resolution order: explicit arg → saved UI config / token → env var.
        from app.secrets.provider_config import get_provider_config, get_provider_token
        cfg = get_provider_config(self.name)
        self._vault_url = vault_url or cfg.get("vault_url") or os.environ.get("AZURE_KEY_VAULT_URL")
        # Optional service-principal login. When all three are present we sign in
        # explicitly; otherwise the default Azure credential chain is used
        # (Managed Identity, `az login`, env vars, etc.). The client secret is
        # the sensitive "token".
        self._client_id = cfg.get("client_id") or os.environ.get("AZURE_CLIENT_ID")
        self._tenant_id = cfg.get("tenant_id") or os.environ.get("AZURE_TENANT_ID")
        self._client_secret = get_provider_token(self.name) or os.environ.get("AZURE_CLIENT_SECRET")
        if not self._vault_url:
            raise RuntimeError(
                "AzureKeyVaultSecrets requires the AZURE_KEY_VAULT_URL env var."
            )
        try:
            from azure.keyvault.secrets import SecretClient  # noqa: F401
            from azure.identity import DefaultAzureCredential  # noqa: F401
        except ImportError as e:
            raise RuntimeError(
                "AzureKeyVaultSecrets requires `azure-keyvault-secrets` and "
                "`azure-identity`. Install: pip install azure-keyvault-secrets azure-identity"
            ) from e

    def _credential(self):
        if self._client_id and self._tenant_id and self._client_secret:
            from azure.identity import ClientSecretCredential
            return ClientSecretCredential(
                tenant_id=self._tenant_id,
                client_id=self._client_id,
                client_secret=self._client_secret,
            )
        from azure.identity import DefaultAzureCredential
        return DefaultAzureCredential()

    def _client(self):
        from azure.keyvault.secrets import SecretClient
        return SecretClient(vault_url=self._vault_url, credential=self._credential())

    def _name(self, key: str) -> str:
        # Key Vault secret names allow only [0-9A-Za-z-]; map anything else to '-'.
        return "".join(c if (c.isalnum() or c == "-") else "-" for c in key)

    async def get(self, key: str) -> Optional[str]:
        client = self._client()
        try:
            return client.get_secret(self._name(key)).value
        except Exception:
            return None

    async def set(self, key: str, value: str) -> None:
        client = self._client()
        client.set_secret(self._name(key), value)

    async def delete(self, key: str) -> bool:
        client = self._client()
        try:
            client.begin_delete_secret(self._name(key))
            return True
        except Exception:
            return False

    async def list_keys(self, prefix: str = "") -> List[str]:
        client = self._client()
        try:
            keys = [
                p.name for p in client.list_properties_of_secrets()
                if (p.name or "").startswith(prefix)
            ]
            return sorted(keys)
        except Exception:
            return []

    async def test_connection(self) -> dict:
        # Real probe: force an authenticated round-trip (list_keys above swallows
        # errors, so the default check could falsely pass).
        try:
            client = self._client()
            next(iter(client.list_properties_of_secrets()), None)
        except Exception as e:
            return {"ok": False, "message": f"Could not reach {self._vault_url}: {type(e).__name__}: {e}"}
        return {"ok": True, "message": f"Azure Key Vault reachable at {self._vault_url}."}

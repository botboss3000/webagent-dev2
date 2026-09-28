"""
Abstract secrets-backend interface.

A SecretsBackend stores opaque string values keyed by name. It is intentionally
decoupled from the application DB so that:
  - Vault rotation, audit, expiry are external concerns.
  - DB compromise alone does not leak secrets when vault is configured.
  - Bootstrap secrets (e.g. DB password) can live separately from the DB itself.

Implementations:
  - EnvSecrets          (read-only; reads os.environ)
  - InlineDBSecrets     (writes to auth_elements.secret_ref — default/legacy)
  - OSKeyringSecrets    (Windows Credential Manager / macOS Keychain / Secret Service)
  - GCPSecretManager    (stub — extend when deployed on GCP)
  - AWSSecretsManager   (stub — extend when deployed on AWS)
"""

from abc import ABC, abstractmethod
from typing import List, Optional


class SecretsBackend(ABC):
    """Async key/value store for secret strings."""

    name: str = "unknown"

    @abstractmethod
    async def get(self, key: str) -> Optional[str]:
        ...

    @abstractmethod
    async def set(self, key: str, value: str) -> None:
        ...

    @abstractmethod
    async def delete(self, key: str) -> bool:
        ...

    @abstractmethod
    async def list_keys(self, prefix: str = "") -> List[str]:
        ...

    async def test_connection(self) -> dict:
        """
        Sanity check. Default: try to list keys; specific backends may override.
        Returns {"ok": bool, "message": str}
        """
        try:
            await self.list_keys()
            return {"ok": True, "message": f"{self.name} reachable"}
        except Exception as e:
            return {"ok": False, "message": str(e)}

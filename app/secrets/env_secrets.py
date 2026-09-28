"""
Environment-variable secrets backend (read-only).

Useful for Cloud Run / Docker deployments where secrets are injected as env vars.
Writes raise PermissionError — env vars are immutable from the running process.
"""

import os
from typing import List, Optional

from app.secrets.interface import SecretsBackend

FEATURE = {
    "id": "env",
    "display_name": "Environment variables",
    "category": "secrets",
    "status": "stable",
    "summary": "Read secrets from process env vars (read-only). Good for Cloud Run / Docker.",
}


class EnvSecrets(SecretsBackend):
    name = "env"

    def __init__(self, prefix: str = "WEBAGENT_SECRET_"):
        # Only env vars with this prefix are visible. Key "foo" maps to
        # env var "WEBAGENT_SECRET_FOO". Prevents accidental exposure of
        # every env var.
        self._prefix = prefix

    def _env_name(self, key: str) -> str:
        return f"{self._prefix}{key.upper().replace('-', '_').replace('.', '_')}"

    async def get(self, key: str) -> Optional[str]:
        return os.environ.get(self._env_name(key))

    async def set(self, key: str, value: str) -> None:
        raise PermissionError(
            "EnvSecrets is read-only. Set values via the deployment environment."
        )

    async def delete(self, key: str) -> bool:
        raise PermissionError("EnvSecrets is read-only.")

    async def list_keys(self, prefix: str = "") -> List[str]:
        wanted = self._prefix + prefix.upper().replace("-", "_")
        keys = []
        for k in os.environ:
            if k.startswith(wanted):
                # strip the prefix; lowercase for parity with set/get callers
                keys.append(k[len(self._prefix):].lower())
        return sorted(keys)

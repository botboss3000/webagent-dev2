"""
OS keyring secrets backend.

Uses the `keyring` pip package (optional dep). On Windows this delegates to
Credential Manager; on macOS to Keychain; on Linux to Secret Service (gnome-keyring
or KWallet via D-Bus).

Headless Linux without a Secret Service daemon will fail on first access — fall
back to InlineDBSecrets or EnvSecrets there.
"""

from typing import List, Optional

from app.secrets.interface import SecretsBackend

FEATURE = {
    "id": "os_keyring",
    "display_name": "OS keyring",
    "category": "secrets",
    "status": "beta",
    "summary": "Use the OS credential store (Windows Credential Manager / macOS Keychain / Secret Service).",
    "requires": ["the optional 'keyring' package", "a desktop secret service"],
}

_SERVICE = "webagent_vault"


class OSKeyringSecrets(SecretsBackend):
    name = "os_keyring"

    def __init__(self):
        try:
            import keyring  # noqa: F401
        except ImportError as e:
            raise RuntimeError(
                "OSKeyringSecrets requires the `keyring` package. "
                "Install with: pip install keyring"
            ) from e

    async def get(self, key: str) -> Optional[str]:
        import keyring
        return keyring.get_password(_SERVICE, key)

    async def set(self, key: str, value: str) -> None:
        import keyring
        keyring.set_password(_SERVICE, key, value)

    async def delete(self, key: str) -> bool:
        import keyring
        try:
            keyring.delete_password(_SERVICE, key)
            return True
        except keyring.errors.PasswordDeleteError:
            return False

    async def list_keys(self, prefix: str = "") -> List[str]:
        # `keyring` has no list API on most backends (Credential Manager exposes
        # one but the pip wrapper doesn't surface it portably). We maintain a
        # sidecar index file. For now return an empty list — UI calls .get(key)
        # by explicit name only.
        return []

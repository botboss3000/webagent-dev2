"""
Inline-DB secrets backend.

Stores secrets directly in the application database's `auth_elements` table.
Service is fixed to "_secrets_vault" and label is the user-provided key.

This is the LEGACY default — equivalent to the pre-vault behavior. Surfaces a
warning when activated so admins know to upgrade for production.
"""

from typing import List, Optional

from app.secrets.interface import SecretsBackend

FEATURE = {
    "id": "inline_db",
    "display_name": "App-DB vault",
    "category": "secrets",
    "status": "stable",
    "summary": "Store secrets in the app database (auth_elements). Bootstrap default.",
}


class InlineDBSecrets(SecretsBackend):
    name = "inline_db"

    # Reserved service identifier for vault entries inside auth_elements.
    _SERVICE = "_secrets_vault"
    _USER_ID = "_vault"  # tenancy: vault is global, not per-user

    def _get_db(self):
        # Imported lazily to avoid circular import (db -> secrets -> db).
        # The inline vault is installation-global.  An ambient get_db() may be
        # task-locally overridden to a user's data plane during an agent turn;
        # key hierarchy reads must never follow that override.
        from app.db import get_app_db
        return get_app_db()

    async def get(self, key: str) -> Optional[str]:
        row = await self._get_db().auth_element_get(
            user_id=self._USER_ID, service=self._SERVICE, label=key,
        )
        if not row:
            return None
        ref = row.get("secret_ref")
        return ref if ref else None

    async def set(self, key: str, value: str) -> None:
        await self._get_db().auth_element_set(
            user_id=self._USER_ID,
            service=self._SERVICE,
            config={},
            secret_ref=value,
            label=key,
        )

    async def delete(self, key: str) -> bool:
        return await self._get_db().auth_element_delete(
            user_id=self._USER_ID, service=self._SERVICE, label=key,
        )

    async def list_keys(self, prefix: str = "") -> List[str]:
        rows = await self._get_db().auth_element_list(
            user_id=self._USER_ID, service=self._SERVICE,
        )
        keys = [r.get("label", "") for r in rows if r.get("label", "").startswith(prefix)]
        return sorted(keys)

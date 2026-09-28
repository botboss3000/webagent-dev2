"""First-boot security defaults: turn ON the OS-keyring vault + per-tenant field
encryption automatically on a FRESH deployment — and ONLY on a fresh one.

Why a guarded seeder instead of just flipping the in-code default: the keys for
field encryption live in this machine's OS keyring, not in the database. That is
exactly what makes it secure, but it means silently switching an *existing*
install (one that already has stored secrets) would machine-bind credentials the
admin never chose to encrypt — and a later DB restore on another box would strand
them. So we seed only when there is nothing to lose and the environment can
actually support it.

The seed fires only when ALL of these hold (otherwise it is a clean no-op that
leaves the install exactly as it was, re-seedable on a later boot):

  * not env-locked (cloud/Cloud-Run installs configure via env vars explicitly);
  * neither the secrets-provider nor the encryption-level has been persisted yet
    (nobody — admin or a prior seed — has decided);
  * the OS keyring resolves to a real, secure backend AND a throwaway
    write/read/delete round-trip actually succeeds;
  * the field backend constructs (the crypto dependency is present);
  * the database holds no stored secrets yet (nothing to strand or migrate).

On success it writes the install's master key (KEK) into the keyring, switches
the secrets provider to ``os_keyring`` and the encryption level to ``field``,
and rebuilds the DB wrapper. Persisting those two mode files is itself the
"already decided" marker, so the seeder is a no-op on every subsequent boot.
"""

from __future__ import annotations

import logging
import os

logger = logging.getLogger(__name__)

_PROBE_KEY = "wa:__seed_probe__"


def _env_locked() -> bool:
    return os.environ.get("WEBAGENT_CONFIG_SOURCE", "").lower() == "env"


def _has_stored_secrets() -> bool:
    """True if the credentials vault already holds any secret rows.

    A brand-new deployment has none; an existing install that has connected even
    one integration does. Treats an absent table (truly fresh DB) as empty.
    """
    try:
        from app.db import get_db
        raw = get_db().get_raw_client()
        res = raw.table("auth_elements").select("user_id").limit(1).execute()
        return bool(getattr(res, "data", None))
    except Exception:
        return False


async def seed_security_defaults() -> dict:
    """Enable os_keyring + field encryption on a fresh, capable install. No-op otherwise.

    Returns a small summary dict ({"seeded": bool, "reason": str}) for logging.
    Never raises — any failure degrades to a no-op so it can never block boot.
    """
    try:
        if _env_locked():
            return {"seeded": False, "reason": "env-locked"}

        # Respect any explicit prior choice (admin toggle or a previous seed).
        from app.secrets import mode_configured, keyring_is_secure
        from app.encryption import level_configured, probe_level
        if mode_configured() or level_configured():
            return {"seeded": False, "reason": "already-configured"}

        # The environment must be able to hold keys securely.
        if not keyring_is_secure():
            return {"seeded": False, "reason": "no-secure-keyring"}

        # The field backend must construct (crypto dependency present).
        field_err = probe_level("field")
        if field_err:
            return {"seeded": False, "reason": f"field-unavailable: {field_err}"}

        # Never disturb an install that already has secrets to protect/strand.
        if _has_stored_secrets():
            return {"seeded": False, "reason": "existing-secrets"}

        # Authoritative writability check — a real round-trip on the keyring
        # backend WITHOUT persisting any mode, so a failure leaves the install
        # pristine and able to seed on a later boot.
        try:
            from app.secrets import _construct
            vault = _construct("os_keyring")
            await vault.set(_PROBE_KEY, "ok")
            ok = (await vault.get(_PROBE_KEY)) == "ok"
            await vault.delete(_PROBE_KEY)
            if not ok:
                return {"seeded": False, "reason": "keyring-roundtrip-failed"}
        except Exception as e:
            return {"seeded": False, "reason": f"keyring-unavailable: {e}"}

        # Commit: keyring vault first (so the KEK lands there), mint the install
        # KEK, then turn on field encryption and rebuild the DB wrapper.
        from app.secrets import set_mode as set_secrets_mode
        from app.encryption import set_level
        from app.encryption.vault_keys import get_vault_keys
        from app.db import reset_db_instance

        set_secrets_mode("os_keyring")
        await get_vault_keys().ensure_kek()
        set_level("field")
        reset_db_instance()
        logger.info("Security defaults seeded ON (secrets=os_keyring, encryption=field)")
        return {"seeded": True, "reason": "fresh-install"}
    except Exception as e:  # pragma: no cover - defensive: never block boot
        logger.warning("Security-defaults seed failed (left unchanged): %s", e)
        return {"seeded": False, "reason": f"error: {e}"}

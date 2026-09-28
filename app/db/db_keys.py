"""Per-database file keys for full-database (SQLCipher) encryption.

Each in-scope database file (local, vault, logs, recordings, wiki) gets its own
random 256-bit key. That key is **wrapped (Fernet) under the install KEK** and
stored in the OS keyring at ``wa:dbkey:<db_id>``. So:

  * the database *files* on disk hold no key material;
  * the keyring holds the wrapped per-file keys;
  * a single master key (the KEK, the same one field-level encryption uses) is
    the one thing that unlocks every database — back it up and you can recover
    them all.

This module is deliberately **synchronous**: it is called from the SQLite
connection-open path, which is sync. It reads the OS keyring directly (the same
store the ``os_keyring`` secrets backend writes to) rather than going through the
async secrets API, so full-DB encryption requires the ``os_keyring`` vault.

Unwrapped keys are cached in-process (they don't change except on an explicit
rotate, which clears the cache) so connections don't hit the keyring every open.
"""

from __future__ import annotations

import logging
import os
from typing import Dict, Optional

logger = logging.getLogger(__name__)

# The keyring service the os_keyring secrets backend uses, and the KEK slot the
# vault key manager stores the install master key in. Imported so a rename in
# either place can't silently desync this module.
from app.secrets.keyring_secrets import _SERVICE as SERVICE
from app.encryption.vault_keys import KEK_ACTIVE

_DBKEY_PREFIX = "wa:dbkey:"

# db_id -> 64-char hex key (32 raw bytes). Cleared on rotate.
_cache: Dict[str, str] = {}


def _keyring():
    """Return the keyring module, or raise a clear error if unavailable."""
    import keyring  # raises ImportError if the optional dep is missing
    return keyring


def _fernet_kek(create: bool = False):
    """Return a Fernet built from the install KEK, optionally creating it.

    The KEK lives in the keyring at ``wa:kek:active`` — the same slot the field
    encryption key manager uses, so the two share one root of trust.
    """
    from cryptography.fernet import Fernet
    kr = _keyring()
    raw = kr.get_password(SERVICE, KEK_ACTIVE)
    if not raw:
        if not create:
            raise RuntimeError("No install KEK present in the keyring (wa:kek:active).")
        key = Fernet.generate_key()
        kr.set_password(SERVICE, KEK_ACTIVE, key.decode("utf-8"))
        logger.info("Generated install KEK for full-DB encryption (wa:kek:active)")
        raw = key.decode("utf-8")
    return Fernet(raw.encode("utf-8") if isinstance(raw, str) else raw)


def _slot(db_id: str) -> str:
    return f"{_DBKEY_PREFIX}{db_id}"


def has_key(db_id: str) -> bool:
    """True if a wrapped per-file key already exists for this database."""
    if db_id in _cache:
        return True
    try:
        return bool(_keyring().get_password(SERVICE, _slot(db_id)))
    except Exception:
        return False


def get_raw_key(db_id: str) -> Optional[str]:
    """Return the unwrapped 64-hex key for this database, or None if none exists."""
    cached = _cache.get(db_id)
    if cached:
        return cached
    try:
        kr = _keyring()
        wrapped = kr.get_password(SERVICE, _slot(db_id))
        if not wrapped:
            return None
        kek = _fernet_kek(create=False)
        raw = kek.decrypt(wrapped.encode("utf-8") if isinstance(wrapped, str) else wrapped)
        hexkey = raw.hex()
        _cache[db_id] = hexkey
        return hexkey
    except Exception as e:
        logger.error("Could not unwrap DB key for '%s': %s", db_id, e)
        return None


def get_or_create_raw_key(db_id: str) -> str:
    """Return this database's 64-hex key, minting + wrapping one on first use."""
    existing = get_raw_key(db_id)
    if existing:
        return existing
    kr = _keyring()
    kek = _fernet_kek(create=True)
    raw = os.urandom(32)
    wrapped = kek.encrypt(raw).decode("utf-8")
    kr.set_password(SERVICE, _slot(db_id), wrapped)
    hexkey = raw.hex()
    _cache[db_id] = hexkey
    logger.info("Minted + wrapped a new full-DB encryption key for '%s'", db_id)
    return hexkey


def delete_key(db_id: str) -> None:
    """Remove a database's wrapped key from the keyring (after it's decrypted).

    Only call once the file itself is back to plaintext — deleting a key while
    the file is still encrypted would strand the data.
    """
    _cache.pop(db_id, None)
    try:
        _keyring().delete_password(SERVICE, _slot(db_id))
    except Exception:
        pass


def clear_cache() -> None:
    _cache.clear()

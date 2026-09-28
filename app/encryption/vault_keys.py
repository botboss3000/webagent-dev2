"""
VaultKeyManager — multi-tenant key hierarchy backed by the configured
SecretsBackend (app/secrets/).

Vault key layout:
    "wa:kek:active"            → urlsafe-base64-encoded 32-byte KEK
    "wa:kek:v<n>"              → retired KEK versions (kept during rotation grace)
    "wa:dek:<user_id>:v<n>"    → KEK-wrapped DEK (Fernet token of the DEK bytes)

The DB holds metadata only (table `tenant_key_meta`) and never any key bytes.
This way an attacker who exfiltrates only the DB has zero key material; an
attacker who exfiltrates only the vault has only encrypted blobs.

In-process caches keep unwrap costs low: KEK and unwrapped DEKs are LRU-cached
with a TTL. Cache invalidation hooks are exposed for rotation/migration paths.
"""

from __future__ import annotations

import logging
import os
import time
from typing import Dict, List, Optional, Tuple

from cryptography.fernet import Fernet, InvalidToken

logger = logging.getLogger(__name__)

KEK_ACTIVE = "wa:kek:active"
KEK_PREFIX = "wa:kek:"
DEK_PREFIX = "wa:dek:"

_CACHE_TTL_SECONDS = int(os.environ.get("WEBAGENT_ENC_CACHE_TTL", "300"))


def _now() -> float:
    return time.monotonic()


class VaultKeyManager:
    """Owns KEK and per-tenant DEKs; persists wrapped blobs to the SecretsBackend."""

    def __init__(self) -> None:
        # cache: (key_id, version) → (fernet_instance, inserted_at)
        self._dek_cache: Dict[Tuple[str, int], Tuple[Fernet, float]] = {}
        # KEK is cached by version; "active" is mapped to its real version too.
        self._kek_cache: Dict[str, Tuple[Fernet, float]] = {}
        # Peer KEKs for cross-instance decryption (P2P vault sync)
        self._peer_keks: Dict[str, Fernet] = {}  # peer_id -> Fernet

    # ── KEK ────────────────────────────────────────────────────────────────

    async def _vault(self):
        # Lazy import to dodge import cycles (secrets → db → encryption).
        from app.secrets import get_secrets
        return get_secrets()

    async def get_kek(self, version: str = "active") -> Optional[Fernet]:
        """Return a Fernet built from the KEK bytes for `version`, or None."""
        cached = self._kek_cache.get(version)
        if cached and _now() - cached[1] < _CACHE_TTL_SECONDS:
            return cached[0]
        vault = await self._vault()
        key = KEK_ACTIVE if version == "active" else f"{KEK_PREFIX}v{version}"
        raw = await vault.get(key)
        if not raw:
            return None
        try:
            f = Fernet(raw.encode("utf-8") if isinstance(raw, str) else raw)
        except Exception as e:
            logger.error("Stored KEK %s is malformed: %s", key, e)
            return None
        self._kek_cache[version] = (f, _now())
        return f

    async def ensure_kek(self) -> Fernet:
        """Return the active KEK, generating one if absent."""
        f = await self.get_kek("active")
        if f is not None:
            return f
        return await self.generate_kek()

    async def generate_kek(self) -> Fernet:
        """Create a fresh KEK and write it to the vault as active."""
        vault = await self._vault()
        key_bytes = Fernet.generate_key()  # already urlsafe-base64-encoded
        await vault.set(KEK_ACTIVE, key_bytes.decode("utf-8"))
        self._kek_cache.clear()  # any cached Fernet is now stale
        self._dek_cache.clear()  # DEK unwrap depended on prior KEK
        logger.info("Generated new KEK and stored as %s", KEK_ACTIVE)
        return Fernet(key_bytes)

    async def rotate_kek(self) -> dict:
        """
        Generate a new KEK, re-wrap every per-tenant DEK with it, and retire the
        old KEK to "wa:kek:v<n>". Row data is never touched.
        Returns a summary {old_version, new_version, deks_rewrapped}.
        """
        vault = await self._vault()
        old = await self.get_kek("active")
        if old is None:
            # No KEK yet — first install. Just generate one.
            await self.generate_kek()
            return {"old_version": None, "new_version": "active", "deks_rewrapped": 0}

        # Bump retired-version counter.
        retired_keys = [k for k in await vault.list_keys(prefix=KEK_PREFIX) if k.startswith(f"{KEK_PREFIX}v")]
        existing_versions = []
        for k in retired_keys:
            try:
                existing_versions.append(int(k[len(KEK_PREFIX) + 1:]))
            except ValueError:
                continue
        next_retired = (max(existing_versions) + 1) if existing_versions else 1

        # Move old → retired slot, then generate new active.
        old_token = await vault.get(KEK_ACTIVE)
        if old_token:
            await vault.set(f"{KEK_PREFIX}v{next_retired}", old_token)

        new_f = await self.generate_kek()

        # Discover every DEK to re-wrap. Prefer the vault's own listing (it's
        # the source of truth) but fall back to `tenant_key_meta` for vault
        # backends that can't enumerate (OS keyring).
        dek_keys = list(await vault.list_keys(prefix=DEK_PREFIX))
        if not dek_keys:
            for t in await self.list_tenants():
                uid = t["user_id"]
                for row in await self.list_dek_versions(uid):
                    dek_keys.append(self._dek_key(uid, int(row["key_version"])))

        n_rewrapped = 0
        for key in dek_keys:
            wrapped = await vault.get(key)
            if not wrapped:
                continue
            try:
                dek_bytes = old.decrypt(wrapped.encode("utf-8") if isinstance(wrapped, str) else wrapped)
            except InvalidToken:
                logger.warning("DEK %s could not be unwrapped with old KEK; skipping", key)
                continue
            new_wrapped = new_f.encrypt(dek_bytes).decode("utf-8")
            await vault.set(key, new_wrapped)
            n_rewrapped += 1

        self._dek_cache.clear()
        return {
            "old_version": next_retired,
            "new_version": "active",
            "deks_rewrapped": n_rewrapped,
        }

    # ── DEKs ───────────────────────────────────────────────────────────────

    @staticmethod
    def _dek_key(user_id: str, version: int) -> str:
        return f"{DEK_PREFIX}{user_id}:v{version}"

    async def get_dek(self, user_id: str, version: int) -> Optional[Fernet]:
        """Fetch + unwrap a specific DEK version. Returns Fernet or None."""
        cache_key = (user_id, version)
        cached = self._dek_cache.get(cache_key)
        if cached and _now() - cached[1] < _CACHE_TTL_SECONDS:
            return cached[0]

        vault = await self._vault()
        wrapped = await vault.get(self._dek_key(user_id, version))
        if not wrapped:
            return None
        # Try active KEK first, then retired versions if any.
        for kek_version in ("active", *await self._list_retired_kek_versions()):
            kek = await self.get_kek(kek_version)
            if kek is None:
                continue
            try:
                dek_bytes = kek.decrypt(wrapped.encode("utf-8") if isinstance(wrapped, str) else wrapped)
                f = Fernet(dek_bytes)
                self._dek_cache[cache_key] = (f, _now())
                return f
            except InvalidToken:
                continue
        logger.error("Could not unwrap DEK %s with any known KEK version", self._dek_key(user_id, version))
        return None

    async def get_or_create_dek(self, user_id: str) -> Tuple[Fernet, int]:
        """
        Return (Fernet, version) for the active DEK of this tenant, generating
        one on first use. Also upserts the tenant_key_meta row.
        """
        active_version = await self._read_active_version(user_id)
        if active_version is not None:
            f = await self.get_dek(user_id, active_version)
            if f is not None:
                return f, active_version
            # Metadata exists but vault is missing the entry — fall through and regenerate.
            logger.warning(
                "Active DEK meta exists for user %s v%d but vault has no entry; regenerating",
                user_id, active_version,
            )

        # Generate fresh DEK (32 bytes encoded urlsafe-base64 by Fernet).
        kek = await self.ensure_kek()
        dek_bytes = Fernet.generate_key()
        wrapped = kek.encrypt(dek_bytes).decode("utf-8")

        next_version = (active_version or 0) + 1
        vault = await self._vault()
        await vault.set(self._dek_key(user_id, next_version), wrapped)

        await self._upsert_meta(user_id, next_version, status="active", algo="fernet")
        f = Fernet(dek_bytes)
        self._dek_cache[(user_id, next_version)] = (f, _now())
        return f, next_version

    async def rotate_dek(self, user_id: str) -> dict:
        """
        Generate a new DEK version for this tenant. Caller is responsible for
        walking the user's rows to re-encrypt with the new version.
        Returns {"old_version": int|None, "new_version": int}.
        """
        old_version = await self._read_active_version(user_id)
        kek = await self.ensure_kek()
        dek_bytes = Fernet.generate_key()
        wrapped = kek.encrypt(dek_bytes).decode("utf-8")
        new_version = (old_version or 0) + 1

        vault = await self._vault()
        await vault.set(self._dek_key(user_id, new_version), wrapped)
        await self._upsert_meta(user_id, new_version, status="active", algo="fernet")
        if old_version is not None:
            await self._mark_retired(user_id, old_version)

        self._dek_cache[(user_id, new_version)] = (Fernet(dek_bytes), _now())
        return {"old_version": old_version, "new_version": new_version}

    async def purge_retired_dek(self, user_id: str, version: int) -> bool:
        """Delete a retired DEK from the vault and meta table."""
        vault = await self._vault()
        await vault.delete(self._dek_key(user_id, version))
        await self._delete_meta(user_id, version)
        self._dek_cache.pop((user_id, version), None)
        return True

    async def list_dek_versions(self, user_id: str) -> List[dict]:
        """Return all tenant_key_meta rows for this user, ordered by version desc."""
        from app.db import get_app_db
        db = _unwrap(get_app_db())
        raw = db.get_raw_client()
        try:
            res = (
                raw.table("tenant_key_meta")
                .select("*")
                .eq("user_id", user_id)
                .execute()
            )
            rows = list(res.data or [])
            rows.sort(key=lambda r: r.get("key_version", 0), reverse=True)
            return rows
        except Exception as e:
            logger.warning("list_dek_versions(%s) failed: %s", user_id, e)
            return []

    async def list_tenants(self) -> List[dict]:
        """Return one summary row per tenant: {user_id, active_version, total_versions}."""
        from app.db import get_app_db
        # Key-version metadata is installation/control-plane state.  Never use
        # ambient get_db() here: during a chat turn it may be overridden with a
        # user-plane backend, which makes an existing tenant look keyless and can
        # cause get_or_create_dek() to overwrite wa:dek:<user>:v1 in the vault.
        db = _unwrap(get_app_db())
        raw = db.get_raw_client()
        try:
            res = raw.table("tenant_key_meta").select("*").execute()
            rows = list(res.data or [])
        except Exception as e:
            logger.warning("list_tenants failed: %s", e)
            return []
        by_user: Dict[str, dict] = {}
        for r in rows:
            uid = r.get("user_id")
            if not uid:
                continue
            entry = by_user.setdefault(uid, {"user_id": uid, "active_version": None, "total_versions": 0})
            entry["total_versions"] += 1
            if r.get("status") == "active":
                entry["active_version"] = r.get("key_version")
        return sorted(by_user.values(), key=lambda d: d["user_id"])

    # ── Metadata helpers ───────────────────────────────────────────────────

    async def _read_active_version(self, user_id: str) -> Optional[int]:
        from app.db import get_app_db
        db = _unwrap(get_app_db())
        raw = db.get_raw_client()
        try:
            res = (
                raw.table("tenant_key_meta")
                .select("*")
                .eq("user_id", user_id)
                .eq("status", "active")
                .execute()
            )
            data = list(res.data or [])
            if not data:
                return None
            data.sort(key=lambda r: r.get("key_version", 0), reverse=True)
            return int(data[0]["key_version"])
        except Exception as e:
            logger.warning("_read_active_version(%s) failed: %s", user_id, e)
            return None

    async def _upsert_meta(self, user_id: str, version: int, status: str, algo: str) -> None:
        from app.db import get_app_db
        db = _unwrap(get_app_db())
        raw = db.get_raw_client()
        from datetime import datetime, timezone
        now = datetime.now(timezone.utc).isoformat()
        row = {
            "user_id": user_id,
            "key_version": version,
            "algo": algo,
            "status": status,
            "created_at": now,
        }
        # Local backend's raw client returns an upsert-like proxy.
        # Easiest portable path: try update, then insert if no rows touched.
        try:
            ex = (
                raw.table("tenant_key_meta")
                .select("user_id")
                .eq("user_id", user_id)
                .eq("key_version", version)
                .execute()
            )
            if ex.data:
                raw.table("tenant_key_meta") \
                   .update({"status": status, "algo": algo}) \
                   .eq("user_id", user_id) \
                   .eq("key_version", version) \
                   .execute()
            else:
                raw.table("tenant_key_meta").insert(row).execute()
        except Exception as e:
            logger.warning("_upsert_meta(%s, v%d) failed: %s", user_id, version, e)

    async def _mark_retired(self, user_id: str, version: int) -> None:
        from app.db import get_app_db
        db = _unwrap(get_app_db())
        raw = db.get_raw_client()
        from datetime import datetime, timezone
        now = datetime.now(timezone.utc).isoformat()
        try:
            raw.table("tenant_key_meta") \
               .update({"status": "retired", "retired_at": now}) \
               .eq("user_id", user_id) \
               .eq("key_version", version) \
               .execute()
        except Exception as e:
            logger.warning("_mark_retired(%s, v%d) failed: %s", user_id, version, e)

    async def _delete_meta(self, user_id: str, version: int) -> None:
        from app.db import get_app_db
        db = _unwrap(get_app_db())
        raw = db.get_raw_client()
        try:
            raw.table("tenant_key_meta") \
               .delete() \
               .eq("user_id", user_id) \
               .eq("key_version", version) \
               .execute()
        except Exception as e:
            logger.warning("_delete_meta(%s, v%d) failed: %s", user_id, version, e)

    async def _list_retired_kek_versions(self) -> List[str]:
        vault = await self._vault()
        keys = await vault.list_keys(prefix=KEK_PREFIX)
        out: List[str] = []
        for k in keys:
            if k.startswith(f"{KEK_PREFIX}v"):
                out.append(k[len(KEK_PREFIX) + 1:])  # strip "wa:kek:v"
        # sort numerically descending so we try newest retired first
        try:
            out.sort(key=lambda s: int(s), reverse=True)
        except ValueError:
            pass
        return out

    def register_peer_kek(self, peer_id: str, kek_bytes: bytes) -> None:
        """Register a peer's KEK for cross-instance Fernet decryption.
        
        Called after a successful P2P handshake + KEK exchange.
        The KEK bytes are the raw Fernet key (urlsafe-base64 encoded).
        """
        try:
            f = Fernet(kek_bytes)
            self._peer_keks[peer_id] = f
            logger.info("Registered peer KEK for %s", peer_id)
        except Exception as e:
            logger.error("Failed to register peer KEK for %s: %s", peer_id, e)

    def unregister_peer_kek(self, peer_id: str) -> None:
        """Remove a peer's KEK (called on revocation or disconnect)."""
        self._peer_keks.pop(peer_id, None)
        logger.info("Unregistered peer KEK for %s", peer_id)

    def try_decrypt_with_peer_keks(self, token: str) -> Optional[bytes]:
        """Try to decrypt a Fernet token using any registered peer KEK.
        
        Returns the plaintext bytes on first successful decryption, or None.
        Used when the local KEK can't decrypt a synced row's secret_ref.
        """
        for peer_id, f in self._peer_keks.items():
            try:
                token_bytes = token.encode("utf-8") if isinstance(token, str) else token
                return f.decrypt(token_bytes)
            except InvalidToken:
                continue
        return None


# ── Module-level singleton ─────────────────────────────────────────────────

_singleton: Optional[VaultKeyManager] = None


def get_vault_keys() -> VaultKeyManager:
    global _singleton
    if _singleton is None:
        _singleton = VaultKeyManager()
    return _singleton


def reset_for_tests() -> None:
    global _singleton
    _singleton = None


def _unwrap(db):
    """Return the underlying StorageBackend, skipping the encryption decorator.

    Key-metadata writes must NEVER go through the encryption layer (no DEK
    exists yet for the user we're about to mint a DEK for, and the
    tenant_key_meta table has no encrypted columns anyway).
    """
    # Local import to avoid the same cycle as get_db itself.
    from app.db.interface import EncryptedStorageBackend
    while isinstance(db, EncryptedStorageBackend):
        db = db._inner
    return db

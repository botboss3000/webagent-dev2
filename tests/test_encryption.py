"""
Unit tests for the encryption module.

Covers:
  - NoEncryption pass-through.
  - FieldFernet round-trip with per-tenant DEKs.
  - Tenant isolation: a DEK for user_a cannot decrypt user_b's ciphertext.
  - KEK rotation re-wraps every DEK in the vault; row data still decrypts.
  - DEK rotation per tenant: new ciphertext uses new version; old still readable
    until purged.
  - Idempotent encryption (re-encrypting an already-encrypted blob is a no-op).
  - EncryptedStorageBackend decorator transparently encrypts on write and
    decrypts on read.
  - Vault-sentinel rows (inline-DB secrets storage) bypass the shim.
"""

from __future__ import annotations

import asyncio
import os
import shutil
import tempfile
import unittest
from unittest import mock
from typing import Dict, List, Optional


def _async(coro):
    return asyncio.run(coro)


# ─────────────────────────────────────────────────────────────────────────
# In-memory secrets backend (replaces app/secrets for these tests)
# ─────────────────────────────────────────────────────────────────────────

class _InMemSecrets:
    name = "test_inmem"

    def __init__(self):
        self._store: Dict[str, str] = {}

    async def get(self, key: str) -> Optional[str]:
        return self._store.get(key)

    async def set(self, key: str, value: str) -> None:
        self._store[key] = value

    async def delete(self, key: str) -> bool:
        return self._store.pop(key, None) is not None

    async def list_keys(self, prefix: str = "") -> List[str]:
        return sorted(k for k in self._store if k.startswith(prefix))

    async def test_connection(self) -> dict:
        return {"ok": True, "message": "in-memory"}


# ─────────────────────────────────────────────────────────────────────────
# Test scaffolding: patch app.secrets.get_secrets + spin up a fresh LocalBackend
# ─────────────────────────────────────────────────────────────────────────


class _Base(unittest.TestCase):
    """Set up an isolated SQLite DB + in-memory secrets per test."""

    def setUp(self) -> None:
        # Fresh tmp dir for the SQLite file.
        self.tmpdir = tempfile.mkdtemp(prefix="webagent-enc-test-")
        self.db_path = os.path.join(self.tmpdir, "test.db")

        # Redirect the persisted encryption-mode file to the tmp dir so the tests
        # (set_level writes it; reset_for_tests deletes it) can NEVER touch the
        # real app/encryption_mode.json — otherwise running this suite silently
        # wipes a live install's encryption setting back to "none".
        from app import encryption as _enc_mod
        self._orig_enc_mode_file = _enc_mod._MODE_FILE
        _enc_mod._MODE_FILE = os.path.join(self.tmpdir, "encryption_mode.json")

        # Patch the SecretsBackend factory before constructing anything that
        # touches secrets/encryption modules.
        from app import secrets as _secrets_mod
        self._mem_secrets = _InMemSecrets()
        self._orig_get_secrets = _secrets_mod.get_secrets
        _secrets_mod.get_secrets = lambda: self._mem_secrets  # type: ignore

        # Reset cached singletons so they pick up the patched secrets.
        from app.encryption import reset_for_tests as _enc_reset
        from app.encryption.vault_keys import reset_for_tests as _vkm_reset
        _enc_reset()
        _vkm_reset()

        # Patch both ambient and explicit control-plane accessors to return a
        # fresh LocalBackend pointed at the tmp file.  Key metadata deliberately
        # uses get_app_db so a task-local/user-plane get_db override cannot mint
        # and overwrite a duplicate DEK version.
        from app.db import local as _local_mod
        self._local_backend = _local_mod.LocalBackend(db_path=self.db_path)

        from app import db as _db_mod
        self._orig_get_db = _db_mod.get_db
        self._orig_get_app_db = _db_mod.get_app_db
        _db_mod.get_db = lambda: self._local_backend  # type: ignore
        _db_mod.get_app_db = lambda: self._local_backend  # type: ignore

    def tearDown(self) -> None:
        # Restore patched functions.
        from app import secrets as _secrets_mod
        _secrets_mod.get_secrets = self._orig_get_secrets  # type: ignore
        from app import db as _db_mod
        _db_mod.get_db = self._orig_get_db  # type: ignore
        _db_mod.get_app_db = self._orig_get_app_db  # type: ignore

        # Drop singletons so subsequent tests start clean.
        from app.encryption import reset_for_tests as _enc_reset
        from app.encryption.vault_keys import reset_for_tests as _vkm_reset
        _enc_reset()
        _vkm_reset()

        # Restore the real mode-file path AFTER the reset cleaned up the tmp one,
        # so the live app/encryption_mode.json is never removed.
        from app import encryption as _enc_mod
        _enc_mod._MODE_FILE = self._orig_enc_mode_file

        shutil.rmtree(self.tmpdir, ignore_errors=True)


# ─────────────────────────────────────────────────────────────────────────
# NoEncryption + interface basics
# ─────────────────────────────────────────────────────────────────────────


class NoEncryptionTests(_Base):
    def test_pass_through(self):
        from app.encryption.none import NoEncryption
        e = NoEncryption()
        self.assertEqual(_async(e.encrypt("u", "hello")), "hello")
        self.assertEqual(_async(e.decrypt("u", "hello")), "hello")

    def test_health(self):
        from app.encryption.none import NoEncryption
        h = _async(NoEncryption().health())
        self.assertTrue(h["ok"])
        self.assertEqual(h["level"], "none")


# ─────────────────────────────────────────────────────────────────────────
# FieldFernet round-trip + tenant isolation
# ─────────────────────────────────────────────────────────────────────────


class FieldFernetTests(_Base):
    def test_round_trip(self):
        from app.encryption.field_fernet import FieldFernet
        from app.encryption.interface import is_ciphertext

        e = FieldFernet()
        ct = _async(e.encrypt("alice", "super-secret"))
        self.assertTrue(is_ciphertext(ct))
        self.assertNotIn("super-secret", ct)
        pt = _async(e.decrypt("alice", ct))
        self.assertEqual(pt, "super-secret")

    def test_tenant_isolation(self):
        from app.encryption.field_fernet import FieldFernet
        e = FieldFernet()
        ct = _async(e.encrypt("alice", "alice-token"))
        # Bob has no DEK; decrypt should not yield alice's plaintext.
        out = _async(e.decrypt("bob", ct))
        # With cryptography's Fernet, decrypting alice's token with a fresh bob
        # DEK raises InvalidToken; our decoder returns the ciphertext unchanged.
        self.assertNotEqual(out, "alice-token")
        self.assertTrue(out.startswith("enc:v1:"))

    def test_idempotent_encrypt(self):
        from app.encryption.field_fernet import FieldFernet
        e = FieldFernet()
        ct = _async(e.encrypt("alice", "secret"))
        ct2 = _async(e.encrypt("alice", ct))  # should NOT double-wrap
        self.assertEqual(ct, ct2)

    def test_health_round_trip(self):
        from app.encryption.field_fernet import FieldFernet
        h = _async(FieldFernet().health())
        self.assertTrue(h["ok"], h)

    def test_empty_string_passes_through(self):
        from app.encryption.field_fernet import FieldFernet
        e = FieldFernet()
        self.assertEqual(_async(e.encrypt("alice", "")), "")
        self.assertEqual(_async(e.decrypt("alice", "")), "")


# ─────────────────────────────────────────────────────────────────────────
# Key rotation
# ─────────────────────────────────────────────────────────────────────────


class KeyRotationTests(_Base):
    def test_dek_rotation_new_writes_use_new_version(self):
        from app.encryption.field_fernet import FieldFernet, decode
        from app.encryption.vault_keys import get_vault_keys

        e = FieldFernet()
        ct1 = _async(e.encrypt("alice", "secret"))
        algo, ver1, _ = decode(ct1)
        self.assertEqual(ver1, 1)

        # Rotate; new writes should bump version.
        _async(get_vault_keys().rotate_dek("alice"))
        ct2 = _async(e.encrypt("alice", "secret"))
        algo, ver2, _ = decode(ct2)
        self.assertEqual(ver2, 2)

        # Old ciphertext still decrypts (retired DEK still in vault until purge).
        self.assertEqual(_async(e.decrypt("alice", ct1)), "secret")
        self.assertEqual(_async(e.decrypt("alice", ct2)), "secret")

    def test_kek_rotation_keeps_rows_readable(self):
        from app.encryption.field_fernet import FieldFernet
        from app.encryption.vault_keys import get_vault_keys

        e = FieldFernet()
        ct_alice = _async(e.encrypt("alice", "alpha"))
        ct_bob = _async(e.encrypt("bob", "beta"))

        # Rotate the KEK; every DEK in the vault gets re-wrapped.
        result = _async(get_vault_keys().rotate_kek())
        self.assertEqual(result["deks_rewrapped"], 2)

        # Caches are dropped so reads have to re-fetch & re-unwrap.
        self.assertEqual(_async(e.decrypt("alice", ct_alice)), "alpha")
        self.assertEqual(_async(e.decrypt("bob", ct_bob)), "beta")

    def test_operational_rotation_rewraps_workspace_files(self):
        from pathlib import Path
        from app.encryption.migration import rotate_dek
        from app.encryption.workspace_files import read_encrypted_file, write_encrypted_file

        old_base = os.environ.get("WEBAGENT_USER_DATA_DIR")
        os.environ["WEBAGENT_USER_DATA_DIR"] = self.tmpdir
        try:
            path = Path(self.tmpdir) / "alice" / "files" / "result.txt"
            _async(write_encrypted_file("alice", path, b"survives rotation"))
            result = _async(rotate_dek("alice"))
            self.assertTrue(result["ok"], result)
            self.assertEqual(result["workspace_files_rewrapped"], 1)
            self.assertEqual(
                _async(read_encrypted_file("alice", path)), b"survives rotation"
            )
        finally:
            if old_base is None:
                os.environ.pop("WEBAGENT_USER_DATA_DIR", None)
            else:
                os.environ["WEBAGENT_USER_DATA_DIR"] = old_base


# ─────────────────────────────────────────────────────────────────────────
# Workspace file envelope encryption
# ─────────────────────────────────────────────────────────────────────────


class WorkspaceFileEncryptionTests(_Base):
    def test_round_trip_uses_binary_envelope(self):
        from app.encryption.workspace_files import MAGIC, decrypt_bytes, encrypt_bytes

        plaintext = b"private workspace file\x00\xff"
        encrypted = _async(encrypt_bytes("alice", plaintext))
        self.assertTrue(encrypted.startswith(MAGIC))
        self.assertNotIn(plaintext, encrypted)
        self.assertEqual(_async(decrypt_bytes("alice", encrypted)), plaintext)

    def test_workspace_identity_is_authenticated(self):
        from app.encryption.workspace_files import (
            WorkspaceEncryptionError, decrypt_bytes, encrypt_bytes,
        )

        encrypted = _async(encrypt_bytes("alice", b"alice only"))
        with self.assertRaises(WorkspaceEncryptionError):
            _async(decrypt_bytes("bob", encrypted))

    def test_tampering_fails_closed(self):
        from app.encryption.workspace_files import (
            WorkspaceEncryptionError, decrypt_bytes, encrypt_bytes,
        )

        encrypted = bytearray(_async(encrypt_bytes("alice", b"authentic")))
        encrypted[-1] ^= 1
        with self.assertRaises(WorkspaceEncryptionError):
            _async(decrypt_bytes("alice", bytes(encrypted)))

    def test_legacy_plaintext_is_readable_and_can_migrate_in_place(self):
        from pathlib import Path
        from app.encryption.workspace_files import (
            MAGIC, encrypt_file_in_place, read_encrypted_file,
        )

        path = Path(self.tmpdir) / "legacy.txt"
        path.write_bytes(b"legacy")
        self.assertEqual(_async(read_encrypted_file("alice", path)), b"legacy")
        self.assertTrue(_async(encrypt_file_in_place("alice", path)))
        self.assertTrue(path.read_bytes().startswith(MAGIC))
        self.assertEqual(_async(read_encrypted_file("alice", path)), b"legacy")
        self.assertFalse(_async(encrypt_file_in_place("alice", path)))

    def test_dek_rotation_rewraps_only_the_object_key(self):
        from pathlib import Path
        from app.encryption.vault_keys import get_vault_keys
        from app.encryption.workspace_files import (
            read_encrypted_file, rewrap_file_key, write_encrypted_file,
        )

        path = Path(self.tmpdir) / "rotated.bin"
        _async(write_encrypted_file("alice", path, b"keep me"))
        before = path.read_bytes()
        _async(get_vault_keys().rotate_dek("alice"))
        self.assertTrue(_async(rewrap_file_key("alice", path)))
        after = path.read_bytes()
        self.assertNotEqual(after, before)
        self.assertEqual(_async(read_encrypted_file("alice", path)), b"keep me")
        self.assertFalse(_async(rewrap_file_key("alice", path)))

    def test_local_attachment_store_encrypts_disk_bytes_transparently(self):
        from pathlib import Path
        from app.attachments_store.local_store import LocalAttachmentStore
        from app.encryption.workspace_files import MAGIC

        store = LocalAttachmentStore(upload_dir=self.tmpdir)
        with mock.patch("app.content_access.artifact_url_for_path", return_value="/content/test"):
            result = _async(store.store("alice", b"upload secret", "note.txt", "text/plain"))
        disk_bytes = (Path(self.tmpdir) / result["storage_path"]).read_bytes()
        self.assertTrue(disk_bytes.startswith(MAGIC))
        self.assertNotIn(b"upload secret", disk_bytes)
        self.assertEqual(_async(store.read(result["storage_path"])), b"upload secret")

    def test_workspace_migration_excludes_live_database_and_genui_files(self):
        from pathlib import Path
        from app.encryption.workspace_files import MAGIC
        from app.encryption.workspace_migration import migrate_workspace

        old_base = os.environ.get("WEBAGENT_USER_DATA_DIR")
        os.environ["WEBAGENT_USER_DATA_DIR"] = self.tmpdir
        try:
            home = Path(self.tmpdir) / "alice"
            (home / "files").mkdir(parents=True)
            (home / "genui" / "page").mkdir(parents=True)
            artifact = home / "files" / "report.txt"
            database = home / "alice.db"
            genui = home / "genui" / "page" / "index.html"
            artifact.write_bytes(b"report")
            database.write_bytes(b"sqlite")
            genui.write_bytes(b"<html></html>")

            result = _async(migrate_workspace("alice"))
            self.assertTrue(result["ok"], result)
            self.assertEqual(result["files_encrypted"], 1)
            self.assertTrue(artifact.read_bytes().startswith(MAGIC))
            self.assertEqual(database.read_bytes(), b"sqlite")
            self.assertEqual(genui.read_bytes(), b"<html></html>")
        finally:
            if old_base is None:
                os.environ.pop("WEBAGENT_USER_DATA_DIR", None)
            else:
                os.environ["WEBAGENT_USER_DATA_DIR"] = old_base


# ─────────────────────────────────────────────────────────────────────────
# EncryptedStorageBackend decorator
# ─────────────────────────────────────────────────────────────────────────


class DecoratorTests(_Base):
    def test_round_trip_through_decorator(self):
        from app.db.interface import EncryptedStorageBackend
        from app.encryption.field_fernet import FieldFernet

        wrapped = EncryptedStorageBackend(self._local_backend, FieldFernet())

        _async(wrapped.auth_element_set(
            user_id="alice",
            service="google",
            config={"client_id": "abc"},
            secret_ref='{"access_token": "AT_alice"}',
            label="oauth",
        ))

        # Raw row should be ciphertext.
        raw = _async(self._local_backend.auth_element_get("alice", "google", "oauth"))
        self.assertTrue(raw["secret_ref"].startswith("enc:v1:"))

        # Wrapped read decrypts transparently.
        clean = _async(wrapped.auth_element_get("alice", "google", "oauth"))
        self.assertEqual(clean["secret_ref"], '{"access_token": "AT_alice"}')

    def test_tenant_isolation_through_decorator(self):
        from app.db.interface import EncryptedStorageBackend
        from app.encryption.field_fernet import FieldFernet

        wrapped = EncryptedStorageBackend(self._local_backend, FieldFernet())
        _async(wrapped.auth_element_set("alice", "google", {}, '{"t":"a"}', "oauth"))
        _async(wrapped.auth_element_set("bob",   "google", {}, '{"t":"b"}', "oauth"))

        a = _async(wrapped.auth_element_get("alice", "google", "oauth"))
        b = _async(wrapped.auth_element_get("bob",   "google", "oauth"))
        self.assertEqual(a["secret_ref"], '{"t":"a"}')
        self.assertEqual(b["secret_ref"], '{"t":"b"}')

        # Raw ciphertexts must differ.
        raw_a = _async(self._local_backend.auth_element_get("alice", "google", "oauth"))
        raw_b = _async(self._local_backend.auth_element_get("bob",   "google", "oauth"))
        self.assertNotEqual(raw_a["secret_ref"], raw_b["secret_ref"])

    def test_failed_decryption_is_marked_without_exposing_ciphertext(self):
        from unittest.mock import AsyncMock

        from app.db.interface import EncryptedStorageBackend

        _async(self._local_backend.auth_element_set(
            "alice", "llm", {}, "enc:v1:fernet:unreadable", "default",
        ))
        enc = AsyncMock()
        enc.decrypt.return_value = "enc:v1:fernet:unreadable"
        wrapped = EncryptedStorageBackend(self._local_backend, enc)

        row = _async(wrapped.auth_element_get("alice", "llm", "default"))

        self.assertEqual(row["secret_ref"], "")
        self.assertEqual(row["_secret_error"], "decrypt_failed")

    def test_vault_sentinel_bypasses_encryption(self):
        from app.db.interface import EncryptedStorageBackend, _VAULT_SENTINEL_USER, _VAULT_SENTINEL_SERVICE
        from app.encryption.field_fernet import FieldFernet

        wrapped = EncryptedStorageBackend(self._local_backend, FieldFernet())
        _async(wrapped.auth_element_set(
            user_id=_VAULT_SENTINEL_USER,
            service=_VAULT_SENTINEL_SERVICE,
            config={},
            secret_ref="raw-wrapped-dek-bytes",
            label="wa:dek:alice:v1",
        ))
        raw = _async(self._local_backend.auth_element_get(
            _VAULT_SENTINEL_USER, _VAULT_SENTINEL_SERVICE, "wa:dek:alice:v1",
        ))
        # Stored unchanged — no enc: prefix.
        self.assertEqual(raw["secret_ref"], "raw-wrapped-dek-bytes")

    def test_list_decrypts(self):
        from app.db.interface import EncryptedStorageBackend
        from app.encryption.field_fernet import FieldFernet

        wrapped = EncryptedStorageBackend(self._local_backend, FieldFernet())
        _async(wrapped.auth_element_set("alice", "google", {}, "tok-g", "oauth"))
        _async(wrapped.auth_element_set("alice", "github", {}, "tok-h", "oauth"))

        items = _async(wrapped.auth_element_list("alice"))
        secrets = {item["service"]: item["secret_ref"] for item in items}
        self.assertEqual(secrets["google"], "tok-g")
        self.assertEqual(secrets["github"], "tok-h")


# ─────────────────────────────────────────────────────────────────────────
# Migration loops
# ─────────────────────────────────────────────────────────────────────────


class MigrationTests(_Base):
    def test_encrypt_all_then_decrypt_all(self):
        from app.encryption import set_level, get_encryption
        from app.encryption.migration import encrypt_all, decrypt_all

        # Seed plaintext rows directly via the unwrapped backend.
        _async(self._local_backend.auth_element_set("alice", "google", {}, "plain-alice", "oauth"))
        _async(self._local_backend.auth_element_set("bob",   "google", {}, "plain-bob",   "oauth"))

        # Bootstrap a user_profiles row so _all_users() picks them up.
        # Insert directly (skipping the abstract method).
        raw = self._local_backend.get_raw_client()
        raw.table("user_profiles").insert({"user_id": "alice"}).execute()
        raw.table("user_profiles").insert({"user_id": "bob"}).execute()

        # Switch encryption on.
        set_level("field")
        result = _async(encrypt_all())
        self.assertTrue(result["ok"], result)
        self.assertEqual(result["rows_encrypted"], 2)

        # Re-running is idempotent.
        result2 = _async(encrypt_all())
        self.assertEqual(result2["rows_encrypted"], 0)

        # Raw rows are ciphertext.
        a = _async(self._local_backend.auth_element_get("alice", "google", "oauth"))
        self.assertTrue(a["secret_ref"].startswith("enc:v1:"))

        # Decrypt-all restores plaintext.
        decrypt_result = _async(decrypt_all())
        self.assertTrue(decrypt_result["ok"], decrypt_result)
        a2 = _async(self._local_backend.auth_element_get("alice", "google", "oauth"))
        self.assertEqual(a2["secret_ref"], "plain-alice")


# ─────────────────────────────────────────────────────────────────────────
# Factory state
# ─────────────────────────────────────────────────────────────────────────


class FactoryTests(_Base):
    def test_set_level_persists_and_resets_backend(self):
        from app.encryption import (
            set_level, get_level, get_encryption, list_levels,
        )
        self.assertIn("field", list_levels())
        # Default level is "none" with no encryption_mode.json in cwd.
        before = get_encryption()
        from app.encryption.none import NoEncryption
        self.assertIsInstance(before, NoEncryption)

        set_level("field")
        self.assertEqual(get_level(), "field")
        after = get_encryption()
        from app.encryption.field_fernet import FieldFernet
        self.assertIsInstance(after, FieldFernet)


if __name__ == "__main__":
    unittest.main()

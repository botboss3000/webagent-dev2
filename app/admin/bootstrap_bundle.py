"""
Bootstrap bundle — one encrypted "setup code" that provisions a fresh install.

WHAT THIS IS (in plain terms)
-----------------------------
An admin can package the pieces that make THIS install what it is — its database
connection, its secrets-vault choice, and its LLM provider + API key + preset
model roster — into a single portable **code**. That code is carried to a
freshly-cloned install (pasted on the setup page, pasted later in Data
Settings, or dropped next to the clone as ``bootstrap.json``) where it is
decoded and applied, so the new box comes up already configured.

TWO CODE FORMATS
----------------
The Data Settings Export/Import UI uses **keyless** ``WABOOT2`` codes
(``encrypt_code_open`` / ``decrypt_code_open``): the encryption key travels
with the code itself, so there's no password to type. This isn't a weaker
lock — the export/import endpoints already require an authenticated admin, so
a second shared secret was pure friction, not real security. These codes never
carry the admin login (a real password can't be recovered from its stored
hash without asking the admin to type it, and this path no longer does).

The legacy **password-locked** ``WABOOT1`` format (``encrypt_code`` /
``decrypt_code``, PBKDF2-HMAC-SHA256 → Fernet) still exists for the
unauthenticated, fresh-install boot path: a hand-authored ``bootstrap.json``
next to the clone, decoded with the ``BOOTSTRAP_ADMIN_PASSWORD`` env var (see
``apply_boot_file``). That path has no logged-in admin to gate on yet, so the
password still does double duty there — it unlocks the bundle AND, if an
``admin`` section is present, becomes the new install's admin login.

DELIBERATELY NOT A PLUGIN
-------------------------
This is an admin/deploy provisioning concern that reaches into core config
(DB / vault / LLM / auth), not a drop-in capability, so it lives here in
``app/admin/`` rather than under ``plugins/``. It calls the SAME save paths the
admin UI uses (``_persist_llm_config``, ``save_config``, ``set_secrets_*``,
``register_user``) — it never invents a second way to store anything.

Crypto note: server-side on purpose. Browser ``crypto.subtle`` is unavailable on
plain-http LAN addresses (see docs/claude/deployment.md "Secure-context APIs"),
and the exact case this feature serves is moving config between devices — so all
encrypt/decrypt happens here in Python using ``cryptography`` (already a runtime
dep for the vault), never in the page.
"""

from __future__ import annotations

import base64
import json
import logging
import os
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

logger = logging.getLogger(__name__)

# ── Bundle constants ────────────────────────────────────────────────────────

CODE_PREFIX = "WABOOT1"          # legacy password-locked codes (bootstrap.json + BOOTSTRAP_ADMIN_PASSWORD boot path only)
CODE_PREFIX_OPEN = "WABOOT2"     # keyless codes — used by the Data Settings Export/Import UI (no password needed;
                                 # those endpoints are already admin-only, so there's no separate lock to add)
BUNDLE_VERSION = 2
_KDF_ITERATIONS = 200_000
_SALT_BYTES = 16

# The sections a bundle can carry. Order = the order they are applied.
SECTION_ADMIN = "admin"
SECTION_LLM = "llm"
SECTION_MODEL_ROSTERS = "model_rosters"
SECTION_EXPERIENCE_TIERS = "experience_tiers"
SECTION_DATABASE = "database"
SECTION_VAULT = "vault"
ALL_SECTIONS = (
    SECTION_ADMIN,
    SECTION_LLM,
    SECTION_MODEL_ROSTERS,
    SECTION_EXPERIENCE_TIERS,
    SECTION_DATABASE,
    SECTION_VAULT,
)

# Per-section merge choices when importing into an install that already has data.
MERGE_FILL = "fill"       # add only what's missing; keep existing values
MERGE_OVERWRITE = "overwrite"
MERGE_SKIP = "skip"

ADMIN_USER_ID = "admin"


class BundleError(Exception):
    """Raised for a malformed code or the wrong password (kept opaque to the
    caller so we never leak which of the two it was)."""


# ── Crypto: password → key, encrypt / decrypt ───────────────────────────────

def _derive_key(password: str, salt: bytes) -> bytes:
    """PBKDF2-HMAC-SHA256(password, salt) → a urlsafe-base64 Fernet key."""
    from cryptography.hazmat.primitives import hashes
    from cryptography.hazmat.primitives.kdf.pbkdf2 import PBKDF2HMAC

    kdf = PBKDF2HMAC(
        algorithm=hashes.SHA256(),
        length=32,
        salt=salt,
        iterations=_KDF_ITERATIONS,
    )
    return base64.urlsafe_b64encode(kdf.derive((password or "").encode("utf-8")))


def encrypt_code(plaintext: str, password: str) -> str:
    """Encrypt ``plaintext`` under ``password`` → a portable code string.

    Format: ``WABOOT1.<b64url(salt)>.<fernet-token>``. The salt travels with the
    code (it's not secret); only the password unlocks it.
    """
    from cryptography.fernet import Fernet

    salt = os.urandom(_SALT_BYTES)
    token = Fernet(_derive_key(password, salt)).encrypt(plaintext.encode("utf-8"))
    salt_b64 = base64.urlsafe_b64encode(salt).decode("ascii").rstrip("=")
    return f"{CODE_PREFIX}.{salt_b64}.{token.decode('ascii')}"


def decrypt_code(code: str, password: str) -> str:
    """Reverse :func:`encrypt_code`. Raises :class:`BundleError` on a bad code or
    a wrong password."""
    from cryptography.fernet import Fernet, InvalidToken

    s = (code or "").strip()
    parts = s.split(".")
    if len(parts) != 3 or parts[0] != CODE_PREFIX:
        raise BundleError("This doesn't look like a WebAgent setup code.")
    try:
        pad = "=" * (-len(parts[1]) % 4)
        salt = base64.urlsafe_b64decode(parts[1] + pad)
        token = parts[2].encode("ascii")
    except Exception as e:
        raise BundleError("The setup code is corrupted.") from e
    try:
        raw = Fernet(_derive_key(password, salt)).decrypt(token)
    except (InvalidToken, Exception) as e:
        raise BundleError("Wrong password, or the code was tampered with.") from e
    return raw.decode("utf-8")


def encrypt_code_open(plaintext: str) -> str:
    """Encrypt ``plaintext`` with a random key that travels WITH the code — no
    password needed to decode. Used by the Data Settings Export/Import UI: those
    endpoints already require an admin bearer token, so a second shared secret
    doesn't add security, only friction. Format:
    ``WABOOT2.<b64url(key)>.<fernet-token>``.
    """
    from cryptography.fernet import Fernet

    key = Fernet.generate_key()
    token = Fernet(key).encrypt(plaintext.encode("utf-8"))
    key_b64 = key.decode("ascii").rstrip("=")
    return f"{CODE_PREFIX_OPEN}.{key_b64}.{token.decode('ascii')}"


def decrypt_code_open(code: str) -> str:
    """Reverse :func:`encrypt_code_open`. Raises :class:`BundleError` on a
    malformed or tampered code."""
    from cryptography.fernet import Fernet, InvalidToken

    s = (code or "").strip()
    parts = s.split(".")
    if len(parts) != 3 or parts[0] != CODE_PREFIX_OPEN:
        raise BundleError("This doesn't look like a WebAgent setup code.")
    try:
        pad = "=" * (-len(parts[1]) % 4)
        key = (parts[1] + pad).encode("ascii")
        token = parts[2].encode("ascii")
    except Exception as e:
        raise BundleError("The setup code is corrupted.") from e
    try:
        raw = Fernet(key).decrypt(token)
    except (InvalidToken, Exception) as e:
        raise BundleError("The setup code is corrupted or was tampered with.") from e
    return raw.decode("utf-8")


# ── Admin-password check (still used by _section_present / the boot-file path) ──

async def has_admin_password() -> bool:
    """True when an admin account exists at all (i.e. a password is set)."""
    try:
        from app.auth import users
        return await users.get_user_by_id(ADMIN_USER_ID) is not None
    except Exception:
        return False


# ── Gather: build a bundle from THIS install ────────────────────────────────

async def _gather_admin(password_plain: str) -> Optional[dict]:
    from app.auth import users
    admin = await users.get_user_by_id(ADMIN_USER_ID)
    if admin is None:
        return None
    return {
        "username": admin.username,
        "display_name": admin.display_name,
        "password": password_plain,   # the verified plaintext the admin just typed
    }


async def _gather_llm() -> Optional[dict]:
    """Legacy-compatible LLM config, preferring the named platform default."""
    try:
        from app.entitlements.rosters import (
            PLATFORM_DEFAULT_ROSTER_ID,
            load_platform_roster_config,
        )
        cfg = await load_platform_roster_config(PLATFORM_DEFAULT_ROSTER_ID)
        if cfg:
            return cfg
        from app.admin.settings import _load_own_llm_config
        cfg = await _load_own_llm_config(ADMIN_USER_ID)
    except Exception as e:
        logger.warning("bootstrap: could not read LLM config: %s", e)
        return None
    return cfg if isinstance(cfg, dict) and cfg else None


async def _gather_model_rosters(*, strip_secrets: bool = False) -> Optional[dict]:
    """Export named roster shape plus ID-keyed encrypted-vault credentials.

    The returned object is itself placed inside the encrypted setup code. The
    deploy/embed path may request a keyless shape when the destination already
    shares the source vault.
    """
    try:
        from app.entitlements.rosters import export_platform_rosters
        payload = await export_platform_rosters(strip_secrets=strip_secrets)
    except Exception as e:
        logger.warning("bootstrap: could not read named model rosters: %s", e)
        return None
    return payload if payload.get("rosters") else None


async def _gather_experience_tiers() -> Optional[dict]:
    """Export policy definitions only; assignments and audit never travel."""
    try:
        from app.entitlements.tiers import export_experience_tiers
        payload = await export_experience_tiers()
    except Exception as e:
        logger.warning("bootstrap: could not read experience tiers: %s", e)
        return None
    return payload if payload.get("tiers") else None


def _strip_llm_section_secrets(llm: dict) -> dict:
    """Return the LLM section with every API key blanked, keeping the non-secret
    provider / default model / saved-model roster.

    Used by the deploy embed path (``config_embed``) when the new server will run
    on THIS app's shared database + inline vault: there the keys already resolve
    from shared infrastructure on boot, so re-shipping them in the bundle is
    redundant. Reuses the SAME stripper the settings save path uses so the shape
    stays identical. Fail-safe: if stripping can't complete, keep the working
    keys — a redundant embed is harmless; a half-stripped one might not be."""
    try:
        from app.admin.settings import _strip_provider_secrets
        return _strip_provider_secrets(llm)
    except Exception as e:
        logger.warning("bootstrap: could not strip LLM keys, keeping them: %s", e)
        return llm


async def _gather_database() -> Optional[dict]:
    """DB connection config + the resolved password (from the credential store)."""
    try:
        from app.db.connection_config import load_config
        cfg = load_config()
    except Exception as e:
        logger.warning("bootstrap: could not read DB config: %s", e)
        return None
    d = cfg.to_dict()
    # Resolve the real password so the clone can connect without re-typing it.
    try:
        from app.db import cred_store
        key = getattr(cfg, "password_secret_key", None)
        if key:
            pw = cred_store.get_secret(key)
            if pw:
                d["password"] = pw
    except Exception as e:
        logger.warning("bootstrap: could not resolve DB secret: %s", e)
    return d


async def _gather_vault() -> Optional[dict]:
    """Vault provider mode + its non-secret connection config. The vault's OWN
    access token is stored keyring-first and is not read back here — the note in
    the UI tells the admin to re-enter it on the new device if it's a remote
    vault."""
    try:
        from app.secrets import get_secrets_status
        st = get_secrets_status() or {}
    except Exception as e:
        logger.warning("bootstrap: could not read vault status: %s", e)
        return None
    mode = st.get("mode") or st.get("provider")
    if not mode:
        return None
    return {"mode": mode, "config": st.get("config") or {}}


async def gather_bundle(sections: List[str], *, strip_llm_secrets: bool = False) -> dict:
    """Assemble the requested sections into a bundle dict (pre-encryption).

    Admin login is NOT gathered here. The Export/Import UI can't recover a
    plaintext password from a stored hash and no longer asks for it; the deploy
    path doesn't need to carry the admin at all, because logins live in the shared
    ``user_accounts`` table and so travel with the ``database`` section itself. (A
    plaintext ``admin`` section is still honoured on the *apply* side for a
    hand-authored ``bootstrap.json`` — see ``apply_boot_file``.)

    ``strip_llm_secrets`` blanks the LLM API keys while keeping the rest of the
    LLM section. The deploy embed path sets this when the new server will read the
    keys from a shared database + inline vault anyway (see ``config_embed``); the
    generic Export/Import path leaves it off so a code pasted into an unrelated
    box still carries working keys.
    """
    out: Dict[str, Any] = {}
    wanted = set(sections or [])
    if SECTION_LLM in wanted:
        l = await _gather_llm()
        if l:
            if strip_llm_secrets:
                l = _strip_llm_section_secrets(l)
            out[SECTION_LLM] = l
        # Selecting LLM configuration carries the first-class named rosters as
        # well. Older v1 bundles without this section remain importable.
        r = await _gather_model_rosters(strip_secrets=strip_llm_secrets)
        if r:
            out[SECTION_MODEL_ROSTERS] = r
        t = await _gather_experience_tiers()
        if t:
            out[SECTION_EXPERIENCE_TIERS] = t
    elif SECTION_MODEL_ROSTERS in wanted:
        r = await _gather_model_rosters(strip_secrets=strip_llm_secrets)
        if r:
            out[SECTION_MODEL_ROSTERS] = r
        t = await _gather_experience_tiers()
        if t:
            out[SECTION_EXPERIENCE_TIERS] = t
    if SECTION_EXPERIENCE_TIERS in wanted and SECTION_EXPERIENCE_TIERS not in out:
        t = await _gather_experience_tiers()
        if t:
            out[SECTION_EXPERIENCE_TIERS] = t
    if SECTION_DATABASE in wanted:
        d = await _gather_database()
        if d:
            out[SECTION_DATABASE] = d
    if SECTION_VAULT in wanted:
        v = await _gather_vault()
        if v:
            out[SECTION_VAULT] = v
    return {"v": BUNDLE_VERSION, "sections": out}


async def export_code(sections: List[str], *, strip_llm_secrets: bool = False) -> str:
    """Gather the chosen sections and return a portable code. No password is
    required — the export endpoint is already admin-only.

    ``strip_llm_secrets`` is forwarded to :func:`gather_bundle`; the deploy embed
    path passes it when the LLM keys are already resolvable from shared infra."""
    bundle = await gather_bundle(sections, strip_llm_secrets=strip_llm_secrets)
    if not bundle.get("sections"):
        raise BundleError("Nothing to share — none of the selected sections had any data.")
    return encrypt_code_open(json.dumps(bundle, separators=(",", ":")))


async def export_locked_code(sections: List[str], password: str) -> str:
    """Gather the chosen sections and return a PASSWORD-LOCKED ``WABOOT1`` code.

    NOTE: the Deploy "Include this app's configuration" path no longer uses this —
    it embeds a keyless ``WABOOT2`` code (:func:`export_code`) so it doesn't need
    an admin password (see ``app/deploy/config_embed``). This helper is retained
    only for producing a hand-authored, password-locked ``bootstrap.json`` for the
    ``BOOTSTRAP_ADMIN_PASSWORD`` boot path (``apply_boot_file`` still decodes such a
    file). Admin login is never part of the bundle; only the requested
    llm/database/vault sections travel.
    """
    if not (password or "").strip():
        raise BundleError("A password is required to lock the configuration bundle.")
    bundle = await gather_bundle(sections)
    if not bundle.get("sections"):
        raise BundleError("None of the selected sections had any data to include.")
    return encrypt_code(json.dumps(bundle, separators=(",", ":")), password)


# ── Decode + preview ────────────────────────────────────────────────────────

def decode_bundle(code: str, password: str = "") -> dict:
    """Decrypt + parse a code into its bundle dict. Raises BundleError.

    Keyless ``WABOOT2`` codes (from the Export/Import UI) need no password —
    the key travels with the code. Legacy password-locked ``WABOOT1`` codes
    (from ``BOOTSTRAP_ADMIN_PASSWORD``-driven boot-file provisioning) still do.
    """
    s = (code or "").strip()
    raw = decrypt_code_open(s) if s.startswith(CODE_PREFIX_OPEN + ".") else decrypt_code(s, password)
    try:
        obj = json.loads(raw)
    except Exception as e:
        raise BundleError("The setup code decoded but its contents are unreadable.") from e
    if not isinstance(obj, dict) or "sections" not in obj:
        raise BundleError("The setup code is not a valid bundle.")
    return obj


def _summarize_section(name: str, data: dict) -> str:
    """A short human line describing what a section would bring in (no secrets)."""
    if name == SECTION_ADMIN:
        return f"Admin login “{data.get('username', 'admin')}” (+ password)"
    if name == SECTION_LLM:
        prov = data.get("provider") or "?"
        model = data.get("model") or "?"
        roster = len(data.get("multi_providers") or [])
        key = "with API key" if data.get("api_key") else "no key"
        return f"LLM: {prov} · default model {model} · {roster} saved model(s) · {key}"
    if name == SECTION_MODEL_ROSTERS:
        rosters = data.get("rosters") if isinstance(data, dict) else []
        count = len(rosters) if isinstance(rosters, list) else 0
        return f"Named platform model rosters: {count}"
    if name == SECTION_EXPERIENCE_TIERS:
        tiers = data.get("tiers") if isinstance(data, dict) else []
        count = len(tiers) if isinstance(tiers, list) else 0
        return f"Experience tiers: {count}"
    if name == SECTION_DATABASE:
        prov = data.get("provider") or "?"
        return f"Database: {prov}" + (" (+ password)" if data.get("password") else "")
    if name == SECTION_VAULT:
        return f"Secrets vault: {data.get('mode', '?')}"
    return name


async def preview(code: str, password: str = "") -> dict:
    """Decode a code and report, per section, what it carries and whether this
    install already has that thing configured (so the UI can offer fill/overwrite/
    skip). Never applies anything."""
    bundle = decode_bundle(code, password)
    sections = bundle.get("sections") or {}
    rows = []
    for name in ALL_SECTIONS:
        if name not in sections:
            continue
        rows.append({
            "section": name,
            "summary": _summarize_section(name, sections[name]),
            "present_now": await _section_present(name),
        })
    return {"sections": rows, "initialized": await _is_initialized()}


async def _section_present(name: str) -> bool:
    """Does this install already have this section configured?"""
    if name == SECTION_ADMIN:
        return await has_admin_password()
    if name == SECTION_LLM:
        cfg = await _gather_llm()
        return bool(cfg and (cfg.get("api_key") or cfg.get("model")))
    if name == SECTION_MODEL_ROSTERS:
        return bool(await _gather_model_rosters(strip_secrets=True))
    if name == SECTION_EXPERIENCE_TIERS:
        return bool(await _gather_experience_tiers())
    if name == SECTION_DATABASE:
        try:
            from app.db.connection_config import load_config
            return bool(getattr(load_config(), "provider", None))
        except Exception:
            return False
    if name == SECTION_VAULT:
        v = await _gather_vault()
        return bool(v and v.get("mode"))
    return False


async def _is_initialized() -> bool:
    try:
        from app.auth import users
        return await users.admin_exists()
    except Exception:
        return False


# ── Apply ───────────────────────────────────────────────────────────────────

async def _apply_admin(data: dict, choice: str) -> str:
    # Only reached for a hand-authored bootstrap.json that includes a plaintext
    # ``admin`` section (the deploy path carries no admin — logins live in the
    # shared user_accounts table, so a shared-DB box already has it).
    from app.auth import users
    username = data.get("username") or "admin"
    password = data.get("password") or ""
    display = data.get("display_name") or "Admin"
    if not password:
        return "skipped (no password in bundle)"
    if not await users.admin_exists():
        await users.register_user(username, password, display_name=display,
                                  is_approved=True, user_id=ADMIN_USER_ID)
        return f"created admin “{username}”"
    # Admin already exists.
    if choice == MERGE_OVERWRITE:
        admin = await users.get_user_by_id(ADMIN_USER_ID)
        if admin:
            await users.change_password(admin.username, password)
            return "reset existing admin password"
    return "kept existing admin"


async def _apply_llm(data: dict, choice: str) -> str:
    from app.admin.settings import _load_own_llm_config, _persist_llm_config
    incoming = dict(data)
    if choice == MERGE_OVERWRITE:
        merged = incoming
    else:  # fill blanks
        current = await _load_own_llm_config(ADMIN_USER_ID) or {}
        merged = dict(current)
        for k, v in incoming.items():
            if not merged.get(k) and v:
                merged[k] = v
        # If the current config has no usable model roster, take the bundle's.
        if not (current.get("multi_providers")) and incoming.get("multi_providers"):
            merged["multi_providers"] = incoming["multi_providers"]
    await _persist_llm_config(ADMIN_USER_ID, merged)
    return f"LLM configured ({merged.get('provider', '?')} · {merged.get('model', '?')})"


async def _apply_model_rosters(data: dict, choice: str) -> str:
    from app.entitlements.rosters import import_platform_rosters
    count = await import_platform_rosters(
        data,
        overwrite=choice == MERGE_OVERWRITE,
    )
    return f"{count} named model roster(s) imported"


async def _apply_experience_tiers(data: dict, choice: str) -> str:
    from app.entitlements.tiers import import_experience_tiers
    count = await import_experience_tiers(
        data,
        overwrite=choice == MERGE_OVERWRITE,
    )
    return f"{count} experience tier(s) imported"


async def _apply_database(data: dict, choice: str, activate: bool) -> str:
    from app.db.connection_config import DBConnectionConfig, load_config, save_config
    from app.db import cred_store
    if choice == MERGE_FILL and _section_present_sync_db():
        return "kept existing database"
    d = dict(data)
    password = d.pop("password", None)
    cfg = DBConnectionConfig(
        provider=d.get("provider"),
        host=d.get("host"), port=d.get("port"),
        database=d.get("database"), username=d.get("username"),
        ssl_mode=d.get("ssl_mode"), schema=d.get("schema"),
        options=d.get("options"),
    )
    if password:
        key = d.get("password_secret_key") or f"db_password_{cfg.provider}"
        cred_store.set_secret(key, password)
        cfg.password_secret_key = key
    save_config(cfg)
    msg = f"saved database config ({cfg.provider})"
    if activate:
        # Treat importing a bundle exactly like clicking the Database-page
        # Activate button — flip the live backend on now so a fresh clone comes
        # up connected with no manual step. Reuses the SAME activation path
        # (password resolve → env → rebuild → verify) for every provider, not
        # just sqlite. A failure here is reported, not fatal to the rest of the
        # import (the admin can still re-Activate from the Database page).
        try:
            from app.admin.storage import _activate_saved_backend
            payload, status = await _activate_saved_backend()
            if status == 200:
                msg += " + activated"
            else:
                msg += f" (saved, but activation deferred: {payload.get('error', 'not switched')})"
        except Exception as e:
            logger.warning("bootstrap: DB activation after import failed: %s", e)
            msg += " (saved, activation deferred)"
    return msg


def _section_present_sync_db() -> bool:
    """Does this install have a database DELIBERATELY configured?

    A brand-new clone has no ``db_connection.json`` on disk — ``load_config()``
    still hands back the built-in default (``provider="sqlite"``), which is
    truthy. Trusting that default would make ``_apply_database`` treat every fresh
    box as "already configured" and silently drop a carried database on a
    fill-merge (the exact bug that stopped deploy bundles from bringing their DB
    across). So "present" means a config file was actually SAVED, not merely the
    in-memory default.
    """
    try:
        import os
        from app.db import connection_config as cc
        return os.path.exists(cc._CONFIG_FILE)
    except Exception:
        return False


async def _apply_vault(data: dict, choice: str) -> str:
    from app.secrets import set_mode as set_secrets_mode, set_provider_config
    mode = data.get("mode")
    if not mode:
        return "skipped (no vault mode)"
    try:
        set_secrets_mode(mode)
        if data.get("config"):
            set_provider_config(mode, data["config"])
    except Exception as e:
        return f"vault not applied ({e})"
    return f"secrets vault set to {mode}"


async def apply_bundle(bundle: dict, choices: Optional[Dict[str, str]] = None,
                       *, activate_db: bool = False) -> dict:
    """Apply a decoded bundle's sections, each honouring its merge choice.

    ``choices`` maps section → fill/overwrite/skip (default fill). ``activate_db``
    switches the live backend after saving (used on the fresh-install / boot path,
    where the admin isn't there to click Activate).
    """
    choices = choices or {}
    sections = bundle.get("sections") or {}
    results: Dict[str, str] = {}
    for name in ALL_SECTIONS:
        if name not in sections:
            continue
        choice = choices.get(name, MERGE_FILL)
        if choice == MERGE_SKIP:
            results[name] = "skipped"
            continue
        try:
            if name == SECTION_ADMIN:
                results[name] = await _apply_admin(sections[name], choice)
            elif name == SECTION_LLM:
                results[name] = await _apply_llm(sections[name], choice)
            elif name == SECTION_MODEL_ROSTERS:
                results[name] = await _apply_model_rosters(sections[name], choice)
            elif name == SECTION_EXPERIENCE_TIERS:
                results[name] = await _apply_experience_tiers(sections[name], choice)
            elif name == SECTION_DATABASE:
                results[name] = await _apply_database(sections[name], choice, activate_db)
            elif name == SECTION_VAULT:
                results[name] = await _apply_vault(sections[name], choice)
        except Exception as e:
            logger.warning("bootstrap: applying %s failed: %s", name, e)
            results[name] = f"error: {e}"
    return {"ok": True, "results": results}


# ── Boot-time file path (bootstrap.json next to the clone) ───────────────────

def _repo_root() -> Path:
    # app/admin/bootstrap_bundle.py → parents[2] = repo root
    return Path(__file__).resolve().parents[2]


def _boot_file() -> Optional[Path]:
    for p in (_repo_root() / "bootstrap.json", _repo_root() / "data" / "bootstrap.json"):
        if p.exists():
            return p
    return None


async def apply_boot_file() -> Optional[dict]:
    """On startup: if a ``bootstrap.json`` or ``WA_BOOTSTRAP_CODE`` environment
    value is present AND the install is not yet initialized, provision from it,
    then scrub the LLM key out of a file-backed bundle.

    The file may hold any of three things:
      * a **keyless ``WABOOT2`` code** — what the Deploy panel's "Include this
        app's configuration" writes; it decodes on its own, no password;
      * a legacy **password-locked ``WABOOT1`` code** — decoded with
        ``BOOTSTRAP_ADMIN_PASSWORD`` (the same env var that presets the admin),
        for a hand-authored encrypted ``bootstrap.json``;
      * a raw (unencrypted) bundle JSON — for a trusted operator who secures the
        file themselves.
    Applying only ever happens on a fresh install (no LLM configured yet), so it
    never clobbers a configured box.
    """
    path = _boot_file()
    env_text = os.environ.get("WA_BOOTSTRAP_CODE", "").strip()
    if path is None and not env_text:
        return None
    # Freshness gate: an already-provisioned box has an LLM config. We deliberately
    # do NOT gate on admin_exists() here — an encrypted boot file is decrypted with
    # BOOTSTRAP_ADMIN_PASSWORD, and that same env var makes users.py create the
    # admin at import time, so the admin can already exist on a genuinely fresh
    # clone. Keying on "no LLM configured yet" avoids clobbering a real install
    # while still letting the intended fresh-clone provisioning through.
    if await _section_present(SECTION_LLM):
        logger.info("bootstrap setup present but install already has an LLM config — ignoring.")
        return None

    if path is not None:
        try:
            text = path.read_text(encoding="utf-8").strip()
        except Exception as e:
            logger.warning("bootstrap: could not read %s: %s", path, e)
            return None
        source_name = path.name
    else:
        text = env_text
        source_name = "WA_BOOTSTRAP_CODE"

    bundle: Optional[dict] = None
    if text.startswith(CODE_PREFIX_OPEN + "."):
        # Keyless code (the Deploy "Include this app's configuration" path) — the
        # key travels with the code, so no BOOTSTRAP_ADMIN_PASSWORD is needed.
        try:
            bundle = decode_bundle(text)
        except BundleError as e:
            logger.warning("bootstrap: could not decode %s: %s", source_name, e)
            return None
    elif text.startswith(CODE_PREFIX + "."):
        pw = os.environ.get("BOOTSTRAP_ADMIN_PASSWORD", "")
        if not pw:
            logger.warning("bootstrap.json is a password-locked code but BOOTSTRAP_ADMIN_PASSWORD is unset — cannot decode.")
            return None
        try:
            bundle = decode_bundle(text, pw)
        except BundleError as e:
            logger.warning("bootstrap: could not decode %s: %s", source_name, e)
            return None
    else:
        try:
            obj = json.loads(text)
            bundle = obj if isinstance(obj, dict) and "sections" in obj else {"v": BUNDLE_VERSION, "sections": obj}
        except Exception as e:
            logger.warning("bootstrap: %s is neither a code nor valid JSON: %s", source_name, e)
            return None

    bundle = _strip_comment_keys(bundle)
    logger.info("bootstrap: provisioning fresh install from %s", source_name)
    res = await apply_bundle(bundle, choices=None, activate_db=True)
    if path is not None:
        _scrub_boot_file(path, bundle)
    return res


def _strip_comment_keys(obj: Any) -> Any:
    """Recursively drop dict keys starting with "_" so the hand-editable
    bootstrap.example.json template's ``_readme``/``_comment`` notes never reach
    the apply path."""
    if isinstance(obj, dict):
        return {k: _strip_comment_keys(v) for k, v in obj.items() if not str(k).startswith("_")}
    if isinstance(obj, list):
        return [_strip_comment_keys(v) for v in obj]
    return obj


def _scrub_boot_file(path: Path, bundle: dict) -> None:
    """Once secrets are safely in the vault/keyring, remove the plaintext LLM key
    from the file (the vault copy is the durable one). A best-effort courtesy — a
    failure here is logged, not fatal."""
    try:
        sections = bundle.get("sections") or {}
        llm = sections.get(SECTION_LLM)
        named = sections.get(SECTION_MODEL_ROSTERS)
        named_rows = named.get("rosters") if isinstance(named, dict) else []
        has_named_secret = any(
            isinstance(row, dict) and row.get("credential_bundle")
            for row in (named_rows if isinstance(named_rows, list) else [])
        )
        if (isinstance(llm, dict) and llm.get("api_key")) or has_named_secret:
            marker = path.with_suffix(".applied.json")
            path.rename(marker)
            logger.info("bootstrap: applied — renamed %s to %s (key now in vault).",
                        path.name, marker.name)
    except Exception as e:
        logger.warning("bootstrap: could not scrub %s: %s", path, e)

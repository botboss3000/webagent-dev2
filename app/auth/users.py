"""User account store — login credentials for users and admins.

Storage is the shared database's central `user_accounts` table (the account
plane), NOT a local file: every instance/device sees one identical account list,
so a login created on one server works on all of them. The methods are pinned to
the authority DB by app/db/router.py CONTROL_METHODS, so they are never routed to
a per-tenant database.

Passwords are bcrypt hashes (bcrypt directly — passlib has compat issues on
Windows + Python 3.14), never plaintext. Remember-me tokens support persistent
sessions.

History: this used to persist to app/auth/users.json. On first boot after the
move, `migrate_legacy_file()` imports that file into the DB and renames it to
`users.json.bak` (kept as a backup, never deleted). See `bootstrap_admin_from_env`
and `migrate_legacy_file`, both called from the app startup hook (app/main.py)."""

import json
import logging
import os as _os
import secrets
from pathlib import Path
from typing import Optional

import bcrypt

logger = logging.getLogger(__name__)

# Legacy per-instance store — read ONCE by migrate_legacy_file() to seed the DB,
# then renamed to <name>.bak. No longer the source of truth.
_USER_FILE = Path(__file__).resolve().parent / "users.json"

# Canonical bootstrap-admin id. The login NAME may be anything the operator picks,
# but the user_id stays fixed so every admin gate (is_admin seed, delete
# protection, open-mode fallback) keys off one stable value.
ADMIN_USER_ID = "admin"

# How long a fresh login pass stays valid before it must renew, per user.
# This is the default for any account that hasn't picked its own value on the
# Manage Account page; each user can shorten or lengthen it (clamped to the
# bounds below). 30 days keeps people signed in across normal gaps; the silent
# auto-renew (when on) then keeps active sessions alive indefinitely.
DEFAULT_SESSION_LIFETIME_MINUTES = 60 * 24 * 30          # 30 days
MIN_SESSION_LIFETIME_MINUTES = 60                        # 1 hour floor
MAX_SESSION_LIFETIME_MINUTES = 60 * 24 * 90              # 90 day ceiling


def clamp_session_lifetime(minutes: int) -> int:
    """Pin a requested session lifetime into the allowed [1h, 90d] range."""
    try:
        m = int(minutes)
    except (TypeError, ValueError):
        return DEFAULT_SESSION_LIFETIME_MINUTES
    return max(MIN_SESSION_LIFETIME_MINUTES, min(MAX_SESSION_LIFETIME_MINUTES, m))


# ── Data ────────────────────────────────────────────────────────────────────

class User:
    """In-memory view of one `user_accounts` row. Read-only value object — all
    mutations go through the async functions below (which write the DB)."""

    __slots__ = ("username", "password_hash", "user_id", "display_name",
                 "remember_token", "is_approved",
                 "session_lifetime_minutes", "auto_renew", "social_links")

    def __init__(self, username: str, password_hash: str, user_id: str,
                 display_name: str = "", remember_token: str = "",
                 is_approved: bool = True,
                 session_lifetime_minutes: int = DEFAULT_SESSION_LIFETIME_MINUTES,
                 auto_renew: bool = True,
                 social_links: Optional[dict] = None):
        self.username = username
        self.password_hash = password_hash
        self.user_id = user_id
        self.display_name = display_name or username
        self.remember_token = remember_token
        self.is_approved = is_approved
        # Linked social sign-in identities: {provider_id: external_id}. Lets the
        # same Google/GitHub/etc. account always resolve to this user even if the
        # provider's email later changes. See app/api/social_auth.py.
        self.social_links = dict(social_links or {})
        # Per-user session policy (set on the Manage Account page):
        #   session_lifetime_minutes — how long one login pass lasts
        #   auto_renew — whether the pass silently renews so the user effectively
        #                never gets signed out while they keep using the app
        self.session_lifetime_minutes = clamp_session_lifetime(session_lifetime_minutes)
        self.auto_renew = bool(auto_renew)

    def to_dict(self) -> dict:
        return {
            "username": self.username,
            "password_hash": self.password_hash,
            "user_id": self.user_id,
            "display_name": self.display_name,
            "remember_token": self.remember_token,
            "is_approved": self.is_approved,
            "session_lifetime_minutes": self.session_lifetime_minutes,
            "auto_renew": self.auto_renew,
            "social_links": self.social_links,
        }

    @classmethod
    def from_dict(cls, d: dict) -> "User":
        return cls(
            username=d["username"],
            password_hash=d["password_hash"],
            user_id=d["user_id"],
            display_name=d.get("display_name", d["username"]),
            remember_token=d.get("remember_token", ""),
            is_approved=d.get("is_approved", True),
            session_lifetime_minutes=d.get("session_lifetime_minutes",
                                           DEFAULT_SESSION_LIFETIME_MINUTES),
            auto_renew=d.get("auto_renew", True),
            social_links=d.get("social_links", {}),
        )

    @classmethod
    def from_row(cls, row: Optional[dict]) -> Optional["User"]:
        """Build a User from a `user_accounts` DB row (or None → None). Coerces
        the DB's integer flags to bool and parses the social_links JSON text."""
        if not row:
            return None
        links = row.get("social_links")
        if isinstance(links, str):
            try:
                links = json.loads(links or "{}")
            except Exception:
                links = {}
        return cls(
            username=row["username"],
            password_hash=row.get("password_hash", ""),
            user_id=row["user_id"],
            display_name=row.get("display_name") or row["username"],
            remember_token=row.get("remember_token", "") or "",
            is_approved=bool(row.get("is_approved", 1)),
            session_lifetime_minutes=row.get("session_lifetime_minutes",
                                             DEFAULT_SESSION_LIFETIME_MINUTES),
            auto_renew=bool(row.get("auto_renew", 1)),
            social_links=links if isinstance(links, dict) else {},
        )


# ── Password / token helpers ─────────────────────────────────────────────────

def _hash_password(password: str) -> str:
    """Return bcrypt hash as string."""
    return bcrypt.hashpw(password.encode("utf-8"), bcrypt.gensalt()).decode("utf-8")


def _verify_password(password: str, password_hash: str) -> bool:
    """Check password against bcrypt hash."""
    try:
        return bcrypt.checkpw(
            password.encode("utf-8"), password_hash.encode("utf-8")
        )
    except Exception:
        return False


def _generate_remember_token() -> str:
    """Generate a cryptographically secure 32-byte hex token."""
    return secrets.token_hex(32)


def _db():
    """The storage backend. Imported lazily to avoid an import cycle and so the
    account methods route to the central authority DB (CONTROL_METHODS)."""
    from app.db import get_db
    return get_db()


# ── Read API (async, DB-backed) ──────────────────────────────────────────────

async def authenticate(username: str, password: str) -> Optional[User]:
    """Verify credentials. Returns User on success, None on failure."""
    row = await _db().get_user_account_by_username(username)
    if row is None:
        return None
    if not _verify_password(password, row.get("password_hash", "")):
        return None
    return User.from_row(row)


async def get_user(username: str) -> Optional[User]:
    return User.from_row(await _db().get_user_account_by_username(username))


async def get_user_by_id(user_id: str) -> Optional[User]:
    """Look up a user by user_id (vs username)."""
    return User.from_row(await _db().get_user_account_by_id(user_id))


async def list_users() -> list[User]:
    """Return all registered users."""
    rows = await _db().list_user_accounts()
    return [User.from_row(r) for r in rows]


async def admin_exists() -> bool:
    """Whether an admin account exists, keyed on the canonical bootstrap user_id
    ("admin") — so an admin who chose a custom login name still counts."""
    return (await _db().get_user_account_by_id(ADMIN_USER_ID)) is not None


def is_default_seed() -> bool:
    """Return whether the admin was seeded with a default password.
    Always False now — no default seed is used."""
    return False


# ── Remember-me tokens ───────────────────────────────────────────────────────

async def set_remember_token(
    username: str,
    *,
    device_id: str = "",
) -> Optional[str]:
    """Generate and store a new remember token for the user.
    Returns the token string, or None if user not found.
    Every call invalidates the previous token (single-session)."""
    row = await _db().get_user_account_by_username(username)
    if row is None:
        return None
    token = _generate_remember_token()
    await _db().update_user_account(row["user_id"], remember_token=token)
    from app.auth.revocation import bind_remember_token, clear_remember_tokens

    clear_remember_tokens(row["user_id"])
    if device_id and not bind_remember_token(row["user_id"], device_id, token):
        # Never hand the client a persistent credential that failed to bind.
        await _db().update_user_account(row["user_id"], remember_token="")
        return None
    return token


async def resolve_remember_token(token: str) -> Optional[User]:
    """Look up user by remember token. Returns User or None."""
    return User.from_row(await _db().get_user_account_by_remember_token(token))


async def clear_remember_token(username: str) -> bool:
    """Clear the remember token for a user (sign-out)."""
    row = await _db().get_user_account_by_username(username)
    if row is None:
        return False
    await _db().update_user_account(row["user_id"], remember_token="")
    from app.auth.revocation import clear_remember_tokens

    clear_remember_tokens(row["user_id"])
    return True


# ── Registration / mutation ──────────────────────────────────────────────────

async def register_user(username: str, password: str, display_name: str = "",
                        is_approved: bool = True,
                        user_id: Optional[str] = None) -> Optional[User]:
    """Register a new user. Returns User on success, None if username taken.
    Set is_approved=False when admin approval is required.

    `user_id` defaults to the username (the normal case — the login name *is*
    the stable id). The bootstrap admin passes user_id="admin" explicitly so the
    login name can be anything the operator chooses while the canonical admin id
    that the rest of the app keys privileges on stays fixed.
    """
    row = await _db().create_user_account(
        user_id=user_id or username,
        username=username,
        password_hash=_hash_password(password),
        display_name=display_name or username,
        is_approved=is_approved,
    )
    user = User.from_row(row)
    # The bootstrap admin's *privilege* (user_profiles.is_admin) must travel with
    # its *login* on EVERY backend. The is_admin=1 seed historically lived only in
    # the LocalBackend SQLite migrations, so a shared/remote control DB (Postgres/
    # remote DB) never received it — a signed-in "admin" then had no Admin Tools.
    # Stamping it here (a backend-agnostic, control-DB-routed write) closes that
    # gap for the setup page, the env-var bootstrap, and the bundle path alike.
    if user is not None and user.user_id == ADMIN_USER_ID:
        try:
            await _db().set_user_admin(ADMIN_USER_ID, True)
        except Exception as e:  # best-effort — never block account creation
            logger.warning("Could not stamp is_admin on the bootstrap admin: %s", e)
    return user


async def set_user_approval(username: str, is_approved: bool) -> Optional[User]:
    """Set the is_approved flag on a user. Returns updated User or None."""
    row = await _db().get_user_account_by_username(username)
    if row is None:
        return None
    return User.from_row(
        await _db().update_user_account(row["user_id"], is_approved=is_approved)
    )


async def delete_user(username: str) -> bool:
    """Remove a user from the store. Returns True if deleted.
    Protects the bootstrap admin regardless of its (possibly custom) login name."""
    row = await _db().get_user_account_by_username(username)
    if row is None:
        return False
    if row["user_id"] == "admin":
        return False
    eraser = getattr(_db(), "erase_user_owned_data", None)
    if eraser is not None:
        await eraser(row["user_id"])
    await _purge_local_user_state(row["user_id"], remove_files=False)
    deleted = await _db().delete_user_account(row["user_id"])
    if deleted:
        await _purge_local_user_state(row["user_id"], remove_files=True)
        _scrub_legacy_backup(row["user_id"])
    return deleted


async def set_session_policy(username: str, lifetime_minutes: int,
                             auto_renew: bool) -> Optional[User]:
    """Set a user's session policy (login-pass lifetime + auto-renew).

    The lifetime is clamped into the allowed range. Returns the updated User,
    or None if the user is not found. Does NOT touch the remember token —
    callers decide whether to issue/clear it based on the new auto_renew value.
    """
    row = await _db().get_user_account_by_username(username)
    if row is None:
        return None
    return User.from_row(await _db().update_user_account(
        row["user_id"],
        session_lifetime_minutes=clamp_session_lifetime(lifetime_minutes),
        auto_renew=bool(auto_renew),
    ))


# ── Social sign-in links ────────────────────────────────────────────────────

async def get_user_by_social(provider: str, external_id: str) -> Optional[User]:
    """Find the user who has linked this (provider, external_id), or None.

    Keyed on the provider's STABLE id (not email) so a user keeps their account
    even if they change their email at the provider.
    """
    if not provider or not external_id:
        return None
    return User.from_row(await _db().get_user_account_by_social(provider, external_id))


async def link_social(username: str, provider: str, external_id: str) -> Optional[User]:
    """Record that `username`'s account is linked to (provider, external_id)."""
    if not provider or not external_id:
        return None
    row = await _db().get_user_account_by_username(username)
    if row is None:
        return None
    try:
        links = json.loads(row.get("social_links") or "{}")
    except Exception:
        links = {}
    links[provider] = str(external_id)
    return User.from_row(
        await _db().update_user_account(row["user_id"], social_links=links)
    )


async def register_social_user(email: str, display_name: str, provider: str,
                               external_id: str, is_approved: bool = True) -> Optional[User]:
    """Create a new account for a first-time social sign-in and link it.

    The password is a random, unusable value — the person signs in through the
    provider, not with a password. Returns None if the email is already taken
    (the caller should link to the existing account instead).
    """
    row = await _db().create_user_account(
        user_id=email,
        username=email,
        password_hash=_hash_password(secrets.token_urlsafe(32)),
        display_name=display_name or email,
        is_approved=is_approved,
        social_links={provider: str(external_id)} if provider and external_id else None,
    )
    return User.from_row(row)


# ── Self-service profile/password/delete ────────────────────────────────────

class UpdateConflict(Exception):
    """Raised when a username/email change collides with an existing user."""


async def update_user(current_username: str,
                      new_username: Optional[str] = None,
                      new_display_name: Optional[str] = None) -> Optional[User]:
    """Update a user's username (email) and/or display name.

    Keeps user_id stable so existing DB references stay valid.
    Raises UpdateConflict if new_username collides with another user.
    Returns the updated User, or None if current_username not found.
    """
    row = await _db().get_user_account_by_username(current_username)
    if row is None:
        return None

    updates: dict = {}
    if new_username is not None and new_username != current_username:
        updates["username"] = new_username
    if new_display_name is not None:
        updates["display_name"] = new_display_name or (new_username or current_username)

    if not updates:
        return User.from_row(row)

    try:
        updated = await _db().update_user_account(row["user_id"], **updates)
    except ValueError:
        # DB raised on a UNIQUE(username) collision.
        raise UpdateConflict(new_username)
    return User.from_row(updated)


async def change_password(username: str, new_password: str) -> bool:
    """Set a new password hash for the user.

    Caller must verify the current password first (e.g. via authenticate()).
    Returns True on success, False if user not found.
    """
    row = await _db().get_user_account_by_username(username)
    if row is None:
        return False
    await _db().update_user_account(
        row["user_id"], password_hash=_hash_password(new_password)
    )
    return True


async def delete_user_self(username: str) -> tuple[bool, str]:
    """Delete the user identified by `username` (self-service).

    Blocks deletion of the bootstrap admin (user_id == "admin").
    Returns (ok, reason). reason is "" on success, otherwise a short code:
      - "not_found"  — username not in store
      - "protected"  — the admin seed cannot be deleted
    """
    row = await _db().get_user_account_by_username(username)
    if row is None:
        return False, "not_found"
    if row["user_id"] == "admin":
        return False, "protected"
    eraser = getattr(_db(), "erase_user_owned_data", None)
    if eraser is not None:
        await eraser(row["user_id"])
    await _purge_local_user_state(row["user_id"], remove_files=False)
    ok = await _db().delete_user_account(row["user_id"])
    if ok:
        await _purge_local_user_state(row["user_id"], remove_files=True)
        _scrub_legacy_backup(row["user_id"])
    return (True, "") if ok else (False, "not_found")


async def _purge_local_user_state(
    user_id: str,
    *,
    remove_files: bool,
) -> None:
    """Remove browser-sync files and process-local caches for a deleted user."""
    from app.agent.browser_history_cache import (
        get_browser_history_cache,
        get_browser_turn_replay_cache,
    )
    from app.db.user_store import purge_user_store_files

    await purge_user_store_files(user_id, remove_files=remove_files)
    await get_browser_history_cache().purge_user(user_id)
    await get_browser_turn_replay_cache().purge_user(user_id)


def _scrub_legacy_backup(user_id: str) -> None:
    """Remove one account from the pre-database JSON backup, if present."""
    backup = _USER_FILE.with_name(f"{_USER_FILE.name}.bak")
    if not backup.exists():
        return
    try:
        rows = json.loads(backup.read_text(encoding="utf-8"))
        if not isinstance(rows, list):
            raise ValueError("legacy account backup is not a list")
        kept = [
            row for row in rows
            if not isinstance(row, dict) or str(row.get("user_id") or "") != user_id
        ]
        if len(kept) == len(rows):
            return
        temporary = backup.with_suffix(f"{backup.suffix}.tmp")
        temporary.write_text(
            json.dumps(kept, indent=2, ensure_ascii=False),
            encoding="utf-8",
        )
        temporary.replace(backup)
    except Exception as exc:
        raise RuntimeError("Could not scrub deleted user from legacy backup") from exc


# ── Startup: legacy-file migration + env-var admin bootstrap ──────────────────

async def migrate_legacy_file() -> int:
    """One-time import of the old app/auth/users.json into the DB.

    Only runs when the DB has NO accounts yet (a fresh move) AND the legacy file
    exists — so it never clobbers accounts already in the DB. On success the file
    is renamed to `users.json.bak` (kept as a backup, never deleted). Returns the
    number of accounts imported. Best-effort: logs and returns 0 on any failure so
    a bad file can never block startup."""
    try:
        if not _USER_FILE.exists():
            return 0
        if await _db().count_user_accounts() > 0:
            return 0  # DB already populated — the move already happened
        data = json.loads(_USER_FILE.read_text(encoding="utf-8"))
    except Exception as e:
        logger.warning("Legacy users.json migration skipped (unreadable): %s", e)
        return 0

    imported = 0
    for d in data or []:
        try:
            u = User.from_dict(d)
            created = await _db().create_user_account(
                user_id=u.user_id,
                username=u.username,
                password_hash=u.password_hash,
                display_name=u.display_name,
                is_approved=u.is_approved,
                session_lifetime_minutes=u.session_lifetime_minutes,
                auto_renew=u.auto_renew,
                social_links=u.social_links,
            )
            if created and u.remember_token:
                # Preserve live "remember me" sessions across the move.
                await _db().update_user_account(u.user_id, remember_token=u.remember_token)
            if created:
                imported += 1
        except Exception as e:  # noqa: BLE001 — one bad row must not abort the rest
            logger.warning("Failed to import legacy account %r: %s", d.get("username"), e)

    if imported:
        logger.info("Migrated %d account(s) from users.json into the shared DB", imported)
    try:
        _USER_FILE.rename(_USER_FILE.with_suffix(".json.bak"))
    except Exception as e:
        logger.warning("Could not rename legacy users.json to .bak: %s", e)
    return imported


async def bootstrap_admin_from_env() -> None:
    """On first start, create the admin from BOOTSTRAP_ADMIN_PASSWORD if set and
    no admin exists yet. Replaces the old import-time bootstrap (which can't reach
    the async DB). Called from the app startup hook AFTER migrate_legacy_file()."""
    password = _os.environ.get("BOOTSTRAP_ADMIN_PASSWORD", "")
    if not password:
        return
    if await admin_exists():
        return
    await register_user(ADMIN_USER_ID, password, "Admin", user_id=ADMIN_USER_ID)
    logger.info("Bootstrap admin created from BOOTSTRAP_ADMIN_PASSWORD env var")


async def ensure_admin_privilege() -> bool:
    """Self-heal: guarantee the bootstrap admin's profile carries is_admin=1 in the
    ACTIVE control database, whatever the backend.

    The admin *login* lives in user_accounts; the *privilege* lives in
    user_profiles.is_admin. That privilege was only ever seeded by the LocalBackend
    SQLite migrations, so a box whose control DB is a shared/remote Postgres or
    remote DB (e.g. a fresh deploy that inherits an existing shared database) ends up
    with a valid admin login but NO admin privilege — signed in, yet locked out of
    Admin Tools. Run once at startup (after the account bootstrap) it repairs any
    such box, and repairs the shared DB itself so later deploys inherit the flag.

    No-op (returns False) when no admin login exists yet, so it never creates a
    stray privileged profile on a truly fresh, not-yet-set-up box."""
    if not await admin_exists():
        return False
    try:
        await _db().set_user_admin(ADMIN_USER_ID, True)
        return True
    except Exception as e:  # best-effort — must never block startup
        logger.warning("Could not ensure is_admin on the bootstrap admin: %s", e)
        return False

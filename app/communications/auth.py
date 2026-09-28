"""
User registration + auth flow for communication channels.

Tracks three tiers:
  - anonymous: first contact, no registration
  - channel_verified: proved ownership of phone/chat ID
  - full: email + password (cross-device linking enabled)

Data stored in channel_identities and users tables.
"""

import asyncio
import json
import logging
import secrets
import time
import uuid
from dataclasses import dataclass, field
from typing import Optional

from app.db import get_app_db

logger = logging.getLogger(__name__)

# ── Data types ───────────────────────────────────────────────────────────────


@dataclass
class ChannelIdentity:
    """Represents a user on one communication channel."""

    channel: str          # 'telegram', 'whatsapp', 'sms'
    external_id: str      # chat_id, phone number, etc.
    user_id: str          # internal: 'telegram:12345' or 'user:uuid'
    user_tier: str        # 'anonymous', 'channel_verified', 'full'
    display_name: str = ""
    email: str = ""
    email_verified: bool = False
    linked_user_id: Optional[str] = None  # UUID in users table (cross-device)
    metadata: dict = field(default_factory=dict)


@dataclass
class VerificationCode:
    code: str
    channel: str
    external_id: str
    created_at: float
    expires_at: float
    used: bool = False


# ── In-memory verification codes (replace with DB for production) ──────────

_verification_codes: dict[str, VerificationCode] = {}


def _generate_code() -> str:
    return str(secrets.randbelow(900000) + 100000)  # 6-digit


def _code_key(channel: str, external_id: str) -> str:
    return f"{channel}:{external_id}"


# ── Public API ──────────────────────────────────────────────────────────────


async def get_identity(channel: str, external_id: str) -> Optional[ChannelIdentity]:
    """
    Look up a channel identity from the database.
    Returns None if this external_id has never been seen.
    """
    try:
        def _read_rows():
            db = get_app_db()
            raw = db.get_raw_client()
            resp = (
                raw.table("channel_identities")
                .select("*")
                .eq("channel", channel)
                .eq("external_id", external_id)
                .limit(1)
                .execute()
            )
            return resp.data if hasattr(resp, 'data') else (resp or [])

        # The raw query-builder API is synchronous for local and remote
        # backends. Keep its I/O off the FastAPI event loop so a cold anonymous
        # login cannot freeze the shell's concurrent boot requests.
        try:
            rows = await asyncio.to_thread(_read_rows)
        except Exception:
            return None

        if not rows:
            return None

        row = rows[0]
        return ChannelIdentity(
            channel=row["channel"],
            external_id=row["external_id"],
            user_id=row["user_id"],
            user_tier=row.get("user_tier", "anonymous"),
            display_name=row.get("display_name", ""),
            email=row.get("email", ""),
            email_verified=bool(row.get("email_verified", False)),
            linked_user_id=row.get("linked_user_id"),
            metadata=json.loads(row.get("metadata", "{}")),
        )
    except Exception as e:
        logger.warning("get_identity error: %s", e)
        return None


async def create_anonymous_identity(channel: str, external_id: str) -> ChannelIdentity:
    """
    Create a new anonymous identity for a first-time contact.

    Generates a UUID-based user_id so the internal account is not tied to
    the raw channel identifier (e.g. Telegram chat_id).  The mapping from
    (channel, external_id) → user_id is persisted in channel_identities and
    used on every subsequent message from the same contact.
    """
    user_id = f"anon_{uuid.uuid4().hex[:16]}"
    identity = ChannelIdentity(
        channel=channel,
        external_id=external_id,
        user_id=user_id,
        user_tier="anonymous",
    )

    await _upsert_identity(identity)
    return identity


async def upgrade_to_verified(identity: ChannelIdentity, display_name: str = "") -> ChannelIdentity:
    """
    Upgrade an identity from anonymous to channel_verified.
    This means the user proved ownership of the channel.
    """
    identity.user_tier = "channel_verified"
    if display_name:
        identity.display_name = display_name
    await _upsert_identity(identity)
    return identity


async def start_verification(identity: ChannelIdentity, plugin) -> str:
    """
    Generate and send a verification code to the user via their channel.
    Returns the code (for logging). The code is also sent to the user.

    The plugin must have a send_message method.
    """
    code = _generate_code()
    key = _code_key(identity.channel, identity.external_id)
    now = time.time()
    _verification_codes[key] = VerificationCode(
        code=code,
        channel=identity.channel,
        external_id=identity.external_id,
        created_at=now,
        expires_at=now + 300,  # 5 min TTL
    )

    message = (
        f"🔐 Your verification code is: {code}\n\n"
        f"This code expires in 5 minutes. Reply with the code to verify."
    )
    await plugin.send_message(identity.external_id, message)
    return code


async def verify_code(channel: str, external_id: str, code: str) -> bool:
    """Check a verification code. Returns True if valid and not expired."""
    key = _code_key(channel, external_id)
    vc = _verification_codes.get(key)
    if vc is None:
        return False
    if vc.used:
        return False
    if time.time() > vc.expires_at:
        return False
    if vc.code != code:
        return False
    vc.used = True
    return True


async def get_or_create_identity(channel: str, external_id: str) -> ChannelIdentity:
    """
    Get existing identity or create anonymous one.
    This is the main entry point for webhook handlers.
    """
    identity = await get_identity(channel, external_id)
    if identity is None:
        identity = await create_anonymous_identity(channel, external_id)
    return identity


# ── DB helpers ──────────────────────────────────────────────────────────────


async def _upsert_identity(identity: ChannelIdentity) -> None:
    """Insert or update a channel identity in the database."""
    try:
        row = {
            "channel": identity.channel,
            "external_id": identity.external_id,
            "user_id": identity.user_id,
            "user_tier": identity.user_tier,
            "display_name": identity.display_name,
            "email": identity.email,
            "email_verified": identity.email_verified,
            "linked_user_id": identity.linked_user_id,
            "metadata": json.dumps(identity.metadata),
        }

        # Upsert by (channel, external_id)
        def _write_row():
            db = get_app_db()
            raw = db.get_raw_client()
            raw.table("channel_identities").upsert(
                row, on_conflict="channel,external_id"
            ).execute()

        await asyncio.to_thread(_write_row)
    except Exception as e:
        logger.warning("Failed to upsert identity: %s", e)


# ── Migration helpers ──────────────────────────────────────────────────────


async def migrate_anonymous_to_user(anon_user_id: str, target_user_id: str) -> int:
    """
    Migrate all interactions and sessions from an anonymous user to an
    existing registered user.  Updates the channel_identity row to point
    at the target user, re-parents sessions and interactions, then returns
    the number of interactions moved.
    """
    db = get_app_db()
    raw = db.get_raw_client()
    moved = 0
    try:
        # Local backends (SQLite or Postgres) expose a connection factory via the
        # raw-client proxy; remote backends use the query-builder branch.
        factory = getattr(raw, '_conn_factory', None)
        if factory:
            conn = factory()
            try:
                cur = conn.execute(
                    "UPDATE interactions SET session_id = ? WHERE session_id = ?",
                    (target_user_id, anon_user_id),
                )
                moved = cur.rowcount

                conn.execute(
                    "DELETE FROM sessions WHERE id = ? AND user_id = ?",
                    (anon_user_id, anon_user_id),
                )

                conn.execute(
                    "UPDATE channel_identities SET user_id = ? WHERE user_id = ?",
                    (target_user_id, anon_user_id),
                )
                # A guest recovery credential must never become a passwordless
                # login token for the newly registered account.
                try:
                    conn.execute(
                        "DELETE FROM anonymous_guest_credentials WHERE user_id = ?",
                        (anon_user_id,),
                    )
                except Exception:
                    pass
                conn.commit()
            finally:
                conn.close()
        else:
            raw.table("interactions").update({"session_id": target_user_id}).eq("session_id", anon_user_id).execute()
            raw.table("sessions").delete().eq("id", anon_user_id).eq("user_id", anon_user_id).execute()
            raw.table("channel_identities").update({"user_id": target_user_id}).eq("user_id", anon_user_id).execute()
            try:
                raw.table("anonymous_guest_credentials").delete().eq("user_id", anon_user_id).execute()
            except Exception:
                pass
    except Exception as e:
        logger.warning("migrate_anonymous_to_user error: %s", e)
    return moved


async def find_user_by_display_name(display_name: str) -> str | None:
    """
    Look up an existing non-anonymous user by display name in
    channel_identities.  Returns the user_id if found, else None.
    """
    try:
        db = get_app_db()
        raw = db.get_raw_client()
        resp = (
            raw.table("channel_identities")
            .select("user_id, user_tier")
            .eq("display_name", display_name)
            .neq("user_tier", "anonymous")
            .limit(1)
            .execute()
        )
        rows = resp.data if hasattr(resp, 'data') else (resp or [])
        if rows:
            return rows[0]["user_id"]
    except Exception as e:
        logger.warning("find_user_by_display_name error: %s", e)
    return None


# ── Registration system prompt ──────────────────────────────────────────────


def _load_app_level_prompt(key: str) -> str:
    """Load a single app-level prompt text from app/defaults/app-prompts.json."""
    import json
    from app.util.paths import app_prompts_path
    p = app_prompts_path()
    try:
        data = json.loads(p.read_text(encoding="utf-8"))
        entry = data.get("app_level_prompts", {}).get(key, {})
        return entry.get("text") or entry.get("template", "") or ""
    except Exception:
        return ""


def get_registration_system_prompt(identity: ChannelIdentity) -> str:
    """
    Build a system prompt for the agent when handling an unregistered user.
    This tells the agent to guide the user through registration.
    """
    tpl = _load_app_level_prompt("registration_system_prompt")
    if tpl:
        return tpl.format(channel=identity.channel)
    # Inline fallback if JSON is missing
    return f"""
# [SYSTEM — REGISTRATION MODE]

You are talking to an unregistered user via {identity.channel}.

Your job is to:
1. Welcome them warmly
2. Explain they can use you anonymously (limited) or register for full access
3. If they want to register, collect their name
4. Call `register_user(name)` tool to save their info
5. Tell them they're now verified on this channel

Rules:
- Be friendly and helpful
- If they want to stay anonymous, that's fine — just proceed with limited service
- If they provide a name, register them immediately with `register_user`
- Do NOT ask for email, password, or any sensitive info (that comes later)
""".strip()


def get_anonymous_limit_prompt() -> str:
    """System prompt fragment for anonymous users — limits their turns."""
    text = _load_app_level_prompt("anonymous_limit_prompt")
    if text:
        return text.strip()
    return "[NOTE: User is ANONYMOUS — 5 turn limit. Encourage registration for full access.]"

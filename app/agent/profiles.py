"""Agent-owned members, authentication identities, and capability profiles.

The installation account plane may prove an identity, but it does not assign
authority inside an agent.  This module is the sole resolver for that boundary.
It deliberately uses lazy, idempotent DDL so upgraded agent authority files are
usable before a process-wide migration completes.
"""

from __future__ import annotations

import hashlib
import json
import secrets
import sqlite3
import uuid
from datetime import datetime, timedelta, timezone
from typing import Any, Iterable, Optional

from app.agent.member_workspace import member_db_path, parse_subject_id, subject_id


VISITOR = "visitor"
MEMBER = "member"
ADMIN = "agent-administrator"
BUILTIN_SLUGS = (VISITOR, MEMBER, ADMIN)
VISITOR_CORE_TOOLS = frozenset({"get_time", "get_date", "calculate", "request_agent_login"})
ADMIN_PROFILE_TOOLS = frozenset({"manage_agent_profiles"})


SCHEMA_SQL = """
CREATE TABLE IF NOT EXISTS agent_profiles (
    id TEXT PRIMARY KEY,
    agent_id TEXT NOT NULL,
    slug TEXT NOT NULL,
    name TEXT NOT NULL,
    description TEXT NOT NULL DEFAULT '',
    kind TEXT NOT NULL DEFAULT 'custom',
    policy_json TEXT NOT NULL DEFAULT '{}',
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    UNIQUE(agent_id, slug)
);
CREATE TABLE IF NOT EXISTS agent_members (
    id TEXT PRIMARY KEY,
    agent_id TEXT NOT NULL,
    profile_id TEXT NOT NULL,
    display_name TEXT NOT NULL DEFAULT '',
    email_normalized TEXT NOT NULL DEFAULT '',
    status TEXT NOT NULL DEFAULT 'active',
    is_agent_admin INTEGER NOT NULL DEFAULT 0,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    UNIQUE(agent_id, id)
);
CREATE INDEX IF NOT EXISTS idx_agent_members_profile ON agent_members(agent_id, profile_id);
CREATE TABLE IF NOT EXISTS agent_member_identities (
    id TEXT PRIMARY KEY,
    agent_id TEXT NOT NULL,
    member_id TEXT NOT NULL,
    provider TEXT NOT NULL,
    external_subject TEXT NOT NULL,
    verified_at TEXT,
    created_at TEXT NOT NULL,
    UNIQUE(agent_id, provider, external_subject)
);
CREATE INDEX IF NOT EXISTS idx_agent_member_identities_member
    ON agent_member_identities(agent_id, member_id);
CREATE TABLE IF NOT EXISTS agent_member_credentials (
    member_id TEXT PRIMARY KEY,
    agent_id TEXT NOT NULL,
    username_normalized TEXT NOT NULL,
    password_hash TEXT NOT NULL,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    UNIQUE(agent_id, username_normalized)
);
CREATE TABLE IF NOT EXISTS agent_auth_policy (
    agent_id TEXT PRIMARY KEY,
    app_login_enabled INTEGER NOT NULL DEFAULT 1,
    app_login_enrollment TEXT NOT NULL DEFAULT 'open',
    local_signup_mode TEXT NOT NULL DEFAULT 'open',
    default_profile_id TEXT NOT NULL,
    transcript_review INTEGER NOT NULL DEFAULT 0,
    updated_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS agent_guest_credentials (
    credential_hash TEXT PRIMARY KEY,
    agent_id TEXT NOT NULL,
    member_id TEXT NOT NULL,
    created_at TEXT NOT NULL,
    rotated_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS agent_profile_usage (
    agent_id TEXT NOT NULL,
    member_id TEXT NOT NULL,
    period TEXT NOT NULL,
    period_key TEXT NOT NULL,
    turns INTEGER NOT NULL DEFAULT 0,
    updated_at TEXT NOT NULL,
    PRIMARY KEY(agent_id, member_id, period, period_key)
);
CREATE TABLE IF NOT EXISTS agent_invites (
    code_hash TEXT PRIMARY KEY,
    agent_id TEXT NOT NULL,
    profile_id TEXT NOT NULL,
    max_uses INTEGER NOT NULL DEFAULT 1,
    use_count INTEGER NOT NULL DEFAULT 0,
    expires_at TEXT,
    created_at TEXT NOT NULL
);
"""


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _profile_id(agent_id: str, slug: str) -> str:
    return f"profile--{agent_id}--{slug}"


def _json(value: Any, default: Any) -> Any:
    if isinstance(value, type(default)):
        return value
    if isinstance(value, str):
        try:
            parsed = json.loads(value or "{}")
            return parsed if isinstance(parsed, type(default)) else default
        except Exception:
            return default
    return default


def _conn(agent_id: str) -> sqlite3.Connection:
    from app.db import get_agent_db
    backend = get_agent_db(agent_id)
    conn = backend._get_conn()
    conn.executescript(SCHEMA_SQL)
    return conn


def _default_policies(agent: Optional[dict] = None) -> dict[str, dict]:
    public: dict = {}
    if agent:
        try:
            from app.agent.public_policy import normalize_public_access
            pub = normalize_public_access(agent)
            caps = pub.get("capabilities") or {}
            public = {
                "abilities": list(caps.get("abilities") or []),
                "tools": list(caps.get("tools") or []),
                "features": list(caps.get("features") or []),
                "limits": dict(pub.get("usage") or {}),
                "funding": dict(pub.get("funding") or {}),
                "chat_ui": dict(pub.get("chat_ui") or {}),
            }
        except Exception:
            public = {}
    return {
        VISITOR: {"abilities": public.get("abilities", []), "tools": public.get("tools", []),
                  "features": public.get("features", []), "limits": public.get("limits", {}),
                  "funding": public.get("funding", {}), "chat_ui": public.get("chat_ui", {}),
                  "prompt_overlay": ""},
        MEMBER: {"abilities": ["*"], "tools": ["*"], "features": ["*"],
                 "limits": {}, "funding": {"mode": "member"}, "chat_ui": {},
                 "prompt_overlay": ""},
        ADMIN: {"abilities": ["*"], "tools": ["*"], "features": ["*"],
                "limits": {}, "funding": {"mode": "member"}, "chat_ui": {},
                "prompt_overlay": ""},
    }


async def ensure_builtins(agent_id: str, *, agent: Optional[dict] = None,
                          creator_user_id: str = "") -> dict[str, dict]:
    conn = _conn(agent_id)
    now = _now()
    defaults = _default_policies(agent)
    labels = {
        VISITOR: ("Visitor", "Unauthenticated visitors", "visitor"),
        MEMBER: ("Member", "Authenticated members of this agent", "member"),
        ADMIN: ("Agent administrator", "Administrators of this agent", "administrator"),
    }
    for slug in BUILTIN_SLUGS:
        name, description, kind = labels[slug]
        conn.execute(
            """INSERT OR IGNORE INTO agent_profiles
               (id,agent_id,slug,name,description,kind,policy_json,created_at,updated_at)
               VALUES (?,?,?,?,?,?,?,?,?)""",
            (_profile_id(agent_id, slug), agent_id, slug, name, description, kind,
             json.dumps(defaults[slug]), now, now),
        )
    conn.execute(
        """INSERT OR IGNORE INTO agent_auth_policy
           (agent_id,app_login_enabled,app_login_enrollment,local_signup_mode,
            default_profile_id,transcript_review,updated_at)
           VALUES (?,1,'open','open',?,0,?)""",
        (agent_id, _profile_id(agent_id, MEMBER), now),
    )
    conn.commit()
    if creator_user_id:
        await ensure_app_member(agent_id, creator_user_id, is_admin=True)
    return {row["slug"]: _profile_row(row) for row in conn.execute(
        "SELECT * FROM agent_profiles WHERE agent_id=?", (agent_id,)
    ).fetchall()}


def _profile_row(row: Any) -> dict:
    data = dict(row)
    data["policy"] = _json(data.pop("policy_json", "{}"), {})
    return data


def _member_row(row: Any, profile: Optional[dict] = None) -> dict:
    data = dict(row)
    if profile:
        data["profile"] = profile
    data["is_agent_admin"] = bool(data.get("is_agent_admin"))
    data["subject_id"] = subject_id(data["agent_id"], data["id"])
    return data


async def list_profiles(agent_id: str) -> list[dict]:
    await ensure_builtins(agent_id)
    conn = _conn(agent_id)
    return [_profile_row(r) for r in conn.execute(
        "SELECT * FROM agent_profiles WHERE agent_id=? ORDER BY CASE slug WHEN 'visitor' THEN 0 WHEN 'member' THEN 1 WHEN 'agent-administrator' THEN 2 ELSE 3 END, name",
        (agent_id,),
    ).fetchall()]


async def upsert_profile(agent_id: str, *, slug: str, name: str, description: str = "",
                         policy: Optional[dict] = None) -> dict:
    await ensure_builtins(agent_id)
    slug = "-".join(part for part in str(slug or "").strip().lower().replace("_", "-").split("-") if part)
    if not slug or any(not (c.isalnum() or c == "-") for c in slug):
        raise ValueError("Invalid profile slug")
    conn = _conn(agent_id)
    now = _now()
    existing = conn.execute(
        "SELECT * FROM agent_profiles WHERE agent_id=? AND slug=?", (agent_id, slug)
    ).fetchone()
    pid = existing["id"] if existing else _profile_id(agent_id, slug)
    kind = existing["kind"] if existing else "custom"
    clean_policy = policy if isinstance(policy, dict) else (_json(existing["policy_json"], {}) if existing else {})
    conn.execute(
        """INSERT INTO agent_profiles
           (id,agent_id,slug,name,description,kind,policy_json,created_at,updated_at)
           VALUES (?,?,?,?,?,?,?,?,?)
           ON CONFLICT(agent_id,slug) DO UPDATE SET name=excluded.name,
             description=excluded.description,policy_json=excluded.policy_json,updated_at=excluded.updated_at""",
        (pid, agent_id, slug, str(name or slug)[:120], str(description or "")[:500], kind,
         json.dumps(clean_policy), existing["created_at"] if existing else now, now),
    )
    conn.commit()
    return _profile_row(conn.execute("SELECT * FROM agent_profiles WHERE id=?", (pid,)).fetchone())


async def delete_profile(agent_id: str, slug: str) -> None:
    if slug in BUILTIN_SLUGS:
        raise ValueError("Built-in profiles cannot be deleted")
    conn = _conn(agent_id)
    row = conn.execute("SELECT id FROM agent_profiles WHERE agent_id=? AND slug=?", (agent_id, slug)).fetchone()
    if not row:
        raise LookupError("Profile not found")
    if conn.execute("SELECT 1 FROM agent_members WHERE agent_id=? AND profile_id=? LIMIT 1", (agent_id, row["id"])).fetchone():
        raise ValueError("Reassign profile members before deleting this profile")
    conn.execute("DELETE FROM agent_profiles WHERE id=?", (row["id"],))
    conn.commit()


async def auth_policy(agent_id: str) -> dict:
    await ensure_builtins(agent_id)
    row = _conn(agent_id).execute("SELECT * FROM agent_auth_policy WHERE agent_id=?", (agent_id,)).fetchone()
    data = dict(row)
    for key in ("app_login_enabled", "transcript_review"):
        data[key] = bool(data.get(key))
    return data


async def update_auth_policy(agent_id: str, updates: dict) -> dict:
    current = await auth_policy(agent_id)
    allowed = {"app_login_enabled", "app_login_enrollment", "local_signup_mode", "transcript_review"}
    values = {k: updates[k] for k in allowed if k in updates}
    for key in ("app_login_enrollment", "local_signup_mode"):
        if key in values and values[key] not in {"open", "invite", "disabled"}:
            raise ValueError(f"Invalid {key}")
    if not values:
        return current
    conn = _conn(agent_id)
    sets = ",".join(f"{key}=?" for key in values)
    conn.execute(f"UPDATE agent_auth_policy SET {sets},updated_at=? WHERE agent_id=?",
                 (*[int(v) if isinstance(v, bool) else v for v in values.values()], _now(), agent_id))
    conn.commit()
    return await auth_policy(agent_id)


async def _identity_member(agent_id: str, provider: str, external_subject: str) -> Optional[dict]:
    conn = _conn(agent_id)
    row = conn.execute(
        """SELECT m.*,p.id AS p_id,p.agent_id AS p_agent_id,p.slug AS p_slug,
                  p.name AS p_name,p.description AS p_description,p.kind AS p_kind,
                  p.policy_json AS p_policy_json,p.created_at AS p_created_at,p.updated_at AS p_updated_at
           FROM agent_member_identities i
           JOIN agent_members m ON m.id=i.member_id AND m.agent_id=i.agent_id
           JOIN agent_profiles p ON p.id=m.profile_id
           WHERE i.agent_id=? AND i.provider=? AND i.external_subject=? AND m.status='active'""",
        (agent_id, provider, external_subject),
    ).fetchone()
    if not row:
        return None
    d = dict(row)
    profile = _profile_row({
        "id": d.pop("p_id"), "agent_id": d.pop("p_agent_id"), "slug": d.pop("p_slug"),
        "name": d.pop("p_name"), "description": d.pop("p_description"), "kind": d.pop("p_kind"),
        "policy_json": d.pop("p_policy_json"), "created_at": d.pop("p_created_at"),
        "updated_at": d.pop("p_updated_at"),
    })
    return _member_row(d, profile)


async def _create_member(agent_id: str, *, profile_slug: str = MEMBER,
                         display_name: str = "", email: str = "",
                         is_admin: bool = False) -> dict:
    await ensure_builtins(agent_id)
    conn = _conn(agent_id)
    profile_slug = ADMIN if is_admin else profile_slug
    profile = conn.execute("SELECT * FROM agent_profiles WHERE agent_id=? AND slug=?", (agent_id, profile_slug)).fetchone()
    if not profile:
        raise LookupError("Profile not found")
    member_id = uuid.uuid4().hex
    now = _now()
    conn.execute(
        """INSERT INTO agent_members
           (id,agent_id,profile_id,display_name,email_normalized,status,is_agent_admin,created_at,updated_at)
           VALUES (?,?,?,?,?,'active',?,?,?)""",
        (member_id, agent_id, profile["id"], str(display_name or "")[:120],
         str(email or "").strip().lower()[:320], int(is_admin), now, now),
    )
    conn.commit()
    return _member_row(conn.execute("SELECT * FROM agent_members WHERE id=?", (member_id,)).fetchone(), _profile_row(profile))


async def ensure_app_member(agent_id: str, app_user_id: str, *, is_admin: bool = False) -> dict:
    await ensure_builtins(agent_id)
    existing = await _identity_member(agent_id, "app_account", app_user_id)
    if existing:
        if is_admin and not existing["is_agent_admin"]:
            await set_member_admin(agent_id, existing["id"], True)
            existing = await _identity_member(agent_id, "app_account", app_user_id)
        return existing
    policy = await auth_policy(agent_id)
    if not is_admin and (not policy["app_login_enabled"] or policy["app_login_enrollment"] != "open"):
        raise PermissionError("App-account enrollment is not open for this agent")
    member = await _create_member(agent_id, is_admin=is_admin)
    conn = _conn(agent_id)
    conn.execute(
        "INSERT INTO agent_member_identities (id,agent_id,member_id,provider,external_subject,verified_at,created_at) VALUES (?,?,?,?,?,?,?)",
        (uuid.uuid4().hex, agent_id, member["id"], "app_account", app_user_id, _now(), _now()),
    )
    conn.commit()
    return await _identity_member(agent_id, "app_account", app_user_id) or member


def _hash_credential(value: str) -> str:
    return hashlib.sha256(str(value or "").encode("utf-8")).hexdigest()


_GUEST_PRUNE_LAST: dict[str, float] = {}


def _prune_never_used_guest_members(agent_id: str) -> int:
    """Remove expired guest admissions that never created workspace data."""
    now_ts = datetime.now(timezone.utc).timestamp()
    if now_ts - _GUEST_PRUNE_LAST.get(agent_id, 0) < 3600:
        return 0
    _GUEST_PRUNE_LAST[agent_id] = now_ts
    cutoff = (datetime.now(timezone.utc) - timedelta(hours=24)).isoformat()
    conn = _conn(agent_id)
    rows = conn.execute(
        """SELECT member_id FROM agent_guest_credentials
           WHERE agent_id=? AND created_at<?""",
        (agent_id, cutoff),
    ).fetchall()
    removed = 0
    for row in rows:
        member_id = str(row["member_id"])
        # Any workspace database, usage counter, or linked login means the
        # visitor is not an unused admission record and must be preserved.
        if member_db_path(agent_id, member_id).exists():
            continue
        if conn.execute(
            "SELECT 1 FROM agent_profile_usage WHERE agent_id=? AND member_id=? LIMIT 1",
            (agent_id, member_id),
        ).fetchone():
            continue
        if conn.execute(
            "SELECT 1 FROM agent_member_identities WHERE agent_id=? AND member_id=? LIMIT 1",
            (agent_id, member_id),
        ).fetchone():
            continue
        if conn.execute(
            "SELECT 1 FROM agent_member_credentials WHERE agent_id=? AND member_id=? LIMIT 1",
            (agent_id, member_id),
        ).fetchone():
            continue
        conn.execute(
            "DELETE FROM agent_guest_credentials WHERE agent_id=? AND member_id=?",
            (agent_id, member_id),
        )
        conn.execute(
            "DELETE FROM agent_members WHERE agent_id=? AND id=?",
            (agent_id, member_id),
        )
        removed += 1
    if removed:
        conn.commit()
    return removed


async def ensure_guest_member(agent_id: str, guest_credential: str = "") -> tuple[dict, str]:
    await ensure_builtins(agent_id)
    _prune_never_used_guest_members(agent_id)
    conn = _conn(agent_id)
    if guest_credential:
        row = conn.execute(
            "SELECT member_id FROM agent_guest_credentials WHERE agent_id=? AND credential_hash=?",
            (agent_id, _hash_credential(guest_credential)),
        ).fetchone()
        if row:
            member = await get_member(agent_id, row["member_id"])
            if member:
                conn.execute("DELETE FROM agent_guest_credentials WHERE credential_hash=?", (_hash_credential(guest_credential),))
                replacement = secrets.token_urlsafe(32)
                conn.execute("INSERT INTO agent_guest_credentials VALUES (?,?,?,?,?)",
                             (_hash_credential(replacement), agent_id, member["id"], member["created_at"], _now()))
                conn.commit()
                return member, replacement
    member = await _create_member(agent_id, profile_slug=VISITOR)
    credential = secrets.token_urlsafe(32)
    now = _now()
    conn.execute("INSERT INTO agent_guest_credentials VALUES (?,?,?,?,?)",
                 (_hash_credential(credential), agent_id, member["id"], now, now))
    conn.commit()
    return member, credential


async def member_for_guest_credential(agent_id: str, guest_credential: str) -> Optional[dict]:
    if not guest_credential:
        return None
    conn = _conn(agent_id)
    row = conn.execute(
        "SELECT member_id FROM agent_guest_credentials WHERE agent_id=? AND credential_hash=?",
        (agent_id, _hash_credential(guest_credential)),
    ).fetchone()
    return await get_member(agent_id, row["member_id"]) if row else None


async def get_member(agent_id: str, member_id: str) -> Optional[dict]:
    conn = _conn(agent_id)
    row = conn.execute(
        """SELECT m.*,p.id AS p_id,p.agent_id AS p_agent_id,p.slug AS p_slug,p.name AS p_name,
                  p.description AS p_description,p.kind AS p_kind,p.policy_json AS p_policy_json,
                  p.created_at AS p_created_at,p.updated_at AS p_updated_at
           FROM agent_members m JOIN agent_profiles p ON p.id=m.profile_id
           WHERE m.agent_id=? AND m.id=? AND m.status='active'""",
        (agent_id, member_id),
    ).fetchone()
    if not row:
        return None
    d = dict(row)
    profile = _profile_row({"id": d.pop("p_id"), "agent_id": d.pop("p_agent_id"),
        "slug": d.pop("p_slug"), "name": d.pop("p_name"), "description": d.pop("p_description"),
        "kind": d.pop("p_kind"), "policy_json": d.pop("p_policy_json"),
        "created_at": d.pop("p_created_at"), "updated_at": d.pop("p_updated_at")})
    return _member_row(d, profile)


async def resolve_member(agent_id: str, user_id: Optional[str], *, auto_link_app: bool = True) -> Optional[dict]:
    if not agent_id or not user_id:
        return None
    await ensure_builtins(agent_id)
    parsed = parse_subject_id(user_id)
    if parsed and parsed[0] == agent_id:
        return await get_member(agent_id, parsed[1])
    linked = await _identity_member(agent_id, "app_account", str(user_id))
    if linked:
        return linked
    # Creator/legacy agent admins are authoritative input to the one-time bridge;
    # global application administrators are intentionally not consulted.
    try:
        from app.db import get_agent_db
        roles = await get_agent_db(agent_id).get_agent_roles(agent_id)
        if user_id in (roles.get("admin_users") or []):
            return await ensure_app_member(agent_id, str(user_id), is_admin=True)
    except Exception:
        pass
    if auto_link_app and not str(user_id).startswith("anon_"):
        try:
            # A non-anonymous-looking string is not proof of an app account.
            # Only bridge identities that actually exist in the installation
            # account plane; agent-native subjects were handled above.
            from app.db import get_app_db
            account = await get_app_db().get_user_account_by_id(str(user_id))
            if not account:
                return None
            return await ensure_app_member(agent_id, str(user_id), is_admin=False)
        except PermissionError:
            return None
        except Exception:
            return None
    return None


async def set_member_admin(agent_id: str, member_id: str, enabled: bool) -> dict:
    member = await get_member(agent_id, member_id)
    if not member:
        raise LookupError("Member not found")
    conn = _conn(agent_id)
    if not enabled:
        count = conn.execute("SELECT COUNT(*) FROM agent_members WHERE agent_id=? AND status='active' AND is_agent_admin=1", (agent_id,)).fetchone()[0]
        if count <= 1:
            raise ValueError("An agent must retain at least one administrator")
    profile_slug = ADMIN if enabled else MEMBER
    profile_id = _profile_id(agent_id, profile_slug)
    conn.execute("UPDATE agent_members SET is_agent_admin=?,profile_id=?,updated_at=? WHERE agent_id=? AND id=?",
                 (int(enabled), profile_id, _now(), agent_id, member_id))
    conn.commit()
    updated = await get_member(agent_id, member_id)
    if enabled and updated:
        # Keep legacy lifecycle operations (trash/restore) compatible while all
        # authorization decisions continue to resolve from this profile plane.
        try:
            from app.db import get_agent_db
            await get_agent_db(agent_id).add_agent_admin(agent_id, updated["subject_id"])
        except Exception:
            pass
    return updated


async def assign_profile(agent_id: str, member_id: str, profile_slug: str) -> dict:
    member = await get_member(agent_id, member_id)
    if not member:
        raise LookupError("Member not found")
    if member["is_agent_admin"]:
        raise ValueError("Remove the administrator role before assigning an ordinary profile")
    if profile_slug == ADMIN:
        raise ValueError("Use administrator promotion for the protected administrator profile")
    conn = _conn(agent_id)
    profile = conn.execute("SELECT id FROM agent_profiles WHERE agent_id=? AND slug=?", (agent_id, profile_slug)).fetchone()
    if not profile:
        raise LookupError("Profile not found")
    conn.execute("UPDATE agent_members SET profile_id=?,updated_at=? WHERE agent_id=? AND id=?",
                 (profile["id"], _now(), agent_id, member_id))
    conn.commit()
    return await get_member(agent_id, member_id)


async def list_members(agent_id: str) -> list[dict]:
    await ensure_builtins(agent_id)
    conn = _conn(agent_id)
    ids = [r["id"] for r in conn.execute("SELECT id FROM agent_members WHERE agent_id=? AND status='active' ORDER BY created_at", (agent_id,)).fetchall()]
    return [m for m in [await get_member(agent_id, mid) for mid in ids] if m]


async def billing_subject(agent_id: str, member: dict) -> str:
    """Use a linked app wallet without copying any app credential into the agent."""
    conn = _conn(agent_id)
    row = conn.execute(
        "SELECT external_subject FROM agent_member_identities WHERE agent_id=? AND member_id=? AND provider='app_account' LIMIT 1",
        (agent_id, member["id"]),
    ).fetchone()
    return str(row["external_subject"]) if row else member["subject_id"]


async def consume_profile_turn(agent_id: str, member: dict, message: str = "") -> None:
    """Atomically enforce the selected profile's daily/monthly turn ceilings."""
    limits = (((member.get("profile") or {}).get("policy") or {}).get("limits") or {})
    max_chars = int(limits.get("max_message_chars") or 0)
    if max_chars > 0 and len(message or "") > max_chars:
        raise PermissionError(f"This profile allows messages up to {max_chars} characters")
    now = datetime.now(timezone.utc)
    periods = (
        ("day", now.strftime("%Y-%m-%d"), int(limits.get("daily_turns") or 0)),
        ("month", now.strftime("%Y-%m"), int(limits.get("monthly_turns") or 0)),
    )
    conn = _conn(agent_id)
    try:
        conn.execute("BEGIN IMMEDIATE")
        for period, key, ceiling in periods:
            if ceiling <= 0:
                continue
            row = conn.execute(
                "SELECT turns FROM agent_profile_usage WHERE agent_id=? AND member_id=? AND period=? AND period_key=?",
                (agent_id, member["id"], period, key),
            ).fetchone()
            if row and int(row["turns"]) >= ceiling:
                label = "daily" if period == "day" else "monthly"
                raise PermissionError(f"This profile's {label} turn limit has been reached")
            conn.execute(
                """INSERT INTO agent_profile_usage (agent_id,member_id,period,period_key,turns,updated_at)
                   VALUES (?,?,?,?,1,?) ON CONFLICT(agent_id,member_id,period,period_key)
                   DO UPDATE SET turns=turns+1,updated_at=excluded.updated_at""",
                (agent_id, member["id"], period, key, _now()),
            )
        conn.commit()
    except Exception:
        conn.rollback()
        raise


async def register_local(agent_id: str, username: str, password: str,
                         display_name: str = "", guest_credential: str = "",
                         invite_code: str = "") -> dict:
    policy = await auth_policy(agent_id)
    if policy["local_signup_mode"] == "disabled":
        raise PermissionError("Local registration is not open for this agent")
    normalized = str(username or "").strip().lower()
    if len(normalized) < 3 or len(password or "") < 8:
        raise ValueError("Username must be at least 3 characters and password at least 8 characters")
    conn = _conn(agent_id)
    target_profile_id = policy["default_profile_id"]
    invite_row = None
    if policy["local_signup_mode"] == "invite":
        invite_row = conn.execute(
            """SELECT * FROM agent_invites WHERE agent_id=? AND code_hash=?
               AND use_count < max_uses AND (expires_at IS NULL OR expires_at > ?)""",
            (agent_id, _hash_credential(invite_code), _now()),
        ).fetchone()
        if not invite_row:
            raise PermissionError("A valid invitation is required for this agent")
        target_profile_id = invite_row["profile_id"]
        # Reserve the use atomically so concurrent registrations cannot both
        # consume the last slot after racing through the SELECT above.
        reserved = conn.execute(
            "UPDATE agent_invites SET use_count=use_count+1 WHERE code_hash=? AND use_count < max_uses",
            (invite_row["code_hash"],),
        )
        conn.commit()
        if reserved.rowcount != 1:
            raise PermissionError("That invitation has already been used")
    if conn.execute("SELECT 1 FROM agent_member_credentials WHERE agent_id=? AND username_normalized=?",
                    (agent_id, normalized)).fetchone():
        raise ValueError("That agent username is already registered")
    from app.auth.users import _hash_password
    member = await member_for_guest_credential(agent_id, guest_credential)
    if member:
        conn.execute(
            "UPDATE agent_members SET profile_id=?,display_name=?,email_normalized=?,updated_at=? WHERE agent_id=? AND id=?",
            (target_profile_id, str(display_name or username)[:120],
             normalized if "@" in normalized else "", _now(), agent_id, member["id"]),
        )
        conn.execute("DELETE FROM agent_guest_credentials WHERE agent_id=? AND member_id=?",
                     (agent_id, member["id"]))
        conn.commit()
    else:
        target = conn.execute("SELECT slug FROM agent_profiles WHERE id=?", (target_profile_id,)).fetchone()
        member = await _create_member(agent_id, profile_slug=target["slug"] if target else MEMBER,
                                      display_name=display_name or username,
                                      email=username if "@" in username else "")
        conn = _conn(agent_id)
    now = _now()
    try:
        conn.execute("INSERT INTO agent_member_credentials VALUES (?,?,?,?,?,?)",
                     (member["id"], agent_id, normalized, _hash_password(password), now, now))
        conn.execute("INSERT INTO agent_member_identities VALUES (?,?,?,?,?,?,?)",
                     (uuid.uuid4().hex, agent_id, member["id"], "agent_password", normalized, now, now))
        conn.commit()
    except sqlite3.IntegrityError as exc:
        conn.rollback()
        raise ValueError("That agent username is already registered") from exc
    return await get_member(agent_id, member["id"]) or member


async def create_invite(agent_id: str, profile_slug: str = MEMBER,
                        max_uses: int = 1, expires_at: Optional[str] = None) -> dict:
    await ensure_builtins(agent_id)
    conn = _conn(agent_id)
    profile = conn.execute("SELECT id FROM agent_profiles WHERE agent_id=? AND slug=?",
                           (agent_id, profile_slug)).fetchone()
    if not profile or profile_slug == ADMIN:
        raise ValueError("Invitation profile is invalid")
    code = secrets.token_urlsafe(24)
    conn.execute("INSERT INTO agent_invites VALUES (?,?,?,?,?,?,?)",
                 (_hash_credential(code), agent_id, profile["id"], max(1, min(int(max_uses), 10000)),
                  0, expires_at or None, _now()))
    conn.commit()
    return {"invite_code": code, "profile_slug": profile_slug, "max_uses": max_uses,
            "expires_at": expires_at}


async def link_app_identity(agent_id: str, app_user_id: str,
                            guest_credential: str = "") -> dict:
    existing = await _identity_member(agent_id, "app_account", app_user_id)
    if existing:
        return existing
    policy = await auth_policy(agent_id)
    if not policy["app_login_enabled"] or policy["app_login_enrollment"] != "open":
        raise PermissionError("App-account enrollment is not open for this agent")
    member = await member_for_guest_credential(agent_id, guest_credential)
    if member:
        conn = _conn(agent_id)
        conn.execute("UPDATE agent_members SET profile_id=?,updated_at=? WHERE agent_id=? AND id=?",
                     (_profile_id(agent_id, MEMBER), _now(), agent_id, member["id"]))
        conn.execute("DELETE FROM agent_guest_credentials WHERE agent_id=? AND member_id=?",
                     (agent_id, member["id"]))
        conn.execute(
            "INSERT INTO agent_member_identities (id,agent_id,member_id,provider,external_subject,verified_at,created_at) VALUES (?,?,?,?,?,?,?)",
            (uuid.uuid4().hex, agent_id, member["id"], "app_account", app_user_id, _now(), _now()),
        )
        conn.commit()
        return await get_member(agent_id, member["id"]) or member
    return await ensure_app_member(agent_id, app_user_id)


async def authenticate_local(agent_id: str, username: str, password: str) -> Optional[dict]:
    from app.auth.users import _verify_password
    normalized = str(username or "").strip().lower()
    conn = _conn(agent_id)
    row = conn.execute(
        "SELECT member_id,password_hash FROM agent_member_credentials WHERE agent_id=? AND username_normalized=?",
        (agent_id, normalized),
    ).fetchone()
    if not row or not _verify_password(password, row["password_hash"]):
        return None
    return await get_member(agent_id, row["member_id"])


def safe_principal(member: Optional[dict]) -> Optional[dict]:
    if not member:
        return None
    profile = member.get("profile") or {}
    return {
        "member_id": member.get("id"), "subject_id": member.get("subject_id"),
        "display_name": member.get("display_name") or "", "is_agent_admin": bool(member.get("is_agent_admin")),
        "profile": {"id": profile.get("id"), "slug": profile.get("slug"), "name": profile.get("name"),
                    "policy": profile.get("policy") or {}},
    }


def policy_allows(values: Iterable[str], name: str) -> bool:
    allowed = {str(v) for v in (values or [])}
    return "*" in allowed or name in allowed


async def filter_profile_abilities(agent_id: str, user_id: Optional[str], abilities: set[str]) -> tuple[set[str], Optional[dict]]:
    member = await resolve_member(agent_id, user_id)
    if not member:
        return set(abilities), None
    allowed = ((member.get("profile") or {}).get("policy") or {}).get("abilities") or []
    return ({a for a in abilities if policy_allows(allowed, a)}, member)


async def filter_profile_tools(agent_id: str, user_id: Optional[str], tools: dict) -> tuple[dict, Optional[dict]]:
    member = await resolve_member(agent_id, user_id)
    if not member:
        return dict(tools), None
    profile = member.get("profile") or {}
    allowed = (profile.get("policy") or {}).get("tools") or []
    kept = {
        name: info for name, info in tools.items()
        if policy_allows(allowed, name)
    }
    if profile.get("slug") == VISITOR:
        kept.update({name: tools[name] for name in VISITOR_CORE_TOOLS if name in tools})
    if not member.get("is_agent_admin"):
        for name in ADMIN_PROFILE_TOOLS:
            kept.pop(name, None)
    return kept, member

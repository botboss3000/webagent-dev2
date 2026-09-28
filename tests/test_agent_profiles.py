import asyncio
import sqlite3

import pytest

from app.agent import profiles
from app.agent.member_workspace import parse_subject_id, subject_id


@pytest.fixture()
def profile_db(monkeypatch):
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    conn.executescript(profiles.SCHEMA_SQL)
    monkeypatch.setattr(profiles, "_conn", lambda _agent_id: conn)
    yield conn
    conn.close()


def run(coro):
    return asyncio.run(coro)


def test_agent_member_subject_round_trip_handles_delimiters():
    value = subject_id("customer--support", "member--one")
    assert parse_subject_id(value) == ("customer--support", "member--one")
    assert parse_subject_id("agentmember--broken") is None


def test_builtin_profiles_and_creator_admin_are_agent_owned(profile_db, monkeypatch):
    # Avoid the legacy-role bridge during this isolated storage test.
    class Backend:
        async def get_agent_roles(self, _agent_id):
            return {"admin_users": [], "member_users": [], "authorized_users": []}

    monkeypatch.setattr("app.db.get_agent_db", lambda _agent_id: Backend())
    builtins = run(profiles.ensure_builtins("agent-1"))
    assert set(builtins) == {profiles.VISITOR, profiles.MEMBER, profiles.ADMIN}
    assert run(profiles.auth_policy("agent-1"))["local_signup_mode"] == "open"
    assert run(profiles.auth_policy("agent-1"))["app_login_enabled"] is True


def test_guest_registration_upgrades_same_member_and_hashes_password(profile_db, monkeypatch):
    class Backend:
        async def get_agent_roles(self, _agent_id):
            return {"admin_users": [], "member_users": [], "authorized_users": []}

    monkeypatch.setattr("app.db.get_agent_db", lambda _agent_id: Backend())
    visitor, credential = run(profiles.ensure_guest_member("agent-1"))
    registered = run(profiles.register_local(
        "agent-1", "member@example.com", "correct horse battery staple",
        "Member", credential,
    ))
    assert registered["id"] == visitor["id"]
    assert registered["profile"]["slug"] == profiles.MEMBER
    row = profile_db.execute(
        "SELECT password_hash FROM agent_member_credentials WHERE member_id=?",
        (visitor["id"],),
    ).fetchone()
    assert row and row["password_hash"] != "correct horse battery staple"
    assert row["password_hash"].startswith("$2")
    assert run(profiles.authenticate_local(
        "agent-1", "member@example.com", "correct horse battery staple",
    ))["id"] == visitor["id"]


def test_profiles_filter_tools_and_protect_admin_tool(profile_db, monkeypatch):
    class Backend:
        async def get_agent_roles(self, _agent_id):
            return {"admin_users": [], "member_users": [], "authorized_users": []}

    monkeypatch.setattr("app.db.get_agent_db", lambda _agent_id: Backend())
    visitor, _ = run(profiles.ensure_guest_member("agent-1"))
    tools = {name: object() for name in (
        "calculate", "request_agent_login", "web_search", "manage_agent_profiles",
    )}
    kept, _member = run(profiles.filter_profile_tools(
        "agent-1", visitor["subject_id"], tools,
    ))
    assert set(kept) == {"calculate", "request_agent_login"}


def test_last_agent_administrator_cannot_be_demoted(profile_db, monkeypatch):
    class Backend:
        async def get_agent_roles(self, _agent_id):
            return {"admin_users": [], "member_users": [], "authorized_users": []}

    monkeypatch.setattr("app.db.get_agent_db", lambda _agent_id: Backend())
    admin = run(profiles._create_member("agent-1", is_admin=True))
    with pytest.raises(ValueError, match="retain at least one"):
        run(profiles.set_member_admin("agent-1", admin["id"], False))


def test_custom_profile_turn_limit_is_enforced_atomically(profile_db, monkeypatch):
    class Backend:
        async def get_agent_roles(self, _agent_id):
            return {"admin_users": [], "member_users": [], "authorized_users": []}

    monkeypatch.setattr("app.db.get_agent_db", lambda _agent_id: Backend())
    run(profiles.upsert_profile(
        "agent-1", slug="standard", name="Standard",
        policy={"abilities": ["*"], "tools": ["*"], "limits": {"daily_turns": 1}},
    ))
    member = run(profiles._create_member("agent-1", profile_slug="standard"))
    run(profiles.consume_profile_turn("agent-1", member, "first"))
    with pytest.raises(PermissionError, match="daily turn limit"):
        run(profiles.consume_profile_turn("agent-1", member, "second"))


def test_invite_signup_uses_invited_profile_and_preserves_guest(profile_db, monkeypatch):
    class Backend:
        async def get_agent_roles(self, _agent_id):
            return {"admin_users": [], "member_users": [], "authorized_users": []}

    monkeypatch.setattr("app.db.get_agent_db", lambda _agent_id: Backend())
    run(profiles.upsert_profile(
        "agent-1", slug="employee", name="Employee",
        policy={"abilities": ["knowledge"], "tools": ["wiki_search"]},
    ))
    run(profiles.update_auth_policy("agent-1", {"local_signup_mode": "invite"}))
    invitation = run(profiles.create_invite("agent-1", "employee"))
    visitor, credential = run(profiles.ensure_guest_member("agent-1"))
    with pytest.raises(PermissionError, match="invitation"):
        run(profiles.register_local("agent-1", "employee", "password123", "", credential))
    member = run(profiles.register_local(
        "agent-1", "employee", "password123", "", credential,
        invitation["invite_code"],
    ))
    assert member["id"] == visitor["id"]
    assert member["profile"]["slug"] == "employee"


def test_agent_and_member_vaults_follow_owned_workspace(tmp_path, monkeypatch):
    from app.db.local import LocalBackend

    monkeypatch.setattr(LocalBackend, "_init_db", lambda self: None)
    agent_dir = tmp_path / "agent-1"
    member_dir = agent_dir / "members" / "member-1"
    member_dir.mkdir(parents=True)
    agent_backend = LocalBackend(str(agent_dir / "agent-1.db"), seed=False, plane="agent")
    member_backend = LocalBackend(str(member_dir / "member-1.db"), seed=False, plane="user")
    assert agent_backend._agent_vault_path == str(agent_dir / "agent-1.agent-secrets.db")
    assert member_backend._agent_vault_path == str(agent_dir / "agent-1.agent-secrets.db")
    assert member_backend._user_vault_path == str(member_dir / "member-1.user-secrets.db")


def test_agent_local_subject_is_not_an_app_account(profile_db):
    from app.auth.identity import _is_anonymous

    assert _is_anonymous(subject_id("agent-1", "member-1")) is True


def test_installation_admin_has_no_implicit_agent_admin_role(monkeypatch):
    from app.api.agents import _is_agent_admin
    from app.auth.identity import set_verified_caller_uid

    class AppDb:
        async def is_user_admin(self, _user_id):
            raise AssertionError("global app-admin status must not be consulted")

    async def no_member(_agent_id, _user_id, **_kwargs):
        return None

    monkeypatch.setattr(profiles, "resolve_member", no_member)
    set_verified_caller_uid("installation-admin")
    try:
        assert run(_is_agent_admin(AppDb(), "agent-1", "installation-admin")) is False
    finally:
        set_verified_caller_uid(None)

"""Agent-owned profile, membership, and authentication APIs."""

from __future__ import annotations

from typing import Any, Optional

from fastapi import APIRouter, HTTPException, Request
from pydantic import BaseModel, Field

from app.agent import profiles
from app.auth.identity import assert_caller_is, request_user_id
from app.auth.jwt import create_access_token


router = APIRouter(prefix="/api/v1/agents", tags=["agent profiles"])


async def _require_agent(agent_id: str) -> dict:
    from app.db import get_db
    agent = await get_db().get_agent_by_id(agent_id)
    if not agent:
        raise HTTPException(status_code=404, detail="Agent not found")
    return agent


def _token(agent_id: str, member: dict) -> str:
    subject = member["subject_id"]
    return create_access_token(
        username=subject,
        user_id=subject,
        expires_minutes=60 * 24 * 30,
        extra_claims={
            "agent_id": agent_id,
            "agent_member_id": member["id"],
            "agent_identity": True,
        },
    )


async def _agent_admin(request: Request, agent_id: str) -> dict:
    uid = request_user_id(request)
    if not uid:
        raise HTTPException(status_code=401, detail="Not authenticated")
    member = await profiles.resolve_member(agent_id, uid)
    if not member or not member.get("is_agent_admin"):
        raise HTTPException(status_code=403, detail="Agent administrator access required")
    return member


class GuestBody(BaseModel):
    guest_credential: Optional[str] = None
    browser_id: str = ""


class LocalAuthBody(BaseModel):
    username: str = Field(min_length=3, max_length=320)
    password: str = Field(min_length=8, max_length=1024)
    display_name: str = Field(default="", max_length=120)
    guest_credential: str = Field(default="", max_length=512)
    invite_code: str = Field(default="", max_length=512)


class AppLinkBody(BaseModel):
    app_user_id: str = ""
    guest_credential: str = Field(default="", max_length=512)


class ProfileBody(BaseModel):
    slug: str
    name: str
    description: str = ""
    policy: dict[str, Any] = Field(default_factory=dict)


class AuthPolicyBody(BaseModel):
    app_login_enabled: Optional[bool] = None
    app_login_enrollment: Optional[str] = None
    local_signup_mode: Optional[str] = None
    transcript_review: Optional[bool] = None


class AssignmentBody(BaseModel):
    profile_slug: str


class AdminBody(BaseModel):
    enabled: bool


class InviteBody(BaseModel):
    profile_slug: str = profiles.MEMBER
    max_uses: int = Field(default=1, ge=1, le=10000)
    expires_at: Optional[str] = None


@router.get("/{agent_id}/auth/config")
async def public_auth_config(agent_id: str):
    await _require_agent(agent_id)
    try:
        policy = await profiles.auth_policy(agent_id)
    except Exception as exc:
        raise HTTPException(status_code=404, detail="Agent not found") from exc
    return {
        "app_login_enabled": policy["app_login_enabled"],
        "app_login_enrollment": policy["app_login_enrollment"],
        "local_signup_mode": policy["local_signup_mode"],
        "member_profile": profiles.MEMBER,
    }


@router.post("/{agent_id}/auth/guest")
async def guest_login(agent_id: str, body: GuestBody, request: Request):
    from app.db import get_db
    db = get_db()
    agent = await db.get_agent_by_id(agent_id)
    if not agent:
        raise HTTPException(status_code=404, detail="Agent not found")
    if (agent.get("user_mode") or "anonymous") != "anonymous":
        raise HTTPException(status_code=403, detail="This agent is not open to visitors")
    from app.agent.public_policy import require_public_funding
    await require_public_funding(db, agent)
    from app.api.rate_limit import enforce_anon_session_creation, bind_anon_session_identity
    admission_key = body.browser_id or body.guest_credential or agent_id
    reservation = await enforce_anon_session_creation(request, admission_key)
    member, replacement = await profiles.ensure_guest_member(
        agent_id, body.guest_credential or "",
    )
    await bind_anon_session_identity(reservation, member["subject_id"])
    token = create_access_token(
        username=member["subject_id"], user_id=member["subject_id"], expires_minutes=60,
        extra_claims={"agent_id": agent_id, "agent_member_id": member["id"],
                      "agent_identity": True, "anon_admission": True},
    )
    return {
        "token": token,
        "user_id": member["subject_id"],
        "session_id": member["subject_id"],
        "guest_credential": replacement,
        "principal": profiles.safe_principal(member),
        "access_token_expires_in": 60 * 60,
    }


@router.post("/{agent_id}/auth/register")
async def local_register(agent_id: str, body: LocalAuthBody):
    await _require_agent(agent_id)
    try:
        member = await profiles.register_local(
            agent_id, body.username, body.password, body.display_name,
            body.guest_credential, body.invite_code,
        )
    except PermissionError as exc:
        raise HTTPException(status_code=403, detail=str(exc)) from exc
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    return {"token": _token(agent_id, member), "user_id": member["subject_id"],
            "principal": profiles.safe_principal(member)}


@router.post("/{agent_id}/auth/login")
async def local_login(agent_id: str, body: LocalAuthBody):
    await _require_agent(agent_id)
    member = await profiles.authenticate_local(agent_id, body.username, body.password)
    if not member:
        # Deliberately generic: do not disclose whether the username exists.
        raise HTTPException(status_code=401, detail="Invalid agent credentials")
    return {"token": _token(agent_id, member), "user_id": member["subject_id"],
            "principal": profiles.safe_principal(member)}


@router.post("/{agent_id}/auth/app-link")
async def app_link(agent_id: str, body: AppLinkBody, request: Request):
    await _require_agent(agent_id)
    # Prove the actual app-account token subject. Platform administrators have
    # no user-workspace impersonation path and cannot manufacture authority
    # inside an agent.
    proven = await assert_caller_is(request, None)
    if body.app_user_id and body.app_user_id != proven:
        raise HTTPException(status_code=403, detail="App account does not match authenticated caller")
    try:
        member = await profiles.link_app_identity(agent_id, proven, body.guest_credential)
    except PermissionError as exc:
        raise HTTPException(status_code=403, detail=str(exc)) from exc
    return {"token": _token(agent_id, member), "user_id": member["subject_id"],
            "principal": profiles.safe_principal(member)}


@router.get("/{agent_id}/auth/me")
async def auth_me(agent_id: str, request: Request):
    await _require_agent(agent_id)
    uid = request_user_id(request)
    member = await profiles.resolve_member(agent_id, uid, auto_link_app=False) if uid else None
    return {"authenticated": bool(member and (member.get("profile") or {}).get("slug") != profiles.VISITOR),
            "principal": profiles.safe_principal(member)}


@router.get("/{agent_id}/profiles")
async def profile_list(agent_id: str, request: Request):
    await _agent_admin(request, agent_id)
    return {"profiles": await profiles.list_profiles(agent_id),
            "auth_policy": await profiles.auth_policy(agent_id)}


@router.put("/{agent_id}/profiles/{slug}")
async def profile_put(agent_id: str, slug: str, body: ProfileBody, request: Request):
    await _agent_admin(request, agent_id)
    if slug != body.slug:
        raise HTTPException(status_code=400, detail="Profile slug does not match URL")
    try:
        return {"profile": await profiles.upsert_profile(
            agent_id, slug=slug, name=body.name,
            description=body.description, policy=body.policy,
        )}
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


@router.delete("/{agent_id}/profiles/{slug}")
async def profile_delete(agent_id: str, slug: str, request: Request):
    await _agent_admin(request, agent_id)
    try:
        await profiles.delete_profile(agent_id, slug)
    except LookupError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    except ValueError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    return {"deleted": True}


@router.put("/{agent_id}/auth/policy")
async def auth_policy_put(agent_id: str, body: AuthPolicyBody, request: Request):
    await _agent_admin(request, agent_id)
    try:
        return {"auth_policy": await profiles.update_auth_policy(
            agent_id, {k: v for k, v in body.dict().items() if v is not None},
        )}
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


@router.post("/{agent_id}/auth/invitations")
async def invitation_create(agent_id: str, body: InviteBody, request: Request):
    await _agent_admin(request, agent_id)
    try:
        return await profiles.create_invite(
            agent_id, body.profile_slug, body.max_uses, body.expires_at,
        )
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


@router.get("/{agent_id}/profile-members")
async def member_list(agent_id: str, request: Request):
    await _agent_admin(request, agent_id)
    return {"members": [profiles.safe_principal(m) for m in await profiles.list_members(agent_id)]}


@router.get("/{agent_id}/profile-members/{member_id}/transcript")
async def member_transcript_review(agent_id: str, member_id: str, request: Request,
                                   session_id: str = ""):
    """Review a member transcript only under the agent's disclosed opt-in policy."""
    await _agent_admin(request, agent_id)
    policy = await profiles.auth_policy(agent_id)
    if not policy.get("transcript_review"):
        raise HTTPException(status_code=403, detail="Agent transcript review is disabled")
    member = await profiles.get_member(agent_id, member_id)
    if not member:
        raise HTTPException(status_code=404, detail="Member not found")
    from app.db import get_user_db
    conn = get_user_db(member["subject_id"])._get_conn()
    try:
        if not session_id:
            rows = conn.execute(
                "SELECT id,title,status,created_at,updated_at FROM sessions WHERE agent_id=? ORDER BY updated_at DESC LIMIT 100",
                (agent_id,),
            ).fetchall()
            return {"sessions": [dict(row) for row in rows], "disclosed_review": True}
        owns = conn.execute("SELECT 1 FROM sessions WHERE id=? AND agent_id=?", (session_id, agent_id)).fetchone()
        if not owns:
            raise HTTPException(status_code=404, detail="Session not found")
        rows = conn.execute(
            "SELECT id,role,content,created_at FROM interactions WHERE session_id=? ORDER BY created_at,id LIMIT 500",
            (session_id,),
        ).fetchall()
        return {"session_id": session_id, "interactions": [dict(row) for row in rows],
                "disclosed_review": True}
    finally:
        conn.close()


@router.put("/{agent_id}/profile-members/{member_id}/profile")
async def member_profile_put(agent_id: str, member_id: str, body: AssignmentBody, request: Request):
    await _agent_admin(request, agent_id)
    try:
        member = await profiles.assign_profile(agent_id, member_id, body.profile_slug)
    except LookupError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    except ValueError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    return {"principal": profiles.safe_principal(member)}


@router.put("/{agent_id}/profile-members/{member_id}/administrator")
async def member_admin_put(agent_id: str, member_id: str, body: AdminBody, request: Request):
    await _agent_admin(request, agent_id)
    try:
        member = await profiles.set_member_admin(agent_id, member_id, body.enabled)
    except LookupError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    except ValueError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    return {"principal": profiles.safe_principal(member)}

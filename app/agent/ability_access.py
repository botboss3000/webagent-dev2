"""Per-ability caller-access gating.

Each agent may mark any of its enabled abilities with a required **caller-access
level** — ``everyone`` (default), ``registered``, or ``admin`` — stored per agent
in ``agents.metadata.ability_access`` (see ``db.get_agent_ability_access``). This
module turns that map plus the live caller's identity into the subset of
abilities the caller is actually allowed to trigger.

It is enforced at **tool-assembly time** (``app/tools/loader.load_tools``) so the
gated ability's tools are never materialized for a caller below the rung — a real
boundary, not a prompt instruction the model could ignore — and again on the
prompt's ability-bundled skills (``app/agent/prompts.append_skills_section``) so
the agent isn't even told about tools it doesn't have this turn.

The default level is ``everyone``, and the access map only ever stores genuine
restrictions, so an agent with no level set behaves exactly as before.

Fully guarded throughout:
  * caller ranking fails **down** (any error → the lowest, anonymous, rank) so a
    glitch can never escalate privileges; and
  * the legacy per-agent map remains best-effort, while the authoritative tier
    filter fails restrictive to ``chat_core`` if policy resolution is unavailable.
"""

from __future__ import annotations

import logging
from typing import Optional, Set

from app.tools.tool_modes import (
    ACCESS_RANK,
    resolve_ability_access,
)
from app.entitlements.abilities import ability_group, filter_abilities_by_groups
from app.entitlements.service import resolve_capabilities

logger = logging.getLogger(__name__)


def _is_anonymous_id(user_id: Optional[str]) -> bool:
    """A not-signed-in guest. The canonical marker is the ``anon_`` id prefix
    minted by the guest-login path (open registration)."""
    return (not user_id) or str(user_id).startswith("anon_")


async def caller_access_rank(db, user_id: Optional[str], agent_id: str = "") -> int:
    """Rank the live caller on the access ladder: 3 = admin, 2 = registered
    (signed-in, non-anonymous), 1 = everyone (anonymous guest / unknown).

    Mirrors how ``app/api/chat._enforce_agent_access_policy`` distinguishes the
    tiers (global-admin check, then the channel identity's ``user_tier``). Fails
    **down** — any error yields the lowest rank, never an escalation."""
    # Within an agent, only that agent's protected administrator role is the
    # top rung. Installation administrators have no automatic agent authority.
    if agent_id:
        try:
            from app.agent.profiles import resolve_member, VISITOR
            member = await resolve_member(agent_id, user_id)
            if member:
                if member.get("is_agent_admin"):
                    return ACCESS_RANK["admin"]
                slug = ((member.get("profile") or {}).get("slug") or "")
                return ACCESS_RANK["everyone" if slug == VISITOR else "registered"]
        except Exception as e:
            logger.debug("agent profile rank lookup failed (treating as visitor): %s", e)
            return ACCESS_RANK["everyone"]

    # An anonymous guest is the lowest rung.
    if _is_anonymous_id(user_id):
        return ACCESS_RANK["everyone"]

    # Otherwise consult the channel identity's tier as a secondary signal; a
    # missing/unknown tier on a non-anon id is treated as a registered account.
    try:
        conn = db._get_conn()
        try:
            row = conn.execute(
                "SELECT user_tier FROM channel_identities WHERE user_id = ? "
                "ORDER BY updated_at DESC LIMIT 1",
                (user_id,),
            ).fetchone()
        finally:
            conn.close()
        tier = (row["user_tier"] if row else None) or ""
        if str(tier).strip().lower() == "anonymous":
            return ACCESS_RANK["everyone"]
    except Exception as e:
        logger.debug("caller_access_rank tier check failed (treating as registered): %s", e)

    return ACCESS_RANK["registered"]


async def filter_abilities_for_caller(
    agent_id: str,
    enabled: Set[str],
    user_id: Optional[str],
    db=None,
) -> Set[str]:
    """Drop from ``enabled`` any ability whose required caller-access level the
    live caller doesn't meet. Returns the kept subset (a new set).

    The legacy per-agent access map remains best-effort.  The experience-tier
    group filter is authoritative and fails restrictive if its backend cannot
    resolve a policy."""
    if not enabled:
        return enabled
    if db is None:
        from app.db import get_db
        db = get_db()

    kept = set(enabled)
    try:
        access_map = await db.get_agent_ability_access(agent_id) if agent_id else {}
        if access_map:
            rank = await caller_access_rank(db, user_id, agent_id)
            kept = {
                ability for ability in kept
                if ACCESS_RANK.get(resolve_ability_access(ability, access_map), 1) <= rank
            }
    except Exception as e:
        logger.warning("per-agent ability access lookup failed (using defaults): %s", e)

    try:
        from app.agent.profiles import filter_profile_abilities, VISITOR
        profile_kept, member = await filter_profile_abilities(agent_id, user_id, kept)
        if member:
            tier_kept = profile_kept
            if ((member.get("profile") or {}).get("slug") == VISITOR):
                from app.agent.public_policy import NON_DELEGABLE_ABILITY_GROUPS
                tier_kept = {
                    ability for ability in tier_kept
                    if ability_group(ability) not in NON_DELEGABLE_ABILITY_GROUPS
                }
            if len(tier_kept) != len(enabled):
                logger.debug("agent profile gate on %s dropped %s", agent_id,
                             sorted(set(enabled) - tier_kept))
            return tier_kept
        if _is_anonymous_id(user_id) and agent_id:
            agent = await db.get_agent_by_id(agent_id)
            from app.agent.public_policy import normalize_public_access
            public_caps = normalize_public_access(agent or {}).get("capabilities") or {}
            allowed_abilities = set(public_caps.get("abilities") or [])
            from app.agent.public_policy import NON_DELEGABLE_ABILITY_GROUPS
            tier_kept = {
                ability for ability in (kept & allowed_abilities)
                if ability_group(ability) not in NON_DELEGABLE_ABILITY_GROUPS
            }
        else:
            capabilities = await resolve_capabilities(user_id, db=db)
            allowed_groups = set(capabilities.get("ability_groups") or [])
            tier_kept = filter_abilities_by_groups(kept, allowed_groups)
    except Exception as e:
        logger.warning("entitlement ability lookup failed (restricting to chat core): %s", e)
        tier_kept = filter_abilities_by_groups(kept, {"chat_core"})
    if len(tier_kept) != len(enabled):
        logger.debug(
            "ability access gate on agent %s dropped %s",
            agent_id, sorted(set(enabled) - tier_kept),
        )
    return tier_kept

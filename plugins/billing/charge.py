"""The single post-hoc charge path for text runs and image generation.

Every usage event that should hit billing funnels through ``record_and_charge``
so the trial-grant decrement, the wallet debit, the usage_events row, the
platform split and the agent-admin earnings mirror are computed exactly once, in
one place:

  * the chat loop's ``_record_billing_usage`` (app/agent/loop.py) is a thin
    wrapper over it, and
  * the image-generation handler calls it directly after a successful render.

Best-effort by design: any failure is logged and swallowed so the chat/image
flow never breaks because billing misbehaved.
"""

from __future__ import annotations

import logging
import uuid
from typing import Any, Optional

from plugins.billing import wallet as wallet_mod
from plugins.billing import pricing as pricing_mod
from plugins.billing.extensions import apply_record, apply_split

logger = logging.getLogger(__name__)


def locked_house_credits(is_byo_llm: bool, credit_units: float) -> float:
    """Return the immutable house-credit charge; BYO calls never consume it."""
    return 0.0 if is_byo_llm else max(0.0, float(credit_units or 0.0))


async def record_and_charge(
    db: Any,
    agent_id: str,
    user_id: str,
    *,
    input_tokens: int = 0,
    output_tokens: int = 0,
    provider_cost_cents: int = 0,
    model: Optional[str] = None,
    provider: Optional[str] = None,
    entry_id: Optional[str] = None,
    interaction_id: Optional[str] = None,
    session_id: Optional[str] = None,
    cost_usd: float = 0.0,
    cost_source: Optional[str] = None,
    credit_units: float = 0.0,
    credit_source: Optional[str] = None,
    cached_input_tokens: int = 0,
    cache_write_tokens: int = 0,
    uncached_input_tokens: Optional[int] = None,
    reasoning_tokens: int = 0,
    source: str = "chat",
    own_key: bool = False,
) -> Optional[dict]:
    """Compute the charge, record the usage_events row, decrement the trial
    grant, and debit the credit wallet. Returns a compact dict for event-stream
    callers, or None on failure / when billing tables don't exist.

    ``own_key`` tells the engine the user's own key pays for THIS event (e.g.
    image generation on the user's own image provider), bypassing the text-LLM
    probe. Text runs leave it False and let the engine probe the user's text
    config."""
    if not agent_id or not user_id:
        return None
    try:
        # Billing is the CENTRAL account plane: usage_events, wallets, trials
        # and subscriptions must stay in one place even when interaction data is
        # scattered across per-user databases (user BYOD). Resolve the control
        # DB and use it for every billing read/write below; the agent CONFIG is
        # still read via `db` (the caller's own database in the self-contained
        # model). No-op in single-tenant mode (get_app_db() == the one DB).
        from app.db import get_app_db
        from app.db.offload import db_offload
        cdb = get_app_db()

        # The agent record lives in the CALLER's database (user-plane / self-
        # contained), not in the control DB — mirror the loop's split. Offloaded:
        # get_agent_by_id / get_agent_roles run synchronous SQLite I/O inside
        # async wrappers, so they must not run on the main event loop.
        agent = await db_offload(lambda: db.get_agent_by_id(agent_id))
        if not agent:
            return None

        # Anonymous public traffic is sponsored by the agent, never by the
        # visitor. Resolve the payer before charge computation and preserve the
        # caller id on the usage event for abuse/accounting attribution.
        billing_user_id = user_id
        public_funding = None
        if str(user_id).startswith("anon_"):
            from app.agent.public_policy import require_public_funding
            public_funding = await require_public_funding(cdb, agent)
            if public_funding["mode"] == "owner_wallet":
                billing_user_id = public_funding["owner_user_id"]

        usage = pricing_mod.Usage(
            input_tokens=int(input_tokens or 0),
            output_tokens=int(output_tokens or 0),
            provider_cost_cents=int(provider_cost_cents or 0),
            message_count=1,
        )
        result = await pricing_mod.resolve_charge(
            agent, billing_user_id, usage, cdb,
            own_llm=(True if public_funding and public_funding["mode"] == "dedicated_key"
                     else True if own_key else None),
        )
        charge = result.end_user_charge_cents
        locked_credit_units = locked_house_credits(result.is_byo_llm, credit_units)
        locked_credit_source = "byo" if result.is_byo_llm else (credit_source or "unconfigured")
        if public_funding and public_funding["mode"] == "owner_wallet":
            # Public sponsorship pays actual provider cost, with the existing
            # one-cent accounting floor when a provider reports a nonzero call.
            actual_cents = int(provider_cost_cents or 0)
            if actual_cents <= 0 and float(cost_usd or 0) > 0:
                import math
                actual_cents = max(1, int(math.ceil(float(cost_usd) * 100)))
            charge = actual_cents
            result.end_user_charge_cents = charge
            result.wallet_charge_cents = charge
            result.trial_used_cents = 0
            result.is_trial = False
            result.is_exempt = False
            result.strategy = "public_owner_wallet"

        # The end user pays `charge`; the agent admin keeps it. An optional
        # billing extension, if installed, may allocate part of the charge
        # elsewhere (no-op otherwise: nothing deducted, the agent keeps it all)
        # and record its own accounting below.
        event_id = str(uuid.uuid4())
        if public_funding:
            # Sponsoring your own public visitor is a provider-cost payment, not
            # a sale back to yourself; do not create agent-admin earnings.
            deducted_cents, agent_earnings_cents = charge, 0
        else:
            deducted_cents, agent_earnings_cents = await apply_split(cdb, agent, charge)

        # Insert usage_events row (always, even free/exempt, for visibility).
        try:
            if hasattr(cdb, "_get_conn"):
                conn = cdb._get_conn()
                try:
                    conn.execute(
                        "INSERT INTO usage_events ("
                        "id, agent_id, user_id, interaction_id, input_tokens, output_tokens, "
                        "provider_cost_cents, end_user_charge_cents, "
                        "agent_admin_earnings_cents, strategy, is_byo_llm, is_trial, is_exempt, "
                        "model, provider, entry_id, cost_usd, cost_source, credit_units, credit_source, session_id, source, "
                        "cached_input_tokens, cache_write_tokens, uncached_input_tokens, reasoning_tokens"
                        ") VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                        (
                            event_id, agent_id, user_id, interaction_id,
                            usage.input_tokens, usage.output_tokens,
                            int(provider_cost_cents or 0),
                            charge, agent_earnings_cents, result.strategy,
                            1 if result.is_byo_llm else 0,
                            1 if result.is_trial else 0,
                            1 if result.is_exempt else 0,
                            model, provider, entry_id or None,
                            float(cost_usd or 0), cost_source,
                            locked_credit_units, locked_credit_source, session_id, source,
                            int(cached_input_tokens or 0), int(cache_write_tokens or 0),
                            uncached_input_tokens, int(reasoning_tokens or 0),
                        ),
                    )
                    conn.commit()
                finally:
                    conn.close()
            elif hasattr(cdb, "get_raw_client"):
                cdb.get_raw_client().table("usage_events").insert({
                    "id": event_id,
                    "agent_id": agent_id,
                    "user_id": user_id,
                    "interaction_id": interaction_id,
                    "input_tokens": usage.input_tokens,
                    "output_tokens": usage.output_tokens,
                    "provider_cost_cents": int(provider_cost_cents or 0),
                    "end_user_charge_cents": charge,
                    "agent_admin_earnings_cents": agent_earnings_cents,
                    "strategy": result.strategy,
                    "is_byo_llm": 1 if result.is_byo_llm else 0,
                    "is_trial": 1 if result.is_trial else 0,
                    "is_exempt": 1 if result.is_exempt else 0,
                    "model": model,
                    "provider": provider,
                    "entry_id": entry_id or None,
                    "cost_usd": float(cost_usd or 0),
                    "cost_source": cost_source,
                    "credit_units": locked_credit_units,
                    "credit_source": locked_credit_source,
                    "session_id": session_id,
                    "source": source,
                    "cached_input_tokens": int(cached_input_tokens or 0),
                    "cache_write_tokens": int(cache_write_tokens or 0),
                    "uncached_input_tokens": uncached_input_tokens,
                    "reasoning_tokens": int(reasoning_tokens or 0),
                }).execute()
        except Exception as e:
            logger.debug("usage_events insert skipped: %s", e)

        # Record any extension-side accounting (no-op without a billing extension).
        await apply_record(
            cdb, event_id=event_id, agent_id=agent_id, user_id=user_id,
            end_user_charge_cents=charge, deducted_cents=deducted_cents,
            retained_cents=agent_earnings_cents, interaction_id=interaction_id,
        )

        # Cover the charge: trial grant first, then the credit wallet.
        if charge > 0 and not result.is_exempt:
            if result.is_trial and result.trial_used_cents > 0:
                try:
                    await _decrement_trial(cdb, billing_user_id, agent_id, result.trial_used_cents)
                except Exception as e:
                    logger.debug("trial decrement skipped: %s", e)
            if result.wallet_charge_cents > 0:
                try:
                    await wallet_mod.debit(
                        cdb, billing_user_id, result.wallet_charge_cents,
                        kind="usage", ref_id=interaction_id, note=f"agent:{agent_id}",
                    )
                except Exception as e:
                    logger.debug("wallet debit skipped: %s", e)

        # Credit the agent admin's earnings wallet (informational mirror)
        if agent_earnings_cents > 0:
            try:
                roles = await db_offload(lambda: db.get_agent_roles(agent_id))
                admins = roles.get("admin_users") or []
                if admins:
                    await wallet_mod.credit(
                        cdb,
                        owner_type="agent_admin",
                        owner_id=admins[0],
                        amount_cents=agent_earnings_cents,
                        kind="earnings",
                        ref_id=interaction_id,
                        note=f"agent:{agent_id}",
                    )
            except Exception as e:
                logger.debug("earnings credit skipped: %s", e)

        return {
            "end_user_charge_cents": charge,
            "agent_admin_earnings_cents": agent_earnings_cents,
            "strategy": result.strategy,
            "is_byo_llm": result.is_byo_llm,
            "is_trial": result.is_trial,
            "is_exempt": result.is_exempt,
            "credit_units": locked_credit_units,
            "credit_source": locked_credit_source,
        }
    except Exception as e:
        logger.debug("billing record_and_charge skipped: %s", e)
        return None


async def _decrement_trial(db: Any, user_id: str, agent_id: str, cents: int) -> None:
    """Burn ``cents`` of the user's trial grant for this agent (clamped at 0)."""
    if cents <= 0:
        return
    if hasattr(db, "_get_conn"):
        conn = db._get_conn()
        try:
            conn.execute(
                "UPDATE trials SET remaining_cents = "
                "CASE WHEN remaining_cents IS NULL THEN NULL "
                "ELSE MAX(0, remaining_cents - ?) END "
                "WHERE user_id=? AND agent_id=?",
                (cents, user_id, agent_id),
            )
            conn.commit()
        finally:
            conn.close()
    elif hasattr(db, "get_raw_client"):
        cli = db.get_raw_client()
        row = (cli.table("trials").select("remaining_cents")
               .eq("user_id", user_id).eq("agent_id", agent_id).limit(1).execute())
        if row.data:
            remaining = max(0, int(row.data[0].get("remaining_cents") or 0) - cents)
            cli.table("trials").update({"remaining_cents": remaining}) \
               .eq("user_id", user_id).eq("agent_id", agent_id).execute()

"""Lightweight background-usage recorder.

Non-chat LLM calls — git commit messages, chat-pill placeholder/suggestion
text, conversation compaction summaries, embeddings, session titles — don't run
through the billing/charge path in ``app.agent.loop._record_billing_usage``.
This records a ``source='background:*'`` row in ``usage_events`` so those tokens
and their published-price cost still appear in the global per-model usage totals
shown in admin Agent Settings. No wallet/charge logic — purely for visibility.

Best-effort and self-contained: it fetches its own DB handle and swallows every
error, so a billing-table hiccup can never break a background feature.
"""
import logging
import uuid as _uuid
from typing import Any, Optional

logger = logging.getLogger(__name__)

# Sentinel owner for rows that belong to no specific agent/user (system tasks).
SYSTEM_OWNER = "system"


async def record_background_usage(
    *,
    model: str,
    provider: str = "",
    input_tokens: int = 0,
    output_tokens: int = 0,
    label: str = "",
    session_id: Optional[str] = None,
    user_id: Optional[str] = None,
    agent_id: Optional[str] = None,
    interaction_id: Optional[str] = None,
    db: Any = None,
) -> None:
    """Write one ``source='background:<label>'`` row to ``usage_events``.

    ``label`` is a short tag for the originating task (e.g. 'commit', 'suggest',
    'compact', 'embed', 'title'). The published-price cost is computed from the
    model catalog and stored in ``cost_usd`` exactly like chat rows, so global
    totals sum chat and background uniformly. Never raises.

    ``session_id`` / ``user_id`` / ``agent_id`` / ``interaction_id`` link the row
    back to the chat surface that triggered it (so e.g. a compaction summary or
    a summary-lane recap can show the size of the summariser's OWN prompt next to
    that message in the UI). Callers that don't have them leave them None —
    the row is still counted in global totals.
    """
    try:
        in_tok = int(input_tokens or 0)
        out_tok = int(output_tokens or 0)
        if not model or (in_tok <= 0 and out_tok <= 0):
            return
        try:
            from app import model_catalog
            cost_usd, cost_source = model_catalog.cost_for(model, in_tok, out_tok, provider)
        except Exception:
            cost_usd, cost_source = 0.0, "unknown"

        if db is None:
            # usage_events is the CENTRAL billing plane — write it to the control
            # DB so background-usage rows land in the same place as chat usage and
            # the admin dashboard can see them, even when interaction data is
            # scattered across per-user databases (user BYOD). No-op in
            # single-tenant mode (get_control_db() is the one DB).
            from app.db import get_app_db
            db = get_app_db()

        row_id = str(_uuid.uuid4())
        a_id = agent_id or SYSTEM_OWNER
        u_id = user_id or SYSTEM_OWNER
        src = f"background:{label}" if label else "background"

        if hasattr(db, "_get_conn"):
            conn = db._get_conn()
            try:
                conn.execute(
                    "INSERT INTO usage_events ("
                    "id, agent_id, user_id, interaction_id, input_tokens, output_tokens, "
                    "provider_cost_cents, end_user_charge_cents, "
                    "agent_admin_earnings_cents, strategy, is_byo_llm, is_trial, is_exempt, "
                    "model, provider, cost_usd, cost_source, session_id, source"
                    ") VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                    (
                        row_id, a_id, u_id, interaction_id, in_tok, out_tok,
                        0, 0, 0, "free", 0, 0, 1,
                        model, provider or "", float(cost_usd or 0), cost_source,
                        session_id, src,
                    ),
                )
                conn.commit()
            finally:
                conn.close()
        elif hasattr(db, "get_raw_client"):
            db.get_raw_client().table("usage_events").insert({
                "id": row_id, "agent_id": a_id, "user_id": u_id,
                "interaction_id": interaction_id, "input_tokens": in_tok, "output_tokens": out_tok,
                "provider_cost_cents": 0, "end_user_charge_cents": 0,
                "agent_admin_earnings_cents": 0,
                "strategy": "free", "is_byo_llm": 0, "is_trial": 0, "is_exempt": 1,
                "model": model, "provider": provider or "",
                "cost_usd": float(cost_usd or 0), "cost_source": cost_source,
                "session_id": session_id, "source": src,
            }).execute()
    except Exception as e:
        logger.debug("record_background_usage skipped: %s", e)

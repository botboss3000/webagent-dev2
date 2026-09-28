"""Informational usage recording for alternate engines (Claude Code, Codex, …).

Alternate engines run the user's OWN CLI (billed by the user's Claude/Codex
account, never WebAgent credits), so their turns must NOT go through the charge
path — ``record_and_charge`` would compute a price and debit the wallet. But the
chat footer's ctx pill and the IN/OUT counters read the ``usage_events`` ledger,
so an engine turn that DOES report real token usage should still land a row there
— with ``source='chat'`` (so the footer queries see it) and ``cost_usd=0`` (so it
never bills). Rows for engines that report no usage (e.g. the Codex CLI) are
simply never written.

Mirrors plugins/billing/usage.py's insert shape (control-DB local-first in hybrid
mode) but writes ``source='chat'`` / zero cost instead of
``source='background:<label>'`` / catalog price.
"""

from __future__ import annotations

import logging
import uuid
from typing import Optional

logger = logging.getLogger(__name__)


async def record_engine_usage(
    *,
    model: str = "",
    input_tokens: int = 0,
    output_tokens: int = 0,
    session_id: Optional[str] = None,
    user_id: Optional[str] = None,
    agent_id: Optional[str] = None,
) -> None:
    """Write one informational ``source='chat'`` usage_events row (zero cost).

    For alternate-engine turns only — never the charge path. Records the REAL
    tokens the engine reported (so the ctx pill + IN/OUT counters survive a page
    reload) while ``cost_usd=0`` keeps it out of WebAgent billing. Never raises.
    """
    try:
        in_tok = int(input_tokens or 0)
        out_tok = int(output_tokens or 0)
        if not model or (in_tok <= 0 and out_tok <= 0):
            return

        # usage_events is the CENTRAL billing plane — write to the control DB so
        # engine rows land beside chat rows even when interaction data is
        # scattered across per-user databases. No-op in single-tenant mode
        # (get_app_db() is the one DB).
        from app.db import get_app_db
        db = get_app_db()

        row_id = str(uuid.uuid4())
        a_id = agent_id or "system"
        u_id = user_id or "system"

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
                        row_id, a_id, u_id, None, in_tok, out_tok,
                        0, 0, 0, "free", 1, 0, 1,
                        model, "", 0.0, "engine", session_id, "chat",
                    ),
                )
                conn.commit()
            finally:
                conn.close()
        elif hasattr(db, "get_raw_client"):
            db.get_raw_client().table("usage_events").insert({
                "id": row_id, "agent_id": a_id, "user_id": u_id,
                "interaction_id": None, "input_tokens": in_tok, "output_tokens": out_tok,
                "provider_cost_cents": 0, "end_user_charge_cents": 0,
                "agent_admin_earnings_cents": 0,
                "strategy": "free", "is_byo_llm": 1, "is_trial": 0, "is_exempt": 1,
                "model": model, "provider": "",
                "cost_usd": 0.0, "cost_source": "engine",
                "session_id": session_id, "source": "chat",
            }).execute()
    except Exception as e:
        logger.debug("record_engine_usage skipped: %s", e)

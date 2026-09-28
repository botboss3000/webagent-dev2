"""In-memory trigger routing index.

Built from agent_templates at startup. Maps (trigger_type, trigger_key) to
template_id for O(1) lookup when routing incoming triggers to the right agent.

Usage:
    from app.agent import trigger_index
    trigger_index.build()                            # call once at startup
    template_id = trigger_index.get('tool_call', 'run_optimizer')
"""
import logging
from typing import Optional

logger = logging.getLogger(__name__)

# {(trigger_type, trigger_key): template_id}
_index: dict = {}


def build() -> None:
    """Build the trigger index from agent_templates and agents. Safe to call multiple times.

    agent_templates provides system-level defaults; agents overrides those
    (agents.template_id is used as the canonical identifier).
    """
    global _index
    new_index: dict = {}
    try:
        from app.db import get_agent_db, get_app_db
        # Base layer: system templates
        # Also track the template-level key per template_id so the override
        # layer can evict the old key when the user changes it.
        app_conn = get_app_db()._get_conn()
        try:
            rows = app_conn.execute(
                "SELECT id, trigger_type, trigger_key FROM agent_templates"
                " WHERE trigger_type IS NOT NULL AND trigger_key IS NOT NULL"
            ).fetchall()
            try:
                catalog_rows = app_conn.execute(
                    "SELECT agent_id FROM agent_catalog WHERE deleted_at IS NULL"
                ).fetchall()
                legacy_agent_rows = None
            except Exception:
                # Compatibility with the pre-split single database.
                catalog_rows = None
                legacy_agent_rows = app_conn.execute(
                    "SELECT template_id, trigger_type, trigger_key FROM agents"
                    " WHERE trigger_type IS NOT NULL AND trigger_key IS NOT NULL"
                    " AND template_id IS NOT NULL"
                ).fetchall()
        finally:
            app_conn.close()
        template_keys: dict = {}  # template_id → (trigger_type, trigger_key)
        for template_id, trigger_type, trigger_key in rows:
            new_index[(trigger_type, trigger_key)] = template_id
            template_keys[template_id] = (trigger_type, trigger_key)

        # Override layer: per-agent rows (user config via UI).
        # If the agent row has a different key than the template, evict the
        # template's stale entry so the old command stops working.
        agent_rows = list(legacy_agent_rows or [])
        for catalog_row in catalog_rows or []:
            agent_id = catalog_row[0]
            try:
                agent_conn = get_agent_db(agent_id)._get_conn()
                try:
                    agent_rows.extend(agent_conn.execute(
                        "SELECT template_id, trigger_type, trigger_key FROM agents"
                        " WHERE trigger_type IS NOT NULL AND trigger_key IS NOT NULL"
                        " AND template_id IS NOT NULL"
                    ).fetchall())
                finally:
                    agent_conn.close()
            except Exception as exc:
                logger.warning(
                    "Skipping trigger index override for agent %s: %s",
                    agent_id,
                    exc,
                )
        for template_id, trigger_type, trigger_key in agent_rows:
            old = template_keys.get(template_id)
            if old and old != (trigger_type, trigger_key):
                new_index.pop(old, None)
            new_index[(trigger_type, trigger_key)] = template_id

        _index = new_index
        logger.info("Trigger index built: %d entr%s", len(_index), "y" if len(_index) == 1 else "ies")
        for (tt, tk), tid in _index.items():
            logger.debug("  trigger_index[%s, %s] = %s", tt, tk, tid)
    except Exception as exc:
        logger.warning("Failed to build trigger index: %s", exc)


def get(trigger_type: str, trigger_key: str) -> Optional[str]:
    """Return template_id for (trigger_type, trigger_key), or None if not found."""
    return _index.get((trigger_type, trigger_key))


def get_slash_commands() -> dict:
    """Return {trigger_key: template_id} for all slash_command type entries."""
    return {
        tk: tid
        for (tt, tk), tid in _index.items()
        if tt == "slash_command"
    }


def all_entries() -> dict:
    """Return a copy of the full index (for diagnostics)."""
    return dict(_index)

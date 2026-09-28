"""Automation ability — FULLY SELF-CONTAINED drop-in.

Gates the event-subscription and scheduled-task tools: when an external source
fires (Gmail Pub/Sub, Graph subscription, Slack mention, Drive change, etc.) or
a timer/cron elapses, run a prompt — the same kind of real-time / scheduled
trigger the Automation tab creates (a row in ``agent_event_subscriptions`` or
``automations`` plus, for events, a provider-side watch).

This file now owns the ENTIRE automation tool surface end-to-end: the handler
factory (formerly app/tools/automation_tools.py) lives here, plus the per-agent
limit enforcement layered on top. There is no core factory to import — delete
this file and the capability is gone.

Runtime enforcement of per-agent automation limits (max automations, event
subscription gating, recurring-schedule gating, max-per-day ceiling,
approval-required, delivery-channel cap) is layered on top of the in-file
factory handlers via wrapping in ``build_tools``.  The config is read from the
automation connection row's ``config.ability_settings`` JSON.

Discovered generically by core (see app/abilities/__init__.py
"Self-contained abilities"): FEATURE for the catalog/UI/gated tool names,
build_tools for the handlers. All heavy imports are kept lazy (inside the
factory / handler closures) so importing this module stays cheap.
"""

from __future__ import annotations

import json
import logging
from typing import Any, Dict, Optional

logger = logging.getLogger(__name__)

# Populated from the in-file factory's constants inside build_tools(), so the
# loader (which reads these AFTER build_tools) always sees the authoritative
# schemas.
TOOL_SCHEMAS: dict = {}
DESTRUCTIVE: set = set()

# ── Automation tool schemas (single source of truth) ───────────────────────
# Nothing here is marked destructive at the ToolInfo level except cancel_*,
# matching the prior loader behavior (writes are guarded by ownership checks
# inside the handlers).

AUTOMATION_TOOL_SCHEMAS = {
    "list_event_sources": {"type": "object", "properties": {}, "required": []},
    "list_delivery_channels": {"type": "object", "properties": {}, "required": []},
    "event_subscribe": {
        "type": "object",
        "properties": {
            "source": {
                "type": "string",
                "description": "Event source name. Call list_event_sources to see what's enabled (e.g. 'gmail', 'slack', 'outlook_mail', 'google_drive', 'telegram').",
            },
            "event_type": {
                "type": "string",
                "description": "Event type on that source (e.g. 'message_received', 'file_added', 'mention'). See list_event_sources.",
            },
            "prompt": {
                "type": "string",
                "description": "Instructions the agent will run when the event fires. Reference the event payload naturally — e.g. 'Summarize the new email and send it to me on Telegram.'",
            },
            "filter": {
                "type": "object",
                "description": "Source-specific filter. For Gmail: {'query': 'from:airlines.com'}. For Drive: {'parent_id': '<folder-id>'}. Empty = match all.",
                "default": {},
            },
            "task_label": {
                "type": "string",
                "description": "Short human label for this trigger (shown in the Automation tab). Optional.",
                "default": "",
            },
            "trigger_natural": {
                "type": "string",
                "description": "The original English the user wrote (for the Automation tab to display). Optional.",
                "default": "",
            },
            "channel": {
                "type": "string",
                "description": (
                    "Where to deliver when the event fires. **DEFAULT: omit this argument** — "
                    "the tool will set channel='webchat' targeted at the user's current "
                    "session, so the agent's reply lands directly in the chat they're "
                    "sitting in. Only pass a non-webchat value (telegram, slack, sms, "
                    "discord, etc.) if you have FIRST called `list_delivery_channels` "
                    "AND that exact name appears in the returned `channels` array. "
                    "Never invent or assume a channel is available."
                ),
            },
            "channel_recipient": {
                "type": "string",
                "description": (
                    "Channel-specific recipient. For webchat (default), this is the "
                    "session_id where the event reply should land — the tool auto-fills "
                    "it from the current session, so usually omit. For other channels, "
                    "this is the chat_id / phone / address."
                ),
            },
            "silent": {
                "type": "boolean",
                "description": "If true, the agent processes the event without notifying the user.",
                "default": False,
            },
        },
        "required": ["source", "event_type", "prompt"],
    },
    "list_event_subscriptions": {"type": "object", "properties": {}, "required": []},
    "event_unsubscribe": {
        "type": "object",
        "properties": {
            "subscription_id": {"type": "string", "description": "id of the row to remove (from list_event_subscriptions)"},
        },
        "required": ["subscription_id"],
    },
}

AUTOMATION_DESTRUCTIVE: set = set()

# Shared bits of the delivery/run-mode schema, reused by schedule_task + event_subscribe.
_DELIVERY_PROP = {
    "type": "array",
    "description": (
        "Where to send the result. A list of targets — each either a channel "
        "name string, or an object {mode,...}. Modes: "
        "{mode:'here'} (reply in the current chat), {mode:'new_session'} (a fresh "
        "sidebar session), {mode:'headless'} (do the work, surface nothing), "
        "{mode:'channel',channel:'telegram',recipient?:'...'} (a comms channel — "
        "call list_delivery_channels first), {mode:'webhook',url:'https://...'}, "
        "{mode:'file',filename:'out.md'}. Omit to default to the current chat. "
        "Multiple targets fan out (e.g. act silently AND ping me on Telegram)."
    ),
    "items": {"type": "object"},
    "default": [],
}
_RUN_MODE_PROP = {
    "type": "string",
    "enum": ["inline", "fresh_clone", "dedicated_clone", "headless"],
    "description": (
        "Who runs it. 'inline' = this agent (default). 'fresh_clone' = a new "
        "throwaway clone each run (reaped after). 'dedicated_clone' = one long-lived "
        "clone owns it. Clone modes need the Agent Orchestration ability enabled — "
        "otherwise they fall back to inline."
    ),
    "default": "inline",
}
_GUARDRAIL_PROPS = {
    "max_per_day": {"type": "integer", "description": "Guardrail: max fires per day (omit = unlimited)."},
    "disable_after_failures": {"type": "integer", "description": "Auto-disable after this many consecutive failures (omit = never)."},
    "expires_at": {"type": "string", "description": "ISO timestamp after which the automation stops and disables (omit = never)."},
    "retry_max": {"type": "integer", "description": "Retry a failed run up to this many times (with backoff) before giving up the occurrence.", "default": 0},
    "retry_backoff_seconds": {"type": "integer", "description": "Base seconds between retries (grows per attempt).", "default": 0},
    "clone_abilities": {"type": "array", "items": {"type": "string"}, "description": "Abilities to grant a clone runner (clamped to what this agent already has).", "default": []},
}

AUTOMATION_TOOL_SCHEMAS.update({
    "schedule_task": {
        "type": "object",
        "properties": {
            "prompt": {"type": "string", "description": "Instructions to run when the task fires."},
            "task_label": {"type": "string", "description": "Short human label (shown in the Automation tab).", "default": ""},
            "schedule_cron": {"type": "string", "description": "Cron expression for a RECURRING task, e.g. '0 9 * * 1-5' (weekdays 9am). Leave empty for a one-shot — then set in_minutes or at.", "default": ""},
            "in_minutes": {"type": "number", "description": "One-shot: fire this many minutes from now. Ignored if schedule_cron is set.", "default": 0},
            "at": {"type": "string", "description": "One-shot: ISO timestamp to fire at (e.g. '2026-06-08T15:00:00'). Ignored if schedule_cron is set.", "default": ""},
            "timezone": {"type": "string", "description": "IANA timezone for the cron expression.", "default": "UTC"},
            "delivery": _DELIVERY_PROP,
            "run_mode": _RUN_MODE_PROP,
            **_GUARDRAIL_PROPS,
        },
        "required": ["prompt"],
    },
    "remind_me": {
        "type": "object",
        "properties": {
            "note": {"type": "string", "description": "What to remind the user about."},
            "in_minutes": {"type": "number", "description": "Fire this many minutes from now.", "default": 0},
            "at": {"type": "string", "description": "Or an ISO timestamp to fire at.", "default": ""},
        },
        "required": ["note"],
    },
    "list_automations": {"type": "object", "properties": {}, "required": []},
    "update_automation": {
        "type": "object",
        "properties": {
            "automation_id": {"type": "string", "description": "id from list_automations."},
            "prompt": {"type": "string"},
            "task_label": {"type": "string"},
            "schedule_cron": {"type": "string"},
            "timezone": {"type": "string"},
            "enabled": {"type": "boolean"},
            "delivery": _DELIVERY_PROP,
            "run_mode": _RUN_MODE_PROP,
            **_GUARDRAIL_PROPS,
        },
        "required": ["automation_id"],
    },
    "pause_automation": {"type": "object", "properties": {"automation_id": {"type": "string"}}, "required": ["automation_id"]},
    "resume_automation": {"type": "object", "properties": {"automation_id": {"type": "string"}}, "required": ["automation_id"]},
    "cancel_automation": {"type": "object", "properties": {"automation_id": {"type": "string"}}, "required": ["automation_id"]},
    "run_automation_now": {"type": "object", "properties": {"automation_id": {"type": "string"}}, "required": ["automation_id"]},
    "remember_automation_state": {
        "type": "object",
        "properties": {
            "state": {"description": "The state to persist for THIS automation (an object or string). Replaces the prior memory."},
        },
        "required": ["state"],
    },
})

# Add the feature-rich knobs to event_subscribe too.
AUTOMATION_TOOL_SCHEMAS["event_subscribe"]["properties"].update({
    "delivery": _DELIVERY_PROP,
    "run_mode": _RUN_MODE_PROP,
    **_GUARDRAIL_PROPS,
})

# Tools that create, trigger, or change autonomous future actions confirm-gate at
# the ToolInfo level: they schedule work that later runs on its own (and spends
# tokens), so the user is asked first in Ask/Plan mode. Reads, listings and
# internal bookkeeping (remember_automation_state, event_unsubscribe) run free.
AUTOMATION_DESTRUCTIVE.update({
    "cancel_automation", "schedule_task", "run_automation_now", "remind_me",
    "update_automation", "pause_automation", "resume_automation", "event_subscribe",
})


# ── Factory helpers (lazy heavy imports inside) ────────────────────────────

def _build_delivery_spec(*, delivery=None, channel=None, recipient=None,
                         silent: bool = False, default_session_id=None) -> dict:
    """Build a unified delivery spec from a `delivery` target list, or fall back
    to simple channel/recipient/silent inputs (legacy-friendly)."""
    targets = []
    for t in (delivery or []):
        if isinstance(t, str) and t.strip():
            targets.append({"mode": "channel", "channel": t.strip()})
        elif isinstance(t, dict) and t.get("mode"):
            targets.append(t)
    if targets:
        return {"silent": bool(silent), "targets": targets}
    from app.automation.delivery import spec_from_legacy
    return spec_from_legacy(channel, recipient or default_session_id, silent)


def _validate_run_mode(run_mode, enabled_providers):
    rm = (run_mode or "inline").strip()
    if rm in ("fresh_clone", "dedicated_clone") and "agent_orchestration" not in (enabled_providers or set()):
        return "inline", ("Clone run modes need the Agent Orchestration ability enabled on "
                          "this agent — used 'inline' instead.")
    return rm, None


def _parse_at(at: str):
    """Parse an ISO timestamp into a UTC ISO string (naive = assumed UTC)."""
    from datetime import datetime, timezone as _tz
    try:
        dt = datetime.fromisoformat(at.replace("Z", "+00:00"))
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=_tz.utc)
        return dt.astimezone(_tz.utc).isoformat()
    except Exception:
        return None


def build_automation_tools(user_id: str, agent_id: str, session_id: str,
                           enabled_providers=None) -> dict:
    """Return {tool_name: handler} for the automation tools, each closed over the
    caller's user_id / agent_id / session_id. ``enabled_providers`` gates clone
    run modes (which need the agent_orchestration ability)."""

    async def list_event_sources():
        """List the event sources discovered on this server and which event types each supports."""
        try:
            from app.events import get_manager
            mgr = get_manager()
            return json.dumps({
                "status": "ok",
                "sources": mgr.manifest(),
            })
        except Exception as e:
            return json.dumps({"status": "error", "message": str(e)})

    async def list_delivery_channels():
        """List the delivery channels available for this agent's automations (only these may be used)."""
        try:
            from app.events.channels import list_available_channels
            from app.db import get_db as _gd
            chans = await list_available_channels(
                _gd(), user_id=user_id, agent_id=agent_id or "",
                current_session_id=session_id or None,
            )
            return json.dumps({
                "status": "ok",
                "channels": chans,
                "note": (
                    "ONLY suggest channels in this list. Channels NOT listed (Telegram, "
                    "SMS, Slack, Discord, email-out, etc. when absent) require admin "
                    "comms plugin enablement + agent connection setup + per-user recipient "
                    "config — none of which exist yet for those channels. Do not invent "
                    "channels or claim a channel is 'available' that isn't here."
                ),
            })
        except Exception as e:
            return json.dumps({"status": "error", "message": str(e)})

    async def event_subscribe(
        source: str,
        event_type: str,
        prompt: str,
        filter: Optional[Dict[str, Any]] = None,
        task_label: str = "",
        trigger_natural: str = "",
        channel: Optional[str] = None,
        channel_recipient: Optional[str] = None,
        silent: bool = False,
        delivery: Optional[list] = None,
        run_mode: str = "inline",
        clone_abilities: Optional[list] = None,
        max_per_day: Optional[int] = None,
        disable_after_failures: Optional[int] = None,
        expires_at: Optional[str] = None,
        retry_max: int = 0,
        retry_backoff_seconds: int = 0,
    ):
        """Subscribe this agent to a real-time event: when source/event_type fires, run the prompt."""
        if not agent_id:
            return json.dumps({
                "status": "error",
                "message": "event_subscribe requires an agent context (no agent_id on this session)",
            })
        from app.db import get_db
        from app.events import get_manager
        from app.automation.parser import ParsedEventSubscription
        from app.automation.sync import _event_sub_hash, _register_event_sub

        # Pre-flight: refuse to insert a row that the provider can't honor.
        # Avoids stale error rows and gives the agent a clear, actionable hint.
        mgr = get_manager()
        src_obj = mgr.get(source)
        if src_obj is None:
            return json.dumps({
                "status": "error",
                "code": "unknown_source",
                "message": (
                    f"Event source '{source}' is not registered. "
                    "Call list_event_sources to see what's available."
                ),
            })
        if not src_obj.enabled:
            return json.dumps({
                "status": "error",
                "code": "source_not_enabled",
                "source": source,
                "message": (
                    f"Event source '{source}' is discovered but not enabled on this server "
                    "(provider-side config missing — e.g. for Gmail, EVENTS_GMAIL_PUBSUB_TOPIC "
                    "is not set, so push notifications can't be wired up). "
                    "Tell the user real-time push isn't available for this source; do not "
                    "create a subscription. Other options: poll-style sources (see "
                    "list_event_sources for which sources have supports_poll=true), or the "
                    "user can ask the admin to configure the provider env vars."
                ),
            })
        if event_type not in (src_obj.event_types or []):
            return json.dumps({
                "status": "error",
                "code": "unknown_event_type",
                "source": source,
                "supported_event_types": list(src_obj.event_types or []),
                "message": (
                    f"Event type '{event_type}' is not supported by source '{source}'. "
                    f"Supported: {', '.join(src_obj.event_types or []) or '(none)'}."
                ),
            })

        # ── Scope pre-flight ─────────────────────────────────────
        # Avoid inserting a doomed subscription when the user's OAuth
        # token lacks the scope the provider requires (most common
        # cause: user unticked a sensitive-scope box on consent).
        required_provider = getattr(src_obj, "required_provider", None)
        required_groups = src_obj.required_scopes(event_type, filter or {}) if hasattr(src_obj, "required_scopes") else []
        if required_provider and required_groups:
            from app.integrations.oauth_helper import oauth_label as _oauth_label
            from app.db import get_db as _get_db
            _db = _get_db()
            elem = await _db.auth_element_get(user_id, required_provider, _oauth_label(agent_id))
            if not elem:
                return json.dumps({
                    "status": "error",
                    "code": "provider_not_connected",
                    "provider": required_provider,
                    "message": (
                        f"Source '{source}' needs an active '{required_provider}' OAuth "
                        "connection on this agent. Ask the user to connect via the chat's "
                        "OAuth link before subscribing."
                    ),
                })
            try:
                cfg = json.loads(elem.get("config") or "{}") if isinstance(elem.get("config"), str) else (elem.get("config") or {})
            except Exception:
                cfg = {}
            granted = set(cfg.get("scopes") or [])
            missing = [g for g in required_groups if not any(s in granted for s in g)]
            if missing:
                return json.dumps({
                    "status": "error",
                    "code": "scope_missing",
                    "provider": required_provider,
                    "source": source,
                    "mode": "push" if src_obj.supports_push else "poll",
                    "missing_groups": missing,
                    "granted_scopes": sorted(granted),
                    "message": (
                        f"The user's '{required_provider}' token does not have the scope(s) "
                        f"required for source '{source}' "
                        f"({'push' if src_obj.supports_push else 'poll'} mode). "
                        "Tell the user to RECONNECT the integration and TICK every checkbox "
                        "for this provider on the Google/Microsoft consent screen — they "
                        "almost certainly unchecked one previously. Do not insert this "
                        "subscription. "
                        f"Need at least one scope from each group: {missing}. "
                        f"Currently granted: {sorted(granted)}."
                    ),
                })

        # Default delivery: the chat session the user is currently in.
        # `channel="webchat"` + `channel_recipient=<session_id>` tells
        # the event executor to post the agent's reply BACK into the
        # user's current chat instead of spawning an evt-* session
        # that lives in the sidebar but never lights up the chat
        # they're sitting in. Caller can override either field to
        # subscribe in a different session, fresh evt-* session, or
        # a non-webchat channel.
        effective_channel = channel
        effective_recipient = channel_recipient
        if effective_channel is None:
            effective_channel = "webchat"
        if effective_channel == "webchat" and not effective_recipient and session_id:
            effective_recipient = session_id

        parsed = ParsedEventSubscription(
            task_label=task_label or f"{source}/{event_type}",
            prompt=prompt,
            source=source,
            event_type=event_type,
            filter_dict=filter or {},
            trigger_natural=trigger_natural,
            channel=effective_channel,
            channel_recipient=effective_recipient,
            silent=silent,
        )
        # Run mode (clone gating) + unified delivery spec.
        rm, rm_warn = _validate_run_mode(run_mode, enabled_providers)
        spec = _build_delivery_spec(
            delivery=delivery, channel=effective_channel,
            recipient=effective_recipient, silent=silent,
            default_session_id=session_id)

        db = get_db()
        try:
            from app.automation.entitlements import (
                AutomationEntitlementError,
                enforce_automation_entitlement,
            )

            source_hash = _event_sub_hash(parsed)
            existing = await db.list_event_subscriptions(
                agent_id=agent_id,
                owner_user_id=user_id,
            )
            additional = 0 if any(
                row.get("source_hash") == source_hash for row in existing
            ) else 1
            await enforce_automation_entitlement(
                db,
                user_id,
                additional=additional,
            )
            row = await db.upsert_event_subscription(
                agent_id=agent_id,
                owner_user_id=user_id,
                source_hash=source_hash,
                source=parsed.source,
                event_type=parsed.event_type,
                filter_dict=parsed.filter_dict,
                task_label=parsed.task_label,
                prompt=parsed.prompt,
                trigger_natural=parsed.trigger_natural,
                channel=parsed.channel,
                channel_recipient=parsed.channel_recipient,
                silent=parsed.silent,
                enabled=True,
                delivery_json=json.dumps(spec),
                run_mode=rm,
                clone_abilities=json.dumps(clone_abilities or []),
                max_per_day=max_per_day,
                disable_after_failures=disable_after_failures,
                expires_at=expires_at or None,
                retry_max=int(retry_max or 0),
                retry_backoff_seconds=int(retry_backoff_seconds or 0),
                origin="tool",
            )
            await _register_event_sub(db, row, parsed)
            row_after = (await db.list_event_subscriptions(
                agent_id=agent_id, owner_user_id=user_id,
            ))
            fresh = next((r for r in row_after if r.get("id") == row.get("id")), row)
            # Notify the user's open tabs so the Automation tab re-fetches
            # without a manual refresh.
            try:
                from app.api.chat import _emit_to_user_listeners
                await _emit_to_user_listeners(user_id, {
                    "type": "automation_updated",
                    "agent_id": agent_id,
                    "action": "subscribed",
                    "kind": "event_subscription",
                    "subscription_id": fresh.get("id"),
                })
            except Exception:
                pass
            return json.dumps({
                "status": "ok",
                "subscription": fresh,
                "warning": rm_warn,
                "message": (
                    f"Subscribed: when {source}/{event_type} fires, run agent."
                    + (f" Provider-side: {fresh.get('last_status') or 'pending'}." if fresh.get('last_status') else "")
                    + (f" Error: {fresh.get('last_error')}" if fresh.get('last_error') else "")
                ),
            })
        except AutomationEntitlementError as e:
            return json.dumps({
                "status": "error", "code": e.code, "message": str(e),
            })
        except Exception as e:
            logger.exception("event_subscribe failed")
            return json.dumps({"status": "error", "message": str(e)})

    async def list_event_subscriptions():
        """List this agent's existing event subscriptions."""
        from app.db import get_db
        db = get_db()
        rows = await db.list_event_subscriptions(
            agent_id=agent_id or None,
            owner_user_id=user_id,
        )
        return json.dumps({"status": "ok", "subscriptions": rows, "count": len(rows)})

    async def event_unsubscribe(subscription_id: str):
        """Remove one of this agent's event subscriptions by id."""
        from app.db import get_db
        from app.automation.sync import _unregister_event_sub
        db = get_db()
        rows = await db.list_event_subscriptions(
            agent_id=agent_id or None,
            owner_user_id=user_id,
        )
        target = next((r for r in rows if r.get("id") == subscription_id), None)
        if not target:
            return json.dumps({"status": "error", "message": f"Subscription {subscription_id} not found or not owned by user"})
        await _unregister_event_sub(target)
        ok = await db.delete_event_subscription(subscription_id)
        if ok:
            try:
                from app.api.chat import _emit_to_user_listeners
                await _emit_to_user_listeners(user_id, {
                    "type": "automation_updated",
                    "agent_id": agent_id,
                    "action": "unsubscribed",
                    "kind": "event_subscription",
                    "subscription_id": subscription_id,
                })
            except Exception:
                pass
        return json.dumps({
            "status": "ok" if ok else "error",
            "message": f"Subscription {subscription_id} {'removed' if ok else 'delete failed'}",
        })

    # ── Scheduled tasks / one-shot timers ────────────────────────────────

    async def _emit_automation_updated(action: str, automation_id: str):
        try:
            from app.api.chat import _emit_to_user_listeners
            await _emit_to_user_listeners(user_id, {
                "type": "automation_updated", "agent_id": agent_id,
                "action": action, "kind": "automation",
                "automation_id": automation_id,
            })
        except Exception:
            pass

    async def _owned_automation(db, automation_id: str):
        row = await db.get_automation(automation_id)
        if not row or row.get("owner_user_id") != user_id:
            return None
        return row

    async def schedule_task(
        prompt: str, task_label: str = "", schedule_cron: str = "",
        in_minutes: float = 0, at: str = "", timezone: str = "UTC",
        delivery: Optional[list] = None, channel: Optional[str] = None,
        channel_recipient: Optional[str] = None, silent: bool = False,
        run_mode: str = "inline", clone_abilities: Optional[list] = None,
        max_per_day: Optional[int] = None, disable_after_failures: Optional[int] = None,
        expires_at: Optional[str] = None, retry_max: int = 0,
        retry_backoff_seconds: int = 0,
    ):
        """Create a recurring (cron) or one-shot scheduled task that runs the prompt."""
        if not agent_id:
            return json.dumps({"status": "error", "message": "schedule_task requires an agent context"})
        from app.db import get_db
        from app.automation.runner import _compute_next_run
        db = get_db()

        schedule_kind = "cron"
        next_run = None
        if schedule_cron:
            next_run = _compute_next_run(schedule_cron, timezone)
            if not next_run:
                return json.dumps({"status": "error", "message": f"invalid cron expression: {schedule_cron}"})
        else:
            schedule_kind = "once"
            if in_minutes and float(in_minutes) > 0:
                from datetime import datetime, timezone as _tz, timedelta
                next_run = (datetime.now(_tz.utc) + timedelta(minutes=float(in_minutes))).isoformat()
            elif at:
                next_run = _parse_at(at)
            if not next_run:
                return json.dumps({"status": "error", "message": "provide schedule_cron, in_minutes, or at"})

        rm, rm_warn = _validate_run_mode(run_mode, enabled_providers)
        spec = _build_delivery_spec(delivery=delivery, channel=channel,
                                    recipient=channel_recipient, silent=silent,
                                    default_session_id=session_id)
        try:
            from app.automation.entitlements import (
                AutomationEntitlementError,
                enforce_automation_entitlement,
            )

            await enforce_automation_entitlement(db, user_id, additional=1)
            row = await db.create_automation(
                agent_id=agent_id, owner_user_id=user_id, prompt=prompt,
                task_label=task_label or ("Reminder" if schedule_kind == "once" else "Scheduled task"),
                schedule_cron=schedule_cron, schedule_kind=schedule_kind,
                timezone=timezone, next_run_at=next_run,
                delivery_json=json.dumps(spec), run_mode=rm,
                clone_abilities=json.dumps(clone_abilities or []),
                max_per_day=max_per_day, disable_after_failures=disable_after_failures,
                expires_at=expires_at or None, retry_max=int(retry_max or 0),
                retry_backoff_seconds=int(retry_backoff_seconds or 0), origin="tool",
            )
            await _emit_automation_updated("created", row.get("id"))
            return json.dumps({"status": "ok", "automation": row, "warning": rm_warn,
                               "message": f"Scheduled ({schedule_kind}); next run {next_run}."})
        except AutomationEntitlementError as e:
            return json.dumps({
                "status": "error", "code": e.code, "message": str(e),
            })
        except Exception as e:
            logger.exception("schedule_task failed")
            return json.dumps({"status": "error", "message": str(e)})

    async def remind_me(note: str, in_minutes: float = 0, at: str = ""):
        """Set a one-shot reminder that fires in the current chat (e.g. 'in 1 hour')."""
        # NOTE: this prompt is what the agent sees when the timer FIRES. It must
        # read as "deliver now", never as "set a reminder" — an earlier wording
        # made the firing agent re-schedule itself, creating an endless chain.
        return await schedule_task(
            prompt=(
                f"A reminder you previously set for the user has just FIRED. "
                f"Deliver this reminder message to them now: \"{note}\"\n\n"
                "Do NOT call remind_me or schedule_task — do not create any new "
                "reminder or automation. Simply tell the user the reminder message."
            ),
            task_label=f"Reminder: {note[:40]}", in_minutes=in_minutes, at=at,
            run_mode="inline", channel="webchat", channel_recipient=session_id,
        )

    async def list_automations():
        """List this agent's scheduled tasks / timers for the current user."""
        from app.db import get_db
        db = get_db()
        rows = await db.list_automations(agent_id=agent_id or None, owner_user_id=user_id)
        return json.dumps({"status": "ok", "automations": rows, "count": len(rows)})

    async def update_automation(automation_id: str, prompt: Optional[str] = None,
                                task_label: Optional[str] = None, schedule_cron: Optional[str] = None,
                                timezone: Optional[str] = None, enabled: Optional[bool] = None,
                                delivery: Optional[list] = None, run_mode: Optional[str] = None,
                                clone_abilities: Optional[list] = None, max_per_day: Optional[int] = None,
                                disable_after_failures: Optional[int] = None, expires_at: Optional[str] = None,
                                retry_max: Optional[int] = None, retry_backoff_seconds: Optional[int] = None):
        """Edit one of this agent's scheduled tasks."""
        from app.db import get_db
        from app.automation.runner import _compute_next_run
        db = get_db()
        row = await _owned_automation(db, automation_id)
        if not row:
            return json.dumps({"status": "error", "message": "automation not found"})
        fields: Dict[str, Any] = {}
        if prompt is not None: fields["prompt"] = prompt
        if task_label is not None: fields["task_label"] = task_label
        if timezone is not None: fields["timezone"] = timezone
        if enabled is not None: fields["enabled"] = enabled
        if schedule_cron is not None:
            fields["schedule_cron"] = schedule_cron
            fields["next_run_at"] = _compute_next_run(schedule_cron, timezone or row.get("timezone") or "UTC")
        if run_mode is not None:
            fields["run_mode"], _ = _validate_run_mode(run_mode, enabled_providers)
        if delivery is not None:
            fields["delivery_json"] = json.dumps(_build_delivery_spec(delivery=delivery, default_session_id=session_id))
        if clone_abilities is not None: fields["clone_abilities"] = json.dumps(clone_abilities)
        if max_per_day is not None: fields["max_per_day"] = max_per_day
        if disable_after_failures is not None: fields["disable_after_failures"] = disable_after_failures
        if expires_at is not None: fields["expires_at"] = expires_at or None
        if retry_max is not None: fields["retry_max"] = int(retry_max)
        if retry_backoff_seconds is not None: fields["retry_backoff_seconds"] = int(retry_backoff_seconds)
        updated = await db.update_automation(automation_id, **fields)
        await _emit_automation_updated("updated", automation_id)
        return json.dumps({"status": "ok", "automation": updated})

    async def pause_automation(automation_id: str):
        """Pause (disable) a scheduled task without deleting it."""
        from app.db import get_db
        db = get_db()
        if not await _owned_automation(db, automation_id):
            return json.dumps({"status": "error", "message": "automation not found"})
        await db.update_automation(automation_id, enabled=False)
        await _emit_automation_updated("paused", automation_id)
        return json.dumps({"status": "ok", "message": "paused"})

    async def resume_automation(automation_id: str):
        """Resume (re-enable) a paused scheduled task."""
        from app.db import get_db
        from app.automation.runner import _compute_next_run
        db = get_db()
        row = await _owned_automation(db, automation_id)
        if not row:
            return json.dumps({"status": "error", "message": "automation not found"})
        fields: Dict[str, Any] = {"enabled": True}
        if (row.get("schedule_kind") or "cron") != "once" and not row.get("next_run_at"):
            fields["next_run_at"] = _compute_next_run(row.get("schedule_cron") or "", row.get("timezone") or "UTC")
        await db.update_automation(automation_id, **fields)
        await _emit_automation_updated("resumed", automation_id)
        return json.dumps({"status": "ok", "message": "resumed"})

    async def cancel_automation(automation_id: str):
        """Delete a scheduled task (and reap its dedicated clone runner if any)."""
        from app.db import get_db
        db = get_db()
        row = await _owned_automation(db, automation_id)
        if not row:
            return json.dumps({"status": "error", "message": "automation not found"})
        runner_id = (row.get("runner_agent_id") or "").strip()
        ok = await db.delete_automation(automation_id)
        if runner_id and row.get("run_mode") == "dedicated_clone":
            try:
                await db.delete_clone_agent(runner_id)
            except Exception:
                pass
        await _emit_automation_updated("cancelled", automation_id)
        return json.dumps({"status": "ok" if ok else "error",
                           "message": "cancelled" if ok else "delete failed"})

    async def run_automation_now(automation_id: str):
        """Fire a scheduled task immediately (in addition to its schedule)."""
        from app.db import get_db
        db = get_db()
        if not await _owned_automation(db, automation_id):
            return json.dumps({"status": "error", "message": "automation not found"})
        try:
            from app.scheduler import get_scheduler
            res = await get_scheduler().run_now(automation_id)
            return json.dumps({
                "status": "ok", "result": res,
                "note": ("The automation has now run exactly once; its output is in "
                         "result.reply_excerpt and was already delivered per its "
                         "delivery config. Do NOT call run_automation_now again "
                         "for this request."),
            })
        except Exception as e:
            return json.dumps({"status": "error", "message": str(e)})

    async def remember_automation_state(state):
        """Persist state for the automation whose run is currently in progress."""
        from app.automation.runner import set_automation_memory, current_automation_id
        if not current_automation_id():
            return json.dumps({"status": "error", "message": "no automation run in progress"})
        ok = await set_automation_memory(state)
        return json.dumps({"status": "ok" if ok else "error"})

    return {
        "list_event_sources": list_event_sources,
        "list_delivery_channels": list_delivery_channels,
        "event_subscribe": event_subscribe,
        "list_event_subscriptions": list_event_subscriptions,
        "event_unsubscribe": event_unsubscribe,
        "schedule_task": schedule_task,
        "remind_me": remind_me,
        "list_automations": list_automations,
        "update_automation": update_automation,
        "pause_automation": pause_automation,
        "resume_automation": resume_automation,
        "cancel_automation": cancel_automation,
        "run_automation_now": run_automation_now,
        "remember_automation_state": remember_automation_state,
    }

# ── Per-agent config defaults ──────────────────────────────────────────────

_AUTOMATION_CONFIG_DEFAULTS = {
    "max_automations_per_user": 20,
    "allow_event_subscriptions": True,
    "allow_recurring_schedules": True,
    "max_events_per_subscription": 50,
    "require_approval": False,
    "max_delivery_channels": 3,
}


def _err(msg: str) -> str:
    return json.dumps({"status": "error", "error": msg})


def _coerce_bool(v) -> bool:
    """Interpret a settings value as a boolean. The config panel saves every
    field as a STRING, so a naive ``bool(v)`` turns "false"/"off"/"0" into
    True — exactly backwards. Treat the common false-ish strings as False."""
    if isinstance(v, bool):
        return v
    if isinstance(v, (int, float)):
        return bool(v)
    if isinstance(v, str):
        return v.strip().lower() in ("1", "true", "yes", "on", "enabled")
    return bool(v)


async def _load_automation_config(agent_id: str) -> dict:
    """Read the EFFECTIVE per-agent automation limits.

    The agent's own ``ability_settings`` from its automation connection row are
    routed through ``effective_ability_config``, which clamps each one to the
    admin's GLOBAL ceiling (Agent Settings → Agent Tools): the admin can disable
    recurring schedules / event triggers, force human approval on, or lower a
    numeric cap, and the agent can only match it or be stricter — never looser.
    See the ``ceiling`` rules in automation.json + app/admin/ability_config.py."""
    if not agent_id:
        return dict(_AUTOMATION_CONFIG_DEFAULTS)
    limits = dict(_AUTOMATION_CONFIG_DEFAULTS)
    try:
        from app.db import get_db
        db = get_db()
        settings: dict = {}
        conns = await db.get_agent_connections(agent_id)
        for c in conns:
            if c.get("connection_type") == "automation" and c.get("section") == "ability":
                raw = c.get("config")
                if isinstance(raw, str):
                    raw = json.loads(raw or "{}")
                if isinstance(raw, dict):
                    settings = raw.get("ability_settings") or {}
                break
        # Clamp the per-agent settings to the admin ceiling before applying.
        try:
            from app.admin import ability_config as _abcfg
            effective = _abcfg.effective_ability_config("automation", settings)
        except Exception as e:
            logger.debug("automation: effective config failed, using raw per-agent: %s", e)
            effective = dict(settings)
        for k in _AUTOMATION_CONFIG_DEFAULTS:
            if k in effective:
                v = effective[k]
                if k in ("max_automations_per_user", "max_events_per_subscription",
                         "max_delivery_channels"):
                    try:
                        v = int(v)
                    except (TypeError, ValueError):
                        continue
                elif k in ("allow_event_subscriptions", "allow_recurring_schedules",
                          "require_approval"):
                    v = _coerce_bool(v)
                limits[k] = v
    except Exception as e:
        logger.debug("Failed to load automation config for agent %s: %s", agent_id, e)
    return limits


async def _automation_count(user_id: str) -> int:
    """Best-effort count of a user's automations (scheduled + subscriptions)."""
    try:
        from app.db import get_db
        db = get_db()
        autos = await db.list_automations(owner_user_id=user_id)
        subscriptions = await db.list_event_subscriptions(owner_user_id=user_id)
        return len(autos or []) + len(subscriptions or [])
    except Exception:
        return 0


# ── build_tools — wraps core factory handlers with limit enforcement ───────

def build_tools(*, user_id: str = "", session_id: str = "", agent_id: str = "",
                agent_template_id: str = "", enabled_providers=None, **_ctx) -> dict:
    """Return {tool_name: handler} for the event-subscription tools, each closed
    over the caller's user_id / agent_id / session_id and wrapped with per-agent
    limit enforcement. The factory + schema constants are defined in this same
    module (self-contained)."""
    handlers = build_automation_tools(
        user_id=user_id, agent_id=agent_id, session_id=session_id,
        enabled_providers=enabled_providers,
    )

    # ── Wrap schedule_task ──────────────────────────────────────────────
    if "schedule_task" in handlers and agent_id:
        _orig_schedule = handlers["schedule_task"]

        async def _wrapped_schedule(prompt: str, task_label: str = "",
                                    schedule_cron: str = "", in_minutes: float = 0,
                                    at: str = "", timezone: str = "UTC",
                                    delivery=None, channel: str = "",
                                    channel_recipient: str = "", silent: bool = False,
                                    run_mode: str = "inline", clone_abilities=None,
                                    max_per_day=None, disable_after_failures=None,
                                    expires_at: str = "", retry_max: int = 0,
                                    retry_backoff_seconds: int = 0):
            cfg = await _load_automation_config(agent_id)

            # Check recurring-schedule gate.
            if schedule_cron and not cfg.get("allow_recurring_schedules"):
                return _err(
                    "Recurring schedules are not allowed by this agent's "
                    "configuration. Use in_minutes or at for one-shot tasks."
                )

            # Check max automations.
            count = await _automation_count(user_id)
            max_total = cfg.get("max_automations_per_user", 20)
            if count >= max_total:
                return _err(
                    f"Automation limit reached: user already has {count} "
                    f"automations (max {max_total})."
                )

            # Clamp delivery channels.
            max_ch = cfg.get("max_delivery_channels", 3)
            if delivery and isinstance(delivery, list) and len(delivery) > max_ch:
                delivery = delivery[:max_ch]

            # Require-approval: inject enabled=False via a wrapper call.
            # The core handler doesn't accept an `enabled` kwarg, so we create
            # normally and then pause it.
            result_str = await _orig_schedule(
                prompt=prompt, task_label=task_label, schedule_cron=schedule_cron,
                in_minutes=in_minutes, at=at, timezone=timezone,
                delivery=delivery, channel=channel,
                channel_recipient=channel_recipient, silent=silent,
                run_mode=run_mode, clone_abilities=clone_abilities,
                max_per_day=max_per_day,
                disable_after_failures=disable_after_failures,
                expires_at=expires_at, retry_max=retry_max,
                retry_backoff_seconds=retry_backoff_seconds,
            )

            if cfg.get("require_approval"):
                try:
                    data = json.loads(result_str)
                    auto_id = data.get("automation", {}).get("id")
                    if auto_id:
                        from app.db import get_db
                        db = get_db()
                        await db.update_automation(auto_id, enabled=False)
                        data["note"] = (data.get("note") or "") + (
                            " Automation created PAUSED — a human must enable "
                            "it from the Automation tab before it runs."
                        )
                        return json.dumps(data)
                except Exception:
                    pass

            return result_str

        _wrapped_schedule.__doc__ = _orig_schedule.__doc__
        handlers["schedule_task"] = _wrapped_schedule

    # ── Wrap remind_me (delegates to schedule_task, which is now wrapped) ──
    # No separate wrapping needed — remind_me calls schedule_task internally.

    # ── Wrap event_subscribe ────────────────────────────────────────────
    if "event_subscribe" in handlers and agent_id:
        _orig_subscribe = handlers["event_subscribe"]

        async def _wrapped_subscribe(source: str, event_type: str, prompt: str,
                                     filter=None, task_label: str = "",
                                     trigger_natural: str = "",
                                     channel: str = "",
                                     channel_recipient: str = "",
                                     silent: bool = False,
                                     delivery=None, run_mode: str = "inline",
                                     clone_abilities=None,
                                     max_per_day=None,
                                     disable_after_failures=None,
                                     expires_at: str = "",
                                     retry_max: int = 0,
                                     retry_backoff_seconds: int = 0):
            cfg = await _load_automation_config(agent_id)

            # Gate: event subscriptions.
            if not cfg.get("allow_event_subscriptions"):
                return _err(
                    "Event subscriptions are not allowed by this agent's "
                    "configuration. Use schedule_task for timed jobs instead."
                )

            # Check max automations.
            count = await _automation_count(user_id)
            max_total = cfg.get("max_automations_per_user", 20)
            if count >= max_total:
                return _err(
                    f"Automation limit reached: user already has {count} "
                    f"automations (max {max_total})."
                )

            # Clamp max_per_day ceiling.
            cap = cfg.get("max_events_per_subscription", 50)
            if max_per_day is None or (isinstance(max_per_day, (int, float)) and max_per_day > cap):
                max_per_day = cap

            # Clamp delivery channels.
            max_ch = cfg.get("max_delivery_channels", 3)
            if delivery and isinstance(delivery, list) and len(delivery) > max_ch:
                delivery = delivery[:max_ch]

            result_str = await _orig_subscribe(
                source=source, event_type=event_type, prompt=prompt,
                filter=filter, task_label=task_label,
                trigger_natural=trigger_natural,
                channel=channel, channel_recipient=channel_recipient,
                silent=silent, delivery=delivery, run_mode=run_mode,
                clone_abilities=clone_abilities,
                max_per_day=max_per_day,
                disable_after_failures=disable_after_failures,
                expires_at=expires_at, retry_max=retry_max,
                retry_backoff_seconds=retry_backoff_seconds,
            )

            if cfg.get("require_approval"):
                try:
                    data = json.loads(result_str)
                    sub_id = (data.get("subscription") or {}).get("id")
                    if sub_id:
                        from app.db import get_db
                        db = get_db()
                        await db.update_event_subscription(sub_id, enabled=False)
                        data["note"] = (data.get("note") or "") + (
                            " Subscription created PAUSED — a human must "
                            "enable it from the Automation tab before it runs."
                        )
                        return json.dumps(data)
                except Exception:
                    pass

            return result_str

        _wrapped_subscribe.__doc__ = _orig_subscribe.__doc__
        handlers["event_subscribe"] = _wrapped_subscribe

    TOOL_SCHEMAS.clear()
    TOOL_SCHEMAS.update(AUTOMATION_TOOL_SCHEMAS)
    DESTRUCTIVE.clear()
    DESTRUCTIVE.update(AUTOMATION_DESTRUCTIVE)

    return handlers

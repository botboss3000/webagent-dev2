"""Unified delivery for automations, timers, and event subscriptions.

One place that answers two questions for any automation run:

  1. WHERE does the agent run, session-wise?  → a *session plan*
     ('here' an existing session / 'new_session' / 'headless').
  2. WHERE does the result get pushed afterwards? → *external targets*
     (a comms channel / an outbound webhook / a saved file).

The run engine (`app.automation.runner`) uses the session plan to pick the
session the agent turn runs in (the reply lands there automatically), then
calls `deliver_result()` to fan the reply out to any external targets.

Delivery spec shape (stored in the `delivery_json` column):

    {
      "silent": false,
      "targets": [
        {"mode": "here", "session_id": "abc"},      # run in an existing session
        {"mode": "new_session"},                     # run in a fresh sidebar session
        {"mode": "headless"},                         # run, surface nothing
        {"mode": "channel", "channel": "telegram", "recipient": "123"},
        {"mode": "webhook", "url": "https://..."},
        {"mode": "file", "filename": "report.md", "room": "files"}
      ]
    }

Legacy rows (channel / channel_recipient / silent columns, no delivery_json)
are translated by `spec_from_legacy()` so everything flows through one path.

Email is intentionally NOT supported yet — no outbound email channel exists in
`app/communications/plugins`. A `{"mode": "email"}` target resolves to an
invalid target with a clear reason rather than silently dropping.
"""

from __future__ import annotations

import json
import logging
from typing import Any, Dict, List, Optional

logger = logging.getLogger(__name__)

SESSION_MODES = {"here", "new_session", "headless"}
EXTERNAL_MODES = {"channel", "webhook", "file", "email"}


# ── Spec parsing / legacy shim ───────────────────────────────────────────────

def spec_from_legacy(
    channel: Optional[str],
    channel_recipient: Optional[str],
    silent: bool,
) -> Dict[str, Any]:
    """Build a unified spec from the old channel/channel_recipient/silent columns."""
    if silent:
        return {"silent": True, "targets": [{"mode": "headless"}]}
    ch = (channel or "").strip()
    rid = (channel_recipient or "").strip()
    if not ch or ch == "webchat":
        # webchat + a recipient that is a session id → deliver into that session.
        if rid:
            return {"silent": False, "targets": [{"mode": "here", "session_id": rid}]}
        return {"silent": False, "targets": [{"mode": "new_session"}]}
    return {
        "silent": False,
        "targets": [{"mode": "channel", "channel": ch, "recipient": rid or None}],
    }


def parse_spec(
    delivery_json: Any,
    *,
    legacy_channel: Optional[str] = None,
    legacy_recipient: Optional[str] = None,
    legacy_silent: bool = False,
) -> Dict[str, Any]:
    """Normalize a row's delivery into a spec dict, falling back to legacy columns."""
    spec: Dict[str, Any] = {}
    if isinstance(delivery_json, dict):
        spec = delivery_json
    elif isinstance(delivery_json, str) and delivery_json.strip() not in ("", "{}"):
        try:
            spec = json.loads(delivery_json)
        except Exception:
            spec = {}
    if not isinstance(spec, dict) or not spec.get("targets"):
        return spec_from_legacy(legacy_channel, legacy_recipient, legacy_silent)
    return spec


# ── Resolution ───────────────────────────────────────────────────────────────

async def resolve_delivery(
    db,
    *,
    user_id: str,
    agent_id: str,
    spec: Dict[str, Any],
    current_session_id: Optional[str] = None,
) -> Dict[str, Any]:
    """Split a spec into a session plan + validated external targets.

    Returns:
        {
          "session_mode": "here"|"new_session"|"headless",
          "session_id": <id or None>,        # only for "here"
          "silent": bool,
          "external": [ {mode, ...validated fields..., "valid": bool, "error": str?} ],
        }
    """
    targets = spec.get("targets") or []
    silent = bool(spec.get("silent"))
    session_mode = "new_session"
    session_id: Optional[str] = None
    external: List[Dict[str, Any]] = []

    # Channel availability map (admin-enabled + agent-connected + recipient known).
    avail: Dict[str, Dict[str, Any]] = {}
    try:
        from app.events.channels import list_available_channels
        for c in await list_available_channels(
            db, user_id=user_id, agent_id=agent_id, current_session_id=current_session_id
        ):
            avail[c["channel"]] = c
    except Exception as e:
        logger.debug("channel enumeration failed during resolve_delivery: %s", e)

    saw_session_target = False
    for t in targets:
        mode = (t.get("mode") or "").strip()
        if mode == "here":
            session_mode, saw_session_target = "here", True
            session_id = (t.get("session_id") or current_session_id) or None
        elif mode == "new_session":
            session_mode, saw_session_target = "new_session", True
        elif mode == "headless":
            session_mode, saw_session_target = "headless", True
            silent = True
        elif mode == "channel":
            ch = (t.get("channel") or "").strip()
            info = avail.get(ch)
            recipient = (t.get("recipient") or "").strip() or (info.get("recipient") if info else None)
            if not info:
                external.append({"mode": "channel", "channel": ch, "valid": False,
                                 "error": f"channel '{ch}' not available for this agent"})
            elif not recipient:
                external.append({"mode": "channel", "channel": ch, "valid": False,
                                 "error": f"channel '{ch}' has no recipient configured"})
            else:
                external.append({"mode": "channel", "channel": ch,
                                 "recipient": recipient, "valid": True})
        elif mode == "webhook":
            url = (t.get("url") or "").strip()
            external.append({"mode": "webhook", "url": url, "valid": url.startswith(("http://", "https://")),
                             **({} if url.startswith(("http://", "https://")) else {"error": "invalid webhook url"})})
        elif mode == "file":
            external.append({"mode": "file", "filename": t.get("filename") or "automation-result.txt",
                             "room": t.get("room") or "files", "valid": True})
        elif mode == "email":
            external.append({"mode": "email", "valid": False,
                             "error": "no outbound email channel configured"})
        else:
            external.append({"mode": mode or "?", "valid": False, "error": "unknown delivery mode"})

    if not saw_session_target and not external:
        # Nothing specified → default to a fresh session.
        session_mode = "new_session"

    return {
        "session_mode": session_mode,
        "session_id": session_id,
        "silent": silent,
        "external": external,
    }


# ── Dispatch ─────────────────────────────────────────────────────────────────

async def deliver_result(
    db,
    *,
    user_id: str,
    external: List[Dict[str, Any]],
    text: str,
    context: Optional[Dict[str, Any]] = None,
) -> List[Dict[str, Any]]:
    """Send the reply to each external target. Returns per-target outcomes
    (the same target dicts, each with an added 'status' and optional 'error')."""
    results: List[Dict[str, Any]] = []
    for t in external:
        out = dict(t)
        if not t.get("valid"):
            out["status"] = "skipped"
            results.append(out)
            continue
        mode = t.get("mode")
        try:
            if mode == "channel":
                out["status"] = await _send_channel(t["channel"], t["recipient"], text)
            elif mode == "webhook":
                out["status"] = await _send_webhook(t["url"], text, context or {})
            elif mode == "file":
                out["status"], out["url"] = await _save_file(user_id, t["filename"], t["room"], text)
            else:
                out["status"] = "skipped"
        except Exception as e:  # noqa: BLE001
            out["status"] = "error"
            out["error"] = str(e)[:300]
            logger.warning("delivery target %s failed: %s", mode, e)
        results.append(out)
    return results


async def _send_channel(channel: str, recipient: str, text: str) -> str:
    from app.communications.manager import get_plugin_manager
    pm = get_plugin_manager()
    plugin = pm.get_plugin(channel)
    if not plugin or not plugin.enabled:
        raise RuntimeError(f"channel {channel} not enabled")
    await plugin.send_message(recipient, text)
    return "delivered"


async def _send_webhook(url: str, text: str, context: Dict[str, Any]) -> str:
    import httpx
    payload = {"text": text, **{k: v for k, v in context.items() if k in ("automation_id", "subscription_id", "agent_id", "kind")}}
    async with httpx.AsyncClient(timeout=20) as client:
        resp = await client.post(url, json=payload)
        resp.raise_for_status()
    return "delivered"


async def _save_file(user_id: str, filename: str, room: str, text: str):
    from app import user_workspace as ws
    from app.encryption.workspace_files import write_encrypted_file
    name = ws.safe_filename(filename or "automation-result.txt")
    out_dir = ws.user_dir(user_id, room or "files")
    dest = ws.unique_path(out_dir, name)
    await write_encrypted_file(user_id, dest, (text or "").encode("utf-8"))
    return "delivered", ws.public_url(user_id, room or "files", dest.name)

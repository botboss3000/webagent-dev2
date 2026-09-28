"""Persistence for Remote Access.

Two files, both runtime-only (gitignored):

* ``remote_access.json`` — this install's config: which method is active, each
  method's non-secret settings, the signpost settings, and this PC's own
  rendezvous key + push token.  No provider secrets live here — ngrok and
  cloudflared keep their own credentials in their own config (set up once by the
  user), so a leak of this file can't move money or impersonate the tunnel
  provider; it only reveals where this PC currently is.

* ``remote_access_pointers.json`` — the signpost directory when THIS install
  plays the "server" role (e.g. the always-on VM at webagent.live).  It is
  multi-tenant: one entry per rendezvous key, so it can host the "where is my
  PC right now" lookup for many users, not just the operator.  Each entry binds
  a secret push token on first write (trust-on-first-use), so only the PC that
  created the entry can later move it.
"""

from __future__ import annotations

import json
import secrets
import threading
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, Optional

PROJECT_ROOT = Path(__file__).resolve().parents[2]
CONFIG_FILE = PROJECT_ROOT / "data" / "config" / "remote_access.json"
POINTERS_FILE = PROJECT_ROOT / "data" / "config" / "remote_access_pointers.json"

_lock = threading.RLock()

DEFAULT_SIGNPOST_SERVER = "https://webagent.live"

# Methods the card offers. "managed" == the app can start/stop the tunnel.
METHODS = ("same_network", "ngrok", "cloudflare", "tailscale", "manual")

DEFAULT_CONFIG: Dict[str, Any] = {
    "active_method": "same_network",
    "auto_start": False,            # start the managed tunnel on server boot
    "headful_url": "",             # legacy migration fallback; app.db is authoritative
    "slave_running": False,         # legacy migration fallback; app.db is authoritative
    "slave_tokens": {},             # legacy migration fallback; app.db is authoritative
    "rendezvous_key": "",           # public-ish; lives in the phone bookmark
    "push_token": "",               # secret; authorizes pointer updates
    "ngrok": {
        "bin_path": "",            # blank → look up "ngrok" on PATH
        "domain": "",              # reserved domain → stable address (paid)
        "region": "",
    },
    "cloudflare": {
        "bin_path": "",            # blank → "cloudflared" on PATH
        "tunnel": "",              # named tunnel (stable hostname)
        "hostname": "",            # the fixed https hostname you mapped
        "quick": False,            # True → ephemeral trycloudflare URL
    },
    "tailscale": {
        "bin_path": "",
    },
    "manual": {
        "public_url": "",          # the address your port-forward exposes
    },
    "signpost": {
        "enabled": True,
        "role": "client",          # client | server | both
        "server_url": DEFAULT_SIGNPOST_SERVER,
        "label": "",               # friendly name shown on the bookmark page
    },
}


def _deep_merge(base: dict, over: dict) -> dict:
    out = dict(base)
    for k, v in (over or {}).items():
        if isinstance(v, dict) and isinstance(out.get(k), dict):
            out[k] = _deep_merge(out[k], v)
        else:
            out[k] = v
    return out


def load_config() -> Dict[str, Any]:
    """Config with defaults filled in. Generates the key/token on first read."""
    data: Dict[str, Any] = {}
    try:
        if CONFIG_FILE.exists():
            with open(CONFIG_FILE, "r", encoding="utf-8") as f:
                data = json.load(f) or {}
    except Exception:
        data = {}
    cfg = _deep_merge(DEFAULT_CONFIG, data)
    changed = False
    if not cfg.get("rendezvous_key"):
        cfg["rendezvous_key"] = secrets.token_urlsafe(12)
        changed = True
    if not cfg.get("push_token"):
        cfg["push_token"] = secrets.token_urlsafe(24)
        changed = True
    if changed:
        save_config(cfg)
    return cfg


def save_config(cfg: Dict[str, Any]) -> None:
    with _lock:
        try:
            with open(CONFIG_FILE, "w", encoding="utf-8") as f:
                json.dump(cfg, f, indent=2)
        except Exception:
            pass


def update_config(patch: Dict[str, Any]) -> Dict[str, Any]:
    """Deep-merge a patch into the saved config and return the result."""
    with _lock:
        cfg = _deep_merge(load_config(), patch or {})
        save_config(cfg)
        return cfg


def update_slave_state(port: int, *, token: Optional[str] = None,
                       running: Optional[bool] = None, url: Optional[str] = None,
                       clear_token: bool = False, provider: Optional[str] = None,
                       state: Optional[str] = None, slave_pid: Optional[int] = None,
                       tunnel_pid: Optional[int] = None,
                       started_at: Optional[float] = None) -> Dict[str, Any]:
    """Persist detached-slave control/link data in the app database.

    The old JSON fields are read only as a migration/fallback. Once the app-data
    write succeeds, transient ownership details live only in the database.
    """
    key = str(int(port))
    with _lock:
        cfg = load_config()
        link = load_slave_link(port)
        if token is not None:
            link["token"] = str(token)
        if clear_token:
            link.pop("token", None)
        if running is not None:
            link["running"] = bool(running)
        if url is not None:
            # Preserve the last resolved URL when stopping; it remains useful as
            # connection history while `running` says whether it is live.
            if url or not link.get("url"):
                link["url"] = str(url or "").strip().rstrip("/")
        if provider is not None:
            link["provider"] = str(provider or "")
        if state is not None:
            link["state"] = str(state or "")
        if slave_pid is not None:
            link["slave_pid"] = int(slave_pid or 0)
        if tunnel_pid is not None:
            link["tunnel_pid"] = int(tunnel_pid or 0)
        if started_at is not None:
            link["started_at"] = float(started_at or 0)
        link["port"] = int(port)
        link["updated_at"] = datetime.now(timezone.utc).isoformat()
        saved = _save_slave_link(port, link)
        tokens = dict(cfg.get("slave_tokens") or {})
        if saved:
            # Remove pre-database control material after it has migrated.
            changed = key in tokens or bool(cfg.get("slave_running")) or bool(cfg.get("headful_url"))
            tokens.pop(key, None)
            cfg["slave_tokens"] = tokens
            cfg["slave_running"] = False
            cfg["headful_url"] = ""
            if changed:
                save_config(cfg)
        else:
            # Schema bootstrap can lag early startup. Keep the old file fallback
            # usable, and a later status report will retry the database write.
            if clear_token:
                tokens.pop(key, None)
            elif token is not None:
                tokens[key] = str(token)
            cfg["slave_tokens"] = tokens
            if running is not None:
                cfg["slave_running"] = bool(running)
            if url is not None:
                cfg["headful_url"] = str(url or "")
            save_config(cfg)
        return cfg


def _slave_ref(port: int) -> str:
    return f"runtime:tunnel:{int(port)}"


def load_slave_link(port: int) -> Dict[str, Any]:
    """Read durable tunnel ownership/link details from app-data ``app.db``."""
    try:
        from app.db import get_app_db
        raw = get_app_db().get_raw_client()
        res = raw.table("instances").select("metadata").eq("ref", _slave_ref(port)).execute()
        rows = (res.data or []) if hasattr(res, "data") else []
        if rows:
            metadata = json.loads(rows[0].get("metadata") or "{}")
            link = metadata.get("tunnel_link") or {}
            if isinstance(link, dict):
                return dict(link)
    except Exception:
        pass
    # One-time compatibility path from the pre-database JSON fields.
    cfg = load_config()
    token = str((cfg.get("slave_tokens") or {}).get(str(int(port))) or "")
    out: Dict[str, Any] = {
        "port": int(port),
        "running": bool(cfg.get("slave_running")),
        "url": str(cfg.get("headful_url") or "").strip().rstrip("/"),
    }
    if token:
        out["token"] = token
    return out


def _save_slave_link(port: int, link: Dict[str, Any]) -> bool:
    try:
        from app.db import get_app_db
        raw = get_app_db().get_raw_client()
        ref = _slave_ref(port)
        res = raw.table("instances").select("*").eq("ref", ref).execute()
        rows = (res.data or []) if hasattr(res, "data") else []
        row = dict(rows[0]) if rows else {
            "ref": ref,
            "kind": "local_runtime",
            "display_name": "Tunnel link",
            "provider": str(link.get("provider") or ""),
            "status": str(link.get("state") or ""),
            "created_at": datetime.now(timezone.utc).isoformat(),
        }
        try:
            metadata = json.loads(row.get("metadata") or "{}")
        except (TypeError, ValueError, json.JSONDecodeError):
            metadata = {}
        metadata["tunnel_link"] = dict(link)
        row["metadata"] = json.dumps(metadata, ensure_ascii=False)
        row["provider"] = str(link.get("provider") or row.get("provider") or "")
        row["status"] = str(link.get("state") or row.get("status") or "")
        row["updated_at"] = datetime.now(timezone.utc).isoformat()
        raw.table("instances").upsert(row, on_conflict="ref").execute()
        return True
    except Exception:
        # Early startup/tests may precede schema bootstrap. The JSON mirror above
        # keeps the launch usable, and the next report retries the database write.
        return False


def regenerate_keys() -> Dict[str, str]:
    """Rotate this PC's rendezvous key + push token (invalidates old bookmark)."""
    cfg = load_config()
    cfg["rendezvous_key"] = secrets.token_urlsafe(12)
    cfg["push_token"] = secrets.token_urlsafe(24)
    save_config(cfg)
    return {"rendezvous_key": cfg["rendezvous_key"], "push_token": cfg["push_token"]}


# ── Signpost directory (server role) ─────────────────────────────────────────

def _load_pointers() -> Dict[str, Any]:
    try:
        if POINTERS_FILE.exists():
            with open(POINTERS_FILE, "r", encoding="utf-8") as f:
                return json.load(f) or {}
    except Exception:
        pass
    return {}


def _save_pointers(data: Dict[str, Any]) -> None:
    try:
        with open(POINTERS_FILE, "w", encoding="utf-8") as f:
            json.dump(data, f, indent=2)
    except Exception:
        pass


def set_pointer(key: str, url: str, push_token: str, label: str = "") -> Dict[str, Any]:
    """Record/refresh where the PC behind ``key`` currently is.

    Trust-on-first-use: the first write for a key binds ``push_token``; later
    writes must present the same token or they're rejected. Returns a dict with
    ``ok`` (bool) and, on failure, ``error``.
    """
    if not key or not url:
        return {"ok": False, "error": "missing key or url"}
    with _lock:
        data = _load_pointers()
        existing = data.get(key)
        if existing and existing.get("push_token") and existing["push_token"] != push_token:
            return {"ok": False, "error": "push token mismatch"}
        data[key] = {
            "url": url,
            "label": label or (existing or {}).get("label", ""),
            "push_token": push_token or (existing or {}).get("push_token", ""),
            "updated_at": time.time(),
        }
        _save_pointers(data)
    return {"ok": True}


def get_pointer(key: str) -> Optional[Dict[str, Any]]:
    """Public lookup — returns url/label/updated_at only (never the token)."""
    if not key:
        return None
    entry = _load_pointers().get(key)
    if not entry:
        return None
    return {
        "url": entry.get("url", ""),
        "label": entry.get("label", ""),
        "updated_at": entry.get("updated_at", 0),
    }

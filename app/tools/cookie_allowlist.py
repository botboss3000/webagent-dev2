"""Per-user cookie allowlist for the managed browser.

The browser only PERSISTS cookies that are worth keeping. A cookie survives a
save when its registrable domain is either:
  • **first-party** — a site the session actually navigated to (tracked live in
    app/tools/browser.py), or
  • **allowlisted** — in this user's allowlist (managed by the agent on the
    user's behalf), or in the built-in login/SSO allowlist below.
Everything else — the endless ad-tech long tail — is dropped. No denylist to
maintain: anything not first-party-or-allowlisted is junk by definition.

The agent edits a user's allowlist with the ``cookie_allowlist`` tool (Browser
Control) whenever the user says things like "remember my login for X" or "keep me
signed into X". Storage: ``data/config/cookie-allowlist.json`` →
``{"users": {"<user_id>": ["example.com", ...]}}``.
"""

from __future__ import annotations

import json
import logging
import os
import threading
from typing import List, Optional
from urllib.parse import urlparse

logger = logging.getLogger(__name__)

_CONFIG_PATH = os.path.join(
    os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))),
    "data", "config", "cookie-allowlist.json",
)
_lock = threading.Lock()

# Identity / SSO providers whose cookies are kept for everyone — a login often
# sets a cookie on the provider's domain (not the site you typed), and dropping it
# would break the very login this browser exists to use. Registrable domains.
_SSO_BUILTIN: frozenset = frozenset({
    "microsoftonline.com", "live.com", "microsoft.com", "google.com",
    "okta.com", "auth0.com", "onelogin.com", "pingidentity.com",
    "apple.com", "github.com", "gitlab.com", "facebook.com", "amazon.com",
    "duosecurity.com", "cloudflareaccess.com", "salesforce.com",
})

# Registrable-domain extraction. A small multi-part-TLD set keeps "bbc.co.uk"
# from collapsing to "co.uk"; everything else is the last two labels. (A full
# public-suffix list would be heavier than this hygiene feature warrants.)
_MULTI_TLDS: frozenset = frozenset({
    "co.uk", "org.uk", "ac.uk", "gov.uk", "co.jp", "ne.jp", "or.jp",
    "com.au", "net.au", "org.au", "co.nz", "com.br", "com.mx", "co.in",
    "co.za", "com.cn", "com.sg", "com.hk", "com.tr", "co.kr",
})


def registrable_domain(host_or_url: str) -> str:
    """Best-effort eTLD+1 from a host or URL. '' when nothing usable.
    e.g. 'https://app.asana.com/x' -> 'asana.com'; 'login.microsoftonline.com'
    -> 'microsoftonline.com'; '.bbc.co.uk' -> 'bbc.co.uk'."""
    s = (host_or_url or "").strip().lower()
    if not s:
        return ""
    if "://" in s:
        s = urlparse(s).hostname or ""
    s = s.lstrip(".")
    # strip any leftover port/path
    s = s.split("/")[0].split(":")[0]
    parts = [p for p in s.split(".") if p]
    if len(parts) <= 2:
        return ".".join(parts)
    last2 = ".".join(parts[-2:])
    if last2 in _MULTI_TLDS:
        return ".".join(parts[-3:])
    return last2


def _load() -> dict:
    try:
        if os.path.exists(_CONFIG_PATH):
            with open(_CONFIG_PATH, "r", encoding="utf-8") as fh:
                data = json.load(fh)
            if isinstance(data, dict):
                data.setdefault("users", {})
                return data
    except Exception as e:  # noqa: BLE001
        logger.debug("cookie-allowlist load failed: %s", e)
    return {"users": {}}


def _save(data: dict) -> None:
    os.makedirs(os.path.dirname(_CONFIG_PATH), exist_ok=True)
    tmp = _CONFIG_PATH + ".tmp"
    with open(tmp, "w", encoding="utf-8") as fh:
        json.dump(data, fh, indent=2, sort_keys=True)
    os.replace(tmp, _CONFIG_PATH)


def list_for_user(user_id: str) -> List[str]:
    """The user's own allowlisted registrable domains (not the SSO built-ins)."""
    data = _load()
    return sorted(data.get("users", {}).get(user_id or "", []))


def add(user_id: str, domain: str) -> str:
    """Add a domain (normalized to its registrable form). Returns the stored
    registrable domain, or '' if nothing usable was given."""
    reg = registrable_domain(domain)
    if not reg or not user_id:
        return ""
    with _lock:
        data = _load()
        users = data.setdefault("users", {})
        lst = users.setdefault(user_id, [])
        if reg not in lst:
            lst.append(reg)
            _save(data)
    return reg


def remove(user_id: str, domain: str) -> str:
    reg = registrable_domain(domain)
    if not reg or not user_id:
        return ""
    with _lock:
        data = _load()
        lst = data.get("users", {}).get(user_id or "", [])
        if reg in lst:
            lst.remove(reg)
            _save(data)
    return reg


def is_allowed(user_id: str, registrable: str) -> bool:
    """True when this registrable domain should ALWAYS be kept for this user
    (their allowlist or the built-in SSO providers)."""
    if not registrable:
        return False
    if registrable in _SSO_BUILTIN:
        return True
    return registrable in set(_load().get("users", {}).get(user_id or "", []))

"""Ad / tracker blocklist for the managed browser.

Why: a single visit to an ad-supported page silently sets dozens of third-party
tracking cookies. Persisted into a browser session's ``storage_state`` they pile
up (we saw ~1000 in one jar, ~95% pure ad-tech) and bloat the DB for no benefit.

Two uses, sharing one list:
  • ``tracker_url_regex()`` — a Playwright route matcher; the browser ABORTS
    requests to these domains so the cookies are never set in the first place
    (also speeds up page loads). This is the primary mechanism.
  • ``strip_tracker_cookies(state)`` — belt-and-braces: drops any tracker-domain
    cookies from a ``storage_state`` blob before it's written, so existing junk
    gets cleaned out on the next save and nothing slips through.

DELIBERATELY EXCLUDED: anything used for **auth / login / social-login** (e.g.
``connect.facebook.net``, ``accounts.google.com``) or CDNs — blocking those would
break the very logins this browser exists to use. Only pure ad / analytics /
audience-tracking domains are listed.

Extend or override without code edits via ``data/config/tracker-blocklist.json``:
  • a JSON **list** of domains → ADDED to the built-ins, or
  • a JSON **object** ``{"add": [...], "remove": [...]}`` → built-ins plus ``add``,
    minus ``remove`` (``remove`` is your allow-exception list if a blocked domain
    breaks a page).
"""

from __future__ import annotations

import json
import logging
import os
import re
from typing import Optional, Tuple

logger = logging.getLogger(__name__)

# Registrable domains. Matched as a suffix: a host matches ``d`` when it equals
# ``d`` or ends with ``.d`` — so ``adservice.google.com`` blocks only that host
# and its subdomains, never ``accounts.google.com``.
_BUILTIN: frozenset = frozenset({
    # Google ad/analytics (NOT google.com itself — only the ad/measurement hosts)
    "doubleclick.net", "googlesyndication.com", "googleadservices.com",
    "googletagmanager.com", "google-analytics.com", "adservice.google.com",
    "2mdn.net", "app-measurement.com",
    # Big SSPs / exchanges / DSPs
    "pubmatic.com", "sonobi.com", "media.net", "smaato.net", "fwmrm.net",
    "intentiq.com", "nextmillmedia.com", "sparteo.com", "bricks-co.com",
    "adnxs.com", "criteo.com", "criteo.net", "rubiconproject.com",
    "casalemedia.com", "openx.net", "indexww.com", "33across.com",
    "bidswitch.net", "adsrvr.org", "amazon-adsystem.com", "smartadserver.com",
    "adform.net", "3lift.com", "lijit.com", "mathtag.com", "districtm.io",
    "onetag.com", "yieldmo.com", "gumgum.com", "sharethrough.com",
    "contextweb.com", "teads.tv", "serving-sys.com",
    # Audience / identity / data-management
    "crwdcntrl.net", "demdex.net", "everesttech.net", "rlcdn.com", "agkn.com",
    "bluekai.com", "tapad.com", "eyeota.net", "pippio.com", "adsymptotic.com",
    # Verification / brand-safety
    "moatads.com", "adsafeprotected.com", "doubleverify.com",
    # Content recommendation
    "taboola.com", "outbrain.com",
    # Analytics / session-recording
    "scorecardresearch.com", "quantserve.com", "quantcount.com", "hotjar.com",
    "mixpanel.com", "segment.io", "segment.com", "amplitude.com",
    "fullstory.com", "mouseflow.com", "clarity.ms", "heapanalytics.com",
    "chartbeat.com", "parsely.com",
})

_CONFIG_PATH = os.path.join(
    os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))),
    "data", "config", "tracker-blocklist.json",
)

# Caches (computed once; cleared by reload()).
_domains: Optional[frozenset] = None
_url_re: Optional[re.Pattern] = None  # None = not built yet; False sentinel = empty


def _load_overrides() -> Tuple[set, set]:
    """(add, remove) domain sets from the optional config file. Never raises."""
    try:
        if not os.path.exists(_CONFIG_PATH):
            return set(), set()
        with open(_CONFIG_PATH, "r", encoding="utf-8") as fh:
            data = json.load(fh)
    except Exception as e:  # noqa: BLE001
        logger.debug("tracker-blocklist override load failed: %s", e)
        return set(), set()
    if isinstance(data, list):
        return {str(d).lower().lstrip(".") for d in data}, set()
    if isinstance(data, dict):
        add = {str(d).lower().lstrip(".") for d in (data.get("add") or [])}
        remove = {str(d).lower().lstrip(".") for d in (data.get("remove") or [])}
        return add, remove
    return set(), set()


def tracker_domains() -> frozenset:
    """Built-in list merged with the optional config override (cached)."""
    global _domains
    if _domains is None:
        add, remove = _load_overrides()
        _domains = frozenset((set(_BUILTIN) | add) - remove)
    return _domains


def reload() -> None:
    """Drop caches so an edited config file is picked up."""
    global _domains, _url_re
    _domains = None
    _url_re = None


def is_tracker_domain(host: str) -> bool:
    """True when ``host`` is (a subdomain of) a blocked tracker domain."""
    host = (host or "").lower().lstrip(".")
    if not host:
        return False
    return any(host == d or host.endswith("." + d) for d in tracker_domains())


def tracker_url_regex():
    """A compiled regex matching request URLs whose host is a tracker — for
    Playwright's ``context.route(...)``. Returns None when the list is empty."""
    global _url_re
    if _url_re is None:
        doms = sorted(tracker_domains())
        if not doms:
            _url_re = False  # sentinel: built, empty
        else:
            alt = "|".join(re.escape(d) for d in doms)
            _url_re = re.compile(r"^https?://([^/]*\.)?(" + alt + r")([:/]|$)", re.I)
    return _url_re or None


def strip_tracker_cookies(state) -> Tuple[object, int]:
    """Return (state_without_tracker_cookies, dropped_count). Pass-through for a
    blob with no cookies list. Only the ``cookies`` array is touched — logins for
    real sites are kept; localStorage/origins are untouched."""
    if not isinstance(state, dict):
        return state, 0
    cookies = state.get("cookies")
    if not isinstance(cookies, list) or not cookies:
        return state, 0
    kept = [ck for ck in cookies if not is_tracker_domain(ck.get("domain", ""))]
    dropped = len(cookies) - len(kept)
    if dropped:
        state = {**state, "cookies": kept}
    return state, dropped

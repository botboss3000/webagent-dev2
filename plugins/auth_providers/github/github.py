"""GitHub login provider hook.

GitHub's standard OAuth2 code exchange is handled by the generic flow in
`app/auth_providers` (the descriptor's `token_url` + `Accept: application/json`).
The one thing GitHub does differently is the profile: the primary email is often
NOT on the `/user` response (users can keep it private), so we fall back to the
`/user/emails` endpoint and pick the primary, verified address. That single
override is the whole reason this file exists — everything else is data in
`github.json`.

Contract (see `app/auth_providers/registry.py`): a provider folder may ship an
optional `<id>.py` exposing `async def extract_profile(...)` and/or
`async def build_token_request(...)`. Both are looked up by name and, when
present, replace the generic behaviour.
"""

import logging

logger = logging.getLogger(__name__)

_HEADERS = {"User-Agent": "WebAgent", "Accept": "application/vnd.github+json"}


async def extract_profile(*, descriptor, token_data, access_token, http, creds):
    """Return the normalized identity dict for a GitHub login.

    `http` is a live httpx.AsyncClient supplied by the caller.
    """
    auth = {"Authorization": f"Bearer {access_token}", **_HEADERS}
    u = {}
    try:
        r = await http.get("https://api.github.com/user", headers=auth)
        if r.status_code == 200:
            u = r.json()
    except Exception as e:  # noqa: BLE001
        logger.warning("github /user fetch failed: %s", e)

    email = u.get("email") or ""
    if not email:
        # Email is private on the profile — ask the emails endpoint and take the
        # primary verified one (that scope, user:email, is requested at authorize).
        try:
            er = await http.get("https://api.github.com/user/emails", headers=auth)
            if er.status_code == 200:
                emails = er.json() or []
                primary = next(
                    (e for e in emails if e.get("primary") and e.get("verified")), None
                ) or next((e for e in emails if e.get("verified")), None)
                if primary:
                    email = primary.get("email", "")
        except Exception as e:  # noqa: BLE001
            logger.warning("github /user/emails fetch failed: %s", e)

    return {
        "external_id": str(u.get("id", "")),
        "email": email,
        "name": u.get("name") or u.get("login") or "",
        "avatar": u.get("avatar_url", ""),
    }

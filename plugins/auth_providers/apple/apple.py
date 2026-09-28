"""Sign in with Apple provider hook.

Apple deviates from plain OAuth2 in three ways, all handled here:

  1. The ``client_secret`` sent to the token endpoint is not a static string — it
     is a short-lived **ES256 JWT** the app signs with the developer's ``.p8``
     key (claims: iss=Team ID, sub=Services ID, aud=appleid.apple.com).
  2. The signed-in identity is NOT fetched from a userinfo endpoint (Apple has
     none). It lives inside the ``id_token`` (a JWT) returned by the token
     exchange — we read its ``sub`` (stable user id) and ``email``.
  3. The person's **name** is only ever sent ONCE, in the ``user`` form field of
     the very first ``form_post`` callback. The router passes that through as
     ``form`` so we can capture it on first sign-in.

The client-id parameter Apple expects at authorize/token time is the
**Services ID**, so ``apple.json`` sets ``client_id_field: "service_id"`` and the
generic flow substitutes it automatically.

Contract: see ``app/auth_providers/registry.py``. This module exposes
``build_client_secret`` (used by the generic token request when a provider needs
a computed secret) and ``extract_profile``.
"""

import json
import logging
import time

logger = logging.getLogger(__name__)

_AUDIENCE = "https://appleid.apple.com"


def build_client_secret(*, descriptor, creds):
    """Return the ES256-signed JWT Apple accepts as the ``client_secret``.

    Returns "" when the required key material is missing or signing deps are
    unavailable, so the caller surfaces a clean "not configured" error rather
    than crashing.
    """
    service_id = (creds.get("service_id") or "").strip()
    team_id = (creds.get("team_id") or "").strip()
    key_id = (creds.get("key_id") or "").strip()
    private_key = (creds.get("private_key") or "").strip()
    if not (service_id and team_id and key_id and private_key):
        return ""
    try:
        import jwt
    except Exception as e:  # noqa: BLE001
        logger.warning("apple client secret: PyJWT unavailable: %s", e)
        return ""
    now = int(time.time())
    claims = {
        "iss": team_id,
        "iat": now,
        "exp": now + 3600,
        "aud": _AUDIENCE,
        "sub": service_id,
    }
    try:
        return jwt.encode(
            claims, private_key, algorithm="ES256", headers={"kid": key_id}
        )
    except Exception as e:  # noqa: BLE001
        logger.warning("apple client secret signing failed: %s", e)
        return ""


async def extract_profile(*, descriptor, token_data, access_token, http, creds, **ctx):
    """Read identity from Apple's id_token (+ the first-sign-in name form)."""
    id_token = token_data.get("id_token") or ""
    claims = {}
    if id_token:
        try:
            import jwt
            claims = jwt.decode(
                id_token,
                options={
                    "verify_signature": False,
                    "verify_exp": False,
                    "verify_aud": False,
                },
            )
        except Exception as e:  # noqa: BLE001
            logger.warning("apple id_token decode failed: %s", e)

    # Name only arrives once, in the form_post `user` field on first consent.
    name = ""
    form = ctx.get("form") or {}
    raw_user = form.get("user")
    if raw_user:
        try:
            u = json.loads(raw_user) if isinstance(raw_user, str) else raw_user
            nm = (u or {}).get("name") or {}
            name = " ".join(
                p for p in [nm.get("firstName", ""), nm.get("lastName", "")] if p
            ).strip()
        except Exception:  # noqa: BLE001
            pass

    email = claims.get("email", "")
    return {
        "external_id": str(claims.get("sub", "")),
        "email": email,
        "name": name or (email.split("@")[0] if email else "Apple user"),
        "avatar": "",
    }

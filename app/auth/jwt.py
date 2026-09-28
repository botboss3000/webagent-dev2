"""JWT token creation and validation.

Signing secret (hardened 2026-06-28): the HS256 secret is NO LONGER a
hardcoded fallback. A guessable/committed secret lets anyone FORGE a token for
any user (including the bootstrap ``admin``), which defeats every downstream
auth gate. The secret is resolved at import (a synchronous, always-available
boot value) in this order:

  1. ``JWT_SECRET`` env var, if set — explicit operator override (e.g. to share
     one secret across a multi-instance deployment).
  2. A persisted, git-ignored file ``data/config/jwt_secret.txt`` — created on
     first run and reused on every restart so existing tokens stay valid across
     restarts and across sibling processes (server, TUI, subprocess token mint).
  3. Freshly generated with ``secrets.token_urlsafe`` and written to that file.

If the file can't be written (read-only FS, perms), we fall back to an
in-memory random secret for this process — still unforgeable, but tokens won't
survive a restart (everyone re-logs in). That degraded mode is logged loudly.
There is intentionally NO hardcoded default to fall back to.

Shared-deployment convergence (added 2026-07-04): the local file above is
PER-BOX, so a fresh deploy or a redeploy invents its OWN secret and instantly
invalidates every login pass a browser already holds (they look signed in but
every authenticated request 401s). To fix that, ``ensure_shared_jwt_secret`` —
run once at startup after the vault is active — reconciles this box's secret
with a single value stored in the SHARED secrets vault (the same vault the
deploy already configures):

  * vault already holds one  → adopt it (cache locally, swap the live secret),
    so tokens minted by any sibling instance validate here too;
  * vault holds none yet     → publish this box's secret so later instances and
    redeploys reuse it.

Because the deploy carries the shared vault, the signing key effectively travels
with the deploy — no separate carry needed. Unless ``JWT_SECRET`` is pinned
(then the operator owns sharing and we never touch the vault). Best-effort: a
vault error just leaves this box on its local secret until the vault is
reachable, and never blocks startup.
"""

import os
import logging
import secrets
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Optional
import jwt
from jwt.exceptions import PyJWTError

logger = logging.getLogger(__name__)

# Old committed default — kept ONLY to warn if an operator pins it via env.
_KNOWN_WEAK_SECRET = "webagent-dev-secret-change-in-prod"

# app/auth/jwt.py → app/auth → app → repo root
_REPO_ROOT = Path(__file__).resolve().parent.parent.parent
_SECRET_FILE = _REPO_ROOT / "data" / "config" / "jwt_secret.txt"

_ALGORITHM = "HS256"
_ACCESS_TOKEN_EXPIRE_MINUTES = 60 * 24  # 24 hours

# Key under which the ONE shared signing secret lives in the secrets vault, so
# every instance/redeploy on the shared infra signs with the same value.
_VAULT_SECRET_KEY = "jwt_signing_secret"


def _read_secret_file() -> str:
    try:
        s = _SECRET_FILE.read_text(encoding="utf-8").strip()
        return s
    except (FileNotFoundError, NotADirectoryError):
        return ""
    except OSError as e:  # pragma: no cover - defensive
        logger.warning("Could not read JWT secret file %s: %s", _SECRET_FILE, e)
        return ""


def _resolve_secret() -> str:
    """Resolve the signing secret (see module docstring for the order)."""
    env = os.environ.get("JWT_SECRET")
    if env:
        if env == _KNOWN_WEAK_SECRET:
            logger.warning(
                "JWT_SECRET is set to the old hardcoded default — this is "
                "publicly known and forgeable. Set it to a strong random value "
                "or unset it to auto-generate one."
            )
        return env

    existing = _read_secret_file()
    if existing:
        return existing

    new = secrets.token_urlsafe(64)
    try:
        _SECRET_FILE.parent.mkdir(parents=True, exist_ok=True)
        # Atomic, exclusive create so two processes racing on first run don't
        # clobber each other — whoever loses reads the winner's value.
        fd = os.open(str(_SECRET_FILE), os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
        try:
            os.write(fd, new.encode("utf-8"))
        finally:
            os.close(fd)
        logger.info("Generated and persisted a new JWT signing secret.")
        return new
    except FileExistsError:
        # Another process created it between our read and our create — adopt theirs.
        adopted = _read_secret_file()
        return adopted or new
    except OSError as e:
        logger.warning(
            "Could not persist JWT secret to %s (%s). Using an in-memory secret "
            "for this process — tokens will NOT survive a restart (re-login "
            "required) and won't match sibling processes.",
            _SECRET_FILE, e,
        )
        return new


_SECRET = _resolve_secret()


def _persist_local(secret: str) -> None:
    """Overwrite the local cache file with a (possibly vault-sourced) secret, so a
    later restart resolves the same value before the vault reconcile even runs.
    Best-effort — a read-only FS just means we re-adopt from the vault next boot."""
    try:
        _SECRET_FILE.parent.mkdir(parents=True, exist_ok=True)
        tmp = _SECRET_FILE.with_suffix(".tmp")
        tmp.write_text(secret, encoding="utf-8")
        os.replace(str(tmp), str(_SECRET_FILE))
        try:
            os.chmod(str(_SECRET_FILE), 0o600)
        except OSError:
            pass
    except OSError as e:
        logger.warning("Could not cache the shared JWT secret locally (%s): %s",
                       _SECRET_FILE, e)


async def ensure_shared_jwt_secret() -> bool:
    """Converge this instance onto ONE signing secret shared via the vault.

    Run once at startup, AFTER the secrets vault is active (see app/main.py). See
    the module docstring for the contract. Returns True when a shared value was
    adopted or published, False when skipped (env-pinned) or on a vault error.
    Never raises — auth must never be what blocks startup."""
    global _SECRET
    if os.environ.get("JWT_SECRET"):
        # Operator pinned the secret; they own cross-instance sharing.
        return False
    try:
        from app.secrets import get_secrets
        vault = get_secrets()
        shared = await vault.get(_VAULT_SECRET_KEY)
        if shared:
            if shared != _SECRET:
                _SECRET = shared
                _persist_local(shared)
                logger.info("Adopted the shared JWT signing secret from the vault "
                            "— login passes now validate across all instances.")
            return True
        # First one in: publish ours so later instances/redeploys reuse it.
        await vault.set(_VAULT_SECRET_KEY, _SECRET)
        logger.info("Published this instance's JWT signing secret to the shared "
                    "vault so later instances and redeploys reuse it.")
        return True
    except Exception as e:  # noqa: BLE001 — best-effort; must never block startup
        logger.warning("Could not reconcile the JWT signing secret with the vault "
                       "(sign-in may not carry across instances until it is "
                       "reachable): %s", e)
        return False


def get_secret() -> str:
    """The active HS256 signing secret. Other modules that sign/verify their own
    tokens (e.g. OAuth state in app/admin/integrations.py) must use THIS so every
    token in the app is signed with the same key."""
    return _SECRET


def create_access_token(
    username: str,
    user_id: str,
    expires_minutes: Optional[int] = None,
    device_id: Optional[str] = None,
    reauthenticated: bool = False,
    extra_claims: Optional[dict] = None,
) -> str:
    """Create a signed JWT access token.

    ``expires_minutes`` overrides the default 24h lifetime when given — used by
    the browser-connector pairing flow, which mints a long-lived token the user
    pastes into the extension once (so it doesn't 401 a day later). The override
    only changes the expiry; the token is otherwise identical to a login token,
    scoped to the same user_id, so it carries no extra privilege.
    """
    minutes = _ACCESS_TOKEN_EXPIRE_MINUTES if expires_minutes is None else expires_minutes
    expire = datetime.now(timezone.utc) + timedelta(minutes=minutes)
    from app.auth.revocation import current_epoch, register_device

    epoch = current_epoch(user_id)
    resolved_device_id = device_id or secrets.token_urlsafe(18)
    device_version = register_device(
        user_id,
        resolved_device_id,
        epoch,
        reauthenticated=reauthenticated,
    )
    payload = {
        "sub": username,
        "user_id": user_id,
        "rev": epoch,
        "device_id": resolved_device_id,
        "device_version": device_version,
        "exp": expire,
        "iat": datetime.now(timezone.utc),
    }
    if extra_claims:
        # Callers may add scope/binding claims, but may not replace identity,
        # revocation, device, or lifetime fields owned by this function.
        protected = set(payload)
        payload.update({k: v for k, v in extra_claims.items() if k not in protected})
    token = jwt.encode(payload, _SECRET, algorithm=_ALGORITHM)
    return token


def decode_token(token: str) -> Optional[dict]:
    """Decode and validate a JWT token. Returns payload dict or None."""
    try:
        payload = jwt.decode(token, _SECRET, algorithms=[_ALGORITHM])
        from app.auth.revocation import token_is_current

        user_id = str(payload.get("user_id") or payload.get("sub") or "")
        if not token_is_current(
            user_id,
            int(payload.get("rev") or 0),
            str(payload.get("device_id") or ""),
            int(payload.get("device_version") or 0),
        ):
            logger.debug("JWT rejected by revocation epoch/device state")
            return None
        return payload
    except (PyJWTError, TypeError, ValueError, OSError) as e:
        logger.debug("JWT decode failed: %s", e)
        return None


def decode_signed_token(token: str, *, verify_expiry: bool = False) -> Optional[dict]:
    """Verify signature while optionally allowing an expired/revoked token.

    This is intentionally narrower than normal authentication. It exists for
    the device-cache purge status/ack protocol, where an epoch-revoked device
    must still be able to erase local data and report that it did so. Callers
    MUST NOT use the returned payload to authorize any other operation.
    """
    try:
        return jwt.decode(
            token,
            _SECRET,
            algorithms=[_ALGORITHM],
            options={"verify_exp": bool(verify_expiry)},
        )
    except (PyJWTError, TypeError, ValueError, OSError) as e:
        logger.debug("Signed JWT decode failed: %s", e)
        return None

"""
UI-driven connection config for remote secrets vaults.

This is what makes "launch local, then configure the vault in the UI after"
possible. A remote vault's OWN connection details cannot live in the application
database (the vault holds the very secrets needed to open that database — the
chicken-and-egg trap). But "not in the DB" does NOT mean "env vars only": the
details may equally live in a small local file OUTSIDE the DB, which is exactly
what the admin UI writes here. Mirrors how the DB provider config is persisted.

Two kinds of value, stored differently:

  - Non-secret locators (address / URL / project / region / mount / prefix /
    client & tenant ids) → `secrets_config.json`, a plain local file alongside
    `secrets_mode.json`. Safe to keep in the clear; they identify a vault, they
    don't open it.

  - The one sensitive access token → stored KEYRING-FIRST WITH FILE FALLBACK
    (the admin's chosen policy): try the OS credential store (Windows Credential
    Manager / macOS Keychain / Secret Service); if the host has none, fall back
    to `secrets_token_fallback.json` (same trust level as an env var / .env on
    that host). Either way it stays OUTSIDE the application database.

Resolution order every backend follows: explicit constructor arg → value saved
here (config / token) → environment variable. So existing env-based deployments
keep working untouched, and the UI path is purely additive.

Pure stdlib + a LAZY keyring import — this module never imports the `app.secrets`
package or any backend, so there is no circular-import risk when a backend reads
its own saved config from here.
"""

import json
import logging
import os
from typing import Any, Dict, Optional

logger = logging.getLogger(__name__)

# Same directory as secrets_mode.json (app/), so all three sit together and are
# covered by the existing .gitignore rules.
_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
_CONFIG_FILE = os.path.join(_DIR, "secrets_config.json")
_TOKEN_FALLBACK_FILE = os.path.join(_DIR, "secrets_token_fallback.json")

# Keyring service namespace for the bootstrap vault tokens. Distinct from the
# os_keyring SECRETS backend's own service ("webagent_vault") so the two never
# collide: this one stores the *connection* token for OTHER vaults.
_KEYRING_SERVICE = "webagent_vault_bootstrap"


def _env_locked() -> bool:
    return os.environ.get("WEBAGENT_CONFIG_SOURCE", "").lower() == "env"


# ── Non-secret connection config (local file) ───────────────────────────────


def _read_config_file() -> Dict[str, Dict[str, Any]]:
    if not os.path.exists(_CONFIG_FILE):
        return {}
    try:
        with open(_CONFIG_FILE, "r") as f:
            data = json.load(f) or {}
        return data.get("providers") or {}
    except Exception as e:
        logger.warning("Failed to read %s: %s", _CONFIG_FILE, e)
        return {}


def _write_config_file(providers: Dict[str, Dict[str, Any]]) -> None:
    if _env_locked():
        return
    try:
        with open(_CONFIG_FILE, "w") as f:
            json.dump({"providers": providers}, f, indent=2)
    except Exception as e:
        logger.warning("Failed to write %s: %s", _CONFIG_FILE, e)


def get_provider_config(provider: str) -> Dict[str, Any]:
    """Return the saved NON-SECRET connection fields for a provider (never the
    token). Empty dict when nothing has been configured."""
    return dict(_read_config_file().get(provider) or {})


def set_provider_config(provider: str, cfg: Dict[str, Any]) -> None:
    """Persist a provider's non-secret connection fields. Blank/None values are
    dropped so an unset field falls through to its environment-variable default
    rather than overriding it with an empty string."""
    providers = _read_config_file()
    cleaned = {k: v for k, v in (cfg or {}).items() if v not in (None, "")}
    providers[provider] = cleaned
    _write_config_file(providers)


def all_provider_configs() -> Dict[str, Dict[str, Any]]:
    """Every saved provider config (non-secret), for the status payload."""
    return _read_config_file()


# ── Access token (keyring-first, file fallback) ─────────────────────────────


def _read_token_file() -> Dict[str, str]:
    if not os.path.exists(_TOKEN_FALLBACK_FILE):
        return {}
    try:
        with open(_TOKEN_FALLBACK_FILE, "r") as f:
            return json.load(f) or {}
    except Exception as e:
        logger.warning("Failed to read %s: %s", _TOKEN_FALLBACK_FILE, e)
        return {}


def _write_token_file(tokens: Dict[str, str]) -> None:
    if _env_locked():
        return
    try:
        with open(_TOKEN_FALLBACK_FILE, "w") as f:
            json.dump(tokens, f, indent=2)
    except Exception as e:
        logger.warning("Failed to write %s: %s", _TOKEN_FALLBACK_FILE, e)


def set_provider_token(provider: str, token: Optional[str]) -> Optional[str]:
    """Store a provider's access token. Returns where it landed: 'keyring',
    'file', or None when cleared. A blank/None token clears any stored copy.

    Keyring-first with file fallback (the configured policy): try the OS
    credential store; on any failure (no keyring backend, headless Linux without
    a Secret Service, etc.) write the local fallback file instead — never the
    application database."""
    # Clear path.
    if not token:
        _delete_token(provider)
        return None

    # Try keyring first.
    try:
        import keyring
        keyring.set_password(_KEYRING_SERVICE, provider, token)
        # Drop any stale plaintext fallback so the token isn't duplicated on disk.
        tokens = _read_token_file()
        if provider in tokens:
            tokens.pop(provider, None)
            _write_token_file(tokens)
        return "keyring"
    except Exception as e:
        logger.info("Keyring unavailable for vault token (%s); using file fallback.", e)

    # File fallback.
    tokens = _read_token_file()
    tokens[provider] = token
    _write_token_file(tokens)
    return "file"


def get_provider_token(provider: str) -> Optional[str]:
    """Resolve a provider's saved token: keyring first, then the fallback file.
    Returns None when nothing is saved (the backend then falls back to env)."""
    try:
        import keyring
        val = keyring.get_password(_KEYRING_SERVICE, provider)
        if val:
            return val
    except Exception:
        pass
    return _read_token_file().get(provider)


def _delete_token(provider: str) -> None:
    try:
        import keyring
        try:
            keyring.delete_password(_KEYRING_SERVICE, provider)
        except Exception:
            pass
    except Exception:
        pass
    tokens = _read_token_file()
    if provider in tokens:
        tokens.pop(provider, None)
        _write_token_file(tokens)


def token_location(provider: str) -> Optional[str]:
    """Where a provider's token is stored — 'keyring', 'file', or None — without
    returning the token itself. For the status payload / UI placeholder."""
    try:
        import keyring
        if keyring.get_password(_KEYRING_SERVICE, provider):
            return "keyring"
    except Exception:
        pass
    if provider in _read_token_file():
        return "file"
    return None

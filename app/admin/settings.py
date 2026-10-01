"""
Settings endpoints — toggleable features for the agent.

Supports:
  - metadata_logging: stores full prompt context in interactions.metadata
  - provider: per-user AI provider config (provider, base_url, api_key, model)

Provider configs are stored per user_id in the auth_elements DB table.
"""

import json
import logging
import os
import re
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Optional

import httpx
from fastapi import APIRouter, Header, HTTPException, Query, Request
from pydantic import BaseModel, Field

from app.auth.jwt import decode_token
from app.auth.identity import request_user_id
from app.util.config_io import safe_write_json, set_config_key

logger = logging.getLogger(__name__)
router = APIRouter(prefix="/admin/settings", tags=["admin"])

PROJECT_ROOT = Path(__file__).resolve().parent.parent.parent
METADATA_FLAG = PROJECT_ROOT / ".metadata-enabled"
APP_SETTINGS_FILE = PROJECT_ROOT / "data" / "config" / "app-settings.json"

ANONYMOUS_KEY = "__anonymous__"
PLATFORM_LLM_OWNER = "admin"

DEFAULT_PROVIDER = {
    "provider": "",
    "auth_mode": "api_key",
    "base_url": "",
    "api_key": "",
    "model": "",
    "stateful_responses": "auto",
    "providers": {},
    "multi_providers": [],
}

PROVIDER_PRESETS = {
    "openrouter": {
        "name": "OpenRouter",
        "base_url": "https://openrouter.ai/api/v1",
    },
    "abacus": {
        "name": "Abacus.AI (RouteLLM)",
        "base_url": "https://routellm.abacus.ai/v1",
    },
    "openai": {
        "name": "ChatGPT API",
        "base_url": "https://api.openai.com/v1",
    },
    "openai_subscription": {
        "name": "ChatGPT Subscription",
        "base_url": "codex://chatgpt",
        "auth_mode": "chatgpt",
    },
    "gemini": {
        "name": "Google Gemini",
        "base_url": "https://generativelanguage.googleapis.com/v1beta/openai",
    },
    "groq": {
        "name": "Groq",
        "base_url": "https://api.groq.com/openai/v1",
    },
    "together": {
        "name": "Together AI",
        "base_url": "https://api.together.xyz/v1",
    },
    "deepseek": {
        "name": "DeepSeek",
        "base_url": "https://api.deepseek.com/v1",
    },
    "mistral": {
        "name": "Mistral AI",
        "base_url": "https://api.mistral.ai/v1",
    },
    "fireworks": {
        "name": "Fireworks AI",
        "base_url": "https://api.fireworks.ai/inference/v1",
    },
    "xai": {
        "name": "xAI (Grok)",
        "base_url": "https://api.x.ai/v1",
    },
    "perplexity": {
        "name": "Perplexity",
        "base_url": "https://api.perplexity.ai",
    },
    "ollama": {
        "name": "Ollama (local)",
        "base_url": "http://localhost:11434/v1",
    },
    "deepinfra": {
        "name": "DeepInfra",
        "base_url": "https://api.deepinfra.com/v1/openai",
    },
    "lmstudio": {
        "name": "LM Studio (local)",
        "base_url": "http://localhost:1234/v1",
    },
}


# ── User ID extraction ──────────────────────────────────────────────────────

def _resolve_user_id(authorization: str = "", token_qs: str = "") -> str:
    """Extract user_id from Authorization header or query param.
    Returns ANONYMOUS_KEY if no valid token.
    """
    raw = ""
    if authorization.startswith("Bearer "):
        raw = authorization[7:]
    if not raw and token_qs:
        raw = token_qs

    if raw:
        payload = decode_token(raw)
        if payload:
            return payload.get("user_id", ANONYMOUS_KEY)

    return ANONYMOUS_KEY



# ── LLM secret handling — keys live ONLY in the encrypted vault ─────────────
#
# An LLM config can carry several API keys: the default brain's key, one per
# entry in the ``providers`` map, and one per saved-roster model. The vault row
# (``auth_elements``) only Fernet-encrypts ``secret_ref``; its ``config`` blob is
# a plaintext column. So we pack EVERY key into a single JSON bundle stored in
# ``secret_ref`` (encrypted), and keep the plaintext ``config`` blob stripped of
# keys. Reads rehydrate the keys back out of the bundle, so callers see a
# complete config without any plaintext key ever landing on disk or in an
# unencrypted column.

def _has_plaintext_key(cfg: dict) -> bool:
    """True if any api_key is present anywhere in a config dict."""
    if not isinstance(cfg, dict):
        return False
    if cfg.get("api_key"):
        return True
    for v in (cfg.get("providers") or {}).values():
        if isinstance(v, dict) and v.get("api_key"):
            return True
    for e in (cfg.get("multi_providers") or []):
        if isinstance(e, dict) and e.get("api_key"):
            return True
    return False


def _strip_provider_secrets(config: dict) -> dict:
    """Return a copy of a provider config with every api_key blanked — the shape
    written to the vault's plaintext ``config`` blob."""
    clean = dict(config or {})
    clean["api_key"] = ""
    provs = clean.get("providers")
    if isinstance(provs, dict):
        clean["providers"] = {
            k: ({**v, "api_key": ""} if isinstance(v, dict) else v)
            for k, v in provs.items()
        }
    mp = clean.get("multi_providers")
    if isinstance(mp, list):
        clean["multi_providers"] = [
            ({**e, "api_key": ""} if isinstance(e, dict) else e) for e in mp
        ]
    return clean


def _pack_llm_secret(config: dict) -> str:
    """Collect every api_key in a config into one JSON bundle for the encrypted
    ``secret_ref``. Returns "" when there are no keys at all."""
    default = config.get("api_key", "") or ""
    providers = {}
    for name, v in (config.get("providers") or {}).items():
        if isinstance(v, dict) and v.get("api_key"):
            providers[name] = v["api_key"]
    entries = {
        str(entry.get("entry_id") or ""): str(entry.get("api_key") or "")
        for entry in (config.get("multi_providers") or [])
        if isinstance(entry, dict) and entry.get("entry_id") and entry.get("api_key")
    }
    if not default and not providers and not entries:
        return ""
    bundle = {"v": 2, "default": default}
    if providers:
        bundle["providers"] = providers
    if entries:
        bundle["entries"] = entries
    return json.dumps(bundle, separators=(",", ":"))


def _unpack_llm_secret(secret_ref: str) -> dict:
    """Parse a packed bundle. A bare key string (legacy / env-seeded rows) is
    treated as just the default key.

    ``secret_ref`` should already be plaintext by the time it gets here (the
    storage layer decrypts it — see EncryptedStorageBackend in app/db/interface.py).
    If decryption failed there (e.g. no DEK available for this device/vault),
    that layer now returns "" rather than the raw ciphertext, but treat a
    still-encrypted-looking string as unavailable here too rather than smuggling
    it into a config as if it were a real key.
    """
    if not secret_ref:
        return {}
    from app.encryption.interface import is_ciphertext
    if is_ciphertext(secret_ref):
        logger.error("LLM secret_ref is still encrypted (decrypt must have failed) — ignoring it")
        return {}
    s = secret_ref.strip()
    if s.startswith("{"):
        try:
            d = json.loads(s)
            if isinstance(d, dict) and d.get("v"):
                return d
        except Exception:
            pass
    return {"default": secret_ref}   # legacy bare key


def _rehydrate_llm_keys(config: dict, secret_ref: str) -> dict:
    """Splice keys from the encrypted bundle back into a config dict (mutates and
    returns it). A key already present in the (legacy plaintext) config is kept
    only when the bundle has nothing for that slot, so this is safe both before
    and after the migration has stripped the plaintext copies."""
    bundle = _unpack_llm_secret(secret_ref)
    config["api_key"] = bundle.get("default") or config.get("api_key", "") or ""
    bp = bundle.get("providers") or {}
    provs = config.get("providers")
    if isinstance(provs, dict):
        for name, v in provs.items():
            if isinstance(v, dict):
                v["api_key"] = bp.get(name) or v.get("api_key", "") or ""
    by_id = bundle.get("entries") or {}
    legacy = bundle.get("multi") or []
    mp = config.get("multi_providers")
    if isinstance(mp, list):
        for index, entry in enumerate(mp):
            if not isinstance(entry, dict):
                continue
            entry_id = str(entry.get("entry_id") or "")
            keyed = by_id.get(entry_id) if isinstance(by_id, dict) else ""
            positional = legacy[index] if index < len(legacy) else ""
            entry["api_key"] = keyed or positional or entry.get("api_key", "") or ""
    return config


def _llm_credential_scope(user_id: str) -> str:
    return "platform" if user_id == PLATFORM_LLM_OWNER else "user"


async def _load_llm_element(user_id: str):
    from app import model_credentials
    scope = _llm_credential_scope(user_id)
    return await model_credentials.get_owned(
        scope, owner=user_id, service="llm", label="default",
        user_id=user_id if scope == "user" else "",
    )


async def _persist_agent_llm_config(agent_id: str, full: dict) -> dict:
    """Write agent API keys to its owned secret sidecar and return keyless metadata."""
    from app import model_credentials
    secret = _pack_llm_secret(full)
    stripped = _strip_provider_secrets(full)
    await model_credentials.set_owned(
        "agent", owner=f"_agent:{agent_id}", service="llm", label="default",
        agent_id=agent_id, config={}, secret_ref=secret,
    )
    return stripped


async def _load_agent_llm_config(agent_id: str, config: dict) -> dict:
    """Hydrate an agent roster from its owned secret sidecar."""
    from app import model_credentials
    hydrated = json.loads(json.dumps(config or {}))
    elem = await model_credentials.get_owned(
        "agent", owner=f"_agent:{agent_id}", service="llm", label="default",
        agent_id=agent_id,
    )
    if elem:
        _rehydrate_llm_keys(hydrated, elem.get("secret_ref", ""))
        if elem.get("_secret_error") == "decrypt_failed":
            hydrated["_credential_error"] = "decrypt_failed"
    return hydrated


async def _hydrate_agent_record_llm(agent_rec: Optional[dict]) -> Optional[dict]:
    """Return a current runtime copy with owned keys and migrated metadata.

    A tool-calling loop keeps its initial agent dict in memory. Reload the
    control-plane row before each resolution so a roster role change takes
    effect on the next provider call, while an already-issued stream remains
    untouched.
    """
    if not agent_rec or not agent_rec.get("id"):
        return agent_rec
    record = dict(agent_rec)
    try:
        from app.db import get_db
        live = await get_db().get_agent_by_id(str(record["id"]))
        if isinstance(live, dict):
            record = dict(live)
    except Exception:
        # Live reloading improves convergence but must never interrupt a run.
        pass
    raw_meta = record.get("metadata")
    if isinstance(raw_meta, str):
        try:
            meta = json.loads(raw_meta or "{}")
        except Exception:
            meta = {}
    else:
        meta = dict(raw_meta) if isinstance(raw_meta, dict) else {}
    raw = meta.get("llm_config")
    if not isinstance(raw, dict):
        return record
    owned, converted = _legacy_agent_config_to_roster(raw)
    hydrated = await _load_agent_llm_config(str(record["id"]), owned)
    if converted or _has_plaintext_key(raw):
        stripped = await _persist_agent_llm_config(str(record["id"]), hydrated)
        clean_meta = dict(meta)
        clean_meta["llm_config"] = stripped
        try:
            # Agent records are read and updated through the control-plane DB.
            # Writing this migration to an agent-local store left the Agents API
            # serving legacy role flags from the authoritative row.
            from app.db import get_db
            await get_db().update_agent_fields(
                str(record["id"]), "", {"metadata": clean_meta},
                allow_install_admin=True,
            )
        except Exception:
            logger.warning("Could not scrub migrated agent LLM keys from metadata", exc_info=True)
    meta["llm_config"] = hydrated
    record["metadata"] = meta
    return record


async def _persist_llm_config(
    user_id: str, full: dict, *, sync_named_rosters: bool = True,
) -> None:
    """Single write path for a user's LLM config.

    Guarantees no API key is ever stored in plaintext: every key is packed into
    ONE encrypted vault secret (``auth_elements.secret_ref``); the DB ``config``
    blob receives a key-stripped copy.
    """
    full = _without_saved_default(full)
    secret = _pack_llm_secret(full)
    stripped = _strip_provider_secrets(full)
    try:
        from app import model_credentials
        scope = _llm_credential_scope(user_id)
        db = model_credentials.backend_for(
            scope, user_id=user_id if scope == "user" else "",
        )
        await model_credentials.set_owned(
            scope, owner=user_id, service="llm", label="default",
            user_id=user_id if scope == "user" else "",
            config=stripped, secret_ref=secret,
        )
        # Compatibility bridge: the existing Models UI still writes the
        # historical admin row. Keep that row during rollout, while also
        # updating system-managed named rosters. Explicitly administered or
        # imported rosters are never overwritten by this bridge.
        if user_id == PLATFORM_LLM_OWNER and sync_named_rosters:
            try:
                from app.entitlements.rosters import sync_legacy_platform_config
                await sync_legacy_platform_config(full, db=db)
            except Exception as roster_error:
                logger.warning("Failed to sync named platform rosters: %s", roster_error)
    except Exception as e:
        logger.warning("Failed to save LLM config to vault: %s", e)


async def _load_own_llm_config(user_id: str) -> Optional[dict]:
    """Load a user's OWN LLM config from the vault with keys rehydrated —
    NO admin/anonymous fallback. Used by the migration so we
    never copy one user's config onto another."""
    try:
        elem = await _load_llm_element(user_id)
        if elem:
            cfg = elem.get("config") or {}
            if isinstance(cfg, str):
                cfg = json.loads(cfg or "{}")
            _rehydrate_llm_keys(cfg, elem.get("secret_ref", ""))
            if isinstance(cfg.get("multi_providers"), list):
                # One-time, idempotent persistence migration. All later runtime
                # and UI reads see only the canonical role map, never `enabled`.
                cfg, migrated = _role_assignments_for_config(cfg, migrate_legacy=True)
                if migrated:
                    await _persist_llm_config(user_id, cfg)
            if elem.get("_secret_error") == "decrypt_failed":
                cfg["_credential_error"] = "decrypt_failed"
            return cfg
    except Exception:
        pass
    return None


async def _llm_storage_has_plaintext(user_id: str) -> bool:
    """True if a user's vault config blob still holds a plaintext api_key
    (an encrypted secret_ref does NOT count)."""
    try:
        elem = await _load_llm_element(user_id)
    except Exception:
        elem = None
    if elem:
        cfg = elem.get("config") or {}
        if isinstance(cfg, str):
            try:
                cfg = json.loads(cfg or "{}")
            except Exception:
                cfg = {}
        if _has_plaintext_key(cfg):
            return True
    return False


async def migrate_llm_secrets() -> None:
    """One-time scrub of historically plaintext LLM keys.

    For the admin user, fold any plaintext api_key from the DB ``config`` blob
    into the encrypted vault secret and rewrite the store stripped. Idempotent:
    once no plaintext key remains, re-running skips. Safe to call on every
    startup.
    """
    try:
        candidates = {"admin"}
    except Exception:
        candidates = {"admin"}
    secured = 0
    for uid in candidates:
        try:
            if not await _llm_storage_has_plaintext(uid):
                continue
            own = await _load_own_llm_config(uid)
            if not own:
                continue
            await _persist_llm_config(uid, own)
            secured += 1
        except Exception as e:
            logger.warning("LLM secret migration failed for %s: %s", str(uid)[:12], e)
    if secured:
        logger.info("Secured plaintext LLM key(s) for %d user(s) into the vault", secured)


_LEGACY_DEFAULT_FIELDS = (
    "entry_id", "alias_group_id", "provider", "auth_mode", "base_url", "api_key", "model", "display_name",
    "stateful_responses", "routing_mode", "text_capable", "image_capable",
    "image_out_capable", "voice_capable", "use_for_image",
    "use_for_image_out", "use_for_voice", "use_for_system",
    "high_effort_capable", "credential_configured",
    "credential_scope",
    "_user_default_entry_id", "_platform_default_entry_id",
)


def _without_saved_default(config: dict) -> dict:
    """Return the persisted form of an LLM config.

    The model roster is the only saved model authority. Historical top-level
    provider/model fields represented a second app default and are deliberately
    omitted. The provider map remains as configurator draft state.
    """
    clean = dict(config or {})
    for key in _LEGACY_DEFAULT_FIELDS:
        clean.pop(key, None)
    providers = clean.get("providers")
    if isinstance(providers, dict):
        # Provider-form drafts may retain credentials and connection details,
        # but model IDs belong only in the roster.
        clean["providers"] = {
            name: ({key: value for key, value in item.items() if key != "model"}
                   if isinstance(item, dict) else item)
            for name, item in providers.items()
        }
    clean.setdefault("providers", {})
    clean.setdefault("multi_providers", [])
    return clean


def _legacy_agent_config_to_roster(config: dict) -> tuple[dict, bool]:
    """Turn one historical top-level agent model into its sole roster row."""
    if any(
        isinstance(row, dict) and row.get("model")
        for row in (config.get("multi_providers") or [])
    ) or not config.get("model"):
        normalized, changed = _role_assignments_for_config(config, migrate_legacy=True)
        return normalized, changed

    import uuid

    entry = {
        key: config.get(key)
        for key in _LEGACY_DEFAULT_FIELDS
        if not key.startswith("_") and config.get(key) not in (None, "")
    }
    entry["entry_id"] = str(uuid.uuid4())
    entry.setdefault("text_capable", True)
    entry["credential_scope"] = "agent"
    migrated = _without_saved_default(config)
    migrated["use_default"] = config.get("use_default", False)
    migrated["multi_providers"] = [entry]
    migrated["role_assignments"] = {"standard": entry["entry_id"]}
    return migrated, True


def _runtime_config_from_roster(config: dict, *, role: str = "standard") -> dict:
    """Project a roster role into transient runtime fields.

    A role resolves ONLY through a literal `role_assignments` entry ID — there is
    no computed, capability-derived or legacy-flag Standard. An unassigned
    Standard therefore clears every runtime field, and the run fails red with
    "Standard model role not assigned" rather than starting on a model nobody
    appointed.
    """
    runtime = _without_saved_default(config)
    # Agent-owned rows keep their own IDs: the normaliser only mints one when it
    # is missing, and role assignments point at those IDs.
    rows = [dict(row) for row in (runtime.get("multi_providers") or [])
            if isinstance(row, dict) and row.get("model")]
    runtime["multi_providers"] = _agent_roster_entries_with_ids(rows)
    try:
        runtime, _ = _role_assignments_for_config(runtime, migrate_legacy=False)
    except ValueError:
        # A role row that vanished from the roster must not abort resolution: the
        # roster stays authoritative and the role simply resolves to nothing.
        runtime["role_assignments"] = {}
    roster = [row for row in (runtime.get("multi_providers") or [])
              if isinstance(row, dict) and row.get("model")]
    runtime["multi_providers"] = roster
    role = _normalize_role(role)
    assignments = runtime.get("role_assignments") or {}
    selected = _assign_slots(roster, assignments).get("roles", {}).get(role) \
        if str(assignments.get(role) or "") else None
    for key in _LEGACY_DEFAULT_FIELDS:
        runtime[key] = selected.get(key, "") if selected else ""
    return runtime


def alias_failover_candidates(config: dict) -> list[dict]:
    """Return executable configs for the selected row and its aliases.

    The selected runtime row always leads. Remaining members retain saved roster
    order and their own provider, credential, transport, and model metadata. This
    is deliberately a pure projection: it never changes role assignments, session
    overrides, or the persisted roster.
    """
    base = dict(config or {})
    rows = [
        dict(row) for row in (base.get("multi_providers") or [])
        if isinstance(row, dict) and row.get("model")
    ]
    if not rows:
        return [base]
    try:
        roster = _agent_roster_entries_with_ids(rows)
    except ValueError:
        return [base]
    selected_id = str(base.get("entry_id") or "").strip()
    selected = next(
        (row for row in roster if str(row.get("entry_id") or "") == selected_id),
        None,
    )
    group_id = str((selected or {}).get("alias_group_id") or "").strip()
    if not selected or not group_id:
        return [base]

    def usable(row: dict) -> bool:
        if row.get("enabled") is False or row.get("text_capable") is False:
            return False
        provider = str(row.get("provider") or "").strip().lower()
        model = str(row.get("model") or "").strip()
        if not provider or not model:
            return False
        auth_mode = str(row.get("auth_mode") or "api_key").strip().lower()
        keyless = provider in {"ollama", "lmstudio", "openai_subscription"} or auth_mode == "chatgpt"
        return keyless or bool(str(row.get("api_key") or "").strip())

    members = [selected] + [
        row for row in roster
        if str(row.get("entry_id") or "") != selected_id
        and str(row.get("alias_group_id") or "") == group_id
    ]
    candidates: list[dict] = []
    seen: set[str] = set()
    for row in members:
        entry_id = str(row.get("entry_id") or "")
        if entry_id in seen or not usable(row):
            continue
        candidate = dict(base)
        candidate.update(row)
        candidate["multi_providers"] = roster
        candidates.append(candidate)
        seen.add(entry_id)
    return candidates or [base]


async def _resolve_user_config(user_id: str, *, role: str = "standard") -> dict:
    """Resolve a user's owned config, then exactly their platform tier roster."""
    try:
        own = await _load_own_llm_config(user_id)
        if own:
            return _runtime_config_from_roster(own, role=role)
        from app.db import get_db
        from app.entitlements.rosters import resolve_platform_roster_config
        platform = await resolve_platform_roster_config(user_id, db=get_db())
        if platform:
            return _runtime_config_from_roster(platform, role=role)
    except Exception:
        logger.debug("Owned or platform model resolution failed", exc_info=True)
    return _runtime_config_from_roster(DEFAULT_PROVIDER, role=role)


async def resolve_agent_role_config(
    user_id: str, agent_rec: Optional[dict], role: str,
) -> dict:
    """Resolve one semantic role from an agent-owned hydrated roster."""
    agent_rec = await _hydrate_agent_record_llm(agent_rec)
    base = await _resolve_user_config(user_id)
    effective = _effective_config(base, agent_rec)
    selected = _assign_slots(
        effective.get("multi_providers") or [],
        effective.get("role_assignments") or {},
    ).get("roles", {}).get(_normalize_role(role))
    if not selected:
        return {}
    resolved = dict(selected)
    resolved["_credential_user_id"] = user_id
    resolved["_credential_agent_id"] = str((agent_rec or {}).get("id") or "")
    return resolved


async def _require_provider_admin(request: Request) -> str:
    """Return the verified DB-admin caller for provider-management routes.

    These routes configure the platform LLM fallback and therefore must not
    trust a client-supplied user id (or merely a decodable-but-non-admin JWT).
    Runtime resolution deliberately remains separate in
    :func:`_resolve_user_config`, where credentials are required for execution.
    """
    user_id = request_user_id(request)
    if not user_id:
        raise HTTPException(status_code=401, detail="Not authenticated")
    try:
        from app.db import get_db
        is_admin = await get_db().is_user_admin(user_id)
    except Exception:
        is_admin = False
    if not is_admin:
        raise HTTPException(status_code=403, detail="Admin access required.")
    return user_id


def _credential_suffix(value) -> str:
    """Return a non-usable credential hint for an owning settings UI."""
    return str(value or "")[-5:]


def _public_provider_config(config: dict) -> dict:
    """Return browser-safe provider metadata with no usable credentials.

    Empty key fields preserve the existing save contract (blank means keep the
    stored key), while ``credential_configured`` lets the UI show configuration
    state without receiving the secret itself.
    """
    safe = _strip_provider_secrets(config or {})
    safe["credential_configured"] = bool((config or {}).get("api_key"))
    safe["credential_suffix"] = _credential_suffix((config or {}).get("api_key"))

    providers = safe.get("providers")
    original_providers = (config or {}).get("providers") or {}
    if isinstance(providers, dict):
        for name, value in providers.items():
            if isinstance(value, dict):
                original = original_providers.get(name) or {}
                value["credential_configured"] = bool(
                    isinstance(original, dict) and original.get("api_key")
                )
                value["credential_suffix"] = _credential_suffix(original.get("api_key"))

    roster = safe.get("multi_providers")
    original_roster = (config or {}).get("multi_providers") or []
    if isinstance(roster, list):
        roster = _roster_entries_with_ids(roster)
        safe["multi_providers"] = roster
        for index, value in enumerate(roster):
            if isinstance(value, dict):
                original = original_roster[index] if index < len(original_roster) else {}
                value["credential_configured"] = bool(
                    isinstance(original, dict) and original.get("api_key")
                )
                value["credential_suffix"] = _credential_suffix(original.get("api_key"))
    return safe


def _roster_entries_with_ids(entries: list) -> list[dict]:
    """Attach deterministic IDs and repair alias groups for legacy rows."""
    from app.entitlements.rosters import (
        PLATFORM_DEFAULT_ROSTER_ID, normalize_alias_groups, stable_entry_id,
    )

    result = []
    duplicate_counts = {}
    for raw in entries or []:
        if not isinstance(raw, dict):
            continue
        item = dict(raw)
        # Keep generated IDs stable for legacy rows as credential metadata is
        # added during normalization. Explicit IDs distinguish intentionally
        # duplicated models that use separate credential profiles.
        identity = "|".join(str(item.get(key) or "").strip() for key in ("provider", "base_url", "model"))
        duplicate_index = duplicate_counts.get(identity, 0)
        duplicate_counts[identity] = duplicate_index + 1
        try:
            item["entry_id"] = stable_entry_id(
                PLATFORM_DEFAULT_ROSTER_ID,
                item,
                duplicate_index=duplicate_index,
            )
        except ValueError:
            item["entry_id"] = str(item.get("entry_id") or "")
        result.append(item)
    return normalize_alias_groups(result)


def _agent_roster_entries_with_ids(entries: list) -> list[dict]:
    """Return normalized agent-owned rows with unique opaque entry IDs.

    Agent-owned entries are independent roster records. Missing or duplicate
    IDs receive fresh UUIDs; provider/base URL/model are never used as identity.
    """
    import uuid
    from app.entitlements.rosters import normalize_alias_groups

    result = []
    seen = set()
    for raw in entries or []:
        if not isinstance(raw, dict):
            continue
        item = dict(raw)
        entry_id = str(item.get("entry_id") or "").strip()
        if not entry_id or entry_id in seen:
            entry_id = str(uuid.uuid4())
        item["entry_id"] = entry_id
        seen.add(entry_id)
        result.append(item)
    return normalize_alias_groups(result)


def _prune_roster_entries(roster: list, removed: list[dict]) -> list[dict]:
    """Drop agent rows that reference deleted global roster entry IDs."""
    removed_ids = {
        str(entry.get("entry_id") or "").strip()
        for entry in removed if isinstance(entry, dict) and entry.get("entry_id")
    }
    return [
        entry for entry in (roster or [])
        if not (
            isinstance(entry, dict)
            and str(entry.get("entry_id") or "").strip() in removed_ids
        )
    ]


async def user_has_own_llm_config(user_id: str) -> bool:
    """True when the user has their OWN provider config (their own key), as
    opposed to the admin fallback. Used by billing to decide whether a run is
    billed to the platform (inherited models → credits) or to the user's own
    key (free). Cheap existence check — no vault decryption.
    """
    try:
        elem = await _load_llm_element(user_id)
        if not elem:
            return False
        cfg = elem.get("config") or {}
        if isinstance(cfg, str):
            try:
                cfg = json.loads(cfg or "{}")
            except Exception:
                cfg = {}
        if isinstance(cfg, dict) and (cfg.get("api_key") or cfg.get("secret_ref")):
            return True
        return bool(elem.get("secret_ref"))
    except Exception:
        return False


def _agent_llm_override(agent_rec: Optional[dict]) -> Optional[dict]:
    """Return an agent's own LLM config IF it actually carries one.

    Marker-free: the override is whatever the config CARRIES — roster rows or an
    explicit selection. The legacy ``use_default`` flag is no longer read (a stored
    row that still contains it is ignored, never required), because a saved roster
    is the agent's model authority on its own. Historical top-level provider/model
    fields are ignored: the roster's Standard role is the sole model authority.
    """
    if not agent_rec:
        return None
    meta = agent_rec.get("metadata")
    if isinstance(meta, str):
        try:
            meta = json.loads(meta)
        except Exception:
            meta = {}
    if not isinstance(meta, dict):
        return None
    cfg = meta.get("llm_config")
    if not isinstance(cfg, dict):
        return None
    if not (cfg.get("multi_providers") or cfg.get("model") or cfg.get("selection_type")):
        return None
    return _runtime_config_from_roster(cfg)


# ── Role-name normalization ──
# Migrated from legacy "text" / "text_plus" to "standard" / "premium". This
# helper accepts either form (old or new) and always returns the canonical new
# name, so stored configs from before the rename keep working.
_ROLE_ALIASES = {
    "text": "standard", "text_plus": "premium",
    "standard": "standard", "premium": "premium",
    "image_in": "image_in", "image_out": "image_out",
    "system": "system", "voice": "voice",
}
# A semantic role is an explicit entry-id assignment.  Capability flags never
# select a role: a Standard model may support high effort just like Premium.
_STANDARD_ROLES = ("standard", "premium", "image_in", "image_out", "system", "voice")
# These are legacy per-row role selectors.  They are migrated once to the
# configuration-level role_assignments map and must never be persisted again.
_ROSTER_ROLE_FIELDS = frozenset({
    "enabled", "use_for_image", "use_for_image_out", "use_for_system", "use_for_voice",
})
_LEGACY_ROLE_TO_FIELD = {
    "standard": "enabled", "premium": "high_effort_capable",
    "image_in": "use_for_image", "image_out": "use_for_image_out",
    "system": "use_for_system", "voice": "use_for_voice",
}


def _catalog_entry(entry: dict) -> dict:
    """Return a global-catalog representation with role selectors removed.

    high_effort_capable deliberately remains: it is provider capability data,
    not a Premium assignment.
    """
    return {key: value for key, value in dict(entry or {}).items()
            if key not in _ROSTER_ROLE_FIELDS}


def _role_assignments_for_config(config: dict, *, migrate_legacy: bool = False) -> tuple[dict, bool]:
    """Validate the sole semantic-role authority and optionally migrate legacy rows.

    Role values must be live roster entry IDs.  Standard and Premium may refer
    to the same row; no role is inferred once this map exists.
    """
    cfg = dict(config or {})
    roster = _agent_roster_entries_with_ids(cfg.get("multi_providers") or [])
    cfg["multi_providers"] = roster
    valid_ids = {str(row.get("entry_id") or "") for row in roster}
    raw = cfg.get("role_assignments")
    changed = False
    if raw is None and migrate_legacy:
        raw = {}
        for role, field in _LEGACY_ROLE_TO_FIELD.items():
            match = next((row for row in roster if row.get(field) is True), None)
            if match:
                raw[role] = str(match["entry_id"])
        changed = True
    if raw is None:
        raw = {}
    if not isinstance(raw, dict):
        raise ValueError("role_assignments must be an object")
    assignments = {}
    for name, entry_id in raw.items():
        role = _normalize_role(str(name))
        if role not in _STANDARD_ROLES:
            raise ValueError(f"Unknown model role: {name}")
        entry_id = str(entry_id or "").strip()
        if not entry_id:
            continue
        if entry_id not in valid_ids:
            raise ValueError(f"Role {role} references a model that is not in this roster")
        assignments[role] = entry_id
    for row in roster:
        for field in _ROSTER_ROLE_FIELDS:
            if field in row:
                row.pop(field, None)
                changed = True
    if cfg.get("role_assignments") != assignments:
        changed = True
    cfg["role_assignments"] = assignments
    return cfg, changed


# ── Roster-role prompt directives (grep ROLE-DIRECTIVE-INJECT) ─────────────
# PER-AGENT admin-authored prompt injections keyed by roster slot, stored in
# the agent's OWN metadata['llm_config']['model_directives'] (written through
# PUT /agents/{id}, which is agent-admin-gated — only the agents administrator
# can change them). Fixed role slots use their role name; EACH custom roster
# slot uses its slot_ref 'custom:<position>' (positions from _assign_slots,
# matching the footer picker's "Custom N" labels — so custom 1, custom 2, ...
# each get their own injection). The chat-brain roles (standard / premium /
# image_in / custom) ride into the system prompt when the session's model
# selection is that slot (app/agent/prompts.py); the worker roles feed their
# worker prompts (image_out → image_generation.py, system → session_titler).
# voice has no prompt-injection point today (LLM transcription is audio→text)
# — stored for future use.
MODEL_DIRECTIVE_KEYS = ("standard", "premium", "image_in", "image_out",
                        "system", "voice")


async def get_agent_model_directives(agent_id: str) -> dict:
    """Read ONE agent's roster-role prompt directives from
    agents.metadata['llm_config']['model_directives'].

    Async (DB read) — called on the prompt-build hot path and by the image /
    session-namer workers. Always returns every fixed role key plus a
    ``custom`` dict keyed by slot_ref ('custom:<position>' → text)."""
    merged = {}
    try:
        from app.db import get_db
        rec = await get_db().get_agent_by_id(agent_id)
        meta = rec.get("metadata") if rec else {}
        if isinstance(meta, str):
            try:
                meta = json.loads(meta)
            except Exception:  # noqa: BLE001
                meta = {}
        llm = meta.get("llm_config") if isinstance(meta, dict) else {}
        raw = llm.get("model_directives") if isinstance(llm, dict) else {}
        if isinstance(raw, dict):
            merged = raw
    except Exception:  # noqa: BLE001 — directives are best-effort
        merged = {}
    out = {k: str(merged.get(k) or "").strip() for k in MODEL_DIRECTIVE_KEYS}
    custom = merged.get("custom")
    out["custom"] = ({str(k): str(v or "").strip() for k, v in custom.items()}
                     if isinstance(custom, dict) else {})
    return out


def _normalize_role(name: str) -> str:
    """Canonicalize a role name (accepts legacy 'text'/'text_plus')."""
    return _ROLE_ALIASES.get(name, name)


def _assign_slots(providers: list, role_assignments: Optional[dict] = None) -> dict:
    """Build footer slots from the roster plus its LITERAL role assignments.

    A roster row is always selectable as a custom entry. Every semantic role,
    Standard included, exists only when `role_assignments` names one of those
    rows. There is no computed, capability-derived or inherited Standard: a chat
    runs automatically only on the model a user literally signed as Standard, so
    an unassigned Standard resolves to nothing and the run fails red instead.
    """
    rows = [dict(p) for p in providers if isinstance(p, dict) and p.get("model")]
    by_id = {str(p.get("entry_id") or ""): p for p in rows if p.get("entry_id")}
    raw = role_assignments if isinstance(role_assignments, dict) else {}
    roles = {
        role: dict(by_id[entry_id]) if (entry_id := str(raw.get(role) or "")) in by_id else None
        for role in _STANDARD_ROLES
    }

    # Entry IDs are the sole row identity. Identical provider/model/credential
    # values are intentionally allowed as independent saved models. The roster
    # order is also the one user-facing order: a role is metadata on its model,
    # never a separate priority row in the footer picker.
    custom = []
    slot_list = []
    seen = set()
    for index, p in enumerate(providers):
        key = str(p.get("entry_id") or f"legacy:{index}")
        if key in seen:
            continue
        seen.add(key)
        row = dict(p)
        row_roles = [
            role_name for role_name in _STANDARD_ROLES
            if str(raw.get(role_name) or "") == str(row.get("entry_id") or "")
        ]
        if not row_roles:
            custom.append(dict(row))
        # A role-bearing row retains one canonical role for new selections; the
        # complete `roles` list keeps existing non-canonical role selections
        # resolvable and supplies the compact role icons in the footer.
        slot_list.append({
            "type": "role" if row_roles else "custom",
            "role": row_roles[0] if row_roles else "",
            "roles": row_roles,
            "position": len(custom) if not row_roles else 0,
            "entry_id": row.get("entry_id", ""),
            "provider": row.get("provider", ""),
            "auth_mode": row.get("auth_mode", "api_key"),
            "credential_scope": row.get("credential_scope", ""),
            "model": row.get("model", ""),
            "display_name": row.get("display_name", ""),
            "base_url": row.get("base_url", ""),
            "api_key": row.get("api_key", ""),
            "stateful_responses": row.get("stateful_responses", "auto"),
            "routing_mode": row.get("routing_mode", ""),
        })

    return {"roles": roles, "custom": custom, "slot_list": slot_list}


def _public_model_slot(slot: dict) -> dict:
    """Return picker metadata without provider or backend model details.

    A configured display name is the sole public model label. Without one, the
    actual model identifier remains the fallback label. Runtime resolution uses
    the untouched internal slot object, never this presentation projection.
    """
    alias = str(slot.get("display_name") or "").strip()
    public = {
        "type": slot.get("type", "custom"),
        "entry_id": slot.get("entry_id", ""),
        "model": alias or str(slot.get("model") or ""),
        "display_name": alias,
        "reasoning_efforts": supported_efforts(slot.get("model", ""), slot.get("provider", "")),
    }
    if slot.get("type") == "role":
        public["role"] = slot.get("role", "")
    public["roles"] = list(slot.get("roles") or ([] if not slot.get("role") else [slot.get("role")]))
    if slot.get("type") != "role":
        public["position"] = slot.get("position", 0)
    return public


def _resolve_slot(slots: dict, selection_type: str, role: str = "",
                  custom_position: int = 0, entry_id: str = "") -> Optional[dict]:
    """Find a specific slot in the assignment and return its provider entry
    (a dict with provider/base_url/api_key/model), or None if unresolved."""
    for s in slots.get("slot_list", []):
        if (selection_type == "role" and s.get("type") == "role"
                and role in (s.get("roles") or [s.get("role")])):
            return {"provider": s.get("provider", ""), "base_url": s.get("base_url", ""),
                    "auth_mode": s.get("auth_mode", "api_key"),
                    "credential_scope": s.get("credential_scope", ""),
                    "entry_id": s.get("entry_id", ""),
                    "api_key": s.get("api_key", ""), "model": s.get("model", ""),
                    "display_name": s.get("display_name", ""),
                    "stateful_responses": s.get("stateful_responses", "auto"),
                    "routing_mode": s.get("routing_mode", "")}
        if (selection_type == "custom" and s.get("type") == "custom"
                and ((entry_id and s.get("entry_id") == entry_id)
                     or (not entry_id and s.get("position") == custom_position))):
            return {"provider": s.get("provider", ""), "base_url": s.get("base_url", ""),
                    "auth_mode": s.get("auth_mode", "api_key"),
                    "credential_scope": s.get("credential_scope", ""),
                    "api_key": s.get("api_key", ""), "model": s.get("model", ""),
                    "display_name": s.get("display_name", ""),
                    "entry_id": s.get("entry_id", ""),
                    "stateful_responses": s.get("stateful_responses", "auto"),
                    "routing_mode": s.get("routing_mode", "")}
    return None


def _slot_ref(selection_type: str, role: str = "",
              custom_position: int = 0, entry_id: str = "") -> str:
    """The stable string key for a slot — used for per-slot effort look-up.
    e.g. 'role:text', 'custom:2'."""
    if selection_type == "role":
        return f"role:{role}"
    if entry_id:
        return f"entry:{entry_id}"
    return f"custom:{custom_position}"


def _borrow_roster_credentials(roster: list, donors: list) -> list:
    """Give an adopted row the credential of the roster entry it came from.

    A row saved without its own key — the default when a model is adopted from a
    catalog — inherits the key of the SAME entry (matched by entry ID, else by
    provider/base_url/model). The match is deliberately identity-exact so a row can
    never borrow a different entry's credential.
    """
    donor_rows = [row for row in donors if isinstance(row, dict) and row.get("api_key")]
    for row in roster:
        if not isinstance(row, dict) or row.get("api_key"):
            continue
        donor = next((candidate for candidate in donor_rows
                      if _same_roster_entry(candidate, row)), None)
        if not donor:
            continue
        for key in ("entry_id", "provider", "auth_mode", "base_url", "api_key",
                    "display_name", "routing_mode", "credential_scope"):
            if not row.get(key) and donor.get(key):
                row[key] = donor[key]
    return roster


def _merge_agent_override(base: dict, override: dict) -> dict:
    """Layer an agent's (or session's) non-empty override fields over the base.

    When the override carries its own ``multi_providers`` roster it becomes the
    UNION of inherited (app-default) + own models. That roster is NOT raced —
    parallel racing was removed; it only supplies the candidate list for the chat
    model-switcher and the vision / image-out worker picks.

    NEW — slot-based resolution (replaces bare-Model-ID session pinning):
    When a session override has ``selection_type`` ('role' | 'custom'), the model
    is resolved LIVE against the current roster via ``_assign_slots`` + ``_resolve_slot``.
    If the slot no longer resolves (admin removed the model or unticked its capability),
    no model is set → the chat errors with 'No text model configured'.
    """
    merged = dict(base or {})
    mp = override.get("multi_providers")
    own = [p for p in mp if isinstance(p, dict) and p.get("model")] if isinstance(mp, list) else []
    if not isinstance(mp, list):
        for k in ("entry_id", "provider", "auth_mode", "base_url", "api_key", "model", "display_name", "stateful_responses", "routing_mode", "credential_scope"):
            if override.get(k):
                merged[k] = override[k]
    # An agent roster is an explicit adoption boundary.  App and user catalogs
    # may be queried by an add picker, but they are never merged into an agent at
    # runtime.  A session override can pin a model already present in this roster;
    # it cannot introduce a catalog model of its own.
    if isinstance(mp, list):
        seen, roster = set(), []
        def roster_key(row):
            entry_id = str(row.get("entry_id") or "")
            return ("entry", entry_id) if entry_id else (
                "legacy", row.get("provider"), row.get("model"), row.get("base_url"),
            )
        # An EMPTY own roster means "no Standard model role assigned" — the run
        # must fail red rather than silently running on an app/catalog roster.
        # The ONE exception is an EXPLICIT pick: a slot the user chose may resolve
        # against the roster it was offered from (the app-provided catalog),
        # because picking another model is the way out of a missing Standard —
        # an automatic start is not.
        source_rows = own
        if not own and str(override.get("selection_type") or ""):
            source_rows = [dict(row) for row in (merged.get("multi_providers") or [])
                           if isinstance(row, dict) and row.get("model")]
        for row in source_rows:
            key = roster_key(row)
            if key not in seen:
                seen.add(key)
                roster.append(dict(row))
        # An adopted row keeps the credential of the roster entry it came from
        # (matched by entry ID, else provider/base_url/model); it never borrows a
        # different entry's key.
        _borrow_roster_credentials(roster, merged.get("multi_providers") or [])
        merged["multi_providers"] = roster
        merged["role_assignments"] = override.get("role_assignments") or (merged.get("role_assignments") or {})
        selected = next((row for row in roster if _same_roster_entry(row, merged)), None)
        if selected:
            for key in ("entry_id", "provider", "auth_mode", "base_url", "api_key", "model", "display_name", "routing_mode", "credential_scope"):
                merged[key] = selected.get(key, "")
        # A Standard that IS assigned wins. With NO Standard assigned, an explicit
        # selection (the `selected` row above) is kept — the run may still start on
        # the model the user literally picked — while a bare legacy saved default is
        # wiped so it can never stand in for a Standard nobody appointed.
        standard = _assign_slots(roster, merged.get("role_assignments") or {})["roles"].get("standard")
        if standard:
            for key in _LEGACY_DEFAULT_FIELDS:
                merged[key] = standard.get(key, "")
        elif selected is None:
            for key in _LEGACY_DEFAULT_FIELDS:
                merged[key] = ""

    # ── Stale pinned-model guard ──
    # An agent override may pin a default model that was later REMOVED from
    # every available roster (the agent's own multi_providers, the inherited
    # app-defaults, or both). Without this guard the agent runs on a removed
    # model it can never switch away from.
    # Missing selections fail closed. Repointing to a surviving roster entry or
    # the app default hides stale configuration and can run an unintended model.
    # The empty model is surfaced as a fatal chat error by the agent loop.
    roster_entries = [p for p in merged.get("multi_providers", [])
                      if isinstance(p, dict) and p.get("model")]
    roster_entry_ids = {str(p.get("entry_id") or "") for p in roster_entries if p.get("entry_id")}
    roster_models = {p.get("model") for p in roster_entries}
    selected_entry_id = str(merged.get("entry_id") or "")
    if (selected_entry_id and selected_entry_id not in roster_entry_ids) or (
            not selected_entry_id and merged.get("model") and roster_models
            and merged.get("model") not in roster_models):
        for k in ("entry_id", "model", "display_name", "provider", "auth_mode", "base_url", "api_key", "stateful_responses", "credential_scope"):
            merged[k] = ""
    elif override.get("model") and roster_models:
        # Concrete agent-driven model switches carry only the runtime model ID.
        # Rebind presentation metadata to that roster row so an alias from the
        # previously active model cannot bleed onto the newly selected model.
        concrete = next((row for row in roster_entries if _same_roster_entry(row, merged)), None)
        if concrete is None and not selected_entry_id:
            concrete = next((row for row in roster_entries
                             if row.get("model") == merged.get("model")), None)
        merged["display_name"] = str((concrete or {}).get("display_name") or "")

    # ── Slot-based session-override resolution ──
    # A session that picked a slot (instead of pinning a bare model ID) resolves
    # LIVE against the current agent roster. If the slot no longer exists (admin
    # removed the model or unticked its required capability), the model is left
    # empty → the chat will error with 'No standard model configured'.
    sel_type = override.get("selection_type")
    if sel_type:
        union = merged.get("multi_providers") or []
        slots = _assign_slots(union, merged.get("role_assignments") or {})
        sel_role = _normalize_role(override.get("role", ""))
        sel_pos = override.get("custom_position", 0)
        sel_entry_id = str(override.get("entry_id") or "")
        resolved = _resolve_slot(slots, sel_type, sel_role, sel_pos, sel_entry_id)
        if resolved:
            for k in ("entry_id", "provider", "auth_mode", "base_url", "api_key", "model", "display_name", "stateful_responses", "routing_mode", "credential_scope"):
                if resolved.get(k):
                    merged[k] = resolved[k]
            merged["_slot_ref"] = _slot_ref(sel_type, sel_role, sel_pos, sel_entry_id)
        else:
            # Slot unresolvable — leave model blank so the chat errors.
            merged["model"] = ""
            merged["_slot_ref"] = _slot_ref(sel_type, sel_role, sel_pos, sel_entry_id)

    return merged


def _session_llm_override(cfg: Optional[dict]) -> Optional[dict]:
    """Return a session's custom LLM config IF it actually carries a selection.

    Sessions store either a concrete ``model`` OR a ``selection_type`` plus a
    semantic role / stable entry ID (legacy rows may still carry a position), set
    by the chat footer model picker. Marker-free: the selection itself is the
    signal, so the legacy ``use_default`` flag is ignored rather than required. A
    config holding neither (for example only per-slot effort state) means "this
    session has no model pick".
    """
    if not isinstance(cfg, dict):
        return None
    if not cfg.get("model") and not cfg.get("selection_type"):
        return None
    return cfg


# The reasoning-effort scale we expose in the chat footer picker and the Model
# Switcher ability. "default" = send no hint (today's behaviour). The rest map to
# the provider's normalised reasoning.effort levels; providers that don't support
# a given level (e.g. some ignore "minimal") just drop it and the call retries.
from app.reasoning_effort import EFFORT_LEVELS, EFFORT_ORDER, normalize_effort, supported_efforts

REASONING_EFFORT_LEVELS = list(EFFORT_LEVELS)


def _effort_ref(entry_id: Optional[str], legacy_slot_ref: Optional[str] = None) -> str:
    """Canonical reasoning-effort key for a visible roster row.

    Role refs remain the routing/directive identity, but effort belongs to the
    persisted roster entry the user sees and selects. Rows without stable IDs
    retain their legacy role/custom reference."""
    entry_id = str(entry_id or "").strip()
    return f"entry:{entry_id}" if entry_id else str(legacy_slot_ref or "")


def _resolve_session_effort(session_override: Optional[dict],
                              model: Optional[str] = None,
                              slot_ref: Optional[str] = None,
                              legacy_slot_ref: Optional[str] = None) -> Optional[str]:
    """The reasoning-effort level stored for the current roster entry.

    Prefer the canonical entry reference, then a legacy role/custom reference,
    then a bare model ID from pre-slot session data. ``default`` and unknown
    values intentionally resolve to no provider hint."""
    if not isinstance(session_override, dict):
        return None
    effort_map = session_override.get("model_effort")
    if not isinstance(effort_map, dict):
        return None
    for key in (slot_ref, legacy_slot_ref, model):
        if not key:
            continue
        level = (effort_map.get(key) or "").strip().lower()
        if level in REASONING_EFFORT_LEVELS and level != "default":
            return level
    return None


def _apply_effort_to_env(level: Optional[str]) -> None:
    """Stash the resolved reasoning-effort level for the loop to read, or clear it."""
    if level:
        os.environ["LLM_REASONING_EFFORT"] = level
    else:
        os.environ.pop("LLM_REASONING_EFFORT", None)


def _effective_config(
    base: dict,
    agent_rec: Optional[dict],
    session_override: Optional[dict] = None,
) -> dict:
    """User config with any per-agent override, then any per-session override,
    layered on top — resolution order app-default → agent → session."""
    override = _agent_llm_override(agent_rec)
    # Identified agents never inherit a user/app roster.  Legacy agents with no
    # stored model config are treated as an explicitly empty roster.
    if agent_rec:
        merged = _merge_agent_override(base, override if isinstance(override, dict) else {"multi_providers": []})
    else:
        merged = dict(base or {})
    sess = _session_llm_override(session_override)
    if sess:
        merged = _merge_agent_override(merged, sess)
    return merged


def _config_roster_entries(config: dict) -> list[dict]:
    """Return stable-ID entries from the sole saved model authority: the roster."""
    raw = [
        item for item in (config.get("multi_providers") or [])
        if isinstance(item, dict)
    ]
    entries = _roster_entries_with_ids(raw)
    result, seen = [], set()
    for entry in entries:
        identity = (str(entry.get("entry_id") or ""), str(entry.get("provider") or ""),
                    str(entry.get("base_url") or ""), str(entry.get("model") or ""))
        if identity not in seen:
            seen.add(identity)
            result.append(entry)
    return result


def _same_roster_entry(left: dict, right: dict) -> bool:
    left_id, right_id = str(left.get("entry_id") or ""), str(right.get("entry_id") or "")
    # An entry ID names the credential-bearing model row, not merely the
    # provider/model pair. Two users (or two agent rows) may deliberately use
    # the same upstream model through different credentials, so never fall back
    # to provider/model matching once either side has a stable entry ID.
    if left_id or right_id:
        return bool(left_id and right_id and left_id == right_id)
    return all(str(left.get(key) or "") == str(right.get(key) or "")
               for key in ("provider", "base_url", "model", "credential_scope"))


async def _clamp_config_to_entitlements(user_id: str, base: dict, effective: dict) -> tuple[dict, dict]:
    """Clamp agent/session selections to the caller's effective base roster."""
    from app.entitlements.service import resolve_capabilities

    capabilities = await resolve_capabilities(user_id)
    model_policy = capabilities.get("models") or {}
    allowed_ids = set(model_policy.get("allowed_entry_ids") or [])
    base_entries = _config_roster_entries(base)
    platform_roster_id = str(base.get("_platform_roster_id") or "")
    if platform_roster_id:
        # Capabilities resolution owns roster validity.  Empty IDs are not an
        # implicit wildcard: they mean no published entries were resolved.
        if (model_policy.get("available") is False
                or platform_roster_id != str(model_policy.get("roster_id") or "")
                or not allowed_ids):
            base_entries = []
        elif "*" not in allowed_ids:
            base_entries = [entry for entry in base_entries if entry.get("entry_id") in allowed_ids]
    else:
        if not model_policy.get("allow_byo"):
            base_entries = []
        maximum = model_policy.get("max_byo_entries")
        if maximum is not None:
            base_entries = base_entries[:max(0, int(maximum))]

    # Agent-owned BYO rows are credential-scoped records, not aliases for an
    # equally named platform row. Preserve each ID-bearing row when the caller's
    # policy permits BYO models. The API authenticated ownership when it
    # persisted the row, so this does not admit a client-made catalog entry; it
    # prevents entitlement clamping from replacing an agent's distinct
    # credential with a same-provider/model catalog credential.
    if model_policy.get("available") is not False and model_policy.get("allow_byo"):
        known_ids = {str(entry.get("entry_id") or "") for entry in base_entries}
        own_only = [
            entry for entry in _config_roster_entries(effective)
            if str(entry.get("entry_id") or "")
            and str(entry.get("entry_id") or "") not in known_ids
        ]
        maximum = model_policy.get("max_byo_entries")
        if maximum is not None:
            own_only = own_only[:max(0, int(maximum))]
        base_entries.extend(own_only)
    # Do not replace an unavailable explicit selection with an app default. The
    # picker keeps EVERY entitled entry visible — adopting one is the way out of a
    # stale selection — while the runtime selection below still resolves only when
    # the saved model is itself an entitled entry, and otherwise fails closed.
    candidates = base_entries
    selected = next(
        (entry for entry in candidates if _same_roster_entry(entry, effective)), None)
    clamped = dict(effective)
    clamped["multi_providers"] = candidates
    if selected:
        # MODEL-NAME-LEVELS: `display_name` is presentation, NOT entitlement. The
        # clamp may only restrict WHICH entries run (identity, credentials, base
        # URL) — it must never replace the caller's own name for an entry with the
        # catalog's. `effective` already resolved user → agent → session, so keep
        # that name for the same entry; a blank one still falls back to the base
        # row's (catalog) name below.
        own_display_name = str(effective.get("display_name") or "").strip()
        for key in ("entry_id", "provider", "auth_mode", "base_url", "api_key", "model", "display_name",
                    "routing_mode", "credential_scope"):
            clamped[key] = selected.get(key, "")
        if own_display_name:
            clamped["display_name"] = own_display_name
    else:
        for key in ("entry_id", "provider", "auth_mode", "base_url", "api_key", "model", "display_name",
                    "routing_mode", "credential_scope"):
            clamped[key] = ""
    clamped["_entitlement_roster_id"] = model_policy.get("roster_id")
    return clamped, capabilities


def _clamp_reasoning_effort(level: Optional[str], capabilities: dict) -> Optional[str]:
    if not level or level not in EFFORT_ORDER or level == "default":
        return None
    order = EFFORT_ORDER
    maximum = str((capabilities.get("models") or {}).get("max_reasoning_effort") or "default")
    return level if order.get(level, 0) <= order.get(maximum, 0) else (
        None if maximum == "default" else maximum
    )


async def _load_session_override(session_id: Optional[str]) -> Optional[dict]:
    """Fetch a session's stored llm_config (best-effort, None on any failure)."""
    if not session_id:
        return None
    try:
        from app.db import get_db
        return await get_db().get_session_llm_override(session_id)
    except Exception:
        return None


async def load_provider_for_user(user_id: str) -> None:
    """Load a user's provider config into env vars.
    Reads from the auth_elements DB table.
    Called at the start of each agent loop.
    """
    _apply_config_to_env(await _resolve_user_config(user_id))


async def apply_provider_for_run(
    user_id: str,
    agent_rec: Optional[dict] = None,
    session_id: Optional[str] = None,
    *,
    apply_env: bool = True,
) -> dict:
    """Apply the effective provider config for a run to env, honoring a per-agent
    LLM override (``metadata['llm_config']`` with ``use_default=False``) and then a
    per-session override (``sessions.metadata['llm_config']``) layered over the
    user's default. Returns the effective config that was applied.

    This is what makes an agent's *custom model* — or a session's picked model —
    actually take effect at runtime; without it the loop would always run on the
    user's global default.
    """
    agent_rec = await _hydrate_agent_record_llm(agent_rec)
    session_override = await _load_session_override(session_id)
    config_user_id = user_id
    dedicated_public_key = False
    if str(user_id or "").startswith("anon_") and agent_rec:
        from app.agent.public_policy import PLATFORM_SHOWCASE_AGENT_ID, require_public_funding
        if str(agent_rec.get("id") or "") != PLATFORM_SHOWCASE_AGENT_ID:
            from app.db import get_db
            funding = await require_public_funding(get_db(), agent_rec)
            config_user_id = str(funding.get("owner_user_id") or user_id)
            dedicated_public_key = funding.get("mode") == "dedicated_key"
            # Visitors cannot select models from the sponsor's roster. Public
            # traffic always runs the model configuration published by the owner.
            session_override = None
    base = await _resolve_user_config(config_user_id)
    if dedicated_public_key:
        # Use the published agent configuration as its own base. Otherwise the
        # stale-roster guard correctly sees no inherited roster but incorrectly
        # falls back to the visitor/platform model.
        base = dict(_agent_llm_override(agent_rec) or base)
    effective = _effective_config(base, agent_rec, session_override)
    effective = await _ensure_tool_capable(effective, config_user_id)
    if dedicated_public_key:
        # Publication validation requires an agent-owned provider/key override.
        # The visitor's anonymous entitlement must not erase that funded key.
        from app.entitlements.service import resolve_capabilities
        capabilities = await resolve_capabilities(config_user_id)
    else:
        effective, capabilities = await _clamp_config_to_entitlements(
            config_user_id, base, effective,
        )
    # The default loop consumes this config directly so simultaneous chats cannot
    # overwrite each other's credentials through process-global environment
    # variables.  Keep the env application for legacy/background callers.
    if apply_env:
        _apply_config_to_env(effective)
    # Per-session reasoning-effort for the resolved model (footer picker / Model
    # Switcher ability). Decoupled from the model override so an effort can apply
    # even when the chat runs on the agent's default model. The loop reads
    # LLM_REASONING_EFFORT and passes it as the provider reasoning hint.
    # Look up by slot ref (new) or model ID (legacy).
    effort = _resolve_session_effort(
        session_override,
        model=effective.get("model"),
        slot_ref=_effort_ref(effective.get("entry_id")),
        legacy_slot_ref=effective.get("_slot_ref"),
    )
    effort = normalize_effort(
        _clamp_reasoning_effort(effort, capabilities),
        effective.get("model", ""), effective.get("provider", ""),
        str((capabilities.get("models") or {}).get("max_reasoning_effort") or "default"),
    )
    # This transient value lets a caller use the fully resolved run config
    # without reading the shared environment. It is never persisted.
    effective["reasoning_effort"] = effort
    effective["_credential_user_id"] = config_user_id
    effective["_credential_agent_id"] = str((agent_rec or {}).get("id") or "")
    if apply_env:
        _apply_effort_to_env(effort)
    return effective


async def resolve_active_model(
    user_id: str,
    agent_rec: Optional[dict] = None,
    session_id: Optional[str] = None,
) -> dict:
    """Return ``{model, provider, base_url}`` for the effective model of a run,
    WITHOUT mutating env. Mirrors apply_provider_for_run's resolution so the chat
    footer shows exactly the model the loop will use. Used by /current-model-info.
    """
    agent_rec = await _hydrate_agent_record_llm(agent_rec)
    session_override = await _load_session_override(session_id)
    config_user_id = user_id
    dedicated_public_key = False
    if str(user_id or "").startswith("anon_") and agent_rec:
        from app.agent.public_policy import PLATFORM_SHOWCASE_AGENT_ID, require_public_funding
        if str(agent_rec.get("id") or "") != PLATFORM_SHOWCASE_AGENT_ID:
            from app.db import get_db
            funding = await require_public_funding(get_db(), agent_rec)
            config_user_id = str(funding.get("owner_user_id") or user_id)
            dedicated_public_key = funding.get("mode") == "dedicated_key"
            session_override = None
    base = await _resolve_user_config(config_user_id)
    if dedicated_public_key:
        base = dict(_agent_llm_override(agent_rec) or base)
    effective = _effective_config(base, agent_rec, session_override)
    effective = await _ensure_tool_capable(effective, config_user_id)
    if dedicated_public_key:
        from app.entitlements.service import resolve_capabilities
        capabilities = await resolve_capabilities(config_user_id)
    else:
        effective, capabilities = await _clamp_config_to_entitlements(
            config_user_id, base, effective,
        )
    provider = effective.get("provider", "") or ""
    base_url = effective.get("base_url") or (PROVIDER_PRESETS.get(provider, {}) or {}).get("base_url", "")
    effort = _clamp_reasoning_effort(
        _resolve_session_effort(
            session_override,
            model=effective.get("model"),
            slot_ref=_effort_ref(effective.get("entry_id")),
            legacy_slot_ref=effective.get("_slot_ref"),
        ),
        capabilities,
    )
    effort = normalize_effort(
        effort, effective.get("model", ""), provider,
        str((capabilities.get("models") or {}).get("max_reasoning_effort") or "default"),
    )
    return {"model": effective.get("model", "") or "", "provider": provider,
            "base_url": base_url, "display_name": effective.get("display_name", "") or "",
            "entry_id": effective.get("entry_id", "") or "",
            "credential_scope": effective.get("credential_scope", "") or "",
            "reasoning_effort": effort or "default"}


async def resolve_agent_models(
    user_id: str,
    agent_rec: Optional[dict] = None,
    session_id: Optional[str] = None,
) -> dict:
    """Return the candidate models a chat user can switch between for this agent —
    organised as semantic slots (standard / premium / image_in / image_out) followed
    by custom positions. Powers the chat footer model switcher.

    Returns:
      {"slots": [{"type":"role","role":"standard","model":"gpt-4o",...}, ...],
       "active_slot": {"type":"role","role":"standard"} | None,
       "model_effort": {"role:standard":"high", "custom:2":"low"},
       "agent_override": {"model": "<id>"} | None}
    """
    agent_rec = await _hydrate_agent_record_llm(agent_rec)
    session_override = await _load_session_override(session_id)
    base = await _resolve_user_config(user_id)
    effective = _effective_config(base, agent_rec, session_override)
    effective, capabilities = await _clamp_config_to_entitlements(user_id, base, effective)

    # The footer is an agent roster switcher, not the user's Available models
    # catalog. Runtime may still inherit app defaults for execution, but an
    # identified agent exposes only models explicitly persisted in that agent's
    # own roster. The agent Model page remains the place to adopt user-catalog
    # candidates into this roster.
    union = effective.get("multi_providers") or []
    role_assignments = effective.get("role_assignments") or {}
    if agent_rec and agent_rec.get("id"):
        metadata = agent_rec.get("metadata") or {}
        if isinstance(metadata, str):
            try:
                metadata = json.loads(metadata or "{}")
            except Exception:
                metadata = {}
        agent_llm = metadata.get("llm_config") if isinstance(metadata, dict) else {}
        own_roster = (agent_llm or {}).get("multi_providers") or []
        role_assignments = (agent_llm or {}).get("role_assignments") or {}
        if not own_roster:
            # An agent with no rows of its own is not "deprived": it draws its
            # roster from the app-provided catalog (the entitlement-clamped
            # effective roster) — the same source a Standard model is adopted
            # from. Showing nothing would strand the footer with no way to fix a
            # missing Standard model, which is exactly the state the user needs
            # to see and act on.
            own_roster = [
                row for row in (union or [])
                if isinstance(row, dict) and row.get("model")
            ]
            role_assignments = effective.get("role_assignments") or role_assignments

        def _roster_key(row: dict) -> tuple:
            entry_id = str(row.get("entry_id") or "")
            return ("entry", entry_id) if entry_id else (
                "legacy", str(row.get("provider") or ""),
                str(row.get("model") or ""), str(row.get("base_url") or ""),
            )

        effective_by_key = {
            _roster_key(row): row for row in union if isinstance(row, dict)
        }
        # Entitlement-clamped effective rows are preferred when present, but a
        # local agent roster is the footer authority. Do not discard a saved
        # local row merely because it is absent from the unrelated user/base
        # effective roster after inheritance has been retired.
        union = [
            effective_by_key.get(_roster_key(row), row)
            for row in own_roster if isinstance(row, dict) and row.get("model")
        ]
    slots = _assign_slots(union, role_assignments)

    # A footer is a roster selector, not merely a role selector.  Keep semantic
    # role slots for role-aware actions, then append every remaining persisted
    # roster entry once so ordinary local rows (for example a model with no
    # assigned role or custom position) are still selectable.
    emitted_entry_ids = {
        str(slot.get("entry_id") or "")
        for slot in slots["slot_list"]
        if isinstance(slot, dict) and slot.get("entry_id")
    }
    next_custom_position = max(
        [int(slot.get("position") or 0) for slot in slots["slot_list"]
         if isinstance(slot, dict) and slot.get("type") == "custom"] or [0]
    )
    for row in union:
        if not isinstance(row, dict):
            continue
        entry_id = str(row.get("entry_id") or "")
        if entry_id and entry_id in emitted_entry_ids:
            continue
        next_custom_position += 1
        slots["slot_list"].append({
            "type": "custom",
            "position": next_custom_position,
            "entry_id": entry_id,
            "model": row.get("model", ""),
            "display_name": row.get("display_name") or row.get("model", ""),
        })
        if entry_id:
            emitted_entry_ids.add(entry_id)

    # The active slot: either the session's explicit selection or the standard role.
    active_slot = None
    if session_override and session_override.get("selection_type"):
        role = _normalize_role(session_override.get("role", ""))
        active_slot = {
            "type": session_override["selection_type"],
            "role": role,
            "custom_position": session_override.get("custom_position", 0),
            "entry_id": session_override.get("entry_id", ""),
        }
    elif effective.get("model"):
        # A new chat always runs on the literal Standard assignment, never on
        # roster order. The picker preserves that order for presentation, so
        # its first row can be a different model (for example V4.1 Flash).
        standard = slots["roles"].get("standard")
        standard_id = str((standard or {}).get("entry_id") or "")
        selected = next(
            (
                slot for slot in slots["slot_list"]
                if (standard_id and str(slot.get("entry_id") or "") == standard_id)
                or (not standard_id and slot.get("role") == "standard")
            ),
            None,
        )
        if selected:
            active_slot = {
                "type": selected["type"],
                "role": "standard",
                "custom_position": selected.get("position", 0),
                "entry_id": selected.get("entry_id", ""),
            }

    # Every visible picker row is identified by its stable entry ID. Preserve a
    # legacy role selection for routing, but attach the matching row identity so
    # it remains highlighted in the flat roster.
    if active_slot and not active_slot.get("entry_id") and active_slot.get("role"):
        active_role = active_slot["role"]
        matching_slot = next((slot for slot in slots["slot_list"]
                              if active_role in (slot.get("roles") or [])), None)
        if matching_slot:
            active_slot["entry_id"] = matching_slot.get("entry_id", "")

    # Routing may remain role-based, but effort is owned by the visible roster
    # entry. Expose that canonical identity so callers never have to infer it.
    if active_slot:
        active_slot["effort_ref"] = _effort_ref(
            active_slot.get("entry_id"),
            _slot_ref(active_slot.get("type", ""), active_slot.get("role", ""),
                      active_slot.get("custom_position", 0), active_slot.get("entry_id", "")),
        )

    # Agent-driven concrete-model override (the Model Switcher ability's
    # set_model / use_premium_model writes {use_default: False, model: <id>}).
    # The user's footer-picker slot selection never carries a `model` key, so a
    # concrete model here is always the AGENT's temporary upgrade. Unlike the
    # slot selection, it's invisible to the slot resolver — expose it so the
    # chat footer can show the overridden model in warning color while it's
    # live, and revert automatically when the run ends (runner._finish_run
    # clears the override and the UI re-syncs). (grep SESSION-MODEL-OVERRIDE)
    agent_override = None
    if isinstance(session_override, dict):
        # A concrete `model` here is always the AGENT's temporary upgrade — the
        # user's footer-picker slot selection carries no `model` key. Marker-free:
        # the legacy `use_default` flag is no longer required to recognise it.
        _ao_model = session_override.get("model")
        if _ao_model:
            override_slot = next(
                (slot for slot in slots["slot_list"] if slot.get("model") == _ao_model),
                None,
            )
            agent_override = {
                "model": str((override_slot or {}).get("display_name")
                             or (override_slot or {}).get("model")
                             or "Configured model"),
            }

    # Per-slot reasoning-effort the chat has chosen.
    effort_map = (session_override or {}).get("model_effort")
    effort_map = effort_map if isinstance(effort_map, dict) else {}
    effort_map = {
        key: (_clamp_reasoning_effort(str(value).lower(), capabilities) or "default")
        for key, value in effort_map.items()
    }
    # Read old role/custom keys for the active row, but present that value under
    # its canonical entry key. This is a read-only compatibility projection;
    # the next slider save writes only the entry-owned key.
    if active_slot and active_slot.get("effort_ref"):
        resolved_effort = _resolve_session_effort(
            session_override,
            slot_ref=active_slot["effort_ref"],
            legacy_slot_ref=_slot_ref(
                active_slot.get("type", ""), active_slot.get("role", ""),
                active_slot.get("custom_position", 0), active_slot.get("entry_id", ""),
            ),
        )
        if resolved_effort:
            effort_map[active_slot["effort_ref"]] = (
                _clamp_reasoning_effort(resolved_effort, capabilities) or "default"
            )
    safe_slots = [_public_model_slot(slot) for slot in slots["slot_list"]]
    maximum = str((capabilities.get("models") or {}).get("max_reasoning_effort") or "default")
    for internal, slot in zip(slots["slot_list"], safe_slots):
        slot["reasoning_efforts"] = [
            level for level in slot["reasoning_efforts"]
            if EFFORT_ORDER[level] <= EFFORT_ORDER.get(maximum, 0)
        ]
        ref = _effort_ref(internal.get("entry_id"), _slot_ref(
            internal.get("type", ""), internal.get("role", ""),
            internal.get("position", 0), internal.get("entry_id", ""),
        ))
        stored = _resolve_session_effort(
            session_override, model=internal.get("model"), slot_ref=ref,
            legacy_slot_ref=_slot_ref(internal.get("type", ""), internal.get("role", ""),
                                      internal.get("position", 0)),
        )
        if stored:
            effort_map[ref] = normalize_effort(
                _clamp_reasoning_effort(stored, capabilities),
                internal.get("model", ""), internal.get("provider", ""), maximum,
            ) or "default"
    return {"slots": safe_slots, "active_slot": active_slot,
            "model_effort": effort_map, "agent_override": agent_override}


def _slot_for_model(caps: dict, model: str) -> Optional[tuple]:
    """(role, slot_ref) for a model id, via the ordered slot_list — semantic
    roles win over custom positions (mirrors _assign_slots' ordering). None
    when the model is not in the roster at all."""
    d = caps.get("default") or {}
    union = ([d] if d.get("model") else []) + list(caps.get("racers") or [])
    slots = _assign_slots(union)
    for s in slots["slot_list"]:
        if s.get("model") == model:
            if s["type"] == "role":
                return s["role"], _slot_ref("role", s["role"])
            return "custom", _slot_ref(
                "custom", "", s.get("position", 0), s.get("entry_id", "")
            )
    return None


async def session_active_slot(
    user_id: str,
    agent_rec: Optional[dict] = None,
    session_id: Optional[str] = None,
) -> Optional[dict]:
    """Return {"role": ..., "slot_ref": ...} for the session's ACTIVE roster
    slot — the one whose per-agent admin directive rides into the system
    prompt (grep ROLE-DIRECTIVE-INJECT). Mirrors resolve_agent_models' slot
    resolution (SISTER-SYNC: SESSION-MODEL-OVERRIDE):

      • a role-slot selection (footer picker / mode selector) → that role
        (standard | premium | image_in | image_out) with 'role:<role>';
      • a custom-slot selection → role 'custom' with slot_ref
        'custom:<position>' (the footer picker's "Custom N");
      • a concrete-model override (the Model Switcher's set_model /
        use_premium_model) → the model's first-matching slot;
      • no override → the DEFAULT slot (slot_list[0] — exactly what the
        footer picker treats as active).

    Returns None when nothing resolves (no roster) — no directive rides in.
    Model NAMES are never parsed; only the slot classification decides."""
    if not session_id:
        return None
    try:
        session_override = await _load_session_override(session_id)
        caps = await load_llm_capabilities_for_user(user_id, agent_rec=agent_rec)
    except Exception:  # noqa: BLE001 — slot resolution is best-effort
        return None
    session_override = session_override if isinstance(session_override, dict) else {}

    if session_override.get("selection_type") == "role":
        role = _normalize_role(str(session_override.get("role", "")))
        if role in _STANDARD_ROLES:
            return {"role": role, "slot_ref": _slot_ref("role", role)}
        return None
    if session_override.get("selection_type") == "custom":
        pos = int(session_override.get("custom_position") or 0)
        entry_id = str(session_override.get("entry_id") or "")
        return {"role": "custom", "slot_ref": _slot_ref("custom", "", pos, entry_id)}

    model = str(session_override.get("model") or "")
    if model:
        found = _slot_for_model(caps, model)
        if not found:
            return None
        role, slot_ref = found
        return {"role": role, "slot_ref": slot_ref}

    slots = _assign_slots(caps.get("racers") or [])
    first = slots["slot_list"][0] if slots["slot_list"] else None
    if not first:
        return None
    if first["type"] == "role":
        return {"role": first["role"], "slot_ref": _slot_ref("role", first["role"])}
        return {"role": "custom",
            "slot_ref": _slot_ref("custom", "", first.get("position", 0),
                                    first.get("entry_id", ""))}


def _apply_config_to_env(config: dict) -> None:
    """Apply provider config dict to environment variables — the single resolved
    model the run will use (provider/base_url/api_key/model).
    """
    provider = config.get("provider", "")
    base_url = config.get("base_url")
    if not base_url and provider in PROVIDER_PRESETS:
        base_url = PROVIDER_PRESETS[provider]["base_url"]

    if provider:
        os.environ["LLM_PROVIDER"] = provider
        os.environ["OPENROUTER_PROVIDER"] = provider
    if config.get("api_key"):
        os.environ["LLM_API_KEY"] = config["api_key"]
        os.environ["OPENROUTER_API_KEY"] = config["api_key"]
    else:
        os.environ.pop("LLM_API_KEY", None)
        os.environ.pop("OPENROUTER_API_KEY", None)
    if base_url:
        os.environ["LLM_BASE_URL"] = base_url
        os.environ["OPENROUTER_BASE_URL"] = base_url
    else:
        os.environ.pop("LLM_BASE_URL", None)
        os.environ.pop("OPENROUTER_BASE_URL", None)
    if config.get("model"):
        _model = config["model"]
        # OpenRouter uses "provider/model" format; native provider APIs (DeepSeek,
        # Anthropic, etc.) expect bare model names. Strip the prefix when the
        # base_url is NOT openrouter.ai.
        if base_url and "openrouter.ai" not in base_url and "/" in _model:
            _stripped = _model.split("/", 1)[-1]
            logger.debug("stripped provider prefix from model '%s' → '%s' (native API)", _model, _stripped)
            _model = _stripped
        os.environ["LLM_MODEL"] = _model
        os.environ["OPENROUTER_MODEL"] = _model
    else:
        os.environ.pop("LLM_MODEL", None)
        os.environ.pop("OPENROUTER_MODEL", None)

    # Parallel model racing was removed — clear any stale race-engine env so an
    # old process value can never re-trigger the deleted code path.
    os.environ.pop("PARALLEL_MODE", None)
    os.environ.pop("MULTI_PROVIDERS", None)


_SUBSCRIPTION_CAPABILITY_FIELDS = (
    "text_capable", "image_capable", "image_out_capable", "voice_capable",
)


def _is_subscription_model_entry(entry: dict) -> bool:
    """Whether a roster row uses ChatGPT OAuth rather than an API key."""
    entry = entry or {}
    return (
        entry.get("provider") == "openai_subscription"
        or entry.get("auth_mode") == "chatgpt"
        or entry.get("base_url") == "codex://chatgpt"
    )


def _normalize_subscription_model_capabilities(entry: dict) -> dict:
    """Reconcile legacy subscription rows with the authoritative live roster.

    v1 rows can retain missing/false modality flags after the v2 migration.  The
    ChatGPT subscription catalog is first-party metadata, so for a model it knows
    it wins over those persisted summaries. Unknown model ids are left untouched.
    """
    row = dict(entry or {})
    if not _is_subscription_model_entry(row) or not row.get("model"):
        return row
    try:
        from app import openai_subscription
        known = next(
            (item for item in openai_subscription.models()
             if item.get("id") == row.get("model")),
            None,
        )
    except Exception:
        known = None
    if not known:
        return row
    row.setdefault("provider", "openai_subscription")
    row.setdefault("auth_mode", "chatgpt")
    row.setdefault("base_url", "codex://chatgpt")
    for key in _SUBSCRIPTION_CAPABILITY_FIELDS:
        if key in known:
            row[key] = bool(known[key])
    return row


async def load_llm_capabilities_for_user(user_id: str, agent_rec: Optional[dict] = None) -> dict:
    """Read the user's LLM config (own → admin) and return media-capability
    info WITHOUT touching env vars.

    Used by chat.py at attachment-resolution time to decide whether an attached
    image must be described by a separate vision model before the turn runs. This
    is a per-request DB read on purpose — the env vars set by
    ``_apply_config_to_env`` are process-global and shared across users.

    When ``agent_rec`` is provided, the agent's ``metadata.llm_config`` override
    (per-agent model) is layered in so the ``default`` model reflects what THIS
    agent actually runs on — not just the global app default. (grep AGENT-AWARE-CAPS)

    Shape:
      {
        "default": {model, provider, base_url, api_key, text_capable,
                    image_capable, image_out_capable, use_for_image_out,
                    high_effort_capable},
        "racers":  [{model, provider, base_url, api_key, enabled,
                     text_capable, image_capable, use_for_image,
                     image_out_capable, use_for_image_out,
                     high_effort_capable}, ...],
      }

    ("racers" is the saved-model roster — kept for the worker picks below and the
    chat model-switcher list; the models are NOT raced, parallel racing was
    removed. Exactly one model — "default" — is the brain per run.)
    """
    agent_rec = await _hydrate_agent_record_llm(agent_rec)
    base = await _resolve_user_config(user_id)
    cfg = base

    # Factor in the agent's LLM override so caps reflect what THIS agent runs on.
    # Mirrors the runtime resolution (apply_provider_for_run) so list_models and
    # the switcher tools agree with the model the loop actually uses. (grep AGENT-AWARE-CAPS)
    if agent_rec:
        cfg = _effective_config(cfg, agent_rec)
    cfg, _capabilities = await _clamp_config_to_entitlements(user_id, base, cfg)
    cfg = _normalize_subscription_model_capabilities(cfg)

    default = {
        "entry_id": cfg.get("entry_id", ""),
        "model": cfg.get("model", ""),
        "display_name": cfg.get("display_name", ""),
        "provider": cfg.get("provider", ""),
        "auth_mode": cfg.get("auth_mode", "api_key"),
        "credential_scope": cfg.get("credential_scope", ""),
        "base_url": cfg.get("base_url", ""),
        "api_key": cfg.get("api_key", ""),
        "text_capable": bool(cfg.get("text_capable", True)),
        "image_capable": bool(cfg.get("image_capable", False)),
        "image_out_capable": bool(cfg.get("image_out_capable", False)),
        "voice_capable": bool(cfg.get("voice_capable", False)),
        "use_for_image_out": bool(cfg.get("use_for_image_out", False)),
        "high_effort_capable": bool(cfg.get("high_effort_capable", False)),
    }
    racers = []
    for raw in (cfg.get("multi_providers") or []):
        p = _normalize_subscription_model_capabilities(raw)
        racers.append({
            "entry_id": p.get("entry_id", ""),
            "model": p.get("model", ""),
            "display_name": p.get("display_name", ""),
            "provider": p.get("provider", "custom"),
            "auth_mode": p.get("auth_mode", "api_key"),
            "credential_scope": p.get("credential_scope", ""),
            "base_url": p.get("base_url", ""),
            "api_key": p.get("api_key", ""),
            "enabled": bool(p.get("enabled", False)),
            "text_capable": bool(p.get("text_capable", True)),
            "image_capable": bool(p.get("image_capable", False)),
            "use_for_image": bool(p.get("use_for_image", False)),
            "image_out_capable": bool(p.get("image_out_capable", False)),
            "use_for_image_out": bool(p.get("use_for_image_out", False)),
            "voice_capable": bool(p.get("voice_capable", False)),
            "use_for_voice": bool(p.get("use_for_voice", False)),
            "use_for_system": bool(p.get("use_for_system", False)),
            "high_effort_capable": bool(p.get("high_effort_capable", False)),
        })

    # Reconcile the top-level "default" modality flags with the matching saved row.
    # The top-level fields are a legacy summary that drifts (e.g. they can read
    # image_capable=True left over from an image model even though the active text
    # model's own row correctly says False). The per-model row in multi_providers is
    # the source of truth, so when the default model id matches a row, take that
    # row's modality flags. Prevents a stale summary from routing an image to a
    # blind model. (grep RECONCILE-DEFAULT-MODALITY)
    if default.get("model"):
        match = next((r for r in racers if r.get("model") == default["model"]), None)
        if match:
            for k in ("text_capable", "image_capable",
                      "image_out_capable", "voice_capable",
                      "use_for_image_out", "use_for_voice",
                      "high_effort_capable"):
                if k in match:
                    default[k] = match[k]

    return {
        "default": default,
        "racers": racers,
        "role_assignments": dict(cfg.get("role_assignments") or {}),
        # Internal, non-secret tenant identity used when a worker row authenticates
        # through ChatGPT OAuth instead of carrying an API key.
        "credential_context": {
            "user_id": user_id,
            "agent_id": str((agent_rec or {}).get("id") or ""),
        },
    }


def model_sees_images(entry: dict) -> bool:
    """Can this configured model accept image INPUT? Combines the saved flag with
    the model-catalog guard: a catalog "no image input" overrides a stale True;
    catalog unknown/True trusts the configured flag. Mirrors the _ok() guard in
    pick_vision_model so every caller agrees on what 'can see' means."""
    if not (entry and entry.get("image_capable") and entry.get("model")):
        return False
    return _catalog_modality(entry["model"], entry.get("provider", ""), "in") is not False


def model_makes_images(entry: dict) -> bool:
    """Can this configured model produce image OUTPUT? Catalog-guarded, like
    model_sees_images but for the output modality."""
    if not (entry and entry.get("image_out_capable") and entry.get("model")):
        return False
    return _catalog_modality(entry["model"], entry.get("provider", ""), "out") is not False


def _assigned_capability_entry(caps: dict, role: str) -> Optional[dict]:
    """Return the roster entry explicitly assigned to a semantic role."""
    wanted = str((caps or {}).get("role_assignments", {}).get(role) or "")
    if not wanted:
        return None
    for row in [(caps or {}).get("default") or {}] + list((caps or {}).get("racers") or []):
        if str(row.get("entry_id") or "") == wanted:
            return row
    return None


def _is_tool_capable(entry: dict) -> bool:
    """True unless the catalog DEFINITELY says the model can't call tools. Unknown
    or True both pass (we don't second-guess) — mirrors _ensure_tool_capable."""
    model = (entry or {}).get("model", "")
    if not model:
        return False
    try:
        from app import model_catalog
        meta = model_catalog.lookup(model, provider_hint=entry.get("provider", ""))
    except Exception:
        meta = None
    if not meta:
        return True
    return meta.get("tool_call") is not False


def turn_models_image_capable(caps: dict) -> bool:
    """Can the single model that handles this turn (the default / active model)
    see images? Catalog-guarded: a stale image_capable=True on a text-only model
    can no longer fool the describe step into skipping (the original bug). (grep
    RECONCILE-DEFAULT-MODALITY)
    """
    return model_sees_images(caps.get("default") or {})


def _has_worker_credentials(entry: dict) -> bool:
    """Whether a bounded worker can authenticate this roster entry."""
    entry = entry or {}
    if _is_subscription_model_entry(entry):
        return bool(entry.get("entry_id") and entry.get("credential_scope"))
    return bool(entry.get("base_url") and entry.get("api_key"))


def _worker_model_config(entry: dict, caps: Optional[dict] = None) -> dict:
    """Project a capability row into the shared bounded-completion config."""
    entry = entry or {}
    context = (caps or {}).get("credential_context") or {}
    return {
        "entry_id": entry.get("entry_id", ""),
        "model": entry.get("model", ""),
        "provider": entry.get("provider", ""),
        "auth_mode": entry.get("auth_mode", "api_key"),
        "credential_scope": entry.get("credential_scope", ""),
        "base_url": entry.get("base_url", ""),
        "api_key": entry.get("api_key", ""),
        "user_id": context.get("user_id", ""),
        "agent_id": context.get("agent_id", ""),
    }


def pick_describer(caps: dict) -> Optional[dict]:
    """Choose an authenticated image describer for a text-only turn model."""
    d = caps.get("default") or {}
    if model_sees_images(d) and _has_worker_credentials(d):
        return _worker_model_config(d, caps)
    for r in (caps.get("racers") or []):
        if r.get("use_for_image") and model_sees_images(r) and _has_worker_credentials(r):
            return _worker_model_config(r, caps)
    return None


def _infer_api_shape(base_url: str, provider: str = "") -> str:
    """Infer the image-generation API style from a model's base URL.

    Mirrors IMAGE_PROVIDER_PRESETS in plugins/abilities/Core/image_generation.py:
      api.stability.ai                    → stability
      generativelanguage.googleapis.com   → gemini
      openrouter.ai                       → openrouter (chat-completions image out;
                                            OpenRouter has NO /images/generations)
      everything else                     → openai-compatible /images/generations
    """
    b = (base_url or "").lower()
    if "stability.ai" in b:
        return "stability"
    if "generativelanguage.google" in b:
        return "gemini"
    if "openrouter.ai" in b or (provider or "").lower() == "openrouter":
        return "openrouter"
    return "openai"


def _catalog_modality(model: str, provider: str, direction: str) -> Optional[bool]:
    """Best-effort: does the model catalog say ``model`` supports image in/out?
    direction ∈ {"in","out"}. Returns True / False / None (unknown). Sync lookup
    against the already-loaded catalog cache — never raises, never blocks on a
    network fetch (a cold cache just yields None, and the caller trusts the flag).
    """
    try:
        from app import model_catalog
        meta = model_catalog.lookup(model, provider_hint=provider)
    except Exception:
        meta = None
    if not meta:
        return None
    key = "input_modalities" if direction == "in" else "output_modalities"
    mods = meta.get(key)
    if not isinstance(mods, list):
        return None
    return "image" in [str(m).lower() for m in mods]


def pick_image_generator(caps: dict) -> Optional[dict]:
    """Choose the model that powers the ``generate_image`` tool.

    Prefer the default model when it is flagged ``use_for_image_out`` (and
    capable), else the first saved row flagged ``use_for_image_out``. A row need
    NOT be enabled for text — a user may save an image-only model used solely for
    generation. The model catalog is used as a guard: a candidate the catalog says
    canNOT output images is skipped even if mis-ticked, so a stale checkbox can't
    route generation to a text-only model. Returns
    {model, provider, base_url, api_key, api_shape} or None.
    """
    def _ok(e: dict) -> bool:
        if not (e.get("use_for_image_out") and e.get("image_out_capable")
                and e.get("model") and e.get("api_key")):
            return False
        # Catalog veto: False = definitely can't; None/True = trust the flag.
        return _catalog_modality(e["model"], e.get("provider", ""), "out") is not False

    e = _assigned_capability_entry(caps, "image_out")
    if _ok(e or {}):
        return {"model": e["model"], "provider": e.get("provider", ""),
                "base_url": e.get("base_url", ""), "api_key": e.get("api_key", ""),
                # MODEL-ROW-USAGE: keep the row identity so the image charge is
                # attributed to the exact saved connection, not just model+provider.
                "entry_id": e.get("entry_id", ""),
                "api_shape": _infer_api_shape(e.get("base_url", ""), e.get("provider", ""))}
    return None


def media_routing(caps: dict) -> dict:
    """The single capability-routing picture, computed once and reused by the
    attachment type-router, the image_vision tools, and their guidance messages.

    Encodes the "enforce a decision" rule: if the turn model can't see/make images
    and there is NO valid model to switch to, the only path is delegating to a
    one-shot worker (``must_delegate_*``). A *switch target* must be configured
    text + image (catalog-guarded) AND tool-capable — so an image-in/out model with
    text turned off is excluded (it can only be a worker, never the brain).

    Returns::

        {
          "sees_natively":  bool,           # the turn model can read images
          "makes_natively": bool,           # the turn model can output images
          "describer":      dict | None,    # one-shot vision worker (read)
          "generator":      dict | None,    # one-shot image-out worker (make)
          "vision_switch_targets": [str],   # enabled text+image+tools models
          "gen_switch_targets":    [str],   # enabled text+image-out+tools models
          "must_delegate_vision":  bool,    # can't see & nothing to switch to
          "must_delegate_gen":     bool,    # can't make & nothing to switch to
        }
    """
    caps = caps or {}
    sees = turn_models_image_capable(caps)
    default = caps.get("default") or {}
    makes = model_makes_images(default)

    describer = pick_describer(caps) or pick_vision_model(caps)
    generator = pick_image_generator(caps)

    enabled = [r for r in (caps.get("racers") or []) if r.get("enabled")]
    # The default model is always a candidate brain.
    brains = [default] + enabled
    vision_switch = sorted({
        b["model"] for b in brains
        if b.get("model") and b.get("text_capable")
        and model_sees_images(b) and _is_tool_capable(b)
    })
    gen_switch = sorted({
        b["model"] for b in brains
        if b.get("model") and b.get("text_capable")
        and model_makes_images(b) and _is_tool_capable(b)
    })
    return {
        "sees_natively": sees,
        "makes_natively": makes,
        "describer": describer,
        "generator": generator,
        "vision_switch_targets": vision_switch,
        "gen_switch_targets": gen_switch,
        "must_delegate_vision": (not sees) and (not vision_switch),
        "must_delegate_gen": (not makes) and (not gen_switch),
    }


def high_effort_targets(caps: dict) -> list:
    """The models flagged "high-effort" (the *Eff* tick) that can actually be the
    agent's brain (text + tool-capable). These are the models the Model Control
    ability may upgrade ONTO for a hard task.

    A premium model is a deliberate NON-default upgrade target, so the roster's
    ``enabled`` flag does NOT gate it — the common per-agent configuration (and
    the default template) marks the Eff model ``enabled: false,
    high_effort_capable: true`` ("premium-only": not an everyday brain, but the
    tier the agent upgrades onto). Filtering on ``enabled`` first (the old
    behaviour) silently hid exactly that model. Catalog-guarded via
    ``_is_tool_capable`` so a mis-ticked tool-less model is excluded. Returns a
    sorted list of model ids.
    """
    caps = caps or {}
    premium = _assigned_capability_entry(caps, "premium") or {}
    return [premium["model"]] if (
        premium.get("model") and premium.get("text_capable")
        and _is_tool_capable(premium)
    ) else []


def is_high_effort_model(caps: dict, model: str) -> bool:
    """True when ``model`` is one of the configured high-effort brain models."""
    if not model:
        return False
    return model in high_effort_targets(caps)


def pick_vision_model(caps: dict) -> Optional[dict]:
    """Choose the authenticated model used by the ``process_image`` worker.

    API-backed rows require their API key; ChatGPT-subscription rows instead carry
    the entry/scope identity needed by ``SubscriptionResponseClient``. The catalog
    still vetoes a row that is definitively text-only.
    """
    def _ok(entry: dict) -> bool:
        return model_sees_images(entry) and _has_worker_credentials(entry)

    default = caps.get("default") or {}
    if _ok(default):
        return _worker_model_config(default, caps)
    # Prefer rows the admin explicitly ticked for image input, then any capable row.
    racers = list(caps.get("racers") or [])
    for entry in racers:
        if _ok(entry) and entry.get("use_for_image"):
            return _worker_model_config(entry, caps)
    for entry in racers:
        if _ok(entry):
            return _worker_model_config(entry, caps)
    return None


async def _ensure_tool_capable(effective: dict, user_id: str) -> dict:
    """Safety net for the MAIN turn: if the resolved model definitively cannot call
    tools (catalog ``tool_call`` is False — e.g. an image-only model someone set as
    the brain), swap to a tool-capable enabled text model so the agent loop doesn't
    hard-fail with OpenRouter's "No endpoints found that support tool use". Only
    acts when we KNOW the model can't do tools; unknown/True is left untouched.

    Image generation and vision are handled by their own pickers/workers — this
    only guards the conversational model the loop attaches tools to.
    """
    model = effective.get("model", "")
    if not model:
        return effective
    try:
        from app import model_catalog
        await model_catalog.ensure_fresh()
        meta = model_catalog.lookup(model, provider_hint=effective.get("provider", ""))
    except Exception:
        meta = None
    if not meta or meta.get("tool_call") is not False:
        return effective  # capable, or unknown — don't second-guess

    try:
        caps = await load_llm_capabilities_for_user(user_id)
    except Exception:
        return effective
    candidates = [caps.get("default") or {}]
    candidates += [r for r in (caps.get("racers") or [])
                   if r.get("enabled") and r.get("text_capable") and r.get("model")]
    for c in candidates:
        cm = c.get("model", "")
        if not cm or cm == model:
            continue
        try:
            cmeta = model_catalog.lookup(cm, provider_hint=c.get("provider", ""))
        except Exception:
            cmeta = None
        if cmeta and cmeta.get("tool_call") is False:
            continue  # also tool-less — keep looking
        merged = dict(effective)
        merged["model"] = cm
        for k in ("provider", "base_url", "api_key"):
            if c.get(k):
                merged[k] = c[k]
        logger.info(
            "tool-use fallback: %s cannot call tools; running this turn on %s", model, cm
        )
        return merged
    logger.warning(
        "tool-use fallback: %s cannot call tools and no tool-capable text model is "
        "enabled — the turn may fail. Tick TEXT on a tool-capable model in App Config.",
        model,
    )
    return effective


def _load_app_settings() -> dict:
    try:
        if APP_SETTINGS_FILE.exists():
            with open(APP_SETTINGS_FILE) as f:
                return json.load(f)
    except Exception as e:
        logger.warning("Failed to load app-settings.json: %s", e)
    return {}


def _save_app_settings(data: dict) -> None:
    try:
        clean = dict(data or {})
        # Removed in favour of a signed, document-local Render Recorder lease.
        # Strip the legacy global switch so old TUI/API clients cannot revive it.
        clean.pop("render_recording_enabled", None)
        safe_write_json(APP_SETTINGS_FILE, clean)
    except Exception as e:
        logger.warning("Failed to save app-settings.json: %s", e)


def shared_default_agent_enabled() -> bool:
    """Read the shared-default-agent toggle from app-settings.json, defaulting True."""
    return _load_app_settings().get("shared_default_agent_enabled", True) is True


def get_browser_session_policy() -> dict:
    """Validated runtime policy for server-owned Playwright browsers."""
    raw = _load_app_settings()
    try:
        maximum = int(raw.get("browser_max_concurrent_sessions", 3))
    except (TypeError, ValueError):
        maximum = 3
    try:
        timeout = int(raw.get("browser_idle_timeout_seconds", 300))
    except (TypeError, ValueError):
        timeout = 300
    return {
        "max_concurrent_sessions": max(1, min(20, maximum)),
        "idle_timeout_seconds": max(60, min(86400, timeout)),
        "idle_cleanup_enabled": raw.get("browser_idle_cleanup_enabled", True) is not False,
    }


def _is_metadata_enabled() -> bool:
    return METADATA_FLAG.exists()


# ── Pydantic models ────────────────────────────────────────────────────────

class ProviderConfig(BaseModel):
    provider: str
    auth_mode: str = "api_key"  # api_key | chatgpt
    base_url: str = ""
    api_key: str
    model: str = ""
    display_name: str = Field(default="", max_length=120)
    stateful_responses: str = "auto"  # auto | disabled
    providers: dict = {}
    # Media-capability of the default model (detected on save, user-overridable).
    text_capable: bool = True
    image_capable: bool = False
    image_out_capable: bool = False   # can generate images
    voice_capable: bool = False       # accepts audio/voice input
    use_for_image_out: bool = False   # this model is the image generator
    high_effort_capable: bool = False  # admin-marked "premium" tier the agent may upgrade ONTO
    credential_configured: bool = False  # read-only; API keys are never returned


class MultiProviderEntry(BaseModel):
    # Immutable model identity. New rows may omit it; the server assigns a
    # random UUID. Existing IDs must belong to the roster being updated.
    entry_id: str = ""
    # Stable-ID anchor shared by interchangeable rows. The server normalizes
    # every component after assigning IDs and dissolves single-member groups.
    alias_group_id: str = ""
    provider: str
    auth_mode: str = "api_key"  # api_key | chatgpt
    base_url: str = ""
    api_key: str = ""
    model: str = ""
    display_name: str = Field(default="", max_length=120)
    stateful_responses: str = "auto"  # auto | disabled
    # A provider-owned router. Currently supports OpenRouter's live free pool.
    routing_mode: str = ""
    # Media/provider capabilities (detected on save, user-overridable). Semantic
    # model roles are stored only in llm_config.role_assignments.
    text_capable: bool = True
    image_capable: bool = False
    use_for_image: bool = False   # eligible to describe images for text-only models
    image_out_capable: bool = False  # can generate images
    use_for_image_out: bool = False  # the image generator for the generate_image tool
    voice_capable: bool = False      # accepts audio/voice input
    use_for_voice: bool = False      # eligible for LLM-powered voice input
    use_for_system: bool = False     # eligible for app misc. LLM tasks
    high_effort_capable: bool = False  # admin-marked "premium" tier the agent may upgrade ONTO
    credential_configured: bool = False  # read-only; ignored when persisting
    credential_scope: str = ""


class MultiProvidersRequest(BaseModel):
    providers: list[MultiProviderEntry] = []


class MetadataSetting(BaseModel):
    enabled: bool


class AppSettings(BaseModel):
    # Staged context-cache rollout. Defaults preserve existing prompt assembly;
    # admins may enable individual changes for a cohort without a deploy or migration.
    context_cache_rollout: dict = Field(default_factory=lambda: {
        "external_plan_context": False,
        "telemetry": True,
    })
    extend_llm_to_agents: bool = True
    access_mode: str = "admin_approval"  # admin_approval | public_registered
    # Compact hamburger/sidebar navigation is the default on mobile viewports.
    mobile_mode: bool = True
    # Human-facing site/app name used in the document title and public SEO
    # identity. Kept short because it is rendered into browser chrome and
    # metadata.
    site_name: str = "WebAgent"
    # ── User BYOD (per-user bring-your-own-database) ──
    # OFF (default): single-tenant — every user shares the one admin-configured
    # database, exactly as before (self-hosters and existing installs are
    # unaffected). ON: each user's INTERACTION data (chats, memories, their own
    # LLM keys/vault) is routed to that user's OWN database, while the central
    # admin database keeps the shared parts (accounts, the agent catalog,
    # billing). Read on the DB hot path via get_user_byod_enabled() (cached).
    # See app/db/router.py (TenantRouterBackend) and app/db/tenant.py.
    user_byod: bool = False
    # ── Memory embeddings (app/agent/embed.py) ──
    # Which embedder powers memory save + search, chosen APP-WIDE — never
    # per-agent: every stored vector shares one column of one width, and a search
    # only works if the query was embedded by the SAME model as the stored
    # memories, so all agents must embed identically.
    #   embedding_source — "local" (DEFAULT; in-process FastEmbed model, no
    #                      per-call network hop and no embedding API cost) or
    #                      "cloud" (same provider as the chat model).
    #   embedding_model  — the LOCAL model id when source=local (blank → engine
    #                      default bge-small-en-v1.5, 384-dim). Ignored for cloud.
    #   embedding_dim    — the active vector width; recorded by the reindex so the
    #                      search path + pgvector column agree. Changing source or
    #                      model requires POST /admin/settings/embedding/reindex to
    #                      re-embed existing memories at the new width — until then
    #                      old-width vectors are ignored and search is keyword-only.
    embedding_source: str = "local"
    embedding_model: str = "BAAI/bge-small-en-v1.5"
    embedding_dim: int = 384
    # ── Startup: welcome landing page (ui/splash/splash-page) ──
    # Master on/off for the welcome landing page, app-wide. When True, app/main.py
    # serves the crawlable landing page at the front door (/) to new visitors and
    # routes the app to /app; when False, / serves the app directly and the
    # per-device "show welcome screen" preference is moot. Served to every visitor
    # via the public /api/v1/auth/ui-config endpoint.
    splash_enabled: bool = False
    # ── Appearance: allow per-user theme overrides ──
    # When True, every signed-in user gets a "My appearance" editor on their
    # account page (ui/shared/js/account.js) that writes a sparse theme override
    # to their profile (user_profiles.appearance). /api/v1/auth/ui-config layers
    # that override on top of the global appearance for the signed-in caller, so
    # anything they don't customise still follows the global theme. When False,
    # the editor is hidden and any saved per-user overrides are ignored. Served
    # via the public /api/v1/auth/ui-config endpoint so the account page can gate
    # its editor on it.
    allow_user_appearance: bool = False
    # ── Session completion notifications (sliding panel) ──
    # Master on/off for the sliding notification panel that appears when a
    # background session finishes (ui/chat/js/session-notification.js). Served
    # via the public /api/v1/auth/ui-config endpoint so the panel can gate on it
    # at boot. Defaults True — the panel has always been available.
    session_completion_notifications: bool = True
    # ── Run-contract gates: APP-LEVEL configuration (plugins/gates/) ──
    # Per-gate app-wide configuration, keyed by gate id, then by that gate's own
    # param names from its descriptor — the tailored rows the UI renders:
    #   {"closing_message": {"flags": "needs_input, complete, …",
    #                        "notification": "…", "format": "…"}}
    # There is no generic guidance blob: the app-wide layer IS these params, and
    # they sit UNDER (before) an agent's own values. Read at the completion path
    # by app/agent/closing_message.py (flags win the taxonomy; notification and
    # format APPEND to the descriptor floor). Edited on App Settings → "Agent
    # tools & abilities" → Gates. Every gate in the gates directory is listed
    # there and fixed ON for now — the catalog is loaded at process start, so a
    # gate type cannot yet be turned off. Stored raw so a key from a gate that is
    # later removed survives untouched.
    gate_settings: dict = Field(default_factory=dict)
    # ── Always-on display (Screen Wake Lock) ──
    # Master on/off for keeping the device screen awake while the app tab is
    # visible, app-wide — the same operation as the chat wake_lock control
    # (ui/shared/js/screen-wake.js). Served via the public /api/v1/auth/ui-config
    # endpoint so every visitor's browser can apply it at boot. Defaults False —
    # the screen sleeps normally.
    always_on_display: bool = False
    # ── Voice dictation: optional server/LLM transcription ──
    # OFF keeps browser-native SpeechRecognition only. ON allows recorded audio
    # to be sent to the configured provider. browser_then_llm prefers the
    # browser and falls back to LLM where unavailable; llm_only records and uses
    # the configured provider for every dictation.
    voice_dictation_llm_enabled: bool = True
    voice_dictation_mode: str = "browser_then_llm"
    # ── Mobile: hide header when on-screen keyboard appears ──
    # Master on/off for hiding the main app header (#main-header, #chat-header,
    # #chat-sub-header) when the mobile on-screen keyboard is open and the chat
    # input has focus. Defaults False (no hiding). Only active at narrow viewports
    # (≤800px). Served via /api/v1/auth/ui-config so chat-ui.js can gate on it.
    hide_header_on_keyboard: bool = False
    # ── Mobile: hide-header viewport height threshold ──
    # When hide_header_on_keyboard is ON, this controls when the header hides:
    # 0 = always hide when keyboard is open and input focused. Any positive value
    # = only hide when the remaining viewport height (visualViewport.height, i.e.
    # the space left after the keyboard) is below this many px. Default 200px.
    # Served via /api/v1/auth/ui-config so the viewport tracker can gate on it.
    hide_header_kb_threshold: int = 200
    # Seconds to keep a completed turn's in-memory RunBuffer around for
    # WS-replay on reconnect. 0 = drop immediately. Default 60s gives a
    # smooth refresh-after-completion UX without holding RAM long.
    stream_buffer_retention_seconds: int = 60
    # ── Transcript ordering fallback (chat UI resequencing policy) ──
    # App-wide recovery for timestamp-skewed sessions. TRUE = if sequence
    # drift exceeds the threshold, mark the session so transcript rendering
    # and sorting prefer created_at. FALSE = keep strict session_seq
    # ordering and ignore timestamp drift for UI order.
    transcript_timestamp_order_recovery_enabled: bool = True
    # Drift threshold in seconds before recovery mode activates. A small
    # non-network-safe jitter under this value keeps sequence-first
    # ordering.
    transcript_timestamp_order_recovery_threshold_seconds: int = 60
    # ── Session concurrency cap (app/agent/session_gate.py) ──
    # Maximum number of SESSIONS that may be actively running a turn at the
    # same time, app-wide. 0 = unlimited (the default — exactly the historical
    # behaviour). When the cap is reached, sessions that try to start a run
    # wait in a FIFO queue and begin only after an active session completes.
    max_active_sessions: int = 0
    # ── Default Max Output Tokens (agents) ──
    # The output-token ceiling used ONLY for an agent that has never set its own
    # Max Tokens (Runtime → Limits). A set agent value is authoritative and goes
    # to the provider unchanged — no clamp against the context window, no env
    # override. This is also the value a newly created agent is seeded with.
    # Edited on App Settings → App Functions. Read on the loop's hot path via
    # get_default_max_tokens() (app/agent/loop.py → _global_max_output_tokens).
    default_max_tokens: int = 8096
    # ── Browser-session lifecycle (app/tools/browser.py) ──
    # Server-owned headless Chromium instances are capped app-wide. Inactive
    # sessions are persisted and closed after the timeout; a later action opens
    # the same saved browser-session row again with its cookies and last URL.
    browser_max_concurrent_sessions: int = 3
    browser_idle_timeout_seconds: int = 300
    browser_idle_cleanup_enabled: bool = True
    # ── Anonymous session rate limits (app/api/rate_limit.py) ──
    # Maximum new anonymous sessions per client IP within the window below.
    # 0 disables the check entirely. Env var WEBAGENT_ANON_SESSION_MAX overrides.
    anon_session_max: int = 10
    # Rolling window in seconds for the anonymous-session rate limit above.
    # Env var WEBAGENT_ANON_SESSION_WINDOW overrides.
    anon_session_window: int = 60
    # App-wide circuit breaker for anonymous identity minting, independent of
    # client IP/network. This is the origin-side backstop for distributed bots.
    anon_global_session_max: int = 25
    anon_global_session_window: int = 3600
    # Public account-creation backstops. Human verification at the edge is
    # still recommended; these protect the origin if an edge rule is absent.
    public_registration_ip_max: int = 5
    public_registration_global_max: int = 100
    public_registration_window: int = 3600
    # Maximum DISTINCT browser identities associated with one privacy-safe
    # source fingerprint during the window. Unlike anon_session_max, ordinary
    # token refreshes for the same browser do not consume another identity.
    anon_identity_max: int = 5
    # Several browsers may have distinct guest identities on one network. A
    # returning browser keeps the same identity and does not consume another
    # slot; their anonymous spend still shares the network allowance below.
    anon_identity_window: int = 86400
    # ── Anonymous chat message rate limits (app/api/rate_limit.py) ──
    # Maximum messages per anonymous identity within the window below. Env var
    # WEBAGENT_ANON_CHAT_MAX overrides. 0 disables the per-identity check.
    anon_chat_max: int = 5
    # Rolling window in seconds for the per-identity message limit above.
    # Env var WEBAGENT_ANON_CHAT_WINDOW overrides.
    anon_chat_window: int = 300
    # Daily allowance for one anonymous identity. Reaching it moves the visitor
    # to registration instead of allowing unlimited low-rate usage forever.
    anon_daily_chat_max: int = 10
    anon_daily_chat_window: int = 86400
    # Maximum messages across ALL anonymous identities from one IP within the
    # window below. Catches a single IP cycling through minted tokens. Env var
    # WEBAGENT_ANON_CHAT_IP_MAX overrides. 0 disables the per-IP check.
    anon_chat_ip_max: int = 10
    # Rolling window in seconds for the per-IP message limit above.
    # Env var WEBAGENT_ANON_CHAT_IP_WINDOW overrides.
    anon_chat_ip_window: int = 300
    # App-wide anonymous chat admission circuit breaker. This durable aggregate
    # applies across unrelated source networks before model work starts.
    anon_global_chat_max: int = 50
    anon_global_chat_window: int = 300
    # Operator kill switch and hard app-wide anonymous-use budget. The budget
    # counts admitted anonymous chat turns before model work starts, so it caps
    # exposure even when attackers arrive from unrelated networks. Registered
    # traffic is never included. 0 disables the budget, not anonymous chat.
    anonymous_chat_enabled: bool = True
    anon_budget_max: int = 100
    anon_budget_window: int = 86400
    # Native anonymous spend controls. Token budgets are conservative
    # admission estimates (prompt characters / 4 plus the reserved output);
    # actual provider usage is recorded after each model call for reporting.
    anon_estimated_output_tokens: int = 4096
    anon_estimated_cost_per_1k_microusd: int = 10000
    anon_token_user_max: int = 100000
    anon_token_source_max: int = 100000
    anon_token_global_max: int = 1000000
    anon_cost_user_microusd_max: int = 250000
    # One lifetime $0.25 grant shared by every anonymous identity on the coarse
    # network source. Unlike the other spend controls, this ledger never resets.
    anon_cost_source_microusd_max: int = 250000
    anon_cost_global_microusd_max: int = 2500000
    anon_spend_window: int = 86400
    anon_max_concurrent_runs: int = 2
    anon_run_lease_seconds: int = 900
    # Correlated high-risk traffic progresses from a small delay to a temporary
    # registration-required cooldown. Values are deliberately coarse to avoid
    # treating browser/network signals as proof of identity.
    anon_risk_delay_score: int = 2
    anon_risk_cooldown_score: int = 4
    anon_risk_delay_ms: int = 500
    anon_risk_cooldown_seconds: int = 900
    anon_auto_close_enabled: bool = True
    anon_auto_close_seconds: int = 900
    anon_error_max: int = 10
    anon_error_window: int = 300
    # ── Self-healing / auto-resume (app/agent/runner.py + app/agent/watchdog.py) ──
    # The liveness watchdog re-ignites runs that stopped involuntarily (server
    # restart, vanished task, frozen await) from durable history, fully backend-
    # driven (no user WebSocket). These tune detection + retry behavior.
    run_watchdog_enabled: bool = True              # master on/off for the watchdog
    run_watchdog_poll_seconds: int = 15            # how often the watchdog sweeps active runs
    # live task + no liveness heartbeat for this long ⇒ frozen. The loop beats at
    # each turn boundary, during tool execution, and via a background timer during
    # LLM streaming — so a healthy run beats every ~5s. The per-chunk stall guard
    # (AGENT_STREAM_STALL_SECONDS, default 45s) catches silent streams first; this
    # threshold is the backstop for cases where the stall guard is disabled or the
    # event loop is truly blocked.
    run_frozen_threshold_seconds: int = 120
    # live task with fresh heartbeat but no new interactions for this long ⇒ frozen
    # (catches alterna-engine hangs where the subprocess stays alive but silent)
    run_no_progress_threshold_seconds: int = 900
    run_zombie_grace_seconds: int = 45             # row 'running' + no live task this long ⇒ zombie
    # Per-run resume budget before giving up (failed). This is BOTH the default
    # for runs with no override and the CEILING for the per-agent override
    # (metadata['resume_max_attempts'], set on Agents → Runtime → Limits): the
    # runner clamps a per-agent value to this. Edited on App Settings → App
    # Functions ("Auto-resume recovery") and the TUI Self-Healing page.
    run_max_resume_attempts: int = 6
    run_resume_backoff_seconds: int = 30           # base for exponential resume backoff
    # Per-ERROR-TYPE resume budget: how many times a SINGLE involuntary stop cause
    # (server_restart / zombie / frozen / crash / empty_response) may consume
    # before the run is retired, even while total attempts remain. A run stops as
    # soon as EITHER this per-type cap OR run_max_resume_attempts is exhausted.
    # This is BOTH the default and the CEILING for the per-agent override
    # (metadata['resume_max_attempts_per_cause'], Agents → Runtime → Limits).
    # 0 disables the per-type cap (only the total budget applies).
    run_max_resume_attempts_per_cause: int = 3
    # Closing-message REQUIREMENT-COMPLIANCE budget. NOT error recovery: the
    # closing gate re-injects its instruction while the run's final message does
    # not yet meet the closing contract (flag in the taxonomy, notification above
    # 0 and within the word limit, non-empty message) and nothing has actually
    # failed. Both values are BOTH the default and the CEILING for the per-agent
    # override (metadata['closing_max_attempts'] / ['closing_max_attempts_per_requirement'],
    # Agents → Runtime → Limits) — closing_message.effective_max_attempts clamps
    # to them. 0 disables the per-requirement cap (only the overall applies).
    run_max_closing_attempts: int = 8
    run_max_closing_attempts_per_requirement: int = 3
    # Assertions REQUIREMENT-COMPLIANCE budget. NOT error recovery: the assertions
    # gate re-injects its resolution instruction while an assertion the run
    # DECLARED is still unanswered (app/agent/assertions.py) and nothing has
    # actually failed. This is BOTH the default and the CEILING for the per-agent
    # override (metadata['assertions_max_attempts'], Agents → Runtime → Limits) —
    # assertions.effective_max_attempts clamps to it. Separate from the
    # closing-message budgets above: a different requirement, counter and
    # lifecycle (the declaration beat, not the end-of-run message).
    run_max_assertions_attempts: int = 4
    # User feedback → GitHub issues via the WebAgent relay. Cloners can flip
    # `feedback_enabled` off to hide the form, or point `feedback_relay_url`
    # at their own relay deployment.
    feedback_enabled: bool = True
    feedback_relay_url: str = ""  # empty → use built-in default
    turnstile_site_key: str = ""  # public Cloudflare Turnstile site key
    # ── Agent-loop tunables ──
    # Maximum number of tool calls per turn. 0 = unlimited. Capped at launch by
    # each provider's max_tokens and the wall-clock cap (AGENT_MAX_WALL_SECONDS).
    max_tool_calls: int = 25
    # Maximum wall-clock seconds for a single agent turn (backstop against
    # tool-looping; the streaming chat path has no request timeout of its own).
    # 0 = unlimited. Overridable per-agent via the agent's max_wall_seconds field.
    max_wall_seconds: int = 600
    # Maximum consecutive identical tool calls before the loop halts (stall guard).
    # 0 = disabled. Overridable per-agent via max_identical_tool_calls.
    max_identical_tool_calls: int = 0
    # Render Recorder capture configuration lives in its App Function descriptor.
    # Live capture state is intentionally document-local and is not an app setting.
    # ── App-global system prompt (app/agent/prompts.py) ──
    # A single admin-only block of CRITICAL, non-negotiable instructions injected
    # as the TOPMOST section of EVERY agent's system prompt (fleet agents and
    # spawned clones alike). This is the platform baseline — the bare minimum
    # every agent must obey regardless of its own prompt. Clones are built "from
    # scratch" on top of this (global baseline + their custom directive only), so
    # this is the only inherited identity a fire-and-forget clone carries. Keep it
    # short and rule-like; it is prepended verbatim, ahead of any agent slot.
    global_system_prompt: str = ""
    # ── Appearance: animated background (app/ui_backgrounds + ui/background/) ──
    # Which animated background renders, chosen separately per theme. Values are
    # background ids from the catalog (GET /admin/settings/backgrounds) or the
    # built-in "none" (plain themed background). Applied app-wide for every
    # visitor through the public /api/v1/auth/ui-config endpoint, so the admin's
    # pick is what everyone sees. Dark defaults to the classic starfield; light
    # to the mouse-reactive bullet grid.
    background_dark: str = "stargaze"
    background_light: str = "bullet-grid"
    # ── Appearance: global border + typography (ui/shared/js/appearance.js) ──
    # The single edit-here surface for the app's neutral border and fonts.
    # Served app-wide through the public /api/v1/auth/ui-config endpoint and
    # injected at boot as CSS-variable overrides on top of design-system.css, so
    # editing these values (then reloading) recolours / resizes every container,
    # table and divider border and swaps the UI font for every visitor — no
    # server restart, no CSS edit.
    #   border_color_*  — the neutral border hue, separate per theme (the dark
    #                     and light palettes use different border colours).
    #   border_width    — line weight shared by every neutral border; "0" turns
    #                     them ALL off (accent/status/focus borders are untouched).
    #   font_sans/_mono — the CSS font-family stacks for body text / monospace.
    # A blank value falls back to the design-system.css default for that token.
    border_color_dark: str = "#333333"
    border_color_light: str = "#cccccc"
    border_width: str = "1px"
    font_sans: str = "'Inter', -apple-system, BlinkMacSystemFont, 'Segoe UI', Roboto, sans-serif"
    font_mono: str = "'JetBrains Mono', 'Fira Code', Menlo, ui-monospace, monospace"
    # ── Appearance: full theme palette (ui/shared/js/appearance.js) ──────────
    # The raw palette hues + neutral surfaces + text shades, chosen separately
    # per theme, mirroring the two palette blocks in design-system.css (:root =
    # dark, body.light-mode = light). Each is a #rrggbb value; appearance.js
    # injects it as the matching CSS variable under that theme's selector and
    # also derives the `-rgb` triple (so every rgba(var(--x-rgb), a) tint
    # follows). A blank value falls back to the design-system.css default.
    # The Appearance panel (App Settings) edits these as per-theme swatches +
    # one-click presets; they can also be hand-edited here. Keys come in
    # _dark / _light pairs:
    #   accent        → --brand (+ --brand-strong, --brand-rgb)
    #   secondary     → --purple (+ --purple-strong, --purple-rgb)
    #   success/warning/danger → the status hues (+ their -rgb)
    #   surface_bg    → --bg-0   (the page base surface)
    #   surface_panel → --bg-elev (cards / panels)
    #   surface_tint  → --bg-tint (tinted insets)
    #   text          → --fg-1   (primary text)
    #   text_muted    → --fg-3   (muted text)
    #   ambient       → --ambient-2-rgb (the page-background glow, stored as hex)
    accent_dark: str = "#ffffff"
    accent_light: str = "#000000"
    secondary_dark: str = "#d9d9d9"
    secondary_light: str = "#262626"
    success_dark: str = "#22c55e"
    success_light: str = "#16a34a"
    warning_dark: str = "#eab308"
    warning_light: str = "#ca8a04"
    danger_dark: str = "#ef4444"
    danger_light: str = "#dc2626"
    surface_bg_dark: str = "#000000"
    surface_bg_light: str = "#ffffff"
    surface_panel_dark: str = "#0d0d0d"
    surface_panel_light: str = "#f5f5f5"
    surface_tint_dark: str = "#1a1a1a"
    surface_tint_light: str = "#e8e8e8"
    text_dark: str = "#ffffff"
    text_light: str = "#000000"
    text_muted_dark: str = "#a3a3a3"
    text_muted_light: str = "#666666"
    ambient_dark: str = "#404040"
    ambient_light: str = "#d9d9d9"
    # Chat bubbles — optional per-theme overrides for the user / agent message
    # bubble fill. Blank = follow the theme-derived default in design-system.css.
    # Stored as #rrggbb or #rrggbbaa (trailing aa = opacity; 00 = transparent).
    user_bubble_dark: str = ""
    user_bubble_light: str = ""
    user_bubble_border_dark: str = ""
    user_bubble_border_light: str = ""
    agent_bubble_dark: str = ""
    agent_bubble_light: str = ""
    agent_bubble_border_dark: str = ""
    agent_bubble_border_light: str = ""
    # Chat composer pill fill and border — same shape as the bubbles. Blank =
    # follow their theme-derived defaults.
    chat_pill_bg_dark: str = ""
    chat_pill_bg_light: str = ""
    chat_pill_border_dark: str = ""
    chat_pill_border_light: str = ""
    # ── Appearance: theme-independent style knobs (JSON-only; no UI control) ──
    # Edit these in app-settings.json (or via a POST) and reload — ui/shared/js/
    # appearance.js injects them app-wide. Each default is "no change" so they're
    # inert until edited. Stored as strings (the whole appearance pipeline is
    # string-valued). Shared across both themes.
    #   radius_scale    — corner roundness multiplier (0 = square … 2 = round)
    #   shadow_strength — drop-shadow depth multiplier (0 = flat … 2 = heavy)
    #   ui_scale        — overall UI zoom (1 = normal; e.g. 1.1 = 110%)
    #   reduce_motion   — "on" collapses animations/transitions (accessibility)
    #   cursor_glow     — "off" hides the pointer glow (ui/shared/js/cursor-effects.js)
    # ── TALL chat-composer (#chat-input-row) geometry + button knobs. Each is a
    #    CSS length injected app-wide by appearance.js as the matching
    #    --chat-pill-* variable (see app1.css CHAT-PILL-VARS); the compact 1-line
    #    bars keep their own fixed sizing.
    #   chat_pill_max_width   — composer width cap (e.g. "500px")
    #   chat_pill_radius      — corner roundness
    #   chat_pill_padding     — outer padding (CSS shorthand)
    #   chat_pill_min_height  — resting text-area height
    #   chat_pill_max_height  — grow cap before scrolling
    #   chat_pill_font_size   — input + placeholder text size
    #   chat_pill_attach_size — attach (+) button box
    #   chat_pill_attach_icon — attach (+) glyph
    #   chat_pill_button_size — mic / send button box
    #   chat_pill_button_icon — mic / send glyph
    radius_scale: str = "1"
    shadow_strength: str = "1"
    ui_scale: str = "1"
    reduce_motion: str = "off"
    cursor_glow: str = "on"
    # ── RAM speed layer (ui/shared/js/media-cache.js) — app-wide client knob ──
    #   media_cache_enabled   — "on" (default) | "off": the in-memory
    #                           attachment/thumbnail cache in the browser.
    #   media_cache_budget_mb — memory ceiling (MB) for cached bytes/thumbnails;
    #                           it LRU-evicts above this, so it can't exceed it.
    media_cache_enabled: str = "on"
    media_cache_budget_mb: str = "128"
    chat_pill_max_width: str = "500px"
    chat_pill_radius: str = "20px"
    chat_pill_padding: str = "4px 4px 0px 4px"
    chat_pill_min_height: str = "96px"
    chat_pill_max_height: str = "160px"
    chat_pill_font_size: str = "14px"
    chat_pill_attach_size: str = "38px"
    chat_pill_attach_icon: str = "24px"
    chat_pill_button_size: str = "62px"
    chat_pill_button_icon: str = "44px"
    # ── Chat panel layout (app-wide defaults; ui/shared/js/appearance.js) ──
    #   chat_position         — which side the chat side-panel sits on the desktop
    #                           split: "right" (default) or "left". Injected as a
    #                           #stage flex-direction rule; the resize handle
    #                           detects the side automatically.
    #   chat_default_visible  — whether the chat panel is shown for a FIRST-TIME
    #                           visitor on desktop: "visible" (default) or
    #                           "hidden". Once a visitor toggles the chat their own
    #                           per-browser choice wins; this only seeds the default.
    #   chat_panel_width      — desktop chat-panel default as a positive CSS px or
    #                           % length. A browser's dragged pixel width still wins.
    chat_position: str = "right"
    chat_default_visible: str = "visible"
    chat_panel_width: str = "380px"
    # ── Chat widget (global floating launcher + embed) config ──
    # Admin-configured corners, agent, prompt, and detection toggles for the
    # floating chat widget. Served via /api/v1/auth/ui-config and consumed by
    # webagent-launcher.js. Default {} — the widget uses chat_ui.json defaults
    # for corners and fall-back agent resolution.
    chat_widget: dict = {}
    # User-saved custom themes — a JSON array string of
    # [{id, name, dark:{<token>:val,…}, light:{…}}], managed by the Appearance
    # panel's "Add theme" button. Admin-only editor; not served via ui-config
    # (visitors only need the resolved palette, not the preset list).
    custom_themes: str = "[]"
    # ── Shared default agent ──
    # When True (DEFAULT), every user shares the SAME default WebAgent
    # (id="shared_default", owned by the app admin) instead of getting a per-user
    # clone. Users see it in their roster as read-only; only the app admin can
    # edit prompts/skills/tools. When False, no default is provisioned and a new
    # user starts with an empty roster.
    shared_default_agent_enabled: bool = True


# The two canonical access levels surfaced in User Management:
#   admin_approval   — Private: register, then wait for an admin to approve
#   public_registered — Open Registration: anyone joins, must sign in
VALID_ACCESS_MODES = {"admin_approval", "public_registered"}

# Legacy stored values are migrated on read/write so old configs keep working:
#   open             → public_registered (the no-sign-in auto-admin mode was
#                       retired; that convenience is now covered by "Remember me".
#                       Dormant `== "open"` branches survive but never match.)
#   public_anonymous → public_registered (the old anonymous-chat mode)
#   private          → admin_approval (fully-disabled registration was removed)
_LEGACY_ACCESS_MODES = {
    "open": "public_registered",
    "public_anonymous": "public_registered",
    "private": "admin_approval",
}


def normalize_access_mode(raw: str | None) -> str:
    """Map any stored/posted access_mode to one of the two canonical modes.

    Unknown or empty values fall back to the default (admin_approval), so a
    corrupted setting can never leave the app in an undefined access state.
    """
    val = (raw or "").strip()
    val = _LEGACY_ACCESS_MODES.get(val, val)
    return val if val in VALID_ACCESS_MODES else "admin_approval"

_DEFAULT_BACKGROUNDS = {"dark": "stargaze", "light": "bullet-grid"}


def get_background_config() -> dict:
    """Read the per-theme background choice, validated against what's installed.

    Returns ``{"dark": <id>, "light": <id>}``. A stored id whose folder was
    deleted falls back to the default (or "none" if even that is gone), so a
    removed background never leaves the page pointing at a missing plugin.
    """
    data = _load_app_settings()
    try:
        from app.ui_backgrounds import valid_ids
        allowed = valid_ids() | {"none"}
    except Exception:  # noqa: BLE001
        allowed = None

    def pick(key: str, default: str) -> str:
        val = data.get(key) or default
        if allowed is not None and val not in allowed:
            val = default if default in allowed else "none"
        return val

    return {
        "dark": pick("background_dark", _DEFAULT_BACKGROUNDS["dark"]),
        "light": pick("background_light", _DEFAULT_BACKGROUNDS["light"]),
    }


# Defaults mirror the design-system.css palette (dark :root + body.light-mode).
# These are the values shipped in design-system.css; the appearance config
# overrides them app-wide when the admin edits app-settings.json.
_DEFAULT_APPEARANCE = {
    "border_color_dark": "#333333",
    "border_color_light": "#cccccc",
    "border_width": "1px",
    "font_sans": "'Inter', -apple-system, BlinkMacSystemFont, 'Segoe UI', Roboto, sans-serif",
    "font_mono": "'JetBrains Mono', 'Fira Code', Menlo, ui-monospace, monospace",
    # Full theme palette (per theme). Mirror of the design-system.css palette
    # blocks; kept in sync with the AppSettings fields + ui/shared/js/appearance.js
    # DEFAULTS. get_appearance_config() iterates this dict, so adding a key here
    # automatically carries it through the public /api/v1/auth/ui-config endpoint.
    "accent_dark": "#ffffff",
    "accent_light": "#000000",
    "secondary_dark": "#d9d9d9",
    "secondary_light": "#262626",
    "success_dark": "#22c55e",
    "success_light": "#16a34a",
    "warning_dark": "#eab308",
    "warning_light": "#ca8a04",
    "danger_dark": "#ef4444",
    "danger_light": "#dc2626",
    "surface_bg_dark": "#000000",
    "surface_bg_light": "#ffffff",
    "surface_panel_dark": "#0d0d0d",
    "surface_panel_light": "#f5f5f5",
    "surface_tint_dark": "#1a1a1a",
    "surface_tint_light": "#e8e8e8",
    "text_dark": "#ffffff",
    "text_light": "#000000",
    "text_muted_dark": "#a3a3a3",
    "text_muted_light": "#666666",
    "ambient_dark": "#404040",
    "ambient_light": "#d9d9d9",
    # Chat bubble overrides (blank = follow the theme default). #rrggbb[aa].
    "user_bubble_dark": "",
    "user_bubble_light": "",
    "user_bubble_border_dark": "",
    "user_bubble_border_light": "",
    "agent_bubble_dark": "",
    "agent_bubble_light": "",
    "agent_bubble_border_dark": "",
    "agent_bubble_border_light": "",
    # Chat composer pill fill and border (blank = follow the theme default; #rrggbb[aa]).
    "chat_pill_bg_dark": "",
    "chat_pill_bg_light": "",
    "chat_pill_border_dark": "",
    "chat_pill_border_light": "",
    # Theme-independent style knobs (JSON-only; see the AppSettings fields).
    "radius_scale": "1",
    "shadow_strength": "1",
    "ui_scale": "1",
    "reduce_motion": "off",
    "cursor_glow": "on",
    # RAM speed layer (media-cache.js) — see the AppSettings fields.
    "media_cache_enabled": "on",
    "media_cache_budget_mb": "128",
    # Chat-composer geometry + button knobs (see the AppSettings fields).
    "chat_pill_max_width": "500px",
    "chat_pill_radius": "20px",
    "chat_pill_padding": "4px 4px 0px 4px",
    "chat_pill_min_height": "",
    "chat_pill_max_height": "160px",
    "chat_pill_font_size": "14px",
    "chat_pill_attach_size": "38px",
    "chat_pill_attach_icon": "24px",
    "chat_pill_button_size": "62px",
    "chat_pill_button_icon": "44px",
    # Chat panel layout (see the AppSettings fields). Served via ui-config and
    # applied app-wide by ui/shared/js/appearance.js.
    "chat_position": "right",
    "chat_default_visible": "visible",
    "chat_panel_width": "380px",
}


_CHAT_PANEL_WIDTH_RE = re.compile(r"^(?P<number>(?:\d+(?:\.\d+)?|\.\d+))(?P<unit>px|%)$")


def normalize_chat_panel_width(raw: object) -> str:
    """Return a safe positive desktop chat width, or raise for invalid input."""
    if not isinstance(raw, str):
        raise ValueError("must be a CSS length in px or %")
    value = raw.strip()
    match = _CHAT_PANEL_WIDTH_RE.fullmatch(value)
    if not match or float(match.group("number")) <= 0:
        raise ValueError("must be a positive CSS length in px or %")
    return value


def get_appearance_config() -> dict:
    """Read the global border + font knobs from app-settings.json, falling back
    to the design-system.css defaults for any missing/blank value.

    Returns ``{border_color_dark, border_color_light, border_width, font_sans,
    font_mono}`` — every value concrete (never blank), so the public
    ``/api/v1/auth/ui-config`` endpoint always carries a usable token and the
    boot-time applier (ui/shared/js/appearance.js) can inject it directly. This
    is the server side of the "edit the JSON, reload, the whole app follows"
    flow; the file is re-read on every call, so no server restart is needed."""
    data = _load_app_settings()
    out = {}
    for key, default in _DEFAULT_APPEARANCE.items():
        val = data.get(key)
        value = val.strip() if isinstance(val, str) and val.strip() else default
        if key == "chat_panel_width":
            try:
                value = normalize_chat_panel_width(value)
            except ValueError:
                value = default
        out[key] = value
    return out


def get_splash_enabled() -> bool:
    """Master on/off for the welcome landing page (app-wide), read live from
    app-settings.json. When True, app/main.py serves the landing at the front door
    (/); served via /api/v1/auth/ui-config too. Ships OFF: defaults to False when
    unset/blank so a fresh install opens straight into the app (no landing page)."""
    return _load_app_settings().get("splash_enabled", False) is True


def get_safety_lock_enabled() -> bool:
    """Master switch for the safety lock feature. When False, the feature
    is completely disabled — no splash, no gate, no recovery suppression.
    Defaults to False (disabled) when unset."""
    return _load_app_settings().get("safety_lock_enabled", False) is True


def set_safety_lock_enabled(enabled: bool) -> None:
    """Set or clear the master switch for the safety lock feature."""
    data = _load_app_settings()
    if enabled:
        data["safety_lock_enabled"] = True
    else:
        data.pop("safety_lock_enabled", None)
    _save_app_settings(data)


def get_safety_lock_active() -> bool:
    """Persistent lock flag — set on every graceful shutdown, NEVER cleared
    from disk. Only the master switch (safety_lock_enabled) can suppress it.
    This ensures every restart shows the splash, even after a crash.

    Read from app-settings.json. Defaults to False when unset."""
    return _load_app_settings().get("safety_lock_active", False) is True


def set_safety_lock_active() -> None:
    """Set the persistent lock flag. Called on shutdown. Never cleared from
    disk — the lock stays until the admin turns off the master switch."""
    data = _load_app_settings()
    data["safety_lock_active"] = True
    _save_app_settings(data)


def get_allow_user_appearance() -> bool:
    """Whether signed-in users may set their own theme (per-user appearance),
    read live from app-settings.json. Served via /api/v1/auth/ui-config so the
    account page can show/hide its "My appearance" editor and the same endpoint
    knows whether to layer a caller's saved overrides on top of the global
    theme. Defaults to False (off) when unset/blank."""
    return _load_app_settings().get("allow_user_appearance", False) is True


def get_hide_header_on_keyboard() -> bool:
    """Master on/off for hiding the header when the mobile keyboard is open.
    Always OFF (no hiding) when unset. Served via /api/v1/auth/ui-config so
    chat-ui.js can gate on it. Only applies at ≤800px viewports."""
    return _load_app_settings().get("hide_header_on_keyboard", False) is True


def get_mobile_mode() -> bool:
    """Use compact hamburger/sidebar navigation instead of the header carousel."""
    return _load_app_settings().get("mobile_mode", True) is True


def get_hide_header_kb_threshold() -> int:
    """Viewport height threshold for the hide-header-on-keyboard feature.
    0 = always hide when keyboard is open. Positive = only hide when the
    remaining viewport height is below this many px. Default 200."""
    val = _load_app_settings().get("hide_header_kb_threshold", 200)
    try:
        v = int(val)
        return max(0, v)
    except (TypeError, ValueError):
        return 200


def get_default_max_tokens() -> int:
    """Default output-token ceiling for an agent with no Max Tokens of its own.

    Read live from app-settings.json (App Settings → App Functions) so an admin
    edit takes effect on the next turn without a restart.
    ``WEBAGENT_DEFAULT_MAX_TOKENS`` overrides it for development. The same value
    seeds a newly created agent. Falls back to 8096 if stored unset/non-positive."""
    v = _env_int_for("WEBAGENT_DEFAULT_MAX_TOKENS", 8096)
    return v if v > 0 else 8096


def get_max_active_sessions() -> int:
    """App-wide cap on concurrently active sessions (app/agent/session_gate.py).
    0 = unlimited (the default). Read live from app-settings.json so an admin
    edit takes effect on the next run start without a restart."""
    val = _load_app_settings().get("max_active_sessions", 0)
    try:
        v = int(val)
    except (TypeError, ValueError):
        return 0
    return max(0, v)
def get_transcript_timestamp_order_recovery_enabled() -> bool:
    """Whether timestamp fallback for session-order drift is enabled."""
    return _load_app_settings().get("transcript_timestamp_order_recovery_enabled", True) is True

def get_transcript_timestamp_order_recovery_threshold_seconds() -> int:
    """Maximum allowed gap (in seconds) between adjacent sequenced rows before
    we mark timestamp-order recovery for the transcript."""
    val = _load_app_settings().get("transcript_timestamp_order_recovery_threshold_seconds", 60)
    try:
        v = int(val)
    except (TypeError, ValueError):
        return 60
    if v < 0:
        return 60
    return v


def _env_int_for(name: str, default: int) -> int:
    """Read an int from the environment if set, otherwise from app-settings.json,
    falling back to ``default``."""
    import os
    env = os.environ.get(name, "").strip()
    if env:
        try:
            return int(env)
        except (TypeError, ValueError):
            pass
    # Environment variables use WEBAGENT_* while the persisted settings model
    # uses the field name without that deployment-specific prefix.
    settings_key = name.lower()
    if settings_key.startswith("webagent_"):
        settings_key = settings_key[len("webagent_"):]
    val = _load_app_settings().get(settings_key, default)
    try:
        return int(val)
    except (TypeError, ValueError):
        return default


def get_anon_session_max() -> int:
    """Max new anonymous sessions per client IP within the window."""
    return max(0, _env_int_for("WEBAGENT_ANON_SESSION_MAX", 10))


def get_anon_session_window() -> int:
    """Window in seconds for the anonymous-session rate limit."""
    return max(1, _env_int_for("WEBAGENT_ANON_SESSION_WINDOW", 60))


def get_anon_global_session_max() -> int:
    """App-wide anonymous session mints allowed in the global window."""
    return max(0, _env_int_for("WEBAGENT_ANON_GLOBAL_SESSION_MAX", 25))


def get_anon_global_session_window() -> int:
    return max(1, _env_int_for("WEBAGENT_ANON_GLOBAL_SESSION_WINDOW", 3600))


def get_public_registration_ip_max() -> int:
    return max(0, _env_int_for("WEBAGENT_PUBLIC_REGISTRATION_IP_MAX", 5))


def get_public_registration_global_max() -> int:
    return max(0, _env_int_for("WEBAGENT_PUBLIC_REGISTRATION_GLOBAL_MAX", 100))


def get_public_registration_window() -> int:
    return max(1, _env_int_for("WEBAGENT_PUBLIC_REGISTRATION_WINDOW", 3600))


def get_anon_identity_max() -> int:
    """Max distinct anonymous browser identities per source fingerprint."""
    return max(0, _env_int_for("WEBAGENT_ANON_IDENTITY_MAX", 5))


def get_anon_identity_window() -> int:
    """Window in seconds for distinct anonymous identities per source."""
    return max(1, _env_int_for("WEBAGENT_ANON_IDENTITY_WINDOW", 86400))


def get_anon_chat_max() -> int:
    """Max messages per anonymous identity within the window."""
    return max(0, _env_int_for("WEBAGENT_ANON_CHAT_MAX", 5))


def get_anon_chat_window() -> int:
    """Window in seconds for the per-identity message limit."""
    return max(1, _env_int_for("WEBAGENT_ANON_CHAT_WINDOW", 300))


def get_anon_daily_chat_max() -> int:
    """Daily admitted chat turns for one anonymous identity."""
    return max(0, _env_int_for("WEBAGENT_ANON_DAILY_CHAT_MAX", 10))


def get_anon_daily_chat_window() -> int:
    return max(1, _env_int_for("WEBAGENT_ANON_DAILY_CHAT_WINDOW", 86400))


def get_anon_chat_ip_max() -> int:
    """Max messages across all anon identities from one IP within the window."""
    return max(0, _env_int_for("WEBAGENT_ANON_CHAT_IP_MAX", 10))


def get_anon_chat_ip_window() -> int:
    """Window in seconds for the per-IP message limit."""
    return max(1, _env_int_for("WEBAGENT_ANON_CHAT_IP_WINDOW", 300))


def get_anon_global_chat_max() -> int:
    """App-wide anonymous chat admissions allowed in the global window."""
    return max(0, _env_int_for("WEBAGENT_ANON_GLOBAL_CHAT_MAX", 50))


def get_anon_global_chat_window() -> int:
    return max(1, _env_int_for("WEBAGENT_ANON_GLOBAL_CHAT_WINDOW", 300))


def get_anonymous_chat_enabled() -> bool:
    """Whether anonymous identity minting and chat are enabled app-wide."""
    raw = os.environ.get("WEBAGENT_ANONYMOUS_CHAT_ENABLED", "").strip().lower()
    if raw:
        return raw in {"1", "true", "yes", "on"}
    return _load_app_settings().get("anonymous_chat_enabled", True) is not False


def get_anon_budget_max() -> int:
    """Hard app-wide anonymous chat-turn budget for one budget window."""
    return max(0, _env_int_for("WEBAGENT_ANON_BUDGET_MAX", 100))


def get_anon_budget_window() -> int:
    return max(1, _env_int_for("WEBAGENT_ANON_BUDGET_WINDOW", 86400))


def get_anon_native_controls() -> dict:
    """Return bounded native anonymous spend/risk/concurrency controls."""
    values = {
        "estimated_output_tokens": _env_int_for("WEBAGENT_ANON_ESTIMATED_OUTPUT_TOKENS", 4096),
        "estimated_cost_per_1k_microusd": _env_int_for("WEBAGENT_ANON_ESTIMATED_COST_PER_1K_MICROUSD", 10000),
        "token_user_max": _env_int_for("WEBAGENT_ANON_TOKEN_USER_MAX", 100000),
        "token_source_max": _env_int_for("WEBAGENT_ANON_TOKEN_SOURCE_MAX", 100000),
        "token_global_max": _env_int_for("WEBAGENT_ANON_TOKEN_GLOBAL_MAX", 1000000),
        "cost_user_microusd_max": _env_int_for("WEBAGENT_ANON_COST_USER_MICROUSD_MAX", 250000),
        "cost_source_microusd_max": _env_int_for("WEBAGENT_ANON_COST_SOURCE_MICROUSD_MAX", 250000),
        "cost_global_microusd_max": _env_int_for("WEBAGENT_ANON_COST_GLOBAL_MICROUSD_MAX", 2500000),
        "spend_window": _env_int_for("WEBAGENT_ANON_SPEND_WINDOW", 86400),
        "max_concurrent_runs": _env_int_for("WEBAGENT_ANON_MAX_CONCURRENT_RUNS", 2),
        "run_lease_seconds": _env_int_for("WEBAGENT_ANON_RUN_LEASE_SECONDS", 900),
        "risk_delay_score": _env_int_for("WEBAGENT_ANON_RISK_DELAY_SCORE", 2),
        "risk_cooldown_score": _env_int_for("WEBAGENT_ANON_RISK_COOLDOWN_SCORE", 4),
        "risk_delay_ms": _env_int_for("WEBAGENT_ANON_RISK_DELAY_MS", 500),
        "risk_cooldown_seconds": _env_int_for("WEBAGENT_ANON_RISK_COOLDOWN_SECONDS", 900),
        "auto_close_seconds": _env_int_for("WEBAGENT_ANON_AUTO_CLOSE_SECONDS", 900),
        "error_max": _env_int_for("WEBAGENT_ANON_ERROR_MAX", 10),
        "error_window": _env_int_for("WEBAGENT_ANON_ERROR_WINDOW", 300),
    }
    values = {key: max(0, int(value)) for key, value in values.items()}
    values["spend_window"] = max(1, values["spend_window"])
    values["run_lease_seconds"] = max(30, values["run_lease_seconds"])
    values["error_window"] = max(1, values["error_window"])
    raw = os.environ.get("WEBAGENT_ANON_AUTO_CLOSE_ENABLED", "").strip().lower()
    values["auto_close_enabled"] = (
        raw in {"1", "true", "yes", "on"} if raw
        else _load_app_settings().get("anon_auto_close_enabled", True) is not False
    )
    return values


def get_session_completion_notifications() -> bool:
    """Master on/off for the sliding session-completion notification panel
    (running → done/interrupted/error), read live from app-settings.json. Served
    via /api/v1/auth/ui-config so ui/chat/js/session-notification.js can gate on
    it. Defaults True (on) when unset/blank — the panel has always been
    available."""
    return _load_app_settings().get("session_completion_notifications", True) is not False


def get_always_on_display() -> bool:
    """Master on/off for the always-on display (Screen Wake Lock), read live
    from app-settings.json. Served via /api/v1/auth/ui-config so every visitor's
    browser can apply it at boot (ui/shared/js/main.js) — the same operation as
    the chat wake_lock control. Defaults False (screen sleeps normally) when
    unset/blank."""
    return _load_app_settings().get("always_on_display", False) is True

def get_site_name() -> str:
    """Return the configured site name, falling back to the default brand."""
    value = str(_load_app_settings().get("site_name") or "").strip()
    return value[:120] or "WebAgent"



def get_voice_dictation_config() -> dict:
    """Return the validated app-wide voice dictation policy."""
    raw = _load_app_settings()
    mode = raw.get("voice_dictation_mode", "browser_then_llm")
    if mode not in {"browser_then_llm", "llm_only"}:
        mode = "browser_then_llm"
    return {
        "llm_enabled": raw.get("voice_dictation_llm_enabled", True) is not False,
        "mode": mode,
    }


def valid_background(val: str | None, fallback: str) -> str:
    """Return ``val`` if it names an installed background (or "none"), else
    ``fallback``. Used to sanitise a per-user background choice the same way
    get_background_config validates the global one, so a stale/removed id can
    never leave the page pointing at a missing plugin."""
    v = (val or "").strip()
    try:
        from app.ui_backgrounds import valid_ids
        allowed = valid_ids() | {"none"}
    except Exception:  # noqa: BLE001
        return v or fallback
    if v and v in allowed:
        return v
    return fallback if fallback in allowed else "none"


def get_access_mode() -> str:
    """Read just the access_mode flag from app-settings.json.

    Legacy stored values (public_anonymous, private) are normalized to the
    current three-mode vocabulary so an upgraded config keeps working.
    """
    return normalize_access_mode(_load_app_settings().get("access_mode"))


# User BYOD is read on the DB hot path (every get_db()), so a raw file read
# per call would be wasteful. Cache it for a short window; a flip via the App
# Settings toggle takes effect within the TTL without a restart.
_BYOD_CACHE: dict = {"val": None, "at": 0.0}
_BYOD_TTL_SECONDS = 3.0


def get_user_byod_enabled() -> bool:
    """True when per-user bring-your-own-database routing is switched on (App
    Settings → User BYOD). Sync + cached (see _BYOD_TTL_SECONDS) because
    it gates get_db() on the hot path. Defaults False (single-tenant)."""
    import time as _t
    now = _t.monotonic()
    if _BYOD_CACHE["val"] is not None and (now - _BYOD_CACHE["at"]) < _BYOD_TTL_SECONDS:
        return _BYOD_CACHE["val"]
    try:
        val = bool(_load_app_settings().get("user_byod", False))
    except Exception:  # noqa: BLE001
        val = False
    _BYOD_CACHE.update(val=val, at=now)
    return val


def get_global_system_prompt() -> str:
    """Read the app-global system prompt (the platform baseline injected at the
    top of every agent's prompt). Sync — called from build_system_prompt each
    turn. Returns '' when unset, so callers can prepend unconditionally."""
    try:
        return (str(_load_app_settings().get("global_system_prompt") or "")).strip()
    except Exception:  # noqa: BLE001
        return ""


def get_self_heal_config() -> dict:
    """Read the self-healing / auto-resume tunables from app-settings.json
    (sync — used by the watchdog each tick and by the runner for defaults).
    Falls back to the AppSettings defaults for any missing/invalid value."""
    s = AppSettings(**_load_app_settings())
    # data/config/debug-config.json can force the watchdog on/off, overriding the
    # App Settings value.
    _watchdog_enabled = bool(s.run_watchdog_enabled)
    try:
        from app.admin.debug_config import debug_overrides
        _ov = debug_overrides()
        if "run_watchdog_enabled" in _ov:
            _watchdog_enabled = bool(_ov["run_watchdog_enabled"])
    except Exception:
        pass
    return {
        "watchdog_enabled": _watchdog_enabled,
        "poll_seconds": max(5, int(s.run_watchdog_poll_seconds)),
        "frozen_threshold_seconds": max(15, int(s.run_frozen_threshold_seconds)),
        "zombie_grace_seconds": max(10, int(s.run_zombie_grace_seconds)),
        "no_progress_threshold_seconds": max(60, int(s.run_no_progress_threshold_seconds)),
        "max_resume_attempts": max(0, int(s.run_max_resume_attempts)),
        "max_resume_attempts_per_cause": max(0, int(s.run_max_resume_attempts_per_cause)),
        "backoff_seconds": max(1, int(s.run_resume_backoff_seconds)),
    }


# ── Endpoints ──────────────────────────────────────────────────────────────

@router.get("/providers")
async def get_providers():
    """Return known provider presets (id → name + base_url)."""
    return PROVIDER_PRESETS


@router.get("/provider", response_model=ProviderConfig)
async def get_provider(
    request: Request,
):
    """Get current provider configuration for the requesting user.
    Reads from the auth_elements table in the DB (per-user).
    API key is returned as plaintext.
    """
    user_id = await _require_provider_admin(request)

    # The requesting user's Models draft is private to that user's vault.
    stored = _without_saved_default(
        await _load_own_llm_config(user_id) or DEFAULT_PROVIDER
    )
    config = {**DEFAULT_PROVIDER, **stored}

    # Ensure providers dict exists
    if "providers" not in config:
        config["providers"] = {}

    # Ensure base_url is present
    if not config.get("base_url"):
        prov = config.get("provider", "")
        if prov in PROVIDER_PRESETS:
            config["base_url"] = PROVIDER_PRESETS[prov]["base_url"]
    return ProviderConfig(**_public_provider_config(config))


@router.post("/provider", response_model=dict)
async def set_provider(
    request: Request,
    config: ProviderConfig,
):
    """Save provider-form draft state without creating an app default.
    API keys are packed into the ENCRYPTED vault secret (auth_elements.secret_ref);
    the DB config blob is stored key-stripped. Never
    shared between users.
    """
    user_id = await _require_provider_admin(request)
    # Resolve the CURRENT config from the vault (keys rehydrated) so a partial
    # save — e.g. changing the model with the key field left blank to "keep" — does
    # not wipe the stored key.
    existing = await _load_own_llm_config(user_id) or dict(DEFAULT_PROVIDER)
    existing_providers = existing.get("providers", {})
    request_providers = config.providers or {}

    # Merge incoming providers map over existing
    merged_providers = dict(existing_providers)
    for pk, pv in request_providers.items():
        merged_providers[pk] = pv

    # Ensure current provider's key+model+url live in root too
    subscription_auth = (
        config.provider == "openai_subscription" or config.auth_mode == "chatgpt"
    )
    prior = merged_providers.get(config.provider) or {}
    current_key = "" if subscription_auth else (config.api_key or prior.get("api_key", ""))
    current_url = (
        PROVIDER_PRESETS["openai_subscription"]["base_url"]
        if subscription_auth else (config.base_url or prior.get("base_url", ""))
    )

    # Sync the active provider in the map
    merged_providers[config.provider] = {
        "auth_mode": config.auth_mode,
        "api_key": current_key,
        "base_url": current_url,
        "stateful_responses": config.stateful_responses,
    }

    merged = _without_saved_default({**existing, "providers": merged_providers})

    # Persist through the single secret-safe path: keys packed into the encrypted
    # vault secret, plaintext DB config blob stripped.
    await _persist_llm_config(user_id, merged)

    logger.info("Provider config set for user %s: %s", user_id[:12], config.provider)
    return {"status": "ok", "message": f"Provider set to {config.provider}", "user": user_id}


@router.post("/provider/clear", response_model=dict)
async def clear_provider(
    request: Request,
):
    """Clear provider configuration for the requesting user."""
    user_id = await _require_provider_admin(request)
    # Clear the vault so a cleared key cannot be resurrected on the next read.
    existing = await _load_own_llm_config(user_id) or {}
    cleared = _without_saved_default(existing)
    cleared["providers"] = {}
    await _persist_llm_config(user_id, cleared)
    logger.info("Provider config cleared for user %s", user_id[:12])
    return {"status": "ok", "message": "Provider settings cleared", "user": user_id}


@router.get("/multi-providers")
async def get_multi_providers(
    request: Request,
):
    """Get the saved-model roster for the requesting user (the candidate models
    for the chat switcher + vision/image workers). Returns a list of entries.
    """
    user_id = await _require_provider_admin(request)

    # Saved models are resolved from the requesting user's vault record.
    config = _without_saved_default(await _load_own_llm_config(user_id) or DEFAULT_PROVIDER)
    public = _public_provider_config(config)

    return {
        "providers": public.get("multi_providers", []),
    }


@router.get("/provider-bundle")
async def get_provider_bundle(
    request: Request,
):
    """Combined read: the default-model provider config AND the saved-model roster
    in ONE response, resolved from a SINGLE vault+DB read.

    The Models table front-end reads this instead of calling GET /provider and
    GET /multi-providers one after the other — each of those repeats the same
    per-user vault decryption + DB round-trip via ``_resolve_user_config``, so on
    a remote database the sequential pair was the dominant load cost. Serving both
    slices from one resolve halves that. API keys are returned as plaintext, same
    as GET /provider.
    """
    user_id = await _require_provider_admin(request)

    # One caller-owned vault read hydrates both draft and roster.
    stored = _without_saved_default(
        await _load_own_llm_config(user_id) or DEFAULT_PROVIDER
    )
    config = {**DEFAULT_PROVIDER, **stored}

    if "providers" not in config:
        config["providers"] = {}
    if not config.get("base_url"):
        prov = config.get("provider", "")
        if prov in PROVIDER_PRESETS:
            config["base_url"] = PROVIDER_PRESETS[prov]["base_url"]

    public = _public_provider_config(config)
    return {
        "provider": ProviderConfig(**public),
        "roster": public.get("multi_providers", []),
    }


@router.post("/multi-providers", response_model=dict)
async def set_multi_providers(
    request: Request,
    body: MultiProvidersRequest,
):
    """Save the sole model authority: the roster.

    Standard is derived from roster roles at runtime; no separate default is saved.
    """
    user_id = await _require_provider_admin(request)
    # Vault-first (keys rehydrated) so the default brain's key + providers map are
    # preserved across a roster-only edit.
    existing = await _load_own_llm_config(user_id) or dict(DEFAULT_PROVIDER)

    from app import openai_subscription
    from app.entitlements.rosters import (
        RosterError, assign_entry_ids, validate_live_voice_assignments,
        validate_roster_entries,
    )
    try:
        submitted_roster = validate_roster_entries([
            p.model_dump(exclude={"credential_configured"}) for p in body.providers
        ], allow_credentials=True, require_provider_model=True)
    except RosterError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    existing_roster = _roster_entries_with_ids(existing.get("multi_providers") or [])
    existing_by_id = {entry["entry_id"]: entry for entry in existing_roster}
    allowed_existing = await openai_subscription.existing_entries_with_reservations(
        submitted_roster, existing_roster, scope="user", user_id=user_id,
    )
    try:
        incoming_roster = assign_entry_ids(
            submitted_roster,
            existing_entries=allowed_existing,
        )
    except RosterError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    # This endpoint owns the app-wide inherited catalog, not an agent roster.
    # Accept legacy/UI role fields for a smooth migration, but never persist them
    # as global role positions.
    incoming_roster = [_catalog_entry(entry) for entry in incoming_roster]
    try:
        validate_live_voice_assignments(incoming_roster)
    except RosterError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    # Browser reads intentionally return blank keys. Match by immutable ID so a
    # reorder or role edit preserves each entry's encrypted credential.
    for entry in incoming_roster:
        previous = existing_by_id.get(entry["entry_id"])
        if entry.get("provider") == "openai_subscription" or entry.get("auth_mode") == "chatgpt":
            entry["api_key"] = ""
            entry["credential_scope"] = "user"
            from app import openai_subscription
            try:
                state = await openai_subscription.status(
                    scope="user", user_id=user_id, entry_id=str(entry.get("entry_id") or ""),
                )
            except Exception:
                state = {}
            # Authorization state is transport metadata.  Do not add role fields
            # here: global rows are catalog-only whether or not a subscription is
            # currently authorized.
        elif not entry.get("api_key") and previous:
            entry["api_key"] = previous.get("api_key", "")

    merged = _without_saved_default(existing)
    merged["multi_providers"] = incoming_roster

    incoming_ids = {
        str(entry.get("entry_id") or "").strip() for entry in incoming_roster
        if entry.get("entry_id")
    }
    removed = [
        entry for entry in existing_roster
        if str(entry.get("entry_id") or "").strip() not in incoming_ids
    ]

    # Persist through the single secret-safe path: every roster key + the default
    # key packed into the encrypted vault secret; plaintext stores stripped.
    await _persist_llm_config(user_id, merged)
    if removed:
        from app import openai_subscription
        for row in removed:
            if row.get("provider") == "openai_subscription" or row.get("auth_mode") == "chatgpt":
                await openai_subscription.sign_out(
                    scope="user", user_id=user_id, entry_id=str(row.get("entry_id") or ""),
                )

    pruned_agent_models = 0
    if removed:
        from app.db import get_db
        prune = getattr(get_db(), "prune_agent_model_rosters", None)
        if prune is None:
            raise RuntimeError("Active storage backend cannot prune agent model rosters")
        pruned_agent_models = await prune(removed)

    count = len(body.providers)
    logger.info("Saved-model roster saved for user %s: count=%d", user_id[:12], count)
    return {
        "status": "ok",
        "count": count,
        "pruned_agent_models": pruned_agent_models,
        "providers": _public_provider_config({"multi_providers": incoming_roster})["multi_providers"],
        "message": f"Saved-model roster saved. {count} model(s).",
    }


# Model-id substrings that imply image GENERATION. Dedicated image hosts
# (OpenAI images, Stability, Gemini/Imagen, FLUX/SDXL on Together, Fireworks,
# DeepInfra) don't advertise modalities on /models, so we fall back to the id.
_IMAGE_OUT_NAME_HINTS = (
    "dall-e", "dalle", "gpt-image", "flux", "sdxl", "stable-diffusion",
    "stable-image", "sd3", "imagen", "playground", "diffus",
)


def _name_suggests_image_out(model_id: str) -> bool:
    mid = (model_id or "").lower()
    return any(h in mid for h in _IMAGE_OUT_NAME_HINTS)


def _detect_model_modalities(m: dict) -> tuple:
    """Best-effort detection of a model's media capabilities from a provider's
    /models entry. Returns
    ``(text_capable, image_capable, image_out_capable, voice_capable, video_capable, modality_known)``.

      image_capable      = accepts image INPUT (vision).
      image_out_capable  = can GENERATE images — from the provider's
        ``output_modalities`` when present, plus a model-id name heuristic
        (dedicated image hosts don't report modalities on /models).
      voice_capable      = accepts audio INPUT (voice/audio).
      video_capable      = accepts video input or generates video output.
      modality_known     = the provider reported structured modality info.

    Handles OpenRouter's ``architecture.input_modalities`` list and the legacy
    ``modality`` string (e.g. "text+image->text"); text input is assumed for
    every chat model — image in/out are the meaningful signals.
    """
    arch = m.get("architecture") or {}

    # Image OUTPUT — structured output_modalities, else id name heuristic.
    out_mods = arch.get("output_modalities") or m.get("output_modalities")
    img_out = False
    if isinstance(out_mods, list) and out_mods:
        img_out = "image" in [str(x).lower() for x in out_mods]
    if not img_out:
        img_out = _name_suggests_image_out(m.get("id", ""))

    # Image INPUT (vision), Voice INPUT (audio), and Video input/output.
    mods = arch.get("input_modalities") or m.get("input_modalities")
    if isinstance(mods, list) and mods:
        low = [str(x).lower() for x in mods]
        return (True, "image" in low, img_out, "audio" in low, "video" in low or (isinstance(out_mods, list) and "video" in [str(x).lower() for x in out_mods]), True)
    modality = arch.get("modality") or m.get("modality")
    if isinstance(modality, str) and modality:
        inp, _, out = modality.lower().partition("->")
        return (True, ("image" in inp or "vision" in inp), img_out, "audio" in inp, "video" in inp or "video" in out, True)
    return (True, False, img_out, False, isinstance(out_mods, list) and "video" in [str(x).lower() for x in out_mods], False)


@router.get("/models")
async def get_models(
    request: Request,
    provider: str = "",
    api_key: str = Query("", alias="api_key"),
    base_url: str = Query("", alias="base_url"),
    authorization: Optional[str] = Header(None),
    token: Optional[str] = Query(None),
):
    """Fetch available models from the configured provider's API.
    Uses the requesting user's saved config, or the explicit api_key/base_url
    params passed by the frontend (for per-row model fetching in parallel providers UI).
    """
    await _require_provider_admin(request)

    if provider == "openai_subscription":
        from app import openai_subscription
        return {"models": openai_subscription.models()}

    # Explicit params override saved config (used by parallel provider rows)
    if api_key and base_url:
        # Use the explicitly provided key and URL directly
        pass
    else:
        # Resolve credentials from provider draft state or a matching roster row.
        # Historical top-level default fields are intentionally ignored.
        config = await _load_own_llm_config(PLATFORM_LLM_OWNER) or dict(DEFAULT_PROVIDER)
        prov = provider or ""
        saved = (config.get("providers") or {}).get(prov) or {}
        roster_match = next(
            (item for item in (config.get("multi_providers") or [])
             if isinstance(item, dict)
             and (not prov or item.get("provider") == prov)
             and (not base_url or item.get("base_url") == base_url)),
            {},
        )
        source = saved or roster_match
        api_key = source.get("api_key", "")
        if not api_key:
            return {"error": "No API key configured", "models": []}

        prov = prov or source.get("provider", "")
        base_url = base_url or source.get("base_url", "")

        # If no saved base_url, get from preset
        if not base_url and prov in PROVIDER_PRESETS:
            base_url = PROVIDER_PRESETS[prov]["base_url"]

    if not base_url:
        return {"error": "No base URL configured for this provider", "models": []}

    # Make sure the metadata catalog (models.dev + OpenRouter) is loaded so we
    # can attach context size / cost / description to each model. Cheap after the
    # first fetch — served from the on-disk cache and only re-fetched when stale.
    try:
        from app import model_catalog
        await model_catalog.ensure_fresh()
    except Exception as e:
        logger.warning("model_catalog enrich unavailable: %s", e)
        model_catalog = None

    # Build models endpoint: <base_url>/models (handle trailing slash)
    models_url = base_url.rstrip("/") + "/models"

    try:
        async with httpx.AsyncClient(timeout=15) as client:
            headers = {
                "Authorization": f"Bearer {api_key}",
                "Content-Type": "application/json",
            }
            resp = await client.get(models_url, headers=headers)
            if resp.status_code == 401:
                return {"error": "Invalid API key", "models": []}
            resp.raise_for_status()
            data = resp.json()
            models = []
            for m in data.get("data", []):
                tcap, icap, iocap, vcap, vidcap, known = _detect_model_modalities(m)
                entry = {
                    "id": m["id"],
                    "name": m.get("name", m["id"]),
                    "text_capable": tcap,
                    "image_capable": icap,
                    "image_out_capable": iocap,
                    "voice_capable": vcap,
                    "video_capable": vidcap,
                    "modality_known": known,
                    # Metadata fields (populated below from the catalog when known).
                    "context": None,
                    "max_output": None,
                    "cost_input": None,
                    "cost_output": None,
                    "description": "",
                }
                if model_catalog is not None:
                    meta = model_catalog.lookup(m["id"], provider_hint=provider or "")
                    if meta:
                        entry["context"] = meta.get("context")
                        entry["max_output"] = meta.get("max_output")
                        entry["cost_input"] = meta.get("cost_input")
                        entry["cost_output"] = meta.get("cost_output")
                        entry["description"] = meta.get("description") or ""
                        # Capability fallback: some hosts (DeepSeek's own /models,
                        # self-hosted gateways) advertise NO structured modalities,
                        # so live detection defaults every model to text-only —
                        # wrongly marking vision/voice models (e.g. deepseek-v4-
                        # flash-vision-exp) as Standard-only. The catalog
                        # (models.dev / OpenRouter) knows the true input/output
                        # modalities, so fill the gaps from it. Upgrade-only: never
                        # downgrade what the live response did report.
                        if not known:
                            imods = [str(x).lower() for x in (meta.get("input_modalities") or [])]
                            if imods:
                                entry["image_capable"] = "image" in imods
                                entry["voice_capable"] = "audio" in imods
                                entry["video_capable"] = "video" in imods
                                entry["modality_known"] = True
                        omods = [str(x).lower() for x in (meta.get("output_modalities") or [])]
                        if "image" in omods:
                            entry["image_out_capable"] = True
                        if "video" in omods:
                            entry["video_capable"] = True
                models.append(entry)
            models.sort(key=lambda x: x["id"])
            return {"error": None, "models": models}
    except httpx.RequestError as e:
        logger.warning(f"Failed to fetch models from {models_url}: %s", e)
        return {"error": f"Network error: {e}", "models": []}
    except Exception as e:
        logger.warning(f"Failed to fetch models from {models_url}: %s", e)
        return {"error": str(e), "models": []}


@router.get("/model-catalog")
async def get_model_catalog_status():
    """Status of the merged model-metadata catalog (models.dev + OpenRouter)."""
    try:
        from app import model_catalog
        return {"error": None, **model_catalog.cache_info()}
    except Exception as e:
        return {"error": str(e), "fetched_at": 0, "count": 0, "stale": True}


@router.post("/model-catalog/refresh")
async def refresh_model_catalog():
    """Force a re-fetch of the model-metadata catalog from both sources."""
    try:
        from app import model_catalog
        await model_catalog.refresh(force=True)
        return {"error": None, **model_catalog.cache_info()}
    except Exception as e:
        logger.warning("model_catalog refresh failed: %s", e)
        return {"error": str(e), "fetched_at": 0, "count": 0, "stale": True}


async def _is_admin(db, user_id: str) -> bool:
    """True when this user may see cross-user (global) usage totals."""
    if not user_id:
        return False
    if user_id == "admin":
        return True
    try:
        prof = await db.get_user_profile(user_id)
        return bool(prof and prof.get("is_admin"))
    except Exception:
        return False


@router.get("/model-usage")
async def get_model_usage(
    model: str = Query(""),
    provider: str = Query(""),
    entry_id: str = Query(""),
    include_unassigned: int = Query(0),
    scope: str = Query("user"),
    authorization: Optional[str] = Header(None),
    token: Optional[str] = Query(None),
):
    """Aggregate real token usage and cost for ONE saved model row.

    MODEL-ROW-USAGE: ``entry_id`` identifies the exact saved model-connection
    row. model+provider can repeat across rows (same model saved twice with a
    different credential / effort / injected prompt), so the per-row USAGE cell
    must key on the stable roster ID, not the model string. Legacy rows written
    before entry_id existed carry NULL; ``include_unassigned=1`` folds those
    model/provider rows in — the caller sets it only when the roster has a
    single row for that model, so they can never double-count.

    scope='user' (default) sums only the current user's calls. scope='global'
    (admin only) sums every agent, every user, and background tasks (git commit
    messages, placeholder/suggestion text, compaction, embeddings) for a true
    app-wide model total. cost_usd is the canonical published-price figure;
    total_cost_cents stays as the secondary provider-billed figure."""
    user_id = _resolve_user_id(authorization or "", token or "")
    if not user_id:
        return {"error": "not_authenticated", "total_input_tokens": 0,
                "total_output_tokens": 0, "total_cost_cents": 0, "total_cost_usd": 0.0}

    try:
        from app.db import get_app_db
        db = get_app_db()
        want_global = (scope == "global") and await _is_admin(db, user_id)
        total_in = 0
        total_out = 0
        total_cost_cents = 0
        total_cost_usd = 0.0

        # MODEL-ROW-USAGE: build the WHERE clause once so the two DB backends
        # filter identically. entry_id wins over model when present; the legacy
        # NULL-entry rows are opt-in so a duplicate model never double-counts.
        where: list = []
        params: list = []
        if not want_global:
            where.append("user_id=?")
            params.append(user_id)
        if entry_id:
            if include_unassigned:
                where.append("(entry_id=? OR (entry_id IS NULL AND model=?))")
                params.extend([entry_id, model])
            else:
                where.append("entry_id=?")
                params.append(entry_id)
        else:
            where.append("model=?")
            params.append(model)
        # Global sums every provider for this model (the same model can be billed
        # via different provider strings, and background rows carry their own);
        # user-scope keeps the provider filter for precision, but a blank provider
        # means "any provider".
        if provider:
            where.append("provider=?")
            params.append(provider)

        def _legacy_match(row: dict) -> bool:
            if not entry_id:
                return True
            row_entry = str(row.get("entry_id") or "")
            if row_entry == entry_id:
                return True
            return bool(include_unassigned) and not row_entry and str(row.get("model") or "") == model

        if hasattr(db, "_get_conn"):
            conn = db._get_conn()
            try:
                cols = ("SELECT COALESCE(SUM(input_tokens),0), COALESCE(SUM(output_tokens),0), "
                        "COALESCE(SUM(provider_cost_cents),0), COALESCE(SUM(cost_usd),0) "
                        "FROM usage_events WHERE ")
                sql = cols + " AND ".join(where)
                rows = conn.execute(sql, tuple(params)).fetchall()
                if rows:
                    total_in, total_out = int(rows[0][0]), int(rows[0][1])
                    total_cost_cents = int(rows[0][2])
                    total_cost_usd = float(rows[0][3] or 0)
            finally:
                conn.close()
        elif hasattr(db, "get_raw_client"):
            q = db.get_raw_client().table("usage_events") \
                .select("input_tokens, output_tokens, provider_cost_cents, cost_usd, entry_id, model")
            if not want_global:
                q = q.eq("user_id", user_id)
            q = q.eq("model", model)
            if provider:
                q = q.eq("provider", provider)
            resp = q.execute()
            for row in resp.data:
                if not _legacy_match(row):
                    continue
                total_in += row.get("input_tokens", 0)
                total_out += row.get("output_tokens", 0)
                total_cost_cents += row.get("provider_cost_cents", 0)
                total_cost_usd += row.get("cost_usd", 0) or 0

        return {
            "error": None,
            "scope": "global" if want_global else "user",
            "total_input_tokens": total_in,
            "total_output_tokens": total_out,
            "total_cost_cents": total_cost_cents,
            "total_cost_usd": round(total_cost_usd, 6),
        }
    except Exception as e:
        return {"error": str(e), "total_input_tokens": 0, "total_output_tokens": 0,
                "total_cost_cents": 0, "total_cost_usd": 0.0}


# The analytics chart can break the same daily totals down by model, by chat
# session, or by agent. ``agent_models`` is the deliberately fixed two-dimensional
# variant used by My Agents: every returned series has both an agent and a model.
# This mapping is the ONLY source of a caller-used SQL identifier — a caller-supplied
# column name must never reach the query text.
_ANALYTICS_GROUP_COLUMNS = {"models": "model", "sessions": "session_id", "agents": "agent_id"}
_ANALYTICS_AGENT_MODELS = "agent_models"


def _analytics_agent_model_key(agent_id: str, model: str) -> str:
    """Make a reversible, collision-free client series key for one agent/model."""
    return f"{agent_id}\x1f{model}"


def _analytics_series_labels(user_id: str, grouping: str, keys) -> dict:
    """Resolve display labels (session titles / agent names) for grouped series.

    Neither source lives in the analytics handle: chat sessions are rows in the
    per-user authority store, and agent names come from the app catalogue's
    ``agent_catalog`` table. Labels are a nicety, so any failure degrades to the
    raw series key on the client rather than breaking the chart.
    """
    labels = {}
    wanted = [str(key) for key in keys if key]
    if not wanted or grouping not in ("sessions", "agents"):
        return labels
    try:
        if grouping == "sessions":
            from app.db import get_user_db
            source, table, id_column, name_column = get_user_db(user_id), "sessions", "id", "title"
        else:
            from app.db import get_app_db
            source, table, id_column, name_column = get_app_db(), "agent_catalog", "agent_id", "name"
        for start in range(0, len(wanted), 500):
            chunk = wanted[start:start + 500]
            if hasattr(source, "_get_conn"):
                conn = source._get_conn()
                try:
                    marks = ",".join("?" for _ in chunk)
                    rows = conn.execute(
                        f"SELECT {id_column}, {name_column} FROM {table} WHERE {id_column} IN ({marks})",
                        tuple(chunk),
                    ).fetchall()
                finally:
                    conn.close()
            elif hasattr(source, "get_raw_client"):
                resp = source.get_raw_client().table(table).select(f"{id_column}, {name_column}").in_(id_column, chunk).execute()
                rows = [(row.get(id_column), row.get(name_column)) for row in resp.data or []]
            else:
                return labels
            for key, label in rows:
                text = str(label or "").strip()
                if key and text:
                    labels[str(key)] = text
    except Exception as e:
        logger.warning("analytics series labels (%s) unavailable: %s", grouping, e)
    return labels


@router.get("/model-usage-analytics")
async def get_model_usage_analytics(
    # 0 means all available history, beginning at the oldest matching event.
    days: int = Query(0, ge=0, le=3650),
    scope: str = Query("global"),
    interval: str = Query("day", pattern="^(minute|hour|day)$"),
    # models | sessions | agents | agent_models — what the daily totals are
    # broken down by. agent_models is the nested chart's fixed agent/model matrix.
    group_by: str = Query("models"),
    agent_id: str = Query(""),
    authorization: Optional[str] = Header(None),
    token: Optional[str] = Query(None),
):
    """Time-bucketed all-model usage for the catalog overview chart.

    Global totals are deliberately available only to administrators; other
    callers see their own activity. Empty calendar days are retained so the
    chart has an honest, comparable time axis.
    """
    grouping = str(group_by or "").strip().lower()
    if grouping != _ANALYTICS_AGENT_MODELS and grouping not in _ANALYTICS_GROUP_COLUMNS:
        grouping = "models"
    group_column = _ANALYTICS_GROUP_COLUMNS.get(grouping)
    nested_agent_models = grouping == _ANALYTICS_AGENT_MODELS

    user_id = _resolve_user_id(authorization or "", token or "")
    if not user_id:
        return {"error": "not_authenticated", "group_by": grouping, "days": [], "models": [], "series_labels": {}}

    now = datetime.now(timezone.utc)
    end_day = now.date()
    # Resolution never changes the selected calendar range. Include empty slots
    # through the end of today, just as the daily chart does.
    subday = interval in ("minute", "hour")
    step = timedelta(minutes=1) if interval == "minute" else timedelta(hours=1)
    time_format = "%Y-%m-%dT%H:%M"
    bucket_sql = "strftime('%Y-%m-%dT%H:%M', created_at)" if interval == "minute" else "strftime('%Y-%m-%dT%H:00', created_at)" if interval == "hour" else "substr(created_at,1,10)"
    all_history = days == 0
    start_day = None if all_history else end_day - timedelta(days=days - 1)
    buckets = {}
    if start_day is not None:
        buckets = {
            (start_day + timedelta(days=offset)).isoformat(): {"date": (start_day + timedelta(days=offset)).isoformat(), "models": {}}
            for offset in range(days)
        }
    try:
        from app.db import get_app_db
        db = get_app_db()
        # An individual agent panel always uses the viewing user's own events for
        # that one agent, matching its model table's Usage column. The global
        # catalog is the only surface that may request administrator-wide totals.
        requested_agent_id = str(agent_id or "").strip()
        want_global = not requested_agent_id and scope == "global" and await _is_admin(db, user_id)
        rows = []
        # The nested My Agents chart is a roster view, not an execution-worker
        # view. Clones are stored as agents too, but have status='clone'; only
        # status='active' represents the main/default agent or a saved agent.
        active_roster_ids = set()
        if hasattr(db, "_get_conn"):
            conn = db._get_conn()
            try:
                if nested_agent_models:
                    active_roster_ids = {
                        str(row[0]) for row in conn.execute(
                            "SELECT id FROM agents WHERE status = 'active'"
                        ).fetchall()
                    }
                # `group_column` comes from the fixed mapping above, never from
                # the caller. The session/agent groupings intentionally skip rows
                # that belong to no chat — background calls carry no session_id.
                required_columns = "agent_id IS NOT NULL AND agent_id != '' AND model IS NOT NULL AND model != ''" if nested_agent_models else f"{group_column} IS NOT NULL AND {group_column} != ''"
                where = f"WHERE {required_columns}"
                params = []
                if start_day is not None:
                    where += " AND created_at>=?"
                    params.append(f"{start_day.isoformat()} 00:00:00")
                if requested_agent_id:
                    where += " AND user_id=? AND agent_id=?"
                    params.extend([user_id, requested_agent_id])
                elif not want_global:
                    where += " AND user_id=?"
                    params.append(user_id)
                if nested_agent_models:
                    rows = [
                        (str(day or ""), _analytics_agent_model_key(str(agent or ""), str(model or "")), tokens, cost)
                        for day, agent, model, tokens, cost in conn.execute(
                            f"SELECT {bucket_sql} AS day, agent_id, model, "
                            "COALESCE(SUM(input_tokens),0) + COALESCE(SUM(output_tokens),0), "
                            "COALESCE(SUM(cost_usd),0) FROM usage_events " + where + " GROUP BY day, agent_id, model ORDER BY day, agent_id, model",
                            tuple(params),
                        ).fetchall()
                        if str(agent or "") in active_roster_ids
                    ]
                else:
                    rows = conn.execute(
                        f"SELECT {bucket_sql} AS day, {group_column} AS series, "
                        "COALESCE(SUM(input_tokens),0) + COALESCE(SUM(output_tokens),0), "
                        "COALESCE(SUM(cost_usd),0) FROM usage_events " + where + " GROUP BY day, series ORDER BY day, series",
                        tuple(params),
                    ).fetchall()
            finally:
                conn.close()
        elif hasattr(db, "get_raw_client"):
            if nested_agent_models:
                try:
                    roster_rows = db.get_raw_client().table("agents").select("id").eq("status", "active").execute().data or []
                    active_roster_ids = {str(row.get("id") or "") for row in roster_rows}
                except Exception as e:
                    # Fail closed: rendering no nested groups is preferable to
                    # accidentally presenting runtime clones as saved agents.
                    logger.warning("nested model analytics roster lookup failed: %s", e)
                    active_roster_ids = set()
            fields = "created_at, model, agent_id, input_tokens, output_tokens, cost_usd" if nested_agent_models else f"created_at, model, {group_column}, input_tokens, output_tokens, cost_usd"
            query = db.get_raw_client().table("usage_events").select(fields)
            if start_day is not None:
                query = query.gte("created_at", f"{start_day.isoformat()}T00:00:00+00:00")
            if requested_agent_id:
                query = query.eq("user_id", user_id).eq("agent_id", requested_agent_id)
            elif not want_global:
                query = query.eq("user_id", user_id)
            aggregates = {}
            for row in query.execute().data or []:
                agent = str(row.get("agent_id") or "")
                model = str(row.get("model") or "")
                if nested_agent_models and agent not in active_roster_ids:
                    continue
                series = _analytics_agent_model_key(agent, model) if nested_agent_models and agent and model else str(row.get(group_column) or "")
                timestamp = str(row.get("created_at") or "")
                if subday:
                    event_time = datetime.fromisoformat(timestamp.replace("Z", "+00:00"))
                    if event_time.tzinfo is None:
                        event_time = event_time.replace(tzinfo=timezone.utc)
                    event_time = event_time.astimezone(timezone.utc).replace(second=0, microsecond=0)
                    if interval == "hour":
                        event_time = event_time.replace(minute=0)
                    day = event_time.strftime(time_format)
                else:
                    day = timestamp[:10]
                if not series or (not subday and buckets and day not in buckets):
                    continue
                current = aggregates.setdefault((day, series), [0, 0.0])
                current[0] += int(row.get("input_tokens") or 0) + int(row.get("output_tokens") or 0)
                current[1] += float(row.get("cost_usd") or 0)
            rows = [(day, series, tokens, cost) for (day, series), (tokens, cost) in aggregates.items()]

        history_days = sorted({str(row[0] or "") for row in rows if str(row[0] or "")})
        if all_history and history_days:
            start_day = datetime.fromisoformat(history_days[0]).date()
        if start_day is not None:
            if subday:
                start_time = datetime.combine(start_day, datetime.min.time(), tzinfo=timezone.utc)
                stop_time = datetime.combine(end_day + timedelta(days=1), datetime.min.time(), tzinfo=timezone.utc)
                bucket_count = int((stop_time - start_time) / step)
                buckets = {
                    (start_time + step * offset).strftime(time_format): {
                        "date": (start_time + step * offset).strftime(time_format), "models": {}
                    } for offset in range(bucket_count)
                }
            else:
                buckets = {
                    (start_day + timedelta(days=offset)).isoformat(): {"date": (start_day + timedelta(days=offset)).isoformat(), "models": {}}
                    for offset in range((end_day - start_day).days + 1)
                }

        # `days[].models` remains the wire key for the generic series container so
        # the existing chart consumers keep working unchanged; under the sessions
        # and agents groupings it holds session ids / agent ids, not model names.
        series_totals = {}
        for day, series, tokens, cost in rows or []:
            day, series = str(day or ""), str(series or "")
            if day not in buckets or not series:
                continue
            series_totals[series] = series_totals.get(series, 0) + int(tokens or 0)
            buckets[day]["models"][series] = {"usage_tokens": int(tokens or 0), "cost_usd": round(float(cost or 0), 6)}
        # Sessions are far too numerous to read in id order, so lead with the ones
        # that actually consumed the most — the first colours then mean something.
        if grouping == "sessions":
            ordered = sorted(series_totals, key=lambda name: (-series_totals[name], name))
        else:
            ordered = sorted(series_totals)
        series_meta = {}
        if nested_agent_models:
            agent_ids = []
            for series in ordered:
                agent, model = series.split("\x1f", 1)
                series_meta[series] = {"agent_id": agent, "model": model}
                if agent not in agent_ids:
                    agent_ids.append(agent)
            agent_labels = _analytics_series_labels(user_id, "agents", agent_ids)
            for meta in series_meta.values():
                meta["agent_label"] = agent_labels.get(meta["agent_id"], meta["agent_id"])
            series_labels = {
                series: f"{meta['agent_label']} · {meta['model']}"
                for series, meta in series_meta.items()
            }
        elif grouping == "sessions":
            series_labels = _analytics_series_labels(user_id, "sessions", ordered)
        elif grouping == "agents":
            series_labels = _analytics_series_labels(user_id, "agents", ordered)
        else:
            series_labels = {}
        response_scope = "agent" if requested_agent_id else ("global" if want_global else "user")
        return {
            "error": None,
            "scope": response_scope,
            "interval": interval,
            "timezone": "UTC",
            "group_by": grouping,
            "days": list(buckets.values()),
            "models": ordered,
            "series_labels": series_labels,
            "series_meta": series_meta,
        }
    except Exception as e:
        logger.warning("model usage analytics failed: %s", e)
        return {"error": str(e), "group_by": grouping, "days": [], "models": [], "series_labels": {}}


@router.delete("/model-usage-analytics")
async def clear_model_usage_analytics(
    model: str = Query(""),
    scope: str = Query("global"),
    agent_id: str = Query(""),
    authorization: Optional[str] = Header(None),
    token: Optional[str] = Query(None),
):
    """Clear only one model's usage-event history for the active analytics scope.

    Model configuration is intentionally never changed here. A configured model
    will naturally reappear in analytics when a later token-usage event is saved.
    """
    user_id = _resolve_user_id(authorization or "", token or "")
    if not user_id:
        return {"error": "not_authenticated"}
    model = str(model or "").strip()
    if not model:
        return {"error": "model required"}

    try:
        from app.db import get_app_db
        db = get_app_db()
        requested_agent_id = str(agent_id or "").strip()
        want_global = not requested_agent_id and scope == "global" and await _is_admin(db, user_id)
        deleted = 0
        if hasattr(db, "_get_conn"):
            conn = db._get_conn()
            try:
                where = "WHERE model=?"
                params = [model]
                if requested_agent_id:
                    where += " AND user_id=? AND agent_id=?"
                    params.extend([user_id, requested_agent_id])
                elif not want_global:
                    where += " AND user_id=?"
                    params.append(user_id)
                cursor = conn.execute("DELETE FROM usage_events " + where, tuple(params))
                deleted = max(0, int(cursor.rowcount or 0))
                conn.commit()
            finally:
                conn.close()
        elif hasattr(db, "get_raw_client"):
            query = db.get_raw_client().table("usage_events").delete().eq("model", model)
            if requested_agent_id:
                query = query.eq("user_id", user_id).eq("agent_id", requested_agent_id)
            elif not want_global:
                query = query.eq("user_id", user_id)
            query.execute()
        else:
            return {"error": "unsupported database"}
        response_scope = "agent" if requested_agent_id else ("global" if want_global else "user")
        return {"ok": True, "deleted": deleted, "scope": response_scope}
    except Exception as e:
        logger.warning("model usage analytics history clear failed: %s", e)
        return {"error": str(e)}


# ── Session activity lanes (Sessions page overview chart) ────────────────────
# The model chart buckets by CALENDAR DAY, which cannot express a session's own
# internal rhythm. `usage_events` already stores one row per LLM call with both a
# session id and a wall-clock timestamp, so a session's real activity timeline —
# where it was busy and where it went quiet — can be rebuilt from that ledger
# alone. It is the same per-call source the row totals and the model chart read,
# so a lane and its table row can never disagree.
_SESSION_ACTIVITY_MAX_POINTS = 400


def _activity_timestamp_ms(raw) -> Optional[int]:
    """Parse a ``usage_events.created_at`` into epoch milliseconds (UTC).

    SQLite writes the space-separated form produced by ``datetime('now')`` while
    Postgres returns a ``T``-separated offset form; both must land on ONE axis, so
    the separator is normalised and a naive value is read as UTC rather than as
    the server's local zone.
    """
    text = str(raw or "").strip()
    if not text:
        return None
    text = text.replace(" ", "T", 1)
    if text.endswith("Z"):
        text = text[:-1] + "+00:00"
    try:
        parsed = datetime.fromisoformat(text)
    except ValueError:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return int(parsed.timestamp() * 1000)


# Pinned rows the picker could not order by an explicit sort_order sort last
# among themselves — ui/shared/js/session-ordering.js uses Number.MAX_SAFE_INTEGER
# for exactly that case, so mirror it here.
_MAX_SORT_ORDER = 9007199254740991


def _session_picker_order(user_id: str, session_ids) -> dict:
    """Picker ordering inputs for activity-chart lanes.

    The chart must read in the same order as the chat session picker (and the
    sessions table), so every lane inherits its session row's ``pinned`` /
    ``sort_order`` plus the ``activity_at`` the picker itself sorts by. That
    expression is the one the session list orders on (see
    ``activity_expr`` in app/api/db_viewer.py): a terminal run's completion time
    first, then the newest interaction, then the session row's own timestamps.
    Because a terminal run is already folded in here, a lane can be ordered by
    ui/shared/js/session-ordering.js unchanged — its ``run_updated_at`` branch
    resolves to the same instant, so the chart and the picker cannot disagree.

    Picker metadata is a nicety: an id with no session row (deleted or recycled
    out of the catalog) or a non-SQLite authority store simply keeps the caller's
    ledger fallback, and the chart still orders by real activity.
    """
    meta: dict = {}
    wanted = [str(sid) for sid in session_ids if sid]
    if not wanted:
        return meta
    try:
        from app.db import get_user_db
        db = get_user_db(user_id)
        if not hasattr(db, "_get_conn"):
            return meta
        activity_expr = (
            "COALESCE((SELECT MAX(sr.updated_at) FROM session_runs sr "
            "WHERE sr.session_id = s.id AND sr.status IN ('complete','interrupted','error')), "
            "(SELECT MAX(i.created_at) FROM interactions i WHERE i.session_id = s.id), "
            "s.updated_at, s.created_at)"
        )
        for start in range(0, len(wanted), 500):
            chunk = wanted[start:start + 500]
            conn = db._get_conn()
            try:
                # pinned/sort_order postdate the oldest authority stores.
                columns = {row[1] for row in conn.execute("PRAGMA table_info(sessions)").fetchall()}
                pinned = "s.pinned" if "pinned" in columns else "0"
                sort_order = "s.sort_order" if "sort_order" in columns else "NULL"
                status = "s.status" if "status" in columns else "NULL"
                marks = ",".join("?" for _ in chunk)
                rows = conn.execute(
                    f"SELECT s.id, {activity_expr}, {pinned}, {sort_order}, {status} "
                    f"FROM sessions s WHERE s.id IN ({marks})",
                    tuple(chunk),
                ).fetchall()
            finally:
                conn.close()
            for row in rows or []:
                sid = str(row[0] or "")
                if not sid:
                    continue
                raw_order = row[3]
                meta[sid] = {
                    "activity_ms": _activity_timestamp_ms(row[1]),
                    "pinned": bool(row[2]),
                    "sort_order": float(raw_order) if isinstance(raw_order, (int, float)) else None,
                    "recycled": str(row[4] or "") == "recycled",
                }
    except Exception as e:
        logger.warning("session activity picker order unavailable: %s", e)
    return meta


@router.get("/session-activity")
async def get_session_activity(
    days: int = Query(0, ge=0, le=3650),
    limit: int = Query(120, ge=1, le=500),
    agent_id: str = Query(""),
    authorization: Optional[str] = Header(None),
    token: Optional[str] = Query(None),
):
    """One activity lane per session for the Sessions page overview chart.

    Each session is returned as its wall-clock span plus every LLM call inside it
    as an ``[offset_ms, tokens]`` pair measured from that session's OWN first
    call, so the chart can plot real activity against time and leave a stretch
    with no calls genuinely blank. ``days`` = 0 means all available history.

    Only the ``limit`` highest-ranked sessions come back — an account with
    thousands of sessions must not make the payload unbounded — and a very chatty
    session's call list is coalesced into time buckets. The chart draws one lane
    per 25px and scrolls horizontally, so ``limit`` is the length of the scrollable
    strip rather than a count of what fits on screen. Sessions are ranked and
    returned in the SAME order as the chat session picker: pinned sessions first
    in their user-defined order, then newest activity. Each lane carries the
    picker's ``pinned`` / ``sort_order`` / ``activity_at`` so the client can apply
    ui/shared/js/session-ordering.js directly rather than approximating it.
    """
    user_id = _resolve_user_id(authorization or "", token or "")
    if not user_id:
        return {"error": "not_authenticated", "sessions": []}

    requested_agent_id = str(agent_id or "").strip()
    start_dt = None if days == 0 else datetime.now(timezone.utc) - timedelta(days=days)

    # usage_events lives in the app/control plane (billing writes there), not in
    # the per-user authority store, so it is read through get_app_db like the
    # model analytics endpoint above.
    events = []   # (session_id, ts_ms, agent_id, tokens)
    try:
        from app.db import get_app_db
        db = get_app_db()
        # A row with no session_id is a background call (title generation,
        # embeddings, …) that belongs to no chat and has no lane to sit in.
        where = "user_id=? AND session_id IS NOT NULL AND session_id != ''"
        params = [user_id]
        if requested_agent_id:
            where += " AND agent_id=?"
            params.append(requested_agent_id)
        if start_dt is not None:
            where += " AND created_at>=?"
            params.append(start_dt.strftime("%Y-%m-%d %H:%M:%S"))

        if hasattr(db, "_get_conn"):
            conn = db._get_conn()
            try:
                rows = conn.execute(
                    "SELECT session_id, created_at, agent_id, "
                    "COALESCE(input_tokens,0) + COALESCE(output_tokens,0) "
                    "FROM usage_events WHERE " + where + " ORDER BY created_at ASC",
                    tuple(params),
                ).fetchall()
            finally:
                conn.close()
            for row in rows or []:
                sid = str(row[0] or "")
                ts = _activity_timestamp_ms(row[1])
                if sid and ts is not None:
                    events.append((sid, ts, str(row[2] or ""), int(row[3] or 0)))
        elif hasattr(db, "get_raw_client"):
            query = (db.get_raw_client().table("usage_events")
                     .select("session_id, created_at, agent_id, input_tokens, output_tokens, source")
                     .eq("user_id", user_id))
            if requested_agent_id:
                query = query.eq("agent_id", requested_agent_id)
            if start_dt is not None:
                query = query.gte("created_at", start_dt.strftime("%Y-%m-%d %H:%M:%S"))
            for row in query.execute().data or []:
                sid = str(row.get("session_id") or "")
                ts = _activity_timestamp_ms(row.get("created_at"))
                if not sid or ts is None:
                    continue
                tokens = int(row.get("input_tokens") or 0) + int(row.get("output_tokens") or 0)
                events.append((sid, ts, str(row.get("agent_id") or ""), tokens))
        else:
            return {"error": "unsupported database", "sessions": []}
    except Exception as e:
        logger.warning("session activity lanes failed: %s", e)
        return {"error": str(e), "sessions": []}

    lanes: dict = {}
    for sid, ts, aid, tokens in events:
        lane = lanes.get(sid)
        if lane is None:
            lane = lanes[sid] = {
                "session_id": sid, "agent_id": aid, "start_ms": ts,
                "end_ms": ts, "total_tokens": 0, "calls": [],
            }
        if ts < lane["start_ms"]:
            lane["start_ms"] = ts
        if ts > lane["end_ms"]:
            lane["end_ms"] = ts
        if not lane["agent_id"] and aid:
            lane["agent_id"] = aid
        lane["total_tokens"] += tokens
        lane["calls"].append((ts, tokens))

    # Rank EXACTLY as the chat session picker does — pinned sessions first in
    # their user-defined order, then newest activity, ties broken by session id —
    # so the cap keeps the same sessions the picker would show, in the same
    # order. ui/shared/js/session-ordering.js (compareSessionsByPinnedOrder) is
    # the client-side authority for this ranking and reads the fields attached
    # below; this mirror exists only to choose the cap. A lane always resolves to
    # a real activity instant (the ledger's last call when the session row is
    # gone), so the comparator never falls through to an omitted field.
    picker = _session_picker_order(user_id, lanes.keys())
    for sid, lane in lanes.items():
        info = picker.get(sid) or {}
        activity_ms = info.get("activity_ms")
        lane["order"] = {
            "pinned": bool(info.get("pinned")) and not info.get("recycled"),
            "sort_order": info.get("sort_order"),
            "recycled": bool(info.get("recycled")),
            "activity_ms": int(lane["end_ms"] if activity_ms is None else activity_ms),
        }

    recent = sorted(
        lanes.values(),
        key=lambda lane: (
            0 if lane["order"]["pinned"] else 1,
            (lane["order"]["sort_order"]
             if lane["order"]["sort_order"] is not None else _MAX_SORT_ORDER)
            if lane["order"]["pinned"] else 0,
            -lane["order"]["activity_ms"],
            lane["session_id"],
        ),
    )[:limit]

    sessions = []
    for lane in recent:
        start = lane["start_ms"]
        calls = sorted(lane["calls"])
        if len(calls) <= _SESSION_ACTIVITY_MAX_POINTS:
            points = [[int(ts - start), int(tokens)] for ts, tokens in calls]
        else:
            # Coalesce a very long call list into equal time buckets so one busy
            # session cannot dwarf the rest of the payload.
            span = max(1, lane["end_ms"] - start)
            size = span / _SESSION_ACTIVITY_MAX_POINTS
            acc: dict = {}
            for ts, tokens in calls:
                index = min(_SESSION_ACTIVITY_MAX_POINTS - 1, int((ts - start) / size))
                acc[index] = acc.get(index, 0) + int(tokens)
            points = [[int((index + 0.5) * size), acc[index]] for index in sorted(acc)]
        sessions.append({
            "session_id": lane["session_id"],
            "agent_id": lane["agent_id"],
            "started_at": datetime.fromtimestamp(start / 1000, tz=timezone.utc).isoformat(),
            "ended_at": datetime.fromtimestamp(lane["end_ms"] / 1000, tz=timezone.utc).isoformat(),
            "duration_ms": int(lane["end_ms"] - start),
            "total_tokens": int(lane["total_tokens"]),
            "call_count": len(calls),
            "points": points,
            # Picker ordering inputs — consumed verbatim by the shared comparator
            # in ui/shared/js/session-ordering.js (see the rank above).
            "pinned": lane["order"]["pinned"],
            "recycled": lane["order"]["recycled"],
            "sort_order": lane["order"]["sort_order"],
            "activity_at": datetime.fromtimestamp(
                lane["order"]["activity_ms"] / 1000, tz=timezone.utc).isoformat(),
        })

    # Session titles are a nicety — the same resolver the model chart's Sessions
    # grouping uses, so an unresolvable id simply keeps its short raw form.
    labels = _analytics_series_labels(user_id, "sessions", [s["session_id"] for s in sessions])
    for entry in sessions:
        entry["title"] = labels.get(entry["session_id"], "")
    return {"error": None, "days": days, "sessions": sessions}


@router.get("/model-usage-history")
async def get_model_usage_history(
    model: str = Query(""),
    provider: str = Query(""),
    agent_id: str = Query(""),
    days: int = Query(90, ge=7, le=90),
    authorization: Optional[str] = Header(None),
    token: Optional[str] = Query(None),
):
    """Daily cost history for one model, scoped to the current user's usage.

    When ``agent_id`` is supplied this is additionally scoped to that one agent,
    matching the per-agent Config → Model usage total. Empty calendar days are
    returned as zero buckets so the client can draw an honest time-series axis.
    """
    user_id = _resolve_user_id(authorization or "", token or "")
    if not user_id:
        return {"error": "not_authenticated", "days": [], "total_cost_usd": 0.0}
    if not model:
        return {"error": None, "days": [], "total_cost_usd": 0.0}

    end_day = datetime.now(timezone.utc).date()
    start_day = end_day - timedelta(days=days - 1)
    buckets = {
        (start_day + timedelta(days=offset)).isoformat(): {
            "date": (start_day + timedelta(days=offset)).isoformat(),
            "cost_usd": 0.0,
        }
        for offset in range(days)
    }
    try:
        from app.db import get_app_db
        db = get_app_db()
        if hasattr(db, "_get_conn"):
            conn = db._get_conn()
            try:
                sql = (
                    "SELECT substr(created_at,1,10) AS day, COALESCE(SUM(cost_usd),0) "
                    "FROM usage_events WHERE user_id=? AND model=? AND created_at>=?"
                )
                params = [user_id, model, f"{start_day.isoformat()} 00:00:00"]
                if provider:
                    sql += " AND provider=?"
                    params.append(provider)
                if agent_id:
                    sql += " AND agent_id=?"
                    params.append(agent_id)
                sql += " GROUP BY substr(created_at,1,10) ORDER BY day"
                rows = conn.execute(sql, tuple(params)).fetchall()
                for day, cost in rows or []:
                    if day in buckets:
                        buckets[day]["cost_usd"] = round(float(cost or 0), 6)
            finally:
                conn.close()
        elif hasattr(db, "get_raw_client"):
            query = db.get_raw_client().table("usage_events") \
                .select("created_at, cost_usd") \
                .eq("user_id", user_id).eq("model", model) \
                .gte("created_at", f"{start_day.isoformat()}T00:00:00+00:00")
            if provider:
                query = query.eq("provider", provider)
            if agent_id:
                query = query.eq("agent_id", agent_id)
            for row in query.execute().data or []:
                day = str(row.get("created_at") or "")[:10]
                if day in buckets:
                    buckets[day]["cost_usd"] += float(row.get("cost_usd") or 0)
        points = list(buckets.values())
        for point in points:
            point["cost_usd"] = round(float(point["cost_usd"]), 6)
        return {
            "error": None,
            "days": points,
            "total_cost_usd": round(sum(point["cost_usd"] for point in points), 6),
        }
    except Exception as e:
        logger.warning("model usage history failed: %s", e)
        return {"error": str(e), "days": [], "total_cost_usd": 0.0}


@router.get("/session-cost")
async def get_session_cost(
    session_id: str = Query(""),
    authorization: Optional[str] = Header(None),
    token: Optional[str] = Query(None),
):
    """Authoritative session cost: sum of the locked-in per-call cost_usd for one
    session, plus a per-model breakdown. Stays correct across model switches
    because each call already carries its own model's price. The chat footer
    reconciles its live running total against this on session load."""
    user_id = _resolve_user_id(authorization or "", token or "")
    if not user_id:
        return {"error": "not_authenticated", "total_cost_usd": 0.0, "total_credit_units": 0.0, "by_model": {}, "context_tokens": 0}
    if not session_id:
        return {"error": None, "total_cost_usd": 0.0, "total_credit_units": 0.0, "by_model": {}, "context_tokens": 0}

    context_tokens = 0  # latest exact provider input count for this chat
    context_model = ""  # model from that same call — keeps restored count/label atomic
    context_created_at = ""
    try:
        from app.db import get_app_db
        db = get_app_db()
        by_model: dict = {}
        total = 0.0
        total_credits = 0.0
        has_unknown = False

        if hasattr(db, "_get_conn"):
            conn = db._get_conn()
            try:
                usage_columns = {str(row[1]) for row in conn.execute("PRAGMA table_info(usage_events)").fetchall()}
                credit_sum = "COALESCE(SUM(credit_units),0)" if "credit_units" in usage_columns else "0"
                rows = conn.execute(
                    "SELECT model, COALESCE(SUM(input_tokens),0), COALESCE(SUM(output_tokens),0), "
                    f"COALESCE(SUM(cost_usd),0), {credit_sum} FROM usage_events "
                    "WHERE user_id=? AND session_id=? GROUP BY model",
                    (user_id, session_id),
                ).fetchall()
                for r in rows or []:
                    m = r[0] or ""
                    inp, out, cost = int(r[1] or 0), int(r[2] or 0), float(r[3] or 0)
                    credits = float(r[4] or 0)
                    if m:
                        by_model[m] = {"input": inp, "output": out,
                                       "total": inp + out, "cost_usd": round(cost, 6),
                                       "credit_units": round(credits, 6)}
                    total += cost
                    total_credits += credits
                _unk = conn.execute(
                    "SELECT COUNT(*) FROM usage_events "
                    "WHERE user_id=? AND session_id=? AND cost_source='unknown'",
                    (user_id, session_id),
                ).fetchone()
                has_unknown = bool(_unk and _unk[0] > 0)
                _ctx = conn.execute(
                    "SELECT input_tokens, model, created_at FROM usage_events "
                    "WHERE user_id=? AND session_id=? AND source='chat' "
                    "ORDER BY created_at DESC, id DESC LIMIT 1",
                    (user_id, session_id),
                ).fetchone()
                if _ctx and _ctx[0] is not None:
                    context_tokens = int(_ctx[0])
                    context_model = str(_ctx[1] or "")
                    context_created_at = str(_ctx[2] or "")
                # ── Background summariser work counts toward the session ──
                # Mirrors db_viewer: compaction summariser + summary-lane
                # summarizer calls are real LLM calls for this session. New rows
                # carry session_id+user_id (already in by_model/total above);
                # UNLINKED historical rows (session_id NULL) are credited by time
                # window — each system:compaction / system:closer / system:summary /
                # system:summary message claims the nearest
                # unlinked call at/before it. Kept in lockstep with the
                # /session-messages totals so the pill and the cost panel agree.
                try:
                    _uconn = None
                    try:
                        from app.db import get_user_db
                        _ub = get_user_db(user_id)
                        if hasattr(_ub, "_get_conn"):
                            _uconn = _ub._get_conn()
                            for _bg_srcs, _notice_srcs in (
                                (("background:compact",), ("system:compaction",)),
                                (("background:overview", "background:summarizer", "background:closer"),
                                 ("system:overview", "system:summary", "system:closer")),
                            ):
                                _nph = ",".join("?" * len(_notice_srcs))
                                _notices = [str(r[0]) for r in _uconn.execute(
                                    "SELECT created_at FROM interactions "
                                    f"WHERE session_id=? AND source IN ({_nph}) "
                                    "ORDER BY created_at ASC",
                                    (session_id, *_notice_srcs)).fetchall()]
                                if not _notices:
                                    continue
                                _sph = ",".join("?" * len(_bg_srcs))
                                _rows = conn.execute(
                                    "SELECT id, model, created_at, input_tokens, output_tokens, cost_usd, cost_source "
                                    f"FROM usage_events WHERE source IN ({_sph}) AND session_id IS NULL "
                                    "ORDER BY created_at ASC",
                                    (*_bg_srcs,),
                                ).fetchall()
                                # Credit the ONE nearest unlinked call at/before each
                                # notice (each fold/summary owns its own call), never
                                # consuming the whole unlinked pool — those rows are
                                # shared across all pre-linkage sessions.
                                _credited_ids = set()
                                for _ts in _notices:
                                    _best = None
                                    for _rr in _rows:
                                        if str(_rr[2]) <= _ts:
                                            if _rr[0] not in _credited_ids:
                                                _best = _rr
                                        else:
                                            break
                                    if _best is not None:
                                        _credited_ids.add(_best[0])
                                        _m = _best[1] or ""
                                        _inp, _out = int(_best[3] or 0), int(_best[4] or 0)
                                        _cost = float(_best[5] or 0)
                                        if _m:
                                            _cur = by_model.setdefault(
                                                _m, {"input": 0, "output": 0, "total": 0, "cost_usd": 0.0})
                                            _cur["input"] += _inp
                                            _cur["output"] += _out
                                            _cur["total"] += _inp + _out
                                            _cur["cost_usd"] = round(_cur["cost_usd"] + _cost, 6)
                                        total += _cost
                                        if _best[6] == "unknown":
                                            has_unknown = True
                    finally:
                        if _uconn is not None:
                            _uconn.close()
                except Exception:
                    pass
            finally:
                conn.close()
        elif hasattr(db, "get_raw_client"):
            resp = db.get_raw_client().table("usage_events") \
                .select("model, input_tokens, output_tokens, cost_usd, credit_units") \
                .eq("user_id", user_id).eq("session_id", session_id).execute()
            for row in resp.data or []:
                m = row.get("model") or ""
                inp = row.get("input_tokens", 0) or 0
                out = row.get("output_tokens", 0) or 0
                cost = row.get("cost_usd", 0) or 0
                credits = row.get("credit_units", 0) or 0
                if m:
                    cur = by_model.setdefault(m, {"input": 0, "output": 0, "total": 0, "cost_usd": 0.0, "credit_units": 0.0})
                    cur["input"] += inp
                    cur["output"] += out
                    cur["total"] += inp + out
                    cur["cost_usd"] = round(cur["cost_usd"] + cost, 6)
                    cur["credit_units"] = round(cur["credit_units"] + credits, 6)
                total += cost
                total_credits += credits
            _unk_rows = client.table("usage_events") \
                .select("id") \
                .eq("user_id", user_id).eq("session_id", session_id) \
                .eq("cost_source", "unknown").limit(1).execute()
            has_unknown = bool((_unk_rows.data or []))
            _ctx_rows = client.table("usage_events").select("input_tokens, model, created_at") \
                .eq("user_id", user_id).eq("session_id", session_id) \
                .eq("source", "chat") \
                .order("created_at", desc=True).order("id", desc=True).limit(1).execute()
            _ctx_row = (_ctx_rows.data or [{}])[0]
            context_tokens = int(_ctx_row.get("input_tokens") or 0)
            context_model = str(_ctx_row.get("model") or "")
            context_created_at = str(_ctx_row.get("created_at") or "")

        from app.agent.compaction import projected_context_tokens
        context_tokens = await projected_context_tokens(
            user_id, session_id, context_tokens, context_created_at,
        )
        return {"error": None, "total_cost_usd": round(total, 6),
                "total_credit_units": round(total_credits, 6), "by_model": by_model,
                "has_unknown": has_unknown, "context_tokens": context_tokens,
                "context_model": context_model}
    except Exception as e:
        return {"error": str(e), "total_cost_usd": 0.0, "total_credit_units": 0.0,
                "by_model": {}, "context_tokens": 0}


@router.get("/agent-usage")
async def get_agent_usage(
    agent_id: str = Query(""),
    authorization: Optional[str] = Header(None),
    token: Optional[str] = Query(None),
):
    """Usage the CURRENT user has accrued with one agent: a grand total
    (tokens + cost_usd) plus a per-model breakdown, scoped to this (user, agent)
    pair only — not the agent's all-users total, not the user's all-agents total.
    Powers the cumulative cost figure in the agent's configuration tab, so each
    viewer sees what they personally spent with this agent."""
    user_id = _resolve_user_id(authorization or "", token or "")
    if not user_id:
        return {"error": "not_authenticated", "total_cost_usd": 0.0,
                "total_input_tokens": 0, "total_output_tokens": 0, "by_model": {}, "by_entry": {}}
    if not agent_id:
        return {"error": None, "total_cost_usd": 0.0,
                "total_input_tokens": 0, "total_output_tokens": 0, "by_model": {}, "by_entry": {}}

    try:
        from app.db import get_app_db
        db = get_app_db()
        by_model: dict = {}
        # MODEL-ROW-USAGE: the exact saved row (entry_id) that served each call.
        # Two rows can share a model/provider with different credentials/effort/
        # injections, so the table's USAGE cell resolves by_entry first; by_model
        # stays as the legacy fallback for rows that predate entry_id.
        by_entry: dict = {}
        total_cost = 0.0
        total_in = 0
        total_out = 0

        def _accumulate(model_key: str, entry_key: str, inp: int, out: int, cost: float) -> None:
            nonlocal total_in, total_out, total_cost
            total_in += inp
            total_out += out
            total_cost += cost
            if model_key:
                cur = by_model.setdefault(model_key, {"input": 0, "output": 0, "total": 0, "cost_usd": 0.0})
                cur["input"] += inp
                cur["output"] += out
                cur["total"] += inp + out
                cur["cost_usd"] = round(cur["cost_usd"] + cost, 6)
            if entry_key:
                cur = by_entry.setdefault(entry_key, {"input": 0, "output": 0, "total": 0, "cost_usd": 0.0})
                cur["input"] += inp
                cur["output"] += out
                cur["total"] += inp + out
                cur["cost_usd"] = round(cur["cost_usd"] + cost, 6)

        if hasattr(db, "_get_conn"):
            conn = db._get_conn()
            try:
                rows = conn.execute(
                    "SELECT model, entry_id, COALESCE(SUM(input_tokens),0), "
                    "COALESCE(SUM(output_tokens),0), COALESCE(SUM(cost_usd),0) "
                    "FROM usage_events WHERE agent_id=? AND user_id=? "
                    "GROUP BY model, entry_id",
                    (agent_id, user_id),
                ).fetchall()
                for r in rows or []:
                    _accumulate(str(r[0] or ""), str(r[1] or ""),
                                int(r[2] or 0), int(r[3] or 0), float(r[4] or 0))
            finally:
                conn.close()
        elif hasattr(db, "get_raw_client"):
            resp = db.get_raw_client().table("usage_events") \
                .select("model, entry_id, input_tokens, output_tokens, cost_usd") \
                .eq("agent_id", agent_id).eq("user_id", user_id).execute()
            for row in resp.data or []:
                _accumulate(str(row.get("model") or ""), str(row.get("entry_id") or ""),
                            int(row.get("input_tokens", 0) or 0),
                            int(row.get("output_tokens", 0) or 0),
                            float(row.get("cost_usd", 0) or 0))

        return {
            "error": None,
            "total_cost_usd": round(total_cost, 6),
            "total_input_tokens": total_in,
            "total_output_tokens": total_out,
            "by_model": by_model,
            "by_entry": by_entry,
        }
    except Exception as e:
        return {"error": str(e), "total_cost_usd": 0.0,
                "total_input_tokens": 0, "total_output_tokens": 0,
                "by_model": {}, "by_entry": {}}


@router.post("/agent-usage/reset")
async def reset_agent_usage(
    agent_id: str = Query(""),
    scope: str = Query("all"),
    authorization: Optional[str] = Header(None),
    token: Optional[str] = Query(None),
):
    """Reset the CURRENT user's usage counters for one agent. scope = 'all'
    (delete every row), 'input' / 'output' / 'cost' (zero only that column).
    Scoped to this (user, agent) pair to match the user-scoped figure shown in
    the agent's configuration tab — it never touches other users' usage rows."""
    user_id = _resolve_user_id(authorization or "", token or "")
    if not user_id:
        return {"error": "not_authenticated"}
    if not agent_id:
        return {"error": "agent_id required"}

    try:
        from app.db import get_app_db
        db = get_app_db()
        if hasattr(db, "_get_conn"):
            conn = db._get_conn()
            try:
                if scope == "all":
                    conn.execute("DELETE FROM usage_events WHERE agent_id=? AND user_id=?", (agent_id, user_id))
                elif scope == "input":
                    conn.execute("UPDATE usage_events SET input_tokens=0 WHERE agent_id=? AND user_id=?", (agent_id, user_id))
                elif scope == "output":
                    conn.execute("UPDATE usage_events SET output_tokens=0 WHERE agent_id=? AND user_id=?", (agent_id, user_id))
                elif scope == "cost":
                    conn.execute("UPDATE usage_events SET cost_usd=0 WHERE agent_id=? AND user_id=?", (agent_id, user_id))
                else:
                    return {"error": f"invalid scope: {scope}"}
                conn.commit()
            finally:
                conn.close()
        elif hasattr(db, "get_raw_client"):
            client = db.get_raw_client()
            if scope == "all":
                client.table("usage_events").delete().eq("agent_id", agent_id).eq("user_id", user_id).execute()
            elif scope == "input":
                client.table("usage_events").update({"input_tokens": 0}).eq("agent_id", agent_id).eq("user_id", user_id).execute()
            elif scope == "output":
                client.table("usage_events").update({"output_tokens": 0}).eq("agent_id", agent_id).eq("user_id", user_id).execute()
            elif scope == "cost":
                client.table("usage_events").update({"cost_usd": 0}).eq("agent_id", agent_id).eq("user_id", user_id).execute()
            else:
                return {"error": f"invalid scope: {scope}"}
        else:
            return {"error": "unsupported database"}
        return {"ok": True}
    except Exception as e:
        return {"error": str(e)}


@router.get("/session-model-usage")
async def get_session_model_usage(
    session_id: str = Query(""),
    authorization: Optional[str] = Header(None),
    token: Optional[str] = Query(None),
):
    """Per-model token totals for one session, scoped to the current user.

    usage_events has a session_id column, so we sum by it directly (the old
    interaction_id join was cross-plane and broke once usage_events moved to
    the app plane). Returns a map keyed by model id: {input, output, total}.
    Used by the footer model picker to show each model's session usage."""
    user_id = _resolve_user_id(authorization or "", token or "")
    if not user_id:
        return {"error": "not_authenticated", "usage": {}}
    if not session_id:
        return {"error": None, "usage": {}}

    try:
        from app.db import get_app_db
        db = get_app_db()
        usage: dict = {}

        if hasattr(db, "_get_conn"):
            conn = db._get_conn()
            try:
                rows = conn.execute(
                    "SELECT model, COALESCE(SUM(input_tokens),0), "
                    "COALESCE(SUM(output_tokens),0), COALESCE(SUM(cost_usd),0) "
                    "FROM usage_events "
                    "WHERE user_id=? AND session_id=? "
                    "GROUP BY model",
                    (user_id, session_id),
                ).fetchall()
                for r in rows or []:
                    model = r[0] or ""
                    if not model:
                        continue
                    inp, out = int(r[1] or 0), int(r[2] or 0)
                    usage[model] = {"input": inp, "output": out, "total": inp + out,
                                    "cost_usd": round(float(r[3] or 0), 6)}
            finally:
                conn.close()
        elif hasattr(db, "get_raw_client"):
            client = db.get_raw_client()
            resp = client.table("usage_events") \
                .select("model, input_tokens, output_tokens, cost_usd") \
                .eq("user_id", user_id).eq("session_id", session_id).execute()
            for row in resp.data or []:
                model = row.get("model") or ""
                if not model:
                    continue
                inp = row.get("input_tokens", 0) or 0
                out = row.get("output_tokens", 0) or 0
                cost = row.get("cost_usd", 0) or 0
                cur = usage.setdefault(model, {"input": 0, "output": 0, "total": 0, "cost_usd": 0.0})
                cur["input"] += inp
                cur["output"] += out
                cur["total"] += inp + out
                cur["cost_usd"] = round(cur["cost_usd"] + cost, 6)

        return {"error": None, "usage": usage}
    except Exception as e:
        return {"error": str(e), "usage": {}}


@router.get("/model-info")
async def get_model_info(
    model: str = Query("", alias="model"),
    provider: str = Query("", alias="provider"),
):
    """Full merged metadata for a single model id (context, cost, description…)."""
    try:
        from app import model_catalog
        await model_catalog.ensure_fresh()
        meta = model_catalog.lookup(model, provider)
        return {"error": None, "found": meta is not None,
                "info": meta or model_catalog.enrich(model, provider)}
    except Exception as e:
        return {"error": str(e), "found": False, "info": None}


def _claude_model_window(model: str):
    """Best-effort context window (in tokens) for a Claude Code model id.

    The local `claude` CLI isn't served through the app's OpenRouter catalog, so
    we can't look its window up there (and OpenRouter under-reports Anthropic at
    200K anyway). Map by family instead: current Opus/Sonnet run a 1M window;
    Haiku is 200K. Returns None for a blank/unknown id (e.g. "Claude's default"),
    which the footer fills in live once the run reports its real model.
    """
    m = (model or "").lower()
    if not m:
        return None
    if "haiku" in m:
        return 200_000
    if "opus" in m or "sonnet" in m or "fable" in m or "mythos" in m:
        return 1_000_000
    return None


def _codex_model_window(model: str):
    """Best-effort context window (in tokens) for a Codex CLI model id.

    Same rationale as _claude_model_window: the local `codex` CLI isn't in the
    app's OpenRouter catalog. Map by family — GPT-5 family runs a 400K window,
    o-series 200K, GPT-4.1 1M; blank/unknown returns None (the footer then shows
    no max until the run reports its real model).
    """
    m = (model or "").lower()
    if not m:
        return None
    if "gpt-4.1" in m:
        return 1_000_000
    if "o4" in m or "o3" in m or "o1" in m:
        return 200_000
    if "gpt-5" in m or "gpt-4o" in m:
        return 400_000
    return None


async def _require_model_view_access(request: Request, agent_id: str, session_id: str):
    """Authenticate model metadata reads and enforce session/agent access."""
    user_id = request_user_id(request)
    if not user_id:
        raise HTTPException(status_code=401, detail={"code": "authentication_required"})
    from app.db import get_db
    db = get_db()
    bound_agent_id = ""
    if session_id:
        try:
            await db.assert_session_owned(user_id, session_id)
        except PermissionError:
            participant = False
            try:
                participant = await db.is_session_participant(session_id, user_id, "user")
            except Exception:
                pass
            try:
                bound_agent_id = await db.get_session_agent_id(session_id) or ""
            except Exception:
                bound_agent_id = ""
            if bound_agent_id and not participant:
                raise HTTPException(status_code=404, detail="Session not found.")
        if not bound_agent_id:
            try:
                bound_agent_id = await db.get_session_agent_id(session_id) or ""
            except Exception:
                pass
    if agent_id and bound_agent_id and agent_id != bound_agent_id:
        raise HTTPException(status_code=404, detail="Session not found.")
    effective_agent_id = bound_agent_id or agent_id
    agent_rec = await db.get_agent_by_id(effective_agent_id) if effective_agent_id else None
    if effective_agent_id and not agent_rec:
        raise HTTPException(status_code=404, detail="Agent not found.")
    if agent_rec:
        mode = str(agent_rec.get("user_mode") or "anonymous")
        is_admin = bool(await db.is_user_admin(user_id))
        # Admin access is already sufficient; do not force an unrelated
        # membership-store lookup (which may live in a separate agent DB).
        is_member = (
            False if is_admin
            else bool(await db.is_agent_member(effective_agent_id, user_id))
        )
        registered = not str(user_id).startswith("anon_")
        if not (is_admin or is_member or mode == "anonymous" or (mode == "register" and registered)):
            raise HTTPException(status_code=403, detail={"code": "agent_membership_required"})
    return user_id, agent_rec


@router.get("/current-model-info")
async def get_current_model_info(
    request: Request,
    agent_id: str = Query("", alias="agent_id"),
    session_id: str = Query("", alias="session_id"),
):
    """Resolve the *active* model for the current chat and return its catalog
    metadata (context window, max output, cost) in a single call. Used by the
    chat footer to show context/max next to the in/out token counters.

    The model is resolved exactly as the agent loop resolves it for a run: the
    user's default with any per-agent LLM override (the agent's custom model)
    and then any per-session override (the session's picked model) layered on
    top — so the footer shows the model the run will actually use, not just the
    global default.
    """
    user_id, agent_rec = await _require_model_view_access(request, agent_id, session_id)

    active = await resolve_active_model(user_id, agent_rec, session_id or None)
    model = active.get("model", "")
    provider = active.get("provider", "")

    # Alternate-engine agents (e.g. Local Claude Code) don't run on the app's own
    # model, so the resolved model/context/cost above describe a model they never
    # use. Surface the engine id so the footer can drop the inapplicable price
    # while keeping the (real) token counters. Mirrors loop.py's engine lookup.
    engine = ""
    cc_meta: dict = {}
    try:
        _eng_meta = agent_rec.get("metadata") if agent_rec else None
        if isinstance(_eng_meta, str):
            import json as _json
            _eng_meta = _json.loads(_eng_meta or "{}")
        if isinstance(_eng_meta, dict):
            engine = str(_eng_meta.get("engine") or "").strip()
            _cc = _eng_meta.get("claude_code")
            cc_meta = _cc if isinstance(_cc, dict) else {}
    except Exception:
        engine = ""

    # Local Claude Code: the footer must describe the model the *local* claude
    # program runs, not the app's default LLM. Use the agent's configured model
    # (blank ⇒ "Claude's default", which the engine fills in live from the model
    # the run actually reports), and the real Claude context window for it — never
    # the app default's price (which doesn't apply here). See _claude_model_window.
    if engine == "claude_code":
        cc_model = str(cc_meta.get("model") or "").strip()
        return {
            "error": None, "model": cc_model, "provider": "anthropic",
            "engine": engine, "found": bool(cc_model),
            "context": _claude_model_window(cc_model), "max_output": None,
            "cost_input": None, "cost_output": None,
            "reasoning_effort": str(cc_meta.get("effort") or "default"), "reasoning": None,
        }

    # Local Codex: same treatment — the configured codex model + its window.
    if engine == "codex":
        _cx = _eng_meta.get("codex_code") if isinstance(_eng_meta, dict) else {}
        _cx = _cx if isinstance(_cx, dict) else {}
        cx_model = str(_cx.get("model") or "").strip()
        return {
            "error": None, "model": cx_model, "provider": "openai",
            "engine": engine, "found": bool(cx_model),
            "context": _codex_model_window(cx_model), "max_output": None,
            "cost_input": None, "cost_output": None,
            "reasoning_effort": str(_cx.get("effort") or "default"), "reasoning": None,
        }

    display_name = str(active.get("display_name") or "").strip()
    result = {
        "error": None, "model": display_name or model,
        "display_name": display_name,
        "provider": "" if display_name else provider, "found": False,
        "engine": engine,
        "context": None, "max_output": None, "cost_input": None, "cost_output": None,
        # The reasoning-effort level this chat is running the active model at, and
        # whether the model supports reasoning at all (gates the footer effort UI).
        "reasoning_effort": active.get("reasoning_effort", "default"), "reasoning": None,
    }
    if not model:
        return result
    try:
        from app import model_catalog
        await model_catalog.ensure_fresh()
        meta = model_catalog.lookup(model, provider_hint=provider)
        if meta:
            result.update({
                "found": True,
                "context": meta.get("context"),
                "max_output": meta.get("max_output"),
                "cost_input": meta.get("cost_input"),
                "cost_output": meta.get("cost_output"),
                "reasoning": meta.get("reasoning"),
            })
    except Exception as e:
        result["error"] = str(e)
    return result


@router.get("/agent-models")
async def get_agent_models(
    request: Request,
    agent_id: str = Query("", alias="agent_id"),
    session_id: str = Query("", alias="session_id"),
):
    """List the Text-capable models a chat user can switch between for this agent
    (app default(s) + the agent's own roster — the exact union the run considers),
    plus the model currently active for this chat. Used by the chat footer model
    switcher so it shows ALL of the agent's models, not just the admin defaults.
    """
    user_id, agent_rec = await _require_model_view_access(request, agent_id, session_id)
    try:
        return await resolve_agent_models(user_id, agent_rec, session_id or None)
    except Exception as e:
        logger.warning("get_agent_models failed: %s", e)
        return {"models": [], "active": "", "error": str(e)}


@router.get("/metadata", response_model=MetadataSetting)
async def get_metadata_setting():
    return MetadataSetting(enabled=_is_metadata_enabled())


@router.post("/metadata", response_model=MetadataSetting)
async def set_metadata_setting(setting: MetadataSetting):
    if setting.enabled:
        METADATA_FLAG.touch()
    else:
        METADATA_FLAG.unlink() if METADATA_FLAG.exists() else None
    return MetadataSetting(enabled=_is_metadata_enabled())


@router.get("/backgrounds")
async def list_backgrounds():
    """Drop-in animated-background catalog for the Appearance selector.

    Scans ui/background/<id>/<id>.json fresh each call so a folder dropped in
    (or removed) shows up without a server restart. The built-in "none" option
    is added by the selector UI, not listed here."""
    from app.ui_backgrounds import catalog as _bg_catalog, reload as _bg_reload
    _bg_reload()
    return {"backgrounds": _bg_catalog()}


# ── Run-contract gates: app-level catalog + configuration ─────────────────────
#
# The gates directory (plugins/gates/) is the drop-in catalog for run-contract
# gates. The per-agent Modes page decides WHICH gate a given agent uses; this
# app-level surface lists every gate TYPE, always ON (the catalog loads at
# process start, so a type cannot be switched off yet), and carries the
# app-wide guidance for it. A gate dropped into plugins/gates/ appears here
# automatically — no edit to this file or to the UI.

def _app_gate_schema(descriptor: dict) -> dict:
    """The app-level controls for one gate, as a settings-row schema.

    TAILORED to the gate: the rows ARE the gate's own declared params, so a gate
    that declares none has no app-level settings. There is deliberately no
    generic "guidance" blob — app-wide guidance is expressed through those same
    params (for ``closing_message``: the flag taxonomy, the notification word
    limit, and the notification / output-format guidance), which the resolver
    applies app-wide BEFORE the per-agent values append
    (app/agent/closing_message.py).

    Row kind follows the param's declared ``type``: ``string`` becomes a text
    input or textarea, and ``number`` becomes the shared numeric stepper (with
    the descriptor's ``min`` / ``max`` / ``step`` carried through). A param type
    the row builder cannot render is skipped rather than emitted as a control
    that would silently coerce the value to 0.
    """
    settings = []
    for key, spec in (descriptor.get("params") or {}).items():
        if not isinstance(spec, dict):
            continue
        kind = spec.get("type")
        if kind == "number":
            row = {
                "key": key,
                "label": spec.get("label") or key,
                "type": "number",
                "hint": spec.get("hint") or "",
            }
            for bound in ("min", "max", "step"):
                if spec.get(bound) is not None:
                    row[bound] = spec[bound]
            settings.append(row)
            continue
        if kind != "string":
            continue
        settings.append({
            "key": key,
            "label": spec.get("label") or key,
            "type": "textarea" if spec.get("multiline") else "text",
            "hint": spec.get("hint") or "",
        })
    return {"settings": settings}


@router.get("/gates")
async def get_app_gates():
    """Every gate type, with this gate's saved app-level values.

    `locked_on` is True on every row: gate types are discovered at process
    start, so for now they are always available and cannot be switched off.
    The row is rendered ON to reflect that rather than offering a control that
    would not persist. `values` is {} when the gate has no app-level config.
    """
    from app.agent.gate_registry import gate_type_menu

    saved = _load_app_settings().get("gate_settings")
    saved = saved if isinstance(saved, dict) else {}
    gates = []
    for descriptor in gate_type_menu():
        values = saved.get(descriptor["id"])
        gates.append({
            **descriptor,
            "locked_on": True,
            "enabled": True,
            "app_schema": _app_gate_schema(descriptor),
            "values": values if isinstance(values, dict) else {},
        })
    return {"gates": gates}


@router.put("/gates/{gate_id}")
async def set_app_gate_settings(gate_id: str, request: Request):
    """Save ONE gate's app-level params, merged onto the others.

    A key whose value is blank is removed rather than stored, so clearing a
    field in the UI genuinely unsets the layer (the resolver treats blank as
    unset anyway, so this only keeps the file tidy).
    """
    from app.db import get_db
    from app.agent.gate_registry import gate_types

    caller_id = _resolve_user_id(
        request.headers.get("Authorization", "") or "",
        request.query_params.get("token", "") or "",
    )
    if not await get_db().is_user_admin(caller_id):
        raise HTTPException(status_code=403, detail="Admin access required")

    known = gate_types()
    if gate_id not in known:
        raise HTTPException(status_code=404, detail=f"Unknown gate type: {gate_id}")

    try:
        body = await request.json()
    except Exception:  # noqa: BLE001
        body = {}
    params = (body or {}).get("params")
    params = params if isinstance(params, dict) else {}
    # Only the gate's OWN declared param names are accepted, so a bad payload
    # cannot write arbitrary keys into the file and the saved surface stays
    # exactly the tailored rows the UI renders.
    allowed = set(known[gate_id].get("params") or {})
    values: dict = {}
    for key, raw in params.items():
        if key not in allowed:
            continue
        text = str(raw if raw is not None else "").strip()
        if text:
            values[key] = text

    existing = _load_app_settings()
    saved = existing.get("gate_settings")
    saved = dict(saved) if isinstance(saved, dict) else {}
    if values:
        saved[gate_id] = values
    else:
        saved.pop(gate_id, None)
    _save_app_settings({**existing, "gate_settings": saved})
    # Confirm what a refresh will load, not what we intended to write.
    persisted = _load_app_settings().get("gate_settings")
    persisted = persisted if isinstance(persisted, dict) else {}
    got = persisted.get(gate_id)
    return {"gate_id": gate_id, "values": got if isinstance(got, dict) else {}}


@router.get("/app", response_model=AppSettings)
async def get_app_settings():
    """Return app-wide feature flags."""
    return AppSettings(**_load_app_settings())


@router.get("/anonymous-access")
async def get_anonymous_access_status(request: Request):
    """Return the live anonymous kill-switch and hard-budget status."""
    from app.db import get_db
    caller_id = _resolve_user_id(
        request.headers.get("Authorization", "") or "",
        request.query_params.get("token", "") or "",
    )
    if not await get_db().is_user_admin(caller_id):
        raise HTTPException(status_code=403, detail="Admin access required")
    from app.api.rate_limit import anonymous_budget_status
    return anonymous_budget_status()


@router.post("/app", response_model=AppSettings)
async def set_app_settings(request: Request):
    """Save app-wide feature flags.

    MERGES the posted fields onto the existing settings rather than replacing the
    whole object, so a form that submits just one or two flags can't silently reset
    every other setting (e.g. the global_system_prompt) back to its default. Unknown
    raw keys already in the file (e.g. render_recording_* capture knobs) are
    preserved too."""
    # Admin-only. These are app-WIDE controls (access mode, global system
    # prompt, watchdog tunables, …) so a non-admin must never be able to flip
    # them. There is no global auth middleware, so the gate lives here: resolve
    # the caller from the JWT and require is_admin. The matching GET stays open
    # because non-admin clients read these flags at boot.
    from app.db import get_db
    caller_id = _resolve_user_id(
        request.headers.get("Authorization", "") or "",
        request.query_params.get("token", "") or "",
    )
    if not await get_db().is_user_admin(caller_id):
        raise HTTPException(status_code=403, detail="Admin access required")

    try:
        body = await request.json()
    except Exception:  # noqa: BLE001
        body = {}
    if not isinstance(body, dict):
        body = {}
    if "chat_panel_width" in body:
        try:
            body["chat_panel_width"] = normalize_chat_panel_width(body["chat_panel_width"])
        except ValueError as exc:
            raise HTTPException(status_code=422, detail=f"chat_panel_width {exc}") from exc
    existing = _load_app_settings()
    settings = AppSettings(**{**existing, **body})
    settings.access_mode = normalize_access_mode(settings.access_mode)
    settings.site_name = str(settings.site_name or "").strip()[:120] or "WebAgent"
    if settings.voice_dictation_mode not in {"browser_then_llm", "llm_only"}:
        settings.voice_dictation_mode = "browser_then_llm"
    # Clamp stream buffer retention to a sane range so a bad value can't
    # exhaust RAM (huge) or break replay-after-reconnect entirely (negative).
    try:
        sb = int(settings.stream_buffer_retention_seconds)
    except (TypeError, ValueError):
        sb = 60
    if sb < 0:
        sb = 0
    if sb > 3600:
        sb = 3600
    settings.stream_buffer_retention_seconds = sb
    # Clamp the session-concurrency cap: negative values would block every new
    # run, so force 0 (unlimited) and bound the upper end to something sane.
    try:
        mas = int(settings.max_active_sessions)
    except (TypeError, ValueError):
        mas = 0
    settings.max_active_sessions = max(0, min(9999, mas))
    # Bound the agent default output-token ceiling. A non-positive value would
    # leave every unset agent unable to answer, so force 8096; cap the top end so
    # a bad payload cannot request an absurd output budget.
    try:
        dmt = int(settings.default_max_tokens)
    except (TypeError, ValueError):
        dmt = 8096
    settings.default_max_tokens = 8096 if dmt <= 0 else min(dmt, 1_000_000)
    # Clamp transcript-recovery controls so bad payloads cannot disable ordering or
    # make recovery impossible with a negative threshold.
    settings.transcript_timestamp_order_recovery_enabled = bool(settings.transcript_timestamp_order_recovery_enabled)
    try:
        thr = int(settings.transcript_timestamp_order_recovery_threshold_seconds)
    except (TypeError, ValueError):
        thr = 60
    if thr < 0:
        thr = 60
    settings.transcript_timestamp_order_recovery_threshold_seconds = thr
    try:
        browser_max = int(settings.browser_max_concurrent_sessions)
    except (TypeError, ValueError):
        browser_max = 3
    settings.browser_max_concurrent_sessions = max(1, min(20, browser_max))
    try:
        browser_idle = int(settings.browser_idle_timeout_seconds)
    except (TypeError, ValueError):
        browser_idle = 300
    settings.browser_idle_timeout_seconds = max(60, min(86400, browser_idle))
    # Preserve any unknown raw keys already in the file while writing the merged
    # validated settings on top.
    _save_app_settings({**existing, **settings.model_dump()})
    # Confirm the exact value that reached persistent storage. The Design UI uses
    # this response as its save acknowledgement, so an in-memory model is not
    # enough: a returned width must be the value a refresh will load.
    persisted = AppSettings(**_load_app_settings())
    if any(key.startswith("browser_") for key in body):
        try:
            from app.tools import browser as browser_engine
            await browser_engine.enforce_policy()
        except Exception as exc:  # noqa: BLE001
            logger.warning("Could not apply browser session policy immediately: %s", exc)
    return persisted


@router.get("/embedding")
async def get_embedding_settings():
    """Current memory-embedding source/model/width + whether the in-process
    (local) engine is importable. Read by the admin model-table Advanced panel to
    render the source picker and warn if 'local' is selected without its
    dependency installed. Open (like GET /app) — it exposes no secrets."""
    from app.agent.embed import (
        embed_source, embed_model_name, embed_dim,
        local_embeddings_available, local_models,
    )
    ok, reason = local_embeddings_available()
    return {
        "source": embed_source(),
        "model": embed_model_name(),
        "dim": embed_dim(),
        "local_available": ok,
        "local_reason": reason,
        "local_models": [{"id": k, "dim": v} for k, v in local_models().items()],
    }


@router.post("/embedding/reindex")
async def reindex_embeddings(request: Request):
    """Guided embedding-model switch: optionally set a new source/model, then
    re-embed every stored memory (and RAG doc) chunk so the new model's vectors
    replace the old ones. Admin-only and destructive to existing vectors, so the
    caller is gated exactly like POST /app.

    Body (all optional): {embedding_source: "local"|"cloud", embedding_model: str}.
    When source=local we verify the engine is importable BEFORE touching any
    vectors — otherwise a mis-click would wipe embeddings and leave search on the
    keyword-only fallback with no way to rebuild until the dependency is installed.
    Runs inline; fine for the (small, curated) memory store."""
    from app.db import get_db
    caller_id = _resolve_user_id(
        request.headers.get("Authorization", "") or "",
        request.query_params.get("token", "") or "",
    )
    db = get_db()
    if not await db.is_user_admin(caller_id):
        raise HTTPException(status_code=403, detail="Admin access required")

    try:
        body = await request.json()
    except Exception:  # noqa: BLE001
        body = {}
    if not isinstance(body, dict):
        body = {}

    source = body.get("embedding_source")
    model = body.get("embedding_model")

    # Apply the config change FIRST (so embed_dim() reflects the new choice), but
    # never wipe vectors for a local engine that can't actually run.
    if source is not None:
        source = "local" if str(source).strip().lower() == "local" else "cloud"
        if source == "local":
            from app.agent.embed import local_embeddings_available
            ok, reason = local_embeddings_available()
            if not ok:
                raise HTTPException(
                    status_code=400,
                    detail=f"Local embeddings unavailable: {reason}. "
                           f"Install with: uv sync --extra embeddings-local",
                )
        existing = _load_app_settings()
        patch = {"embedding_source": source}
        if model is not None:
            patch["embedding_model"] = str(model or "")
        merged = AppSettings(**{**existing, **patch})
        _save_app_settings({**existing, **merged.model_dump()})

    # Record the now-active width so the search path + column agree on it.
    from app.agent.embed import embed_dim, embed_model_name, embed_source
    active_dim = embed_dim()
    existing = _load_app_settings()
    existing["embedding_dim"] = active_dim
    _save_app_settings(existing)

    if not hasattr(db, "reindex_embeddings"):
        raise HTTPException(
            status_code=400,
            detail="This storage backend does not support embedding reindex.",
        )
    result = await db.reindex_embeddings()
    return {
        "ok": True,
        "source": embed_source(),
        "model": embed_model_name(),
        "dim": active_dim,
        "result": result,
    }


class FreeModelRankingConfig(BaseModel):
    enabled: bool = False
    cadence_hours: float = Field(default=1.0, ge=0.1, le=168.0)


async def _require_ranking_admin(authorization: Optional[str]) -> str:
    caller_id = _resolve_user_id(authorization or "")
    from app.db import get_db
    if caller_id == ANONYMOUS_KEY or not await get_db().is_user_admin(caller_id):
        raise HTTPException(status_code=403, detail="Admin access required")
    return caller_id


def _ranking_config() -> dict:
    from app.free_model_ranking import normalize_config
    return normalize_config((_load_app_settings() or {}).get("free_model_ranking"))


class FreeModelRankingConnectInput(BaseModel):
    api_key: str


@router.get("/free-model-ranking/config")
async def get_free_model_ranking_config(authorization: Optional[str] = Header(None)):
    await _require_ranking_admin(authorization)
    config = _ranking_config()
    stored = await _load_own_llm_config(PLATFORM_LLM_OWNER) or {}
    key = ((stored.get("providers") or {}).get("openrouter") or {}).get("api_key") or ""
    return {**config, "openrouter_configured": bool(key), "credential_suffix": _credential_suffix(key)}


def _ranking_rows() -> list[dict]:
    return list((_load_app_settings() or {}).get("free_model_ranking_rows") or [])


def _save_ranking_rows(rows: list[dict]) -> None:
    settings = _load_app_settings()
    settings["free_model_ranking_rows"] = rows
    _save_app_settings(settings)


async def _discover_ranking_rows() -> list[dict]:
    """Refresh the free catalog without inventing a health transition.

    Discovery runs on every connect and at the start of every benchmark run, so it
    must carry the recorded status (and its counter base) forward for candidates we
    already know: otherwise the Health column's age would reset to zero whenever the
    catalog was reloaded. Genuinely new candidates start as an unstamped first
    observation.
    """
    from app.free_model_ranking import discover, status_since
    import time
    now = time.time()
    prior = {row.get("model_id"): row for row in _ranking_rows()}
    rows = []
    for item in await discover():
        before = prior.get(item["id"]) or {}
        status = before.get("status") or "pending"
        rows.append({"model_id": item["id"], "display_name": item["name"], "context_length": item.get("context_length", 0),
                     "capabilities": item.get("capabilities") or before.get("capabilities") or {},
                     "eligible": False, "score": 0, "reliability": 0, "latency_ms": None,
                     "tokens_per_second": None, "reason": "not benchmarked", "status": status,
                     "status_since": status_since(before.get("status"), status, before.get("status_since"), now)})
    _save_ranking_rows(rows)
    return rows


@router.get("/free-model-ranking/candidates")
async def get_free_model_ranking_candidates(authorization: Optional[str] = Header(None)):
    await _require_ranking_admin(authorization)
    rows = _ranking_rows()
    if not rows:
        rows = await _discover_ranking_rows()
    return {"rows": rows, "count": len(rows)}


@router.post("/free-model-ranking/connect")
async def connect_free_model_ranking_openrouter(request: Request, body: FreeModelRankingConnectInput):
    """Store the ranking panel's OpenRouter key through the existing encrypted vault.

    This only updates the OpenRouter provider profile; it never changes the active
    app provider and never writes the key into ranking configuration or responses.
    """
    await _require_provider_admin(request)
    key = body.api_key.strip()
    if len(key) < 8:
        raise HTTPException(status_code=422, detail="Enter a valid OpenRouter API key.")
    existing = await _load_own_llm_config(PLATFORM_LLM_OWNER) or dict(DEFAULT_PROVIDER)
    providers = dict(existing.get("providers") or {})
    prior = dict(providers.get("openrouter") or {})
    providers["openrouter"] = {
        **prior,
        "auth_mode": "api_key",
        "api_key": key,
        "base_url": PROVIDER_PRESETS["openrouter"]["base_url"],
    }
    await _persist_llm_config(PLATFORM_LLM_OWNER, _without_saved_default({**existing, "providers": providers}))
    try:
        rows = await _discover_ranking_rows()
        discovery_message = f" OpenRouter free catalog loaded ({len(rows)} models)."
    except Exception as exc:  # A saved credential remains valid even if catalog refresh is transiently unavailable.
        LOG.warning("Free-model catalog refresh after OpenRouter connect failed: %s", exc)
        rows = _ranking_rows()
        discovery_message = " Connected; catalog refresh will retry on Run now."
    return {"connected": True, "credential_suffix": _credential_suffix(key), "candidate_count": len(rows),
            "message": "OpenRouter connected securely." + discovery_message}


@router.put("/free-model-ranking/config")
async def put_free_model_ranking_config(payload: FreeModelRankingConfig, authorization: Optional[str] = Header(None)):
    await _require_ranking_admin(authorization)
    from app.free_model_ranking import normalize_config
    settings = _load_app_settings()
    cfg = normalize_config(payload.model_dump())
    settings["free_model_ranking"] = cfg
    _save_app_settings(settings)
    return cfg


@router.post("/free-model-ranking/run")
async def run_free_model_ranking(authorization: Optional[str] = Header(None)):
    await _require_ranking_admin(authorization)
    cfg = _ranking_config()
    if not cfg["enabled"]:
        return {"ok": False, "message": "Enable the ranking monitor before running a benchmark."}
    stored = await _load_own_llm_config(PLATFORM_LLM_OWNER) or {}
    key = ((stored.get("providers") or {}).get("openrouter") or {}).get("api_key") or ""
    if not key:
        return {"ok": False, "message": "Connect OpenRouter before running a benchmark."}
    from app.free_model_ranking import probe, score, status_since
    import asyncio
    import time
    now = time.time()
    # HEALTH-AGE base: the rows as stored BEFORE the catalog refresh rewrites them.
    # An unchanged verdict keeps the timestamp of the probe that first reported it;
    # only a real transition (pending -> ok, ok -> failed, ...) restamps it.
    prior_by_model = {row.get("model_id"): row for row in _ranking_rows()}
    rows = await _discover_ranking_rows()
    semaphore = asyncio.Semaphore(cfg["concurrency"])
    async def one(row: dict) -> dict:
        async with semaphore:
            result = await probe(row["model_id"], key, cfg)
        metrics = score([result], row.get("manual_rating"), bool(row.get("disabled")))
        before = prior_by_model.get(row["model_id"]) or {}
        status = "ok" if result["ok"] else "failed"
        return {**row, **metrics, "status": status,
                "status_since": status_since(before.get("status"), status, before.get("status_since"), now),
                "latency_ms": result.get("latency_ms"), "tokens_per_second": result.get("tokens_per_second"),
                "agentic_ok": bool(result.get("agentic_ok")),
                "compatibility_ok": bool(result.get("compatibility_ok")),
                "compatibility_reason": result.get("compatibility_reason") or "not tested",
                "last_result": "ok" if result["ok"] else result.get("error", "probe failed"),
                "retry_after": result.get("retry_after"), "updated_at": now}
    completed = await asyncio.gather(*(one(row) for row in rows))
    _save_ranking_rows(completed)
    successes = sum(1 for row in completed if row["status"] == "ok")
    return {"ok": True, "message": f"Benchmarked {len(completed)} free models; {successes} passed.", "rows": completed}

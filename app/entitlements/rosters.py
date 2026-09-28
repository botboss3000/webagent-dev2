"""Named platform model rosters and their encrypted credentials.

Roster shape is installation-control-plane data.  API keys are deliberately
kept out of ``model_rosters.entries_json`` and stored in the app vault as one
ID-keyed bundle per roster.  The stable ``entry_id`` keys make reordering a
roster safe; the legacy LLM secret bundle used list positions instead.
"""

from __future__ import annotations

import json
import logging
import ipaddress
import re
import uuid
from copy import deepcopy
from pathlib import Path
from typing import Any, Iterable, Mapping, Optional
from urllib.parse import urlsplit

logger = logging.getLogger(__name__)

PLATFORM_ROSTER_OWNER = "_platform"
PLATFORM_ROSTER_SERVICE = "llm_roster"
PLATFORM_DEFAULT_ROSTER_ID = "roster-free"
SYSTEM_ROSTER_IDS = (
    "roster-anonymous",
    "roster-free",
    "roster-pro",
    "roster-admin",
)
ROSTER_SECRET_VERSION = 2
_ENTRY_NAMESPACE = uuid.UUID("9f96540e-f7ee-4f4b-b8ae-9525646b6d4b")
_SEED_DIR = Path(__file__).resolve().parents[1] / "defaults" / "model-rosters"
_MANAGED_SOURCES = {"system-seed", "legacy-migration", "environment"}
_ENTRY_ID_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:-]{0,127}$")
_ENTRY_FIELDS = frozenset({
    "entry_id", "provider", "auth_mode", "model", "base_url", "label", "display_name",
    "name", "enabled", "text_capable", "image_capable",
    "image_out_capable", "voice_capable", "high_effort_capable",
    "tool_capable", "use_for_image", "use_for_image_out", "use_for_voice",
    "use_for_system", "input_modalities", "output_modalities", "capabilities",
    "context_window", "max_tokens", "credential_scope", "stateful_responses",
    "routing_mode", "credit_schedule", "credit_input_per_million", "credit_output_per_million",
    # MODEL-PRICE-EDIT: the OWNER's own reference price for a bring-your-own row, USD
    # per 1M tokens. It is an estimate the user keeps for themselves, never a billed
    # figure, and a catalog row never carries one — its price is the admin's, in credits.
    "reference_cost_input", "reference_cost_output",
})
_BOOL_ENTRY_FIELDS = frozenset({
    "enabled", "text_capable", "image_capable", "image_out_capable",
    "voice_capable", "high_effort_capable", "tool_capable", "use_for_image",
    "use_for_image_out", "use_for_voice", "use_for_system",
})
# Platform rosters are catalogs. These fields are positions in an individual
# agent roster and therefore must never survive tier normalization.
_CATALOG_ROLE_FIELDS = frozenset({
    "enabled", "high_effort_capable", "use_for_image", "use_for_image_out",
    "use_for_voice", "use_for_system",
})
_LIST_ENTRY_FIELDS = frozenset({"input_modalities", "output_modalities", "capabilities"})
_LOCAL_PROVIDERS = frozenset({"ollama", "lmstudio"})
OPENROUTER_FREE_ROUTING_MODE = "openrouter_free"
# Ranked aliases retain the same roster-owned OpenRouter credential and native
# fallback; only the per-request resolver may substitute an eligible explicit ID.
OPENROUTER_RANKED_FREE_ROUTING_MODE = "openrouter_ranked_free"
OPENROUTER_FREE_BASE_URL = "https://openrouter.ai/api/v1"
OPENROUTER_FREE_MODEL = "openrouter/free"


class RosterError(ValueError):
    """A roster cannot be normalized without losing its identity or safety."""


def _normalize_managed_routing(item: dict[str, Any], index: int) -> None:
    """Canonicalize provider-owned routes without allowing a hidden static pin."""
    mode = str(item.get("routing_mode") or "").strip().lower()
    if not mode:
        item.pop("routing_mode", None)
        return
    if mode not in {OPENROUTER_FREE_ROUTING_MODE, OPENROUTER_RANKED_FREE_ROUTING_MODE}:
        raise RosterError(f"entries[{index}].routing_mode is unsupported")
    provider = str(item.get("provider") or "").strip().lower()
    model = str(item.get("model") or "").strip().lower()
    base_url = str(item.get("base_url") or "").strip().rstrip("/")
    if provider and provider != "openrouter":
        raise RosterError(f"entries[{index}].routing_mode requires provider openrouter")
    if model and model != OPENROUTER_FREE_MODEL:
        raise RosterError(f"entries[{index}].routing_mode cannot select a static model")
    if base_url and base_url != OPENROUTER_FREE_BASE_URL:
        raise RosterError(f"entries[{index}].routing_mode requires the OpenRouter API URL")
    item.update(
        routing_mode=mode,
        provider="openrouter",
        auth_mode="api_key",
        base_url=OPENROUTER_FREE_BASE_URL,
        model=OPENROUTER_FREE_MODEL,
    )


def _validate_base_url(value: Any, provider: str, *, untrusted: bool) -> str:
    raw = str(value or "").strip()
    if not raw:
        return ""
    if provider.lower() == "openai_subscription" and raw == "codex://chatgpt":
        return raw
    try:
        parsed = urlsplit(raw)
        port = parsed.port
    except ValueError as exc:
        raise RosterError("base_url is invalid") from exc
    if parsed.scheme not in {"http", "https"} or not parsed.hostname:
        raise RosterError("base_url must be an absolute HTTP(S) URL")
    if parsed.username or parsed.password or parsed.query or parsed.fragment:
        raise RosterError("base_url must not contain credentials, a query, or a fragment")
    if port is not None and not (1 <= port <= 65535):
        raise RosterError("base_url port is invalid")
    if untrusted:
        host = parsed.hostname.rstrip(".").lower()
        if provider.lower() in _LOCAL_PROVIDERS or host in {"localhost", "localhost.localdomain"} \
                or host.endswith(".local"):
            raise RosterError("user BYO base_url cannot target a local service")
        try:
            address = ipaddress.ip_address(host.strip("[]"))
        except ValueError:
            address = None
        if address is not None and not address.is_global:
            raise RosterError("user BYO base_url must use a public network address")
    return raw


def validate_roster_entries(
    entries: Iterable[Mapping[str, Any]], *, allow_credentials: bool = False,
    require_provider_model: bool = False, untrusted_urls: bool = False,
) -> list[dict[str, Any]]:
    """Validate the closed public roster-entry schema and return clean copies."""
    result: list[dict[str, Any]] = []
    allowed = set(_ENTRY_FIELDS)
    if allow_credentials:
        allowed.add("api_key")
    for index, raw in enumerate(entries):
        if not isinstance(raw, Mapping):
            raise RosterError(f"entries[{index}] must be an object")
        unknown = sorted(str(key) for key in raw if str(key) not in allowed)
        if unknown:
            raise RosterError(f"entries[{index}] contains unsupported fields: {', '.join(unknown)}")
        item = dict(raw)
        _normalize_managed_routing(item, index)
        entry_id = str(item.get("entry_id") or "").strip()
        if entry_id and not _ENTRY_ID_RE.fullmatch(entry_id):
            raise RosterError(f"entries[{index}].entry_id has an invalid format")
        for field in ("provider", "auth_mode", "model", "label", "display_name", "name",
                      "credential_scope"):
            if field in item:
                item[field] = str(item.get(field) or "").strip()
                maximum = 120 if field == "display_name" else 512
                if len(item[field]) > maximum:
                    raise RosterError(f"entries[{index}].{field} is too long")
        scope = item.get("credential_scope")
        if scope and scope not in {"platform", "agent", "user"}:
            raise RosterError(f"entries[{index}].credential_scope is invalid")
        if "stateful_responses" in item:
            item["stateful_responses"] = str(item.get("stateful_responses") or "").strip()
            if item["stateful_responses"] not in {"auto", "disabled"}:
                raise RosterError(f"entries[{index}].stateful_responses must be auto or disabled")
        if "credit_schedule" in item:
            item["credit_schedule"] = str(item.get("credit_schedule") or "").strip().lower()
            if item["credit_schedule"] not in {"flash", "pro", "custom"}:
                raise RosterError(f"entries[{index}].credit_schedule must be flash, pro, or custom")
        for field in ("credit_input_per_million", "credit_output_per_million",
                      "reference_cost_input", "reference_cost_output"):
            if field not in item:
                continue
            value = item[field]
            if isinstance(value, bool) or not isinstance(value, (int, float)) or value < 0:
                raise RosterError(f"entries[{index}].{field} must be a non-negative number")
            item[field] = float(value)
        if require_provider_model and (not item.get("provider") or not item.get("model")):
            raise RosterError(f"entries[{index}] requires provider and model")
        item["base_url"] = _validate_base_url(
            item.get("base_url"), str(item.get("provider") or ""), untrusted=untrusted_urls,
        )
        for field in _BOOL_ENTRY_FIELDS:
            if field in item and not isinstance(item[field], bool):
                raise RosterError(f"entries[{index}].{field} must be boolean")
        # A platform roster is a capability catalog, never a source of agent roles.
        for field in _CATALOG_ROLE_FIELDS:
            item.pop(field, None)
        for field in _LIST_ENTRY_FIELDS:
            if field not in item:
                continue
            values = item[field]
            if not isinstance(values, list) or not all(isinstance(value, str) for value in values):
                raise RosterError(f"entries[{index}].{field} must be a list of strings")
            item[field] = [value.strip() for value in values if value.strip()]
        for field in ("context_window", "max_tokens"):
            if field in item and (isinstance(item[field], bool) or not isinstance(item[field], int)
                                  or item[field] < 0):
                raise RosterError(f"entries[{index}].{field} must be a non-negative integer")
        result.append(item)
    return result


def validate_live_voice_assignments(entries: Iterable[Mapping[str, Any]]) -> None:
    """Reject roster roles that cannot serve an interactive voice turn."""
    for index, entry in enumerate(entries):
        if not isinstance(entry, Mapping) or not entry.get("use_for_voice"):
            continue
        model = str(entry.get("model") or "").strip()
        if not entry.get("voice_capable"):
            raise RosterError(
                f"entries[{index}] cannot use the Voice role because the model "
                "is not marked as accepting audio input"
            )
        if model.lower().endswith(":batch"):
            raise RosterError(
                f"entries[{index}] cannot use the Voice role because batch-only "
                "models do not support live voice requests"
            )


def _json_object(value: Any) -> dict[str, Any]:
    if isinstance(value, Mapping):
        return dict(value)
    if isinstance(value, str) and value:
        try:
            parsed = json.loads(value)
            return dict(parsed) if isinstance(parsed, Mapping) else {}
        except (TypeError, ValueError):
            return {}
    return {}


def _json_list(value: Any) -> list[dict[str, Any]]:
    if isinstance(value, str):
        try:
            value = json.loads(value or "[]")
        except (TypeError, ValueError):
            return []
    if not isinstance(value, list):
        return []
    return [dict(item) for item in value if isinstance(item, Mapping)]


def _entry_identity(entry: Mapping[str, Any]) -> str:
    return "|".join(
        str(entry.get(name) or "").strip()
        for name in ("provider", "base_url", "model")
    )



def assign_entry_ids(
    entries: Iterable[Mapping[str, Any]],
    *,
    existing_entries: Iterable[Mapping[str, Any]] = (),
) -> list[dict[str, Any]]:
    # Assign server-owned UUIDv4 IDs to new roster rows.
    existing_ids = {
        str(entry.get("entry_id") or "").strip()
        for entry in existing_entries if isinstance(entry, Mapping)
    }
    assigned: list[dict[str, Any]] = []
    seen: set[str] = set()
    for raw in entries:
        if not isinstance(raw, Mapping):
            raise RosterError("roster entries must be objects")
        item = deepcopy(dict(raw))
        entry_id = str(item.get("entry_id") or "").strip()
        if entry_id:
            if entry_id not in existing_ids:
                raise RosterError(f"unknown roster entry_id {entry_id!r}")
            if entry_id in seen:
                raise RosterError(f"duplicate roster entry_id {entry_id!r}")
        else:
            entry_id = str(uuid.uuid4())
        item["entry_id"] = entry_id
        seen.add(entry_id)
        assigned.append(item)
    return assigned

def stable_entry_id(
    roster_id: str,
    entry: Mapping[str, Any],
    *,
    duplicate_index: int = 0,
) -> str:
    """Return an existing entry ID or a deterministic ID for legacy entries."""
    existing = str(entry.get("entry_id") or entry.get("id") or "").strip()
    if existing:
        return existing
    identity = _entry_identity(entry)
    if not identity.replace("|", ""):
        raise RosterError("roster entry requires provider, base_url, or model")
    name = f"{roster_id}:{identity}:{max(0, int(duplicate_index))}"
    return str(uuid.uuid5(_ENTRY_NAMESPACE, name))


def normalize_entries(roster_id: str, entries: Iterable[Mapping[str, Any]]) -> list[dict[str, Any]]:
    """Strip secrets and attach stable, unique IDs without changing order."""
    normalized: list[dict[str, Any]] = []
    seen_ids: set[str] = set()
    duplicate_counts: dict[str, int] = {}
    for raw in entries:
        if not isinstance(raw, Mapping):
            raise RosterError("roster entries must be objects")
        item = deepcopy(dict(raw))
        item.pop("api_key", None)
        item.pop("credential_configured", None)
        item.pop("id", None)
        for field in _CATALOG_ROLE_FIELDS:
            item.pop(field, None)
        _normalize_managed_routing(item, len(normalized))
        identity = _entry_identity(item)
        duplicate_index = duplicate_counts.get(identity, 0)
        duplicate_counts[identity] = duplicate_index + 1
        entry_id = stable_entry_id(roster_id, raw, duplicate_index=duplicate_index)
        if entry_id in seen_ids:
            raise RosterError(f"duplicate roster entry_id {entry_id!r}")
        seen_ids.add(entry_id)
        item["entry_id"] = entry_id
        normalized.append(item)
    return normalized


def config_to_roster(config: Mapping[str, Any], roster_id: str) -> tuple[list[dict[str, Any]], Optional[str], str]:
    """Convert the legacy provider config into keyless entries + a v2 secret."""
    cfg = deepcopy(dict(config or {}))
    candidates = [dict(item) for item in (cfg.get("multi_providers") or []) if isinstance(item, Mapping)]
    root = {
        key: cfg.get(key)
        for key in (
            "provider", "auth_mode", "base_url", "model", "text_capable", "image_capable",
            "image_out_capable", "voice_capable", "use_for_image_out",
            "high_effort_capable", "stateful_responses", "routing_mode", "credit_schedule",
            "credit_input_per_million", "credit_output_per_million",
        )
        if key in cfg
    }
    root["api_key"] = cfg.get("api_key", "") or ""
    root_identity = _entry_identity(root)
    default_index: Optional[int] = None
    if root_identity.replace("|", ""):
        for index, item in enumerate(candidates):
            if _entry_identity(item) == root_identity:
                default_index = index
                for key, value in root.items():
                    if key not in item or (key == "api_key" and not item.get(key)):
                        item[key] = value
                break
        if default_index is None:
            candidates.insert(0, root)
            default_index = 0

    entries = normalize_entries(roster_id, candidates)
    secrets: dict[str, str] = {}
    for raw, entry in zip(candidates, entries):
        key = str(raw.get("api_key") or "")
        if key:
            secrets[entry["entry_id"]] = key
    standard = next((entry for entry in entries if entry.get("enabled") and entry.get("text_capable", True)), None)
    legacy_default = entries[default_index] if default_index is not None else None
    effective_default = standard or legacy_default or (entries[0] if entries else None)
    default_entry_id = effective_default["entry_id"] if effective_default else None
    bundle = {"v": ROSTER_SECRET_VERSION, "entries": secrets}
    return entries, default_entry_id, json.dumps(bundle, separators=(",", ":")) if secrets else ""


def _secret_entries(secret_ref: str) -> dict[str, str]:
    if not secret_ref:
        return {}
    try:
        payload = json.loads(secret_ref)
    except (TypeError, ValueError):
        return {}
    if not isinstance(payload, Mapping) or int(payload.get("v") or 0) != ROSTER_SECRET_VERSION:
        return {}
    entries = payload.get("entries")
    if not isinstance(entries, Mapping):
        return {}
    return {str(key): str(value) for key, value in entries.items() if value}


def roster_to_config(row: Mapping[str, Any], secret_ref: str = "") -> dict[str, Any]:
    """Rehydrate a named roster into the runtime's legacy provider shape."""
    roster_id = str(row.get("id") or row.get("slug") or PLATFORM_DEFAULT_ROSTER_ID)
    entries = normalize_entries(roster_id, _json_list(row.get("entries_json") or row.get("entries")))
    keys = _secret_entries(secret_ref)
    hydrated = [{**entry, "api_key": keys.get(entry["entry_id"], "")} for entry in entries]
    # Tier rows are catalogs rather than role-bearing rosters. Select the first
    # text-capable model as the transient default; an agent's own roster supplies
    # any explicit Standard role during effective configuration resolution.
    standard = next(
        (entry for entry in hydrated if entry.get("text_capable") is not False and entry.get("model")),
        None,
    )
    legacy_default_id = str(row.get("default_entry_id") or "")
    legacy_default = next(
        (entry for entry in hydrated if entry["entry_id"] == legacy_default_id),
        None,
    )
    default = standard or legacy_default or (hydrated[0] if hydrated else {})
    providers: dict[str, dict[str, Any]] = {}
    for entry in hydrated:
        provider = str(entry.get("provider") or "")
        if provider and provider not in providers:
            providers[provider] = {
                "auth_mode": entry.get("auth_mode", "api_key"),
                "base_url": entry.get("base_url", ""),
                "model": entry.get("model", ""),
                "api_key": entry.get("api_key", ""),
            }
    result = {
        "provider": default.get("provider", ""),
        "auth_mode": default.get("auth_mode", "api_key"),
        "base_url": default.get("base_url", ""),
        "api_key": default.get("api_key", ""),
        "model": default.get("model", ""),
        "providers": providers,
        "multi_providers": hydrated,
        "_platform_roster_id": roster_id,
        "_platform_roster_revision": int(row.get("revision") or 1),
        "_platform_default_entry_id": default.get("entry_id"),
    }
    for field in (
        "text_capable", "image_capable", "image_out_capable", "voice_capable",
        "use_for_image_out", "high_effort_capable", "stateful_responses",
        "routing_mode", "credential_scope", "credit_schedule", "credit_input_per_million",
        "credit_output_per_million",
    ):
        if field in default:
            result[field] = default[field]
    return result


def load_seed_shapes() -> list[dict[str, Any]]:
    """Load version-controlled, deliberately keyless system roster shapes."""
    shapes: list[dict[str, Any]] = []
    for path in sorted(_SEED_DIR.glob("*.json")):
        raw = json.loads(path.read_text(encoding="utf-8"))
        if not isinstance(raw, Mapping):
            raise RosterError(f"{path.name} must contain an object")
        shape = dict(raw)
        roster_id = str(shape.get("id") or "").strip()
        if not roster_id:
            raise RosterError(f"{path.name} is missing id")
        entries = _json_list(shape.get("entries_json") or shape.get("entries"))
        if any(item.get("api_key") for item in entries):
            raise RosterError(f"{path.name} must not contain API keys")
        shape["entries_json"] = normalize_entries(roster_id, entries)
        shape.pop("entries", None)
        shapes.append(shape)
    return shapes


async def _read_secret(db, roster_id: str) -> str:
    try:
        elem = await db.auth_element_get(PLATFORM_ROSTER_OWNER, PLATFORM_ROSTER_SERVICE, roster_id)
    except Exception:
        logger.debug("Could not read credential state for roster %s", roster_id, exc_info=True)
        elem = None
    return str((elem or {}).get("secret_ref") or "")


async def _write_secret(db, roster_id: str, secret_ref: str) -> None:
    await db.auth_element_set(
        user_id=PLATFORM_ROSTER_OWNER,
        service=PLATFORM_ROSTER_SERVICE,
        label=roster_id,
        config={"v": 1, "roster_id": roster_id},
        secret_ref=secret_ref,
    )


async def roster_credential_states(roster_id: str, *, db=None) -> dict[str, bool]:
    """Return per-entry configured flags without exposing credential values."""
    if db is None:
        from app.db import get_db
        db = get_db()
    return {entry_id: True for entry_id in _secret_entries(await _read_secret(db, roster_id))}


async def set_roster_entry_credential(
    roster_id: str, entry_id: str, credential: str, *, db=None,
) -> dict[str, bool]:
    """Set one platform credential in the ID-keyed encrypted vault bundle."""
    if db is None:
        from app.db import get_db
        db = get_db()
    row = await db.get_model_roster(roster_id)
    if not row:
        raise RosterError("model roster not found")
    valid_ids = {
        str(entry.get("entry_id") or "")
        for entry in normalize_entries(roster_id, _json_list(row.get("entries_json")))
    }
    if entry_id not in valid_ids:
        raise RosterError("model roster entry not found")
    value = str(credential or "")
    if not value:
        raise RosterError("credential must not be empty")
    secrets = _secret_entries(await _read_secret(db, roster_id))
    secrets[str(entry_id)] = value
    await _write_secret(
        db, roster_id,
        json.dumps({"v": ROSTER_SECRET_VERSION, "entries": secrets}, separators=(",", ":")),
    )
    return {candidate: True for candidate in secrets}


async def delete_roster_entry_credential(
    roster_id: str, entry_id: str, *, db=None,
) -> dict[str, bool]:
    if db is None:
        from app.db import get_db
        db = get_db()
    row = await db.get_model_roster(roster_id)
    if not row:
        raise RosterError("model roster not found")
    valid_ids = {
        str(entry.get("entry_id") or "")
        for entry in normalize_entries(roster_id, _json_list(row.get("entries_json")))
    }
    if entry_id not in valid_ids:
        raise RosterError("model roster entry not found")
    secrets = _secret_entries(await _read_secret(db, roster_id))
    secrets.pop(str(entry_id), None)
    bundle = (
        json.dumps({"v": ROSTER_SECRET_VERSION, "entries": secrets}, separators=(",", ":"))
        if secrets else ""
    )
    await _write_secret(db, roster_id, bundle)
    return {candidate: True for candidate in secrets}


async def load_platform_roster_config(roster_id: str, *, db=None) -> Optional[dict[str, Any]]:
    if db is None:
        from app.db import get_db
        db = get_db()
    published = getattr(db, "get_published_model_roster", None)
    row = await published(roster_id) if published is not None else None
    if row is None and published is None:
        row = await db.get_model_roster(roster_id)
        if not row:
            row = await db.get_model_roster_by_slug(roster_id)
    if not row or str(row.get("status") or "").lower() != "published":
        return None
    config = roster_to_config(row, await _read_secret(db, str(row.get("id") or roster_id)))
    return config if config.get("multi_providers") else None


async def resolve_platform_roster_config(user_id: str, *, db=None) -> Optional[dict[str, Any]]:
    """Resolve exactly the user's published tier roster; invalid refs fail closed."""
    if db is None:
        from app.db import get_db
        db = get_db()
    roster_id = ""
    try:
        from app.entitlements.service import resolve_capabilities
        safe = await resolve_capabilities(user_id, db=db)
        roster_id = str((safe.get("models") or {}).get("roster_id") or roster_id)
    except Exception:
        logger.debug("Could not resolve tier roster for %s", str(user_id)[:12], exc_info=True)
    if not roster_id:
        return None
    return await load_platform_roster_config(roster_id, db=db)


async def _upsert_from_config(
    db,
    roster_id: str,
    config: Mapping[str, Any],
    *,
    shape: Optional[Mapping[str, Any]] = None,
    source: str,
) -> dict[str, Any]:
    entries, default_entry_id, secret_ref = config_to_roster(config, roster_id)
    base = dict(shape or {})
    existing = await db.get_model_roster(roster_id)
    fields = {
        "slug": str(base.get("slug") or roster_id),
        "name": str(base.get("name") or roster_id),
        "description": str(base.get("description") or ""),
        "entries_json": entries,
        "default_entry_id": default_entry_id,
        "status": "published",
        "source": source,
    }
    if existing:
        comparable = dict(fields)
        comparable["entries_json"] = json.dumps(entries, separators=(",", ":"), sort_keys=True)
        if all(existing.get(key) == value for key, value in comparable.items()):
            row = existing
        else:
            row = await db.upsert_model_roster(roster_id, **fields)
    else:
        row = await db.upsert_model_roster(roster_id, **fields)
    await _write_secret(db, roster_id, secret_ref)
    publish = getattr(db, "publish_model_roster", None)
    if publish is not None and int(row.get("published_revision") or 0) != int(row.get("revision") or 1):
        row = await publish(roster_id, actor_user_id=source, action=source)
    return row


async def sync_legacy_platform_config(config: Mapping[str, Any], *, db=None, source: str = "legacy-migration") -> int:
    """Dual-write the legacy admin fallback into still-managed system rosters."""
    if db is None:
        from app.db import get_db
        db = get_db()
    shapes = {shape["id"]: shape for shape in load_seed_shapes()}
    changed = 0
    for roster_id in SYSTEM_ROSTER_IDS:
        existing = await db.get_model_roster(roster_id)
        if existing and str(existing.get("source") or "") not in _MANAGED_SOURCES:
            continue
        await _upsert_from_config(db, roster_id, config, shape=shapes.get(roster_id), source=source)
        changed += 1
    return changed


async def provision_system_rosters(*, db=None, env_config: Optional[Mapping[str, Any]] = None) -> dict[str, int]:
    """Idempotently seed named rosters and migrate the admin fallback once."""
    if db is None:
        from app.db import get_db
        db = get_db()
    legacy = None
    try:
        from app.admin.settings import _load_own_llm_config
        legacy = await _load_own_llm_config("admin")
    except Exception:
        logger.debug("Could not inspect legacy admin LLM fallback", exc_info=True)
    source_config = legacy if isinstance(legacy, Mapping) and legacy.get("model") else env_config
    source = "legacy-migration" if source_config is legacy else "environment"
    created = migrated = 0
    for shape in load_seed_shapes():
        roster_id = shape["id"]
        existing = await db.get_model_roster(roster_id)
        if existing:
            continue
        if source_config:
            await _upsert_from_config(db, roster_id, source_config, shape=shape, source=source)
            migrated += 1
        else:
            seeded = await db.upsert_model_roster(
                roster_id,
                slug=shape.get("slug") or roster_id,
                name=shape.get("name") or roster_id,
                description=shape.get("description") or "",
                entries_json=shape.get("entries_json") or [],
                default_entry_id=shape.get("default_entry_id"),
                status=shape.get("status") or "published",
                source=shape.get("source") or "system-seed",
                revision=int(shape.get("revision") or 1),
            )
            publish = getattr(db, "publish_model_roster", None)
            if publish is not None and seeded.get("status") == "published":
                await publish(roster_id, actor_user_id="system-seed", action="system-seed")
            created += 1
    return {"created": created, "migrated": migrated}


async def export_platform_rosters(*, db=None, strip_secrets: bool = False) -> dict[str, Any]:
    if db is None:
        from app.db import get_db
        db = get_db()
    rows = await db.list_model_rosters()
    exported = []
    for row in rows:
        item = {
            key: row.get(key)
            for key in (
                "id", "slug", "name", "description", "default_entry_id",
                "status", "revision", "source",
            )
        }
        item["entries"] = normalize_entries(str(row.get("id") or ""), _json_list(row.get("entries_json")))
        item["credential_bundle"] = "" if strip_secrets else await _read_secret(db, str(row.get("id") or ""))
        exported.append(item)
    return {"v": 1, "rosters": exported}


async def import_platform_rosters(payload: Mapping[str, Any], *, db=None, overwrite: bool = False) -> int:
    if db is None:
        from app.db import get_db
        db = get_db()
    rosters = payload.get("rosters") if isinstance(payload, Mapping) else None
    if not isinstance(rosters, list):
        raise RosterError("model roster bundle must contain a rosters list")
    applied = 0
    for raw in rosters:
        if not isinstance(raw, Mapping):
            raise RosterError("model roster bundle entries must be objects")
        roster_id = str(raw.get("id") or "").strip()
        slug = str(raw.get("slug") or roster_id).strip()
        if not roster_id or not slug:
            raise RosterError("imported roster requires id and slug")
        existing = await db.get_model_roster(roster_id)
        if existing and not overwrite:
            continue
        entries = normalize_entries(roster_id, _json_list(raw.get("entries") or raw.get("entries_json")))
        entry_ids = {entry["entry_id"] for entry in entries}
        default_id = str(raw.get("default_entry_id") or "") or None
        if default_id and default_id not in entry_ids:
            raise RosterError(f"default_entry_id {default_id!r} is not in roster {roster_id!r}")
        credential_bundle = str(raw.get("credential_bundle") or "")
        if credential_bundle:
            try:
                credential_payload = json.loads(credential_bundle)
            except (TypeError, ValueError) as exc:
                raise RosterError(f"invalid credential bundle for {roster_id!r}") from exc
            if (
                not isinstance(credential_payload, Mapping)
                or int(credential_payload.get("v") or 0) != ROSTER_SECRET_VERSION
                or not isinstance(credential_payload.get("entries"), Mapping)
            ):
                raise RosterError(f"invalid credential bundle for {roster_id!r}")
            keys = _secret_entries(credential_bundle)
            if set(keys) - entry_ids:
                raise RosterError(f"credential bundle contains unknown entry IDs for {roster_id!r}")
        row = await db.upsert_model_roster(
            roster_id,
            slug=slug,
            name=str(raw.get("name") or slug),
            description=str(raw.get("description") or ""),
            entries_json=entries,
            default_entry_id=default_id,
            status="draft" if str(raw.get("status") or "published") == "published" else str(raw.get("status")),
            source="bootstrap-import",
        )
        if credential_bundle:
            await _write_secret(db, roster_id, credential_bundle)
        if str(raw.get("status") or "published") == "published":
            publish = getattr(db, "publish_model_roster", None)
            if publish is not None:
                await publish(roster_id, actor_user_id="bootstrap-import", action="bootstrap-import")
            else:
                await db.upsert_model_roster(roster_id, status="published")
        applied += 1
    return applied

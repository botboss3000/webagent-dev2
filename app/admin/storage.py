"""
Storage admin API: /admin/storage/*

Endpoints for the Storage modal in the Config UI:
  - DB provider config + test + bootstrap + activate
  - Secrets vault provider switch + test
  - Data migration export / import (JSON dump)

All endpoints are admin-only. The is_admin check is performed inside each
handler so unauthenticated probes get a 403, not a 500.
"""

import json
import logging
import os
from typing import Optional, Dict, Any

from fastapi import APIRouter, HTTPException, Request, Response, Query
from fastapi.responses import StreamingResponse, JSONResponse
from pydantic import BaseModel

from app.db.connection_config import (
    DBConnectionConfig,
    PROVIDERS as DB_PROVIDERS,
    PROVIDER_META,
    load_config,
    save_config,
    is_postgres_provider,
)
from app.secrets import (
    get_secrets,
    get_mode as get_secrets_mode,
    set_mode as set_secrets_mode,
    list_providers as list_secret_providers,
    get_secrets_status,
    set_provider_config as set_secrets_provider_config,
    set_provider_token as set_secrets_provider_token,
    token_location as secrets_token_location,
    construct as construct_secrets_provider,
)
from app.genui_store import (
    get_mode as get_genui_mode,
    set_mode as set_genui_mode,
    list_modes as list_genui_modes,
    get_status as get_genui_status,
)
from app.attachments_store import (
    get_mode as get_attachments_mode,
    set_mode as set_attachments_mode,
    list_modes as list_attachments_modes,
    get_provider_config as get_attachments_provider_config,
    set_provider_config as set_attachments_provider_config,
    get_status as get_attachments_status,
)
from app.encryption import (
    get_encryption,
    get_level as get_enc_level,
    set_level as set_enc_level,
    get_status as get_enc_status,
    list_levels as list_enc_levels,
)
from app.encryption.vault_keys import get_vault_keys
from app.db.schema import render_sqlite, render_postgres, render_mysql, DIALECTS
from app.db.schema.tables import TABLE_ORDER

logger = logging.getLogger(__name__)
router = APIRouter(prefix="/admin/storage", tags=["admin-storage"])


# ── Helpers ─────────────────────────────────────────────────────────────────


def _env_locked() -> bool:
    return os.environ.get("WEBAGENT_CONFIG_SOURCE", "").lower() == "env"


async def _require_admin(uid: str) -> None:
    """Ensure uid is an admin. Matches the pattern in app/admin/users.py.

    Routes through the shared chokepoint so 'open' access mode grants the
    bootstrap admin to a tokenless tunnel caller; non-open modes still require a
    real DB admin id. See app.auth.identity.resolve_admin_uid."""
    from app.auth.identity import resolve_admin_uid
    if not await resolve_admin_uid(uid):
        raise HTTPException(status_code=403, detail="Admin required")


# ── Models ──────────────────────────────────────────────────────────────────


class DBConfigBody(BaseModel):
    requesting_user_id: str
    provider: str
    host: Optional[str] = None
    port: Optional[int] = None
    database: str = ""
    username: Optional[str] = None
    password: Optional[str] = None  # plaintext, stored to secrets backend on save
    password_secret_key: Optional[str] = None  # alternative: reuse an existing key
    ssl_mode: str = "require"
    schema_name: str = "public"
    options: Dict[str, Any] = {}


class SecretsModeBody(BaseModel):
    requesting_user_id: str
    provider: str


class SecretsProviderConfigBody(BaseModel):
    requesting_user_id: str
    provider: str
    config: Dict[str, Any] = {}
    # The sensitive access token, entered in the UI. Optional / blank = keep the
    # currently-stored token (so re-saving other fields never wipes it). Stored
    # keyring-first with a local-file fallback; NEVER echoed back.
    token: Optional[str] = None


class SecretsTestBody(BaseModel):
    requesting_user_id: str
    # When given, probe THIS provider (reading its saved config) without
    # activating it; otherwise test the currently-active backend.
    provider: Optional[str] = None


class GenuiModeBody(BaseModel):
    requesting_user_id: str
    mode: str


class ActivateBody(BaseModel):
    requesting_user_id: str


class QrBody(BaseModel):
    requesting_user_id: str
    # Arbitrary text to render as a scannable QR. Used by the Application Data
    # "Share (QR)" button to hand the current DB connection config to another
    # device (the payload is a compact base64 blob built in ui/shared/js/storage.js).
    text: str = ""


class SchemaSQLBody(BaseModel):
    requesting_user_id: str
    dialect: str = "postgres"
    # idempotent controls the *display/copy* SQL only (this endpoint is never an
    # execution path — the live bootstrap calls render_postgres() directly with
    # the idempotent default of True). Default here is TRUE: every remaining
    # provider is plain Postgres that handles CREATE TABLE IF NOT EXISTS, so the
    # re-runnable variant is always safe. A stale cached frontend that omits the
    # flag gets the safe, re-runnable copy.
    idempotent: bool = True


# ── Routes: config + status ─────────────────────────────────────────────────


@router.get("/config")
async def get_storage_config(requesting_user_id: str = Query(...)):
    """Return current DB config + secrets config + provider lists."""
    await _require_admin(requesting_user_id)

    cfg = load_config()
    return {
        "env_locked": _env_locked(),
        "db": {
            "active": cfg.to_dict(),
            "providers": list(DB_PROVIDERS),
            "provider_meta": PROVIDER_META,
        },
        "secrets": get_secrets_status(),
        "genui": get_genui_status(),
    }


@router.post("/qr")
async def make_qr(body: QrBody):
    """Render arbitrary text as an inline SVG QR code (admin-only).

    Reuses app.remote_access.netinfo.qr_svg — the same server-side generator the
    Remote Access + Deploy cards use — so the config-share QR matches those in
    look and dependency handling. Returns qr_svg=None when the optional ``qrcode``
    package isn't installed (the frontend then shows a clear "install qrcode" hint).
    """
    await _require_admin(body.requesting_user_id)
    from app.remote_access import netinfo
    svg = netinfo.qr_svg(body.text or "")
    return {"ok": bool(svg), "qr_svg": svg}


# ── Routes: bootstrap setup bundle ──────────────────────────────────────────
# One portable "setup code" that carries this install's DB + vault + LLM (key +
# preset model roster) to a freshly-cloned install. This UI (export/preview/
# apply) needs no password — the endpoints are already admin-only, and the code
# is encrypted with a key that travels with it (app/admin/bootstrap_bundle.py,
# encrypt_code_open/decode_bundle). It never carries the admin login (the real
# password can't be recovered from its stored hash). The unauthenticated
# first-run variant lives in app/auth/__init__.py (setup_bundle) since a fresh
# install has no admin to gate on yet, and still uses a password-locked code —
# see bootstrap_bundle.py's module docstring. The former per-row DB "Share (QR)"
# folded into this bundle (database is now just one selectable section).


class BootstrapExportBody(BaseModel):
    requesting_user_id: str
    # Which sections to include: any of llm/database/vault.
    sections: list = []


class BootstrapImportBody(BaseModel):
    requesting_user_id: str
    code: str = ""
    # section → "fill" | "overwrite" | "skip" (apply only). Absent = fill-blanks.
    choices: Dict[str, str] = {}


@router.post("/bootstrap/export")
async def bootstrap_export(body: BootstrapExportBody):
    """Package the chosen sections of THIS install into a portable setup code."""
    await _require_admin(body.requesting_user_id)
    from app.admin import bootstrap_bundle as bb
    sections = [s for s in (body.sections or list(bb.ALL_SECTIONS)) if s != bb.SECTION_ADMIN]
    try:
        code = await bb.export_code(sections)
    except bb.BundleError as e:
        return {"ok": False, "error": str(e)}
    return {"ok": True, "code": code}


@router.post("/bootstrap/preview")
async def bootstrap_preview(body: BootstrapImportBody):
    """Decode a pasted code and report, per section, what it carries and
    whether this install already has it — WITHOUT applying anything."""
    await _require_admin(body.requesting_user_id)
    from app.admin import bootstrap_bundle as bb
    try:
        return {"ok": True, **await bb.preview(body.code)}
    except bb.BundleError as e:
        return {"ok": False, "error": str(e)}


@router.post("/bootstrap/apply")
async def bootstrap_apply(body: BootstrapImportBody):
    """Apply a pasted code's sections, each honouring its merge choice."""
    await _require_admin(body.requesting_user_id)
    if _env_locked():
        raise HTTPException(status_code=403, detail="Config is env-locked; edit deployment env vars to change.")
    from app.admin import bootstrap_bundle as bb
    try:
        bundle = bb.decode_bundle(body.code)
    except bb.BundleError as e:
        return {"ok": False, "error": str(e)}
    return await bb.apply_bundle(bundle, body.choices or {})


# ── Routes: DB provider operations ──────────────────────────────────────────


@router.post("/db/test")
async def test_db_connection(body: DBConfigBody):
    """Test a connection without saving config."""
    await _require_admin(body.requesting_user_id)

    cfg = DBConnectionConfig(
        provider=body.provider,
        host=body.host,
        port=body.port,
        database=body.database,
        username=body.username,
        ssl_mode=body.ssl_mode,
        schema=body.schema_name,
        options=body.options,
    )
    errs = cfg.validate()
    if errs:
        return {"ok": False, "error": "Validation: " + "; ".join(errs)}

    # Resolve password from body (plain) — preferred during test, never saved unless /db/config called.
    password = body.password
    if not password and body.password_secret_key:
        from app.db import cred_store
        password = cred_store.get_secret(body.password_secret_key)
        if not password:
            password = await get_secrets().get(body.password_secret_key)

    if cfg.provider == "sqlite":
        # Verify file path is writable
        from app.db.local import DEFAULT_DB_PATH
        path = cfg.database or DEFAULT_DB_PATH
        try:
            d = os.path.dirname(path) or "."
            return {"ok": os.access(d, os.W_OK), "path": path, "writable": os.access(d, os.W_OK)}
        except Exception as e:
            return {"ok": False, "error": str(e)}

    if cfg.dialect == "postgres":
        from app.db.postgres_backend import test_connection as pg_test
        return await pg_test(cfg, password=password)

    # MySQL not supported yet
    return {"ok": False, "error": f"Provider '{cfg.provider}' connection test not yet implemented."}


@router.post("/db/schema-sql")
async def get_schema_sql(body: SchemaSQLBody):
    """Return DDL string rendered for the requested dialect."""
    await _require_admin(body.requesting_user_id)
    if body.dialect not in DIALECTS:
        raise HTTPException(status_code=400, detail=f"Unknown dialect; must be one of {DIALECTS}")
    if body.dialect == "sqlite":
        return {"dialect": body.dialect, "ddl": render_sqlite()}
    if body.dialect == "postgres":
        return {"dialect": body.dialect, "ddl": render_postgres(idempotent=body.idempotent)}
    return {"dialect": body.dialect, "ddl": render_mysql()}


@router.post("/db/bootstrap")
async def bootstrap_schema(body: DBConfigBody):
    """Connect to the configured remote DB and CREATE TABLE all tables."""
    await _require_admin(body.requesting_user_id)

    cfg = DBConnectionConfig(
        provider=body.provider,
        host=body.host, port=body.port,
        database=body.database, username=body.username,
        ssl_mode=body.ssl_mode, schema=body.schema_name,
        options=body.options,
    )
    password = body.password
    if not password and body.password_secret_key:
        from app.db import cred_store
        password = cred_store.get_secret(body.password_secret_key)
        if not password:
            password = await get_secrets().get(body.password_secret_key)

    if cfg.dialect == "postgres":
        from app.db.postgres_backend import bootstrap_schema as pg_bootstrap
        return await pg_bootstrap(cfg, password=password)

    if cfg.provider == "sqlite":
        # SQLite: bootstrap means initializing LocalBackend at the configured path
        try:
            from app.db.local import LocalBackend
            lb = LocalBackend(db_path=cfg.database or None)
            return {"ok": True, "message": f"SQLite initialized at {lb._db_path}"}
        except Exception as e:
            return {"ok": False, "error": str(e)}

    return {"ok": False, "error": f"Bootstrap not implemented for provider '{cfg.provider}'"}


@router.post("/db/config")
async def save_db_config(body: DBConfigBody):
    """
    Persist DB connection config (and store password in active secrets backend).
    Does NOT activate it — call /db/activate to switch the live backend.
    """
    await _require_admin(body.requesting_user_id)
    if _env_locked():
        raise HTTPException(status_code=403, detail="Config is env-locked; edit deployment env vars to change.")

    cfg = DBConnectionConfig(
        provider=body.provider,
        host=body.host, port=body.port,
        database=body.database, username=body.username,
        ssl_mode=body.ssl_mode, schema=body.schema_name,
        options=body.options,
    )
    errs = cfg.validate()
    if errs:
        raise HTTPException(status_code=400, detail="; ".join(errs))

    # Persist connection secrets to the database-INDEPENDENT credential store
    # (OS keyring / file fallback) so a remote DB can be reached on cold start
    # without the password living inside that same database. The legacy vault
    # write is kept as a best-effort secondary (so the admin Secrets UI still
    # lists it) and must never fail the save.
    from app.db import cred_store
    secrets = get_secrets()

    async def _vault_set_best_effort(k: str, v: str) -> None:
        try:
            await secrets.set(k, v)
        except Exception as e:
            logger.warning("Secondary vault set failed for %s (%s); keyring store is authoritative", k, e)

    if body.password:
        key = body.password_secret_key or f"db_password_{cfg.provider}"
        cred_store.set_secret(key, body.password)
        await _vault_set_best_effort(key, body.password)
        cfg.password_secret_key = key
    elif body.password_secret_key:
        cfg.password_secret_key = body.password_secret_key

    save_config(cfg)
    return {"ok": True, "saved": cfg.to_dict()}


async def _activate_saved_backend() -> tuple[dict, int]:
    """Switch the live backend to the SAVED connection config and return
    ``(payload, http_status)``.

    This is the real activation — resolve the password, put it where the
    connection opener reads it, rebuild the backend, and verify it actually
    connected — factored out of the Database-page Activate endpoint so the
    setup-bundle / boot provisioning path can reuse it verbatim. That way an
    imported config comes up connected with no manual Activate click, and both
    callers stay in lock-step (self-heal, revert-on-failure, schema check).
    Refuses (and reverts) rather than leave the app pointed at an unreachable or
    empty database.
    """
    cfg = load_config()

    # SQLite — reset the cached backend so the next get_db() rebuilds it.
    if cfg.provider == "sqlite":
        from app.db import reset_db_instance, get_db_stats
        reset_db_instance()
        return {"ok": True, "mode": "local", "stats": await get_db_stats()}, 200

    # Postgres-family — all share the live asyncpg backend, routed by dialect.
    if cfg.dialect == "postgres":
        # Resolve the password and stash it in the process env. The authoritative
        # source is the database-independent credential store (keyring/file); we
        # self-heal older installs whose password only lived in the legacy vault
        # by copying it across so future cold starts resolve without re-activation.
        from app.db import cred_store
        key_name = cfg.password_secret_key or f"db_password_{cfg.provider}"
        pw = cred_store.get_secret(key_name)
        if not pw:
            try:
                pw = await get_secrets().get(key_name)
            except Exception:
                pw = None
            if pw:
                cred_store.set_secret(key_name, pw)
        if pw:
            os.environ["WEBAGENT_DB_PASSWORD"] = pw
            if cfg.password_secret_key != key_name:
                cfg.password_secret_key = key_name
                save_config(cfg)
        try:
            from app.db import reset_db_instance, get_db, get_db_stats
            reset_db_instance()
            backend = get_db()  # builds PostgresBackend (raises/falls back on failure)
            if type(backend).__name__ not in ("_PostgresBackend",) and not hasattr(backend, "_pool"):
                # get_db fell back (bad creds / unreachable) — surface the problem.
                return {
                    "ok": False,
                    "error": "Postgres activation failed — check host/credentials and that the server is reachable.",
                }, 502
            return {"ok": True, "provider": cfg.provider, "stats": await get_db_stats()}, 200
        except Exception as e:
            return {"ok": False, "error": str(e)}, 502

    # MySQL / other: not wired as a live backend yet.
    return {
        "ok": False,
        "error": (
            f"Provider '{cfg.provider}' can be tested and bootstrapped but is not yet "
            "wired as a live backend."
        ),
    }, 501


@router.post("/db/activate")
async def activate_db(body: ActivateBody):
    """
    Switch the live backend to the saved config. Refuses on backends not yet
    implemented (raw postgres data path is stubbed until ported).
    """
    await _require_admin(body.requesting_user_id)
    payload, status = await _activate_saved_backend()
    if status == 200:
        return payload
    return JSONResponse(status_code=status, content=payload)


# ── Routes: secrets vault ───────────────────────────────────────────────────


@router.get("/secrets/status")
async def secrets_status(requesting_user_id: str = Query(...)):
    await _require_admin(requesting_user_id)
    return get_secrets_status()


@router.post("/secrets/mode")
async def set_secrets_provider(body: SecretsModeBody):
    await _require_admin(body.requesting_user_id)
    if _env_locked():
        raise HTTPException(status_code=403, detail="Config is env-locked.")
    if body.provider not in list_secret_providers():
        raise HTTPException(status_code=400, detail=f"Unknown provider {body.provider}")
    try:
        set_secrets_mode(body.provider)
    except Exception as e:
        raise HTTPException(status_code=400, detail=str(e))
    return {"ok": True, "provider": body.provider}


@router.post("/secrets/provider-config")
async def set_secrets_provider_cfg(body: SecretsProviderConfigBody):
    """Persist a remote vault's connection details entered in the UI.

    Non-secret fields (address / URL / project / region / mount / prefix / ids)
    go to a local file outside the app DB; the access token is stored
    keyring-first with a file fallback. The token is never echoed back — only
    where it landed ('keyring' / 'file') and whether one is now saved."""
    await _require_admin(body.requesting_user_id)
    if _env_locked():
        raise HTTPException(status_code=403, detail="Config is env-locked.")
    if body.provider not in list_secret_providers():
        raise HTTPException(status_code=400, detail=f"Unknown provider {body.provider}")
    try:
        set_secrets_provider_config(body.provider, body.config or {})
        token_storage = None
        if body.token:  # blank/None = keep existing token untouched
            token_storage = set_secrets_provider_token(body.provider, body.token)
    except Exception as e:
        raise HTTPException(status_code=400, detail=str(e))
    return {
        "ok": True,
        "provider": body.provider,
        "config": body.config or {},
        "token_storage": token_storage or secrets_token_location(body.provider),
        "token_saved": secrets_token_location(body.provider) is not None,
    }


@router.post("/secrets/test")
async def test_secrets(body: SecretsTestBody):
    await _require_admin(body.requesting_user_id)
    if body.provider:
        if body.provider not in list_secret_providers():
            raise HTTPException(status_code=400, detail=f"Unknown provider {body.provider}")
        try:
            backend = construct_secrets_provider(body.provider)
        except Exception as e:
            return {"ok": False, "message": f"Could not construct {body.provider}: {e}"}
    else:
        backend = get_secrets()
    return await backend.test_connection()


# ── Routes: server restart ──────────────────────────────────────────────────
# A self-restart so a settings change that needs a fresh process (e.g. a clean
# vault cutover) can be applied from the UI with one click. The restart is
# supervisor-cooperative (app/relauncher.py): a detached relauncher waits for
# this process to exit, lets any external supervisor revive it first, and only
# relaunches run.py itself if nothing else does — so the server always comes back.


class ServerRestartBody(BaseModel):
    requesting_user_id: str


@router.get("/server/restart-info")
async def server_restart_info(requesting_user_id: str = Query(...)):
    """Whether this host can restart itself (so the UI knows to offer the button
    or fall back to asking the user to restart manually)."""
    await _require_admin(requesting_user_id)
    from app.relauncher import auto_restart_available
    ok, reason = auto_restart_available()
    return {"auto_restart_available": ok, "reason": reason}


@router.post("/server/restart")
async def server_restart(body: ServerRestartBody):
    """Restart the server process. Returns immediately; the process exits a beat
    later and the relauncher (or an external supervisor) brings it back up."""
    await _require_admin(body.requesting_user_id)
    from app.relauncher import trigger_restart
    result = trigger_restart()
    if not result.get("auto_restart"):
        # Can't self-revive on this host — don't kill the server; tell the UI.
        raise HTTPException(status_code=409,
                            detail=result.get("reason") or "Automatic restart is not available here.")
    logger.warning("Server restart requested from the admin UI — relaunching...")
    return result


# ── Routes: genui store ─────────────────────────────────────────────────────


@router.get("/genui/status")
async def genui_status(requesting_user_id: str = Query(...)):
    await _require_admin(requesting_user_id)
    return get_genui_status()


@router.post("/genui/mode")
async def set_genui_store_mode(body: GenuiModeBody):
    await _require_admin(body.requesting_user_id)
    if _env_locked():
        raise HTTPException(status_code=403, detail="Config is env-locked.")
    if body.mode not in list_genui_modes():
        raise HTTPException(status_code=400, detail=f"Unknown mode {body.mode}")
    try:
        set_genui_mode(body.mode)
    except Exception as e:
        raise HTTPException(status_code=400, detail=str(e))
    return {"ok": True, "mode": body.mode}


class GenuiMigrateBody(BaseModel):
    requesting_user_id: str
    # When true (single-tenant only), migrate every discoverable user's on-disk
    # pages; otherwise migrate just the requesting admin's own pages.
    all_users: bool = False
    # Replace same-named pages already in the account store (default: keep them).
    overwrite: bool = False


@router.post("/genui/migrate")
async def migrate_genui_to_account(body: GenuiMigrateBody):
    """Copy existing on-disk (filesystem) Gen UI pages into the account database
    store, so they follow the user across devices once storage is set to synced.
    Non-destructive: the source files are left in place."""
    await _require_admin(body.requesting_user_id)
    from app.genui_store.migrate import migrate_all, migrate_user

    if body.all_users:
        # All-users only makes sense on a single shared account database. In
        # user-BYOD mode each user owns a separate DB, so a cross-user write
        # would land in the wrong tenant — refuse and let the caller migrate
        # per-user (the default) instead.
        try:
            from app.admin.settings import get_user_byod_enabled
            if get_user_byod_enabled():
                raise HTTPException(
                    status_code=400,
                    detail="All-users migration is unavailable in user-BYOD mode; "
                           "each user's own device migrates their pages.",
                )
        except HTTPException:
            raise
        except Exception:
            pass
        summary = await migrate_all(overwrite=body.overwrite)
        return {"ok": True, "scope": "all", **summary}

    result = await migrate_user(body.requesting_user_id, overwrite=body.overwrite)
    return {
        "ok": True,
        "scope": "self",
        "totals": {
            "users": 1,
            "copied": len(result["copied"]),
            "skipped": len(result["skipped"]),
            "failed": len(result["failed"]),
        },
        "results": [result],
    }


# ── Routes: attachment store ───────────────────────────────────────────────


class AttachmentsModeBody(BaseModel):
    requesting_user_id: str
    mode: str


class AttachmentsProviderConfigBody(BaseModel):
    requesting_user_id: str
    provider: str
    config: Dict[str, Any] = {}


@router.get("/attachments/status")
async def attachments_status(requesting_user_id: str = Query(...)):
    """Return the active attachment backend + persisted provider configs + counts."""
    await _require_admin(requesting_user_id)
    status = get_attachments_status()
    # Per-backend attachment counts — use a GROUP BY query instead of fetching
    # every row and counting client-side (which causes significant load with
    # large attachment tables). Falls back to the generic client if raw SQL
    # isn't available for this backend.
    counts: Dict[str, int] = {m: 0 for m in status.get("available", [])}
    try:
        from app.db import get_db
        db = get_db()
        from app.db import is_remote_db
        if not is_remote_db():
            # SQLite — raw GROUP BY via the backend's own connection.
            try:
                conn = db._get_conn()
                cursor = conn.execute(
                    "SELECT COALESCE(storage_provider, 'local') AS prov, COUNT(*) AS cnt "
                    "FROM attachments GROUP BY storage_provider"
                )
                for prov, cnt in cursor.fetchall():
                    counts[prov] = cnt
                conn.close()
            except Exception:
                pass
        else:
            # Remote backend (Postgres) — use the generic client.
            try:
                raw = db.get_raw_client()
                res = raw.table("attachments").select("storage_provider").execute()
                for row in (res.data or []):
                    prov = (row or {}).get("storage_provider") or "local"
                    counts[prov] = counts.get(prov, 0) + 1
            except Exception:
                pass
    except Exception:
        pass
    status["counts"] = counts
    return status


@router.post("/attachments/mode")
async def set_attachments_store_mode(body: AttachmentsModeBody):
    """Switch the active attachment backend (admin only, global)."""
    await _require_admin(body.requesting_user_id)
    if _env_locked():
        raise HTTPException(status_code=403, detail="Config is env-locked.")
    if body.mode not in list_attachments_modes():
        raise HTTPException(status_code=400, detail=f"Unknown mode {body.mode}")
    try:
        set_attachments_mode(body.mode)
    except Exception as e:
        raise HTTPException(status_code=400, detail=str(e))
    return {"ok": True, "mode": body.mode}


@router.post("/attachments/provider-config")
async def set_attachments_provider_cfg(body: AttachmentsProviderConfigBody):
    """Persist provider-specific config (bucket, region, prefix, etc.)."""
    await _require_admin(body.requesting_user_id)
    if _env_locked():
        raise HTTPException(status_code=403, detail="Config is env-locked.")
    if body.provider not in list_attachments_modes():
        raise HTTPException(status_code=400, detail=f"Unknown provider {body.provider}")
    try:
        set_attachments_provider_config(body.provider, body.config or {})
    except Exception as e:
        raise HTTPException(status_code=400, detail=str(e))
    return {"ok": True, "provider": body.provider, "config": get_attachments_provider_config(body.provider)}


@router.post("/attachments/test")
async def test_attachments_backend(body: AttachmentsModeBody):
    """Construct the configured backend and write+read+delete a probe object."""
    await _require_admin(body.requesting_user_id)
    if body.mode not in list_attachments_modes():
        raise HTTPException(status_code=400, detail=f"Unknown mode {body.mode}")
    from app.attachments_store import get_store_for_provider
    try:
        store = get_store_for_provider(body.mode)
    except Exception as e:
        return {"ok": False, "error": f"Could not construct backend: {e}"}

    if body.mode == "browser":
        return {"ok": True, "note": "Browser backend stores bytes in the client; nothing to test server-side."}

    probe = b"webagent-attachments-probe"
    try:
        result = await store.store(
            user_id="__probe__",
            file_bytes=probe,
            filename="probe.txt",
            mime_type="text/plain",
        )
        path = result["storage_path"]
        got = await store.read(path)
        await store.delete(path)
        if got != probe:
            return {"ok": False, "error": "Probe round-trip mismatch."}
        return {"ok": True, "storage_path": path, "url_sample": result.get("public_url", "")}
    except Exception as e:
        return {"ok": False, "error": str(e)}


# ── Routes: data migration ──────────────────────────────────────────────────


@router.post("/migrate/export")
async def export_data(requesting_user_id: str = Query(...)):
    """
    Stream a JSON dump of all tables from the current active backend.
    Caller saves the response to a file; that file is the migration payload.
    """
    await _require_admin(requesting_user_id)
    from app.db.browser_policy import require_export_enabled
    try:
        require_export_enabled()
    except PermissionError as exc:
        raise HTTPException(status_code=403, detail=str(exc))
    from app.db.migration import iter_export_json
    return StreamingResponse(
        iter_export_json(),
        media_type="application/json",
        headers={
            "Content-Disposition": 'attachment; filename="webagent-export.json"'
        },
    )


@router.post("/migrate/import")
async def import_data(request: Request, requesting_user_id: str = Query(...)):
    """
    Load a JSON dump into the current active backend.
    Body: the previously-exported JSON document.
    """
    await _require_admin(requesting_user_id)
    try:
        payload = await request.json()
    except Exception:
        raise HTTPException(status_code=400, detail="Body must be valid JSON")
    from app.db.migration import import_payload
    result = await import_payload(payload)
    return result


@router.get("/migrate/info")
async def migrate_info(requesting_user_id: str = Query(...)):
    """Return table order + estimated row counts from current backend."""
    await _require_admin(requesting_user_id)
    from app.db import get_db
    db = get_db()
    raw = db.get_raw_client()
    counts = {}
    for tbl in TABLE_ORDER:
        try:
            res = raw.table(tbl).select("id").execute()
            counts[tbl] = len(res.data or [])
        except Exception:
            counts[tbl] = None
    return {"tables": TABLE_ORDER, "counts": counts}


# ── Routes: encryption ──────────────────────────────────────────────────────


class EncLevelBody(BaseModel):
    requesting_user_id: str
    level: str
    settings: Optional[Dict[str, Any]] = None
    confirm: bool = False  # required when level == "none" (decrypts data)


class EncRotateDekBody(BaseModel):
    requesting_user_id: str
    user_id: str


class EncSimpleBody(BaseModel):
    requesting_user_id: str


def _enc_safety_check(level: str) -> Optional[str]:
    """
    Refuse to enable per-tenant encryption when wrapped DEKs would land in
    the very DB we're trying to protect (inline_db secrets + remote DB).
    Returns an error string if unsafe, else None.
    """
    if level not in ("field", "kms"):
        return None
    secrets_provider = get_secrets_mode()
    if secrets_provider != "inline_db":
        return None
    try:
        db_cfg = load_config()
        db_provider = (db_cfg.provider or "sqlite").lower()
    except Exception:
        db_provider = "sqlite"
    if db_provider == "sqlite":
        return None
    return (
        f"Refusing to enable '{level}' encryption: secrets vault is 'inline_db' while "
        f"the database backend is '{db_provider}'. Wrapped encryption keys would live "
        f"in the same remote DB they're protecting. Switch the secrets vault to "
        f"'os_keyring', 'gcp_secret_manager', or 'aws_secrets_manager' first."
    )


@router.get("/encryption/config")
async def encryption_config(requesting_user_id: str = Query(...)):
    """Return current encryption level + status + safety warnings."""
    await _require_admin(requesting_user_id)
    status = get_enc_status()
    secrets_provider = get_secrets_mode()
    try:
        db_cfg = load_config()
        db_provider = db_cfg.provider
    except Exception:
        db_provider = "sqlite"
    warn = None
    if secrets_provider == "inline_db" and db_provider != "sqlite":
        warn = (
            "Secrets vault is 'inline_db' but the database backend is remote. "
            "Per-tenant encryption keys would live in the same DB they protect. "
            "Switch the secrets vault before enabling encryption."
        )
    return {
        **status,
        "workspace_encryption": {
            "enabled": True,
            "key_management": "server_managed_workspace_dek",
            "file_cipher": "AES-256-GCM",
            "legacy_plaintext_readable": True,
            "excluded_live_storage": [
                "workspace SQLite databases", "GenUI source", "Chrome profiles",
            ],
        },
        "secrets_provider": secrets_provider,
        "db_provider": db_provider,
        "warning": warn,
    }


@router.post("/encryption/level")
async def encryption_set_level(body: EncLevelBody):
    """Switch encryption level and reset the cached DB wrapper."""
    await _require_admin(body.requesting_user_id)
    if _env_locked():
        raise HTTPException(status_code=403, detail="Config is env-locked.")
    if body.level not in list_enc_levels():
        raise HTTPException(status_code=400, detail=f"Unknown level {body.level}")

    # Refuse not-yet-wired levels (full_db / kms placeholders, or a method whose
    # optional dependency is missing) loudly. Without this the level would save,
    # then get_encryption() would silently fall back to NoEncryption — a green
    # "success" that leaves data unencrypted (false sense of security).
    from app.encryption import probe_level
    probe_err = probe_level(body.level, body.settings or {})
    if probe_err:
        return JSONResponse(status_code=501, content={
            "ok": False,
            "error": f"Encryption level '{body.level}' is not available yet: {probe_err}",
        })

    err = _enc_safety_check(body.level)
    if err:
        raise HTTPException(status_code=400, detail=err)

    if body.level == "none" and not body.confirm:
        raise HTTPException(
            status_code=400,
            detail=(
                "Switching to 'none' leaves existing encrypted rows undecryptable through the "
                "decorator path. Re-confirm with confirm=true after running "
                "/admin/storage/encryption/decrypt-all (which restores plaintext)."
            ),
        )

    try:
        set_enc_level(body.level, body.settings or {})
    except Exception as e:
        raise HTTPException(status_code=400, detail=str(e))

    # Force the DB factory to re-evaluate the wrapper.
    from app.db import reset_db_instance
    reset_db_instance()
    return {"ok": True, "level": body.level, "settings": body.settings or {}}


@router.post("/encryption/kek/generate")
async def encryption_generate_kek(body: EncSimpleBody):
    """Generate (or replace) the active KEK in the vault. Use only when none exists."""
    await _require_admin(body.requesting_user_id)
    vkm = get_vault_keys()
    existing = await vkm.get_kek("active")
    if existing is not None:
        return {
            "ok": False,
            "error": "An active KEK already exists. Use /encryption/kek/rotate to replace it.",
        }
    await vkm.generate_kek()
    return {"ok": True, "message": "Active KEK generated and stored in vault."}


@router.post("/encryption/kek/rotate")
async def encryption_rotate_kek(body: EncSimpleBody):
    """Rotate the KEK; re-wraps every DEK in the vault. Row data untouched."""
    await _require_admin(body.requesting_user_id)
    vkm = get_vault_keys()
    try:
        result = await vkm.rotate_kek()
        return {"ok": True, **result}
    except Exception as e:
        return {"ok": False, "error": str(e)}


@router.post("/encryption/dek/rotate")
async def encryption_rotate_dek(body: EncRotateDekBody):
    """Rotate one tenant's DEK and re-encrypt all of their rows."""
    await _require_admin(body.requesting_user_id)
    from app.encryption.migration import rotate_dek
    return await rotate_dek(body.user_id)


@router.post("/encryption/migrate")
async def encryption_migrate(body: EncSimpleBody):
    """Walk all tenants and encrypt any plaintext sensitive values. Idempotent."""
    await _require_admin(body.requesting_user_id)
    from app.encryption.migration import encrypt_all
    return await encrypt_all()


@router.post("/encryption/workspaces/migrate")
async def encryption_migrate_workspaces(body: EncSimpleBody):
    """Encrypt legacy user workspace artifacts with server-managed DEKs."""
    await _require_admin(body.requesting_user_id)
    from app.encryption.workspace_migration import migrate_all_workspaces
    return await migrate_all_workspaces()


@router.post("/encryption/decrypt-all")
async def encryption_decrypt_all(body: EncSimpleBody):
    """Decrypt every encrypted row back to plaintext. Required before level='none'."""
    await _require_admin(body.requesting_user_id)
    from app.encryption.migration import decrypt_all
    return await decrypt_all()


@router.get("/encryption/test")
async def encryption_test(requesting_user_id: str = Query(...)):
    """End-to-end health check: encrypt + decrypt a probe value for a synthetic tenant."""
    await _require_admin(requesting_user_id)
    enc = get_encryption()
    return await enc.health()


@router.get("/encryption/tenants")
async def encryption_tenants(requesting_user_id: str = Query(...)):
    """List every tenant that has key material, with their active + total DEK versions."""
    await _require_admin(requesting_user_id)
    vkm = get_vault_keys()
    return {"tenants": await vkm.list_tenants()}


# ── Full-database (SQLCipher) encryption at rest — opt-in, per-database ────────
# Whole-file encryption of each SQLite database (main/vault/logs/recordings/wiki),
# keyed by a per-file key held in the keyring vault. Distinct from the per-tenant
# 'field' level above (which only encrypts the credential secret column). The
# file migration runs at startup; toggling here sets intent + needs a restart.
# See app/db/db_crypto.py.

class FullDbEncBody(BaseModel):
    requesting_user_id: str
    db_id: str
    enabled: bool


@router.get("/encryption/full-db/status")
async def full_db_status(requesting_user_id: str = Query(...)):
    """Per-database at-rest encryption state + whether it can be enabled here."""
    await _require_admin(requesting_user_id)
    from app.db import db_crypto
    return db_crypto.status()


@router.post("/encryption/full-db/set")
async def full_db_set(body: FullDbEncBody):
    """Turn whole-file encryption on/off for one database. Applied on next restart."""
    await _require_admin(body.requesting_user_id)
    if _env_locked():
        raise HTTPException(status_code=403, detail="Config is env-locked.")
    from app.db import db_crypto
    if body.db_id not in db_crypto.DB_IDS:
        raise HTTPException(status_code=400, detail=f"Unknown database '{body.db_id}'")
    if body.enabled:
        ok, reason = db_crypto.can_enable()
        if not ok:
            # Refuse loudly (engine missing / no keyring) instead of a fake success.
            return JSONResponse(status_code=501, content={"ok": False, "error": reason})
        # Mint the per-file key now so the keyring is ready and any error surfaces
        # here rather than mid-migration at boot.
        try:
            from app.db import db_keys
            db_keys.get_or_create_raw_key(body.db_id)
        except Exception as e:
            return JSONResponse(status_code=500, content={"ok": False, "error": f"Key setup failed: {e}"})
    db_crypto.set_enabled(body.db_id, body.enabled)
    return {
        "ok": True,
        "db_id": body.db_id,
        "enabled": body.enabled,
        "restart_required": True,
        "message": (
            "Will be encrypted at rest on the next server restart."
            if body.enabled else
            "Will be decrypted back to plaintext on the next server restart."
        ),
        "status": db_crypto.status(),
    }


# ── Hybrid local-first backend (on by default) ────────────────────────────────
# Toggle for app/db/hybrid.py. Only meaningful with a reachable Postgres remote;
# on a local-only install it is a no-op. Applied on next restart (like full-DB
# encryption). See app/db/hybrid.py + app/db/README.md.

class HybridSetBody(BaseModel):
    requesting_user_id: str
    enabled: bool


@router.get("/hybrid/status")
async def hybrid_status(requesting_user_id: str = Query(...)):
    """Hybrid local-first on/off + whether a shared remote is actually active
    (the hybrid is a no-op without one)."""
    await _require_admin(requesting_user_id)
    from app.db.hybrid import hybrid_enabled
    from app.db import is_remote_db
    return {
        "enabled": hybrid_enabled(),
        "remote_active": is_remote_db(),
        "env_locked": _env_locked(),
    }


@router.post("/hybrid/set")
async def hybrid_set(body: HybridSetBody):
    """Turn the hybrid local-first backend on/off. Takes effect on next restart."""
    await _require_admin(body.requesting_user_id)
    if _env_locked():
        raise HTTPException(status_code=403, detail="Config is env-locked.")
    from app.db.hybrid import set_hybrid_enabled, hybrid_enabled
    from app.db import is_remote_db
    set_hybrid_enabled(body.enabled)
    remote = is_remote_db()
    if body.enabled and not remote:
        msg = ("Saved, but this install has no shared remote database — the hybrid "
               "layer stays a no-op until a Postgres remote is active.")
    elif body.enabled:
        msg = "Hybrid local-first mode will take effect on the next server restart."
    else:
        msg = "Hybrid mode off; the app uses the remote database directly on the next restart."
    return {
        "ok": True,
        "enabled": hybrid_enabled(),
        "remote_active": remote,
        "restart_required": True,
        "message": msg,
    }


# ── Config library: write the default config files from in-code templates ─────
# Deterministic seed (no LLM), admin-triggered, preview-first, never overwrites.
# Backs the "Config Files" row in the Data Management → Maintenance group.
# See app/util/config_seed.py.

class ConfigSeedBody(BaseModel):
    requesting_user_id: str


@router.get("/config-library/plan")
async def config_library_plan(requesting_user_id: str = Query(...)):
    """Preview which default config files a seed run would create vs skip."""
    await _require_admin(requesting_user_id)
    from app.util.config_seed import plan
    return plan()


@router.post("/config-library/seed")
async def config_library_seed(body: ConfigSeedBody):
    """Write the default for every missing config file (never overwrites existing)."""
    await _require_admin(body.requesting_user_id)
    from app.util.config_seed import seed_missing
    return seed_missing()


# ── Danger Zone: reset selected data groups / delete the whole install ────────
# Backs the "Danger Zone" card at the bottom of Data Settings. A reset can't run
# in-process (the app holds its own DB/vault open), so it writes a one-shot marker
# and self-restarts; the fresh boot wipes the selected file groups before anything
# opens them (see app/util/reset_boot.py + the run_pending_reset() call in
# app/main.py). Self-destruct spawns a detached helper OUTSIDE the repo that
# removes the whole folder once this server exits. Both are admin-only and gated
# behind an explicit confirmation the frontend collects (a hazard dialog for the
# reset; a typed folder-name phrase for the delete).


class ResetBody(BaseModel):
    requesting_user_id: str
    # Any subset of reset_boot.GROUPS ("db", "secrets", "attachments", "genui",
    # "logs"). Anything else is ignored server-side.
    groups: list[str] = []


class DeleteInstallBody(BaseModel):
    requesting_user_id: str
    # Must equal the install folder's name (returned by GET /reset/info) — a
    # deliberate speed-bump so this can never fire from a stray click.
    confirm_phrase: str = ""


@router.get("/reset/info")
async def reset_info(requesting_user_id: str = Query(...)):
    """Facts the Danger Zone needs to render: the install folder name (the phrase
    that must be typed to delete it), the active DB backend, whether a self-restart
    is even possible on this host, and the outcome of the LAST reset (so the card
    can report it after the reboot that ran it)."""
    await _require_admin(requesting_user_id)
    import json as _json
    from app.util import reset_boot
    from app.relauncher import auto_restart_available

    last = None
    try:
        if reset_boot.RESULT_PATH.is_file():
            last = _json.loads(reset_boot.RESULT_PATH.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        last = None
    can_restart, restart_reason = auto_restart_available()
    return {
        "ok": True,
        "folder_name": reset_boot.repo_root().name,
        "provider": load_config().provider,
        "groups": list(reset_boot.GROUPS),
        "auto_restart": can_restart,
        "restart_reason": restart_reason,
        "last": last,
    }


@router.post("/reset/dismiss")
async def reset_dismiss(body: ActivateBody):
    """Clear the stored last-reset result so the card stops showing the banner."""
    await _require_admin(body.requesting_user_id)
    from app.util import reset_boot
    try:
        if reset_boot.RESULT_PATH.is_file():
            reset_boot.RESULT_PATH.unlink()
    except OSError:
        pass
    return {"ok": True}


@router.post("/reset")
async def reset_data(body: ResetBody):
    """Schedule a reset of the selected data groups: write the one-shot marker,
    then self-restart so the fresh boot wipes the files/schema before anything
    opens them. Env-locked installs refuse (the config is externally managed)."""
    await _require_admin(body.requesting_user_id)
    if _env_locked():
        return JSONResponse(status_code=409, content={
            "ok": False,
            "error": "Configuration is locked to environment variables — reset is disabled in this UI.",
        })
    from app.util import reset_boot
    valid = [g for g in body.groups if g in reset_boot.GROUPS]
    if not valid:
        return JSONResponse(status_code=400, content={
            "ok": False, "error": "Select at least one thing to reset.",
        })

    from app.relauncher import auto_restart_available, trigger_restart
    can_restart, reason = auto_restart_available()
    if not can_restart:
        return JSONResponse(status_code=409, content={
            "ok": False,
            "error": ("This host can't restart itself automatically, so the reset can't run: "
                      + reason + " Start the server via run.py / the supervisor and try again."),
        })

    reset_boot.write_marker(valid, requested_by=body.requesting_user_id)
    logger.warning("[reset] scheduled by %s — groups=%s", body.requesting_user_id, valid)
    result = trigger_restart()
    return {
        "ok": True,
        "scheduled": valid,
        "restart": result,
        "message": ("Reset scheduled. The server is restarting now; the selected data is wiped "
                    "on the way back up (a backup is kept under temp/)."),
    }


@router.post("/delete-install")
async def delete_install(body: DeleteInstallBody):
    """Self-destruct: delete the entire installation folder. Requires the typed
    confirm phrase to equal the folder name. Spawns a detached helper (outside the
    repo) that removes the folder once this server exits, then exits."""
    await _require_admin(body.requesting_user_id)
    if _env_locked():
        return JSONResponse(status_code=409, content={
            "ok": False,
            "error": "Configuration is env-locked — installation delete is disabled in this UI.",
        })
    from app.util import reset_boot
    expected = reset_boot.repo_root().name
    if (body.confirm_phrase or "").strip() != expected:
        return JSONResponse(status_code=400, content={
            "ok": False,
            "error": f'To delete the installation, type its folder name exactly: "{expected}".',
        })
    logger.warning("[reset] SELF-DESTRUCT requested by %s — deleting %s",
                   body.requesting_user_id, reset_boot.repo_root())
    result = reset_boot.spawn_self_destruct()
    if result.get("status") == "error":
        return JSONResponse(status_code=500, content={"ok": False, "error": result.get("reason")})
    return {"ok": True, **result}

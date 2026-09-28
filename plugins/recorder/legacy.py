"""Legacy render-recording read API and configuration compatibility.

Migrated from plugins/abilities/Administrator/render_recorder. Existing log-store
rows stay in place; new capture archives are owned by store.py and api.py.
See README.md. No agent ability registration is required by this module.
"""

from __future__ import annotations

import asyncio
import collections
import hashlib
import json
import logging
import os
import time
import uuid
from datetime import datetime, timedelta, timezone
from typing import Any, Deque, Dict, Iterable, List, Optional

import jwt
from fastapi import APIRouter, Depends, HTTPException, Query
from pydantic import BaseModel

from app.api.db_viewer import require_admin
from app.auth.jwt import get_secret

logger = logging.getLogger(__name__)

# ── Constants / defaults ──────────────────────────────────────────────────────

DEFAULT_RETENTION_ROWS = 8000      # max durable rows kept in the table
DEFAULT_RETENTION_HOURS = 48       # max age of durable rows
DEFAULT_MAX_HTML_BYTES = 400_000   # server-side cap on a single html payload
DEFAULT_MAX_BATCH = 200            # max records accepted in one ingest call
FLUSH_INTERVAL_SECONDS = 3         # batch-writer cadence
PRUNE_INTERVAL_SECONDS = 120       # how often the writer prunes the table
CAPTURE_LEASE_SECONDS = 10 * 60    # short-lived; active tabs renew before expiry
CAPTURE_LEASE_RENEW_SECONDS = 2 * 60
CAPTURE_LEASE_AUDIENCE = "webagent-render-recorder"
CAPTURE_LEASE_SCOPE = "render_recorder:capture"
MAX_DETAIL_LEN = 16000
MAX_LABEL_LEN = 500
SETTINGS_FILE = "data/config/app-settings.json"


def _lease_signing_key() -> bytes:
    """Domain-separate capture leases from normal login JWTs."""
    return hashlib.sha256(
        b"webagent-render-recorder-v1\x00" + get_secret().encode("utf-8")
    ).digest()


# Record kinds the recorder will accept. Anything else is dropped.
VALID_KINDS = frozenset({
    "snapshot", "mutation", "lag", "js_error", "console", "network", "nav", "meta",
})

# Defaults handed to the browser so it knows what to capture and how often.
# Overridable per-key via the matching app-settings.json entries.
DEFAULT_CLIENT_CONFIG: Dict[str, Any] = {
    "capture_duration_ms": 0,           # unlimited until explicitly toggled off
    "capture_max_bytes": 2_000_000,     # baseline + serialized mutation payloads
    "capture_max_operations": 5_000,    # stop observing runaway DOM churn
    "snapshot_max_bytes": 400_000,      # one initial chat baseline
    "mutation_node_max_bytes": 12_000,  # cap HTML for one inserted/removed node
    "mutation_batch_max_bytes": 100_000, # cap one frame chunk sent to the server
    "longtask_min_ms": 80,              # report long tasks at/above this duration
    "network_slow_ms": 2500,            # flag fetches slower than this
    "flush_interval_ms": 2000,          # stay below the 200-record ingest cap
    "capture_snapshots": True,
    "capture_lag": True,
    "capture_js_errors": True,
    "capture_console": True,
    "capture_network": True,
    "capture_whole_page": False,        # default to the relevant chat subtree
    "capture_decorative_animations": False, # exclude background surfaces/noise
}


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _uuid() -> str:
    return str(uuid.uuid4())


def _truncate(s: Optional[str], n: int) -> Optional[str]:
    if s is None:
        return None
    if not isinstance(s, str):
        try:
            s = str(s)
        except Exception:
            return None
    return s if len(s) <= n else (s[:n] + f"… [+{len(s) - n} chars]")


def _read_settings() -> Dict[str, Any]:
    s: Dict[str, Any] = {}
    try:
        if os.path.exists(SETTINGS_FILE):
            with open(SETTINGS_FILE, "r", encoding="utf-8") as f:
                s = json.load(f) or {}
    except Exception:
        pass
    # data/config/debug-config.json overrides win — lets an operator force the
    # render recorder on/off from the single consolidated debug file.
    try:
        from app.admin.debug_config import debug_overrides
        s = {**s, **debug_overrides()}
    except Exception:
        pass
    return s


# ── Recorder ──────────────────────────────────────────────────────────────────

class RenderRecorder:
    """Durable sink for browser render recordings. One process-local writer.

    Capture activation is deliberately not stored here: each browser document owns
    its lifecycle and must present a signed, document-bound lease at intake.
    Capture configuration remains app-wide and is re-read for every new lease."""

    def __init__(self) -> None:
        s = _read_settings()
        self.retention_rows = int(s.get("render_recording_retention_rows", DEFAULT_RETENTION_ROWS) or DEFAULT_RETENTION_ROWS)
        self.retention_hours = float(s.get("render_recording_retention_hours", DEFAULT_RETENTION_HOURS) or DEFAULT_RETENTION_HOURS)
        self.max_html_bytes = int(s.get("render_recording_max_html_bytes", DEFAULT_MAX_HTML_BYTES) or DEFAULT_MAX_HTML_BYTES)
        self._pending: Deque[Dict[str, Any]] = collections.deque(maxlen=20000)
        self._task: Optional[asyncio.Task] = None
        self._last_prune: float = 0.0
        self._accepted = 0
        self._dropped = 0

    # ── Live settings ──────────────────────────────────────────────────────

    def client_config(self) -> Dict[str, Any]:
        """Capture knobs handed to the browser.

        Legacy ``app-settings.json`` keys remain supported. The expandable App
        Functions row writes its friendlier values to the standard non-secret
        ability-config store and wins when present.
        """
        s = _read_settings()
        cfg = dict(DEFAULT_CLIENT_CONFIG)
        for k in cfg:
            sk = f"render_recording_{k}"
            if sk in s and s[sk] is not None:
                cfg[k] = s[sk]
        try:
            from app.admin.ability_config import get_ability_config
            ability_cfg = get_ability_config("render_recorder")
        except Exception:
            ability_cfg = {}

        def as_bool(value: Any) -> bool:
            if isinstance(value, bool):
                return value
            if isinstance(value, str):
                return value.strip().lower() in {"1", "true", "yes", "on", "enabled"}
            return bool(value)

        # Direct boolean knobs use the same names as the browser config.
        for key in (
            "capture_lag",
            "capture_js_errors",
            "capture_console",
            "capture_network",
            "capture_decorative_animations",
        ):
            if key in ability_cfg:
                cfg[key] = as_bool(ability_cfg[key])

        try:
            seconds = float(ability_cfg.get("capture_duration_seconds", cfg["capture_duration_ms"] / 1000))
            cfg["capture_duration_ms"] = int(max(0, seconds) * 1000)
        except (TypeError, ValueError, OverflowError):
            pass
        try:
            megabytes = float(ability_cfg.get("capture_max_megabytes", cfg["capture_max_bytes"] / 1_000_000))
            cfg["capture_max_bytes"] = int(max(0.25, min(10, megabytes)) * 1_000_000)
        except (TypeError, ValueError):
            pass
        try:
            operations = int(float(ability_cfg.get("capture_max_operations", cfg["capture_max_operations"])))
            cfg["capture_max_operations"] = max(100, min(20_000, operations))
        except (TypeError, ValueError):
            pass
        if "capture_scope" in ability_cfg:
            cfg["capture_whole_page"] = str(ability_cfg["capture_scope"]).strip().lower() == "whole_page"
        return cfg

    # ── Ingest ───────────────────────────────────────────────────────────────

    def ingest(
        self,
        records: List[Dict[str, Any]],
        *,
        user_id: Optional[str] = None,
        recv_ts: Optional[str] = None,
    ) -> Dict[str, Any]:
        """Validate + queue a batch of browser records. Cheap, never raises.

        Server-trusted fields (``recv_ts``, ``user_id``) are stamped here so the
        browser can't spoof them; everything else is taken from the payload after
        clamping. Returns a small summary."""
        recv_ts = recv_ts or _now_iso()
        accepted = 0
        dropped = 0
        for raw in (records or [])[:DEFAULT_MAX_BATCH]:
            try:
                rec = self._normalize_incoming(raw, user_id=user_id, recv_ts=recv_ts)
                if rec is None:
                    dropped += 1
                    continue
                self._pending.append(rec)
                accepted += 1
            except Exception:
                dropped += 1
        if len(records or []) > DEFAULT_MAX_BATCH:
            dropped += len(records) - DEFAULT_MAX_BATCH
        self._accepted += accepted
        self._dropped += dropped
        if accepted:
            self._ensure_task()
        return {"accepted": accepted, "dropped": dropped}

    def _normalize_incoming(
        self, raw: Dict[str, Any], *, user_id: Optional[str], recv_ts: str
    ) -> Optional[Dict[str, Any]]:
        if not isinstance(raw, dict):
            return None
        kind = str(raw.get("kind") or "").lower()
        if kind not in VALID_KINDS:
            return None
        html = raw.get("html")
        html_bytes = None
        if isinstance(html, str):
            if len(html) > self.max_html_bytes:
                html = html[: self.max_html_bytes] + "<!-- truncated -->"
            html_bytes = len(html)
        else:
            html = None
        detail = raw.get("detail")
        recording_id = _truncate(raw.get("recording_id"), 80)
        try:
            elapsed_ms = max(0, int(raw["elapsed_ms"])) if raw.get("elapsed_ms") is not None else None
        except (TypeError, ValueError):
            elapsed_ms = None
        # Keep capture grouping/timing queryable through the existing detail
        # column, avoiding a migration of the high-volume local recordings DB.
        if recording_id is not None or elapsed_ms is not None:
            if isinstance(detail, dict):
                detail = dict(detail)
            elif detail is None:
                detail = {}
            else:
                detail = {"data": detail}
            if recording_id is not None:
                detail.setdefault("recording_id", recording_id)
            if elapsed_ms is not None:
                detail.setdefault("elapsed_ms", elapsed_ms)
        if detail is not None and not isinstance(detail, str):
            try:
                detail = json.dumps(detail, default=str)
            except Exception:
                detail = str(detail)
        detail = _truncate(detail, MAX_DETAIL_LEN)
        try:
            seq = int(raw["seq"]) if raw.get("seq") is not None else None
        except (TypeError, ValueError):
            seq = None
        try:
            session_seq = int(raw["session_seq"]) if raw.get("session_seq") is not None else None
        except (TypeError, ValueError):
            session_seq = None
        try:
            value_num = float(raw["value_num"]) if raw.get("value_num") is not None else None
        except (TypeError, ValueError):
            value_num = None
        return {
            "id": _uuid(),
            "ts": _truncate(raw.get("ts"), 40) or recv_ts,
            "recv_ts": recv_ts,
            "kind": kind,
            "session_id": _truncate(raw.get("session_id"), 80),
            "turn_id": _truncate(raw.get("turn_id"), 80),
            "session_seq": session_seq,
            "user_id": user_id or _truncate(raw.get("user_id"), 80),
            "agent_id": _truncate(raw.get("agent_id"), 80),
            "client_id": _truncate(raw.get("client_id"), 80),
            "seq": seq,
            "url": _truncate(raw.get("url"), 600),
            "level": _truncate((raw.get("level") or "").lower() or None, 16),
            "label": _truncate(raw.get("label"), MAX_LABEL_LEN),
            "value_num": value_num,
            "detail": detail,
            "html": html,
            "html_bytes": html_bytes,
        }

    # ── Background writer + pruner ─────────────────────────────────────────

    def _ensure_task(self) -> None:
        if self._task and not self._task.done():
            return
        try:
            loop = asyncio.get_running_loop()
        except RuntimeError:
            return
        self._task = loop.create_task(self._run())

    def start(self) -> None:
        """Idempotently start the background writer (call at app startup)."""
        self._ensure_task()

    def stop(self) -> None:
        """Idempotently cancel the background writer (call on shutdown / when the
        ability-background leader steps down)."""
        t = self._task
        self._task = None
        if t is not None and not t.done():
            t.cancel()

    async def _run(self) -> None:
        while True:
            try:
                await asyncio.sleep(FLUSH_INTERVAL_SECONDS)
                await self._flush_once()
                now = time.time()
                if now - self._last_prune >= PRUNE_INTERVAL_SECONDS:
                    self._last_prune = now
                    await self._prune_once()
            except asyncio.CancelledError:
                break
            except Exception as e:
                logger.debug("render recorder writer error: %s", e)

    async def _flush_once(self) -> None:
        if not self._pending:
            return
        batch: List[Dict[str, Any]] = []
        while self._pending and len(batch) < 200:
            try:
                batch.append(self._pending.popleft())
            except IndexError:
                break
        if not batch:
            return
        db = self._db()
        if db is None or not hasattr(db, "insert_render_recordings_batch"):
            return
        try:
            await db.insert_render_recordings_batch(batch)
        except Exception as e:
            logger.debug("insert_render_recordings_batch failed: %s", e)

    async def _prune_once(self) -> None:
        db = self._db()
        if db is None or not hasattr(db, "prune_render_recordings"):
            return
        try:
            await db.prune_render_recordings(
                max_rows=self.retention_rows,
                max_age_seconds=self.retention_hours * 3600.0,
            )
        except Exception as e:
            logger.debug("prune_render_recordings failed: %s", e)

    @staticmethod
    def _db() -> Any:
        # Render recordings live in the dedicated, always-local recordings.db
        # (own WAL), not the main application DB — see app/db/logs_store.py.
        try:
            from app.db.logs_store import get_log_store
            return get_log_store()
        except Exception:
            return None

    # ── Read / clear / stats ───────────────────────────────────────────────

    async def query(
        self,
        *,
        rec_id: Optional[str] = None,
        kinds: Optional[Iterable[str]] = None,
        levels: Optional[Iterable[str]] = None,
        session_id: Optional[str] = None,
        client_id: Optional[str] = None,
        session_seq: Optional[int] = None,
        since_minutes: Optional[float] = None,
        search: Optional[str] = None,
        include_html: bool = False,
        limit: int = 200,
    ) -> List[Dict[str, Any]]:
        db = self._db()
        if db is None or not hasattr(db, "query_render_recordings"):
            return []
        since_iso = None
        if since_minutes:
            from datetime import timedelta
            since_iso = (datetime.now(timezone.utc) - timedelta(minutes=since_minutes)).isoformat()
        rows = await db.query_render_recordings(
            rec_id=rec_id,
            kinds=[str(k).lower() for k in kinds] if kinds else None,
            levels=[str(x).lower() for x in levels] if levels else None,
            session_id=session_id,
            client_id=client_id,
            session_seq=session_seq,
            since=since_iso,
            search=search,
            include_html=include_html,
            limit=limit,
        )
        return [self._normalize_out(r) for r in (rows or [])]

    @staticmethod
    def _normalize_out(r: Dict[str, Any]) -> Dict[str, Any]:
        r = dict(r)
        d = r.get("detail")
        if isinstance(d, str) and d:
            try:
                r["detail"] = json.loads(d)
            except Exception:
                pass
        return r

    async def clear(
        self,
        *,
        older_than_minutes: Optional[float] = None,
        kinds: Optional[Iterable[str]] = None,
        session_id: Optional[str] = None,
        search: Optional[str] = None,
    ) -> Dict[str, Any]:
        db = self._db()
        deleted = 0
        if db is not None and hasattr(db, "clear_render_recordings"):
            try:
                deleted = await db.clear_render_recordings(
                    older_than_seconds=(older_than_minutes * 60.0) if older_than_minutes else None,
                    kinds=[str(k).lower() for k in kinds] if kinds else None,
                    session_id=session_id,
                    search=search,
                )
            except Exception as e:
                logger.debug("clear_render_recordings failed: %s", e)
        # Also drop anything still queued in RAM that matches a full clear.
        pending_dropped = 0
        if older_than_minutes is None and not kinds and not session_id and not search:
            pending_dropped = len(self._pending)
            self._pending.clear()
        return {"db_deleted": deleted, "pending_dropped": pending_dropped}

    async def stats(self) -> Dict[str, Any]:
        db = self._db()
        durable = {"rows": 0, "html_bytes": 0, "by_kind": {}}
        if db is not None and hasattr(db, "render_recordings_stats"):
            try:
                durable = await db.render_recordings_stats()
            except Exception as e:
                logger.debug("render_recordings_stats failed: %s", e)
        return {
            "capture_mode": "tab_local_lease",
            "pending_writes": len(self._pending),
            "accepted_total": self._accepted,
            "dropped_total": self._dropped,
            "retention_rows": self.retention_rows,
            "retention_hours": self.retention_hours,
            "durable": durable,
        }


# ── Module singleton ────────────────────────────────────────────────────────--

_recorder: Optional[RenderRecorder] = None


def get_render_recorder() -> RenderRecorder:
    global _recorder
    if _recorder is None:
        _recorder = RenderRecorder()
    return _recorder


def start_render_recorder() -> None:
    """Start the background writer (idempotent). Kept for back-compat callers."""
    get_render_recorder().start()


# ── Drop-in background-service hooks ─────────────────────────────────────────
# Discovered + run by core's generic "ability-background" leader service
# (app.abilities.background_service_hooks → app/main.py). Replaces the old
# hardwired start line in app/main.py's startup. Idempotent and idle until a
# lease-authorized batch arrives; ingest also self-starts the writer per process.

async def start_background() -> None:
    get_render_recorder().start()


async def stop_background() -> None:
    rec = _recorder
    if rec is not None:
        rec.stop()


# ── HTTP API — browser intake + admin read/clear/toggle ──────────────────────
# Auto-mounted by core's generic ability-router loop
# (app.abilities.ability_routers → app/main.py "ability router"). Paths are
# unchanged from the former app/api/recordings.py, so the browser recorder
# (ui/js/recorder.js) and the admin Diagnostics UI keep working verbatim.
#
#     GET    /api/v1/recordings/config  — feature availability + capture knobs
#     POST   /api/v1/recordings         — browser batch under a signed capture lease
#     GET/POST /api/v1/recordings/enabled — admin control status / lease issue-renew
#     GET    /api/v1/recordings         — filtered list of recent records (admin)
#     GET    /api/v1/recordings/one     — one record WITH its html payload (admin)
#     GET    /api/v1/recordings/stats   — row/byte stats + recorder health (admin)
#     DELETE /api/v1/recordings         — clear records (all, older-than, or by filter) (admin)
#
# Only an admin can obtain a lease. Intake validates its signature, expiry, scope,
# client_id and recording_id, then server-stamps user_id from the lease. This keeps
# sendBeacon usable without exposing a login token and never creates global capture.

router = APIRouter(prefix="/api/v1/recordings", tags=["recordings"])


def _control_available() -> bool:
    from .config import settings
    return settings()["enabled"]


def _split_csv(v: Optional[str]) -> Optional[list]:
    if not v:
        return None
    parts = [p.strip() for p in v.split(",") if p.strip()]
    return parts or None


def _binding(value: Any, field: str) -> str:
    text = str(value or "").strip()
    if not text or len(text) > 80:
        raise HTTPException(status_code=422, detail=f"Valid {field} is required")
    return text


def _issue_capture_lease(
    auth: Dict[str, Any], *, client_id: str, recording_id: Optional[str] = None
) -> Dict[str, Any]:
    """Mint a short-lived recorder-only lease bound to one browser document."""
    user_id = _binding(auth.get("user_id") or auth.get("sub"), "user_id")
    client_id = _binding(client_id, "client_id")
    recording_id = _binding(recording_id or _uuid(), "recording_id")
    now = datetime.now(timezone.utc)
    expires = now + timedelta(seconds=CAPTURE_LEASE_SECONDS)
    payload = {
        "aud": CAPTURE_LEASE_AUDIENCE,
        "scope": CAPTURE_LEASE_SCOPE,
        "user_id": user_id,
        "client_id": client_id,
        "recording_id": recording_id,
        "iat": now,
        "exp": expires,
    }
    return {
        "lease_token": jwt.encode(payload, _lease_signing_key(), algorithm="HS256"),
        "lease_expires_at": expires.isoformat(),
        "lease_renew_after_seconds": CAPTURE_LEASE_SECONDS - CAPTURE_LEASE_RENEW_SECONDS,
        "client_id": client_id,
        "recording_id": recording_id,
    }


def _verify_capture_lease(token: str) -> Dict[str, Any]:
    if not token:
        raise HTTPException(status_code=403, detail="Render recording lease required")
    try:
        payload = jwt.decode(
            token,
            _lease_signing_key(),
            algorithms=["HS256"],
            audience=CAPTURE_LEASE_AUDIENCE,
        )
    except jwt.ExpiredSignatureError as exc:
        raise HTTPException(status_code=403, detail="Render recording lease expired") from exc
    except jwt.PyJWTError as exc:
        raise HTTPException(status_code=403, detail="Invalid render recording lease") from exc
    if payload.get("scope") != CAPTURE_LEASE_SCOPE:
        raise HTTPException(status_code=403, detail="Invalid render recording lease scope")
    _binding(payload.get("user_id"), "lease user_id")
    _binding(payload.get("client_id"), "lease client_id")
    _binding(payload.get("recording_id"), "lease recording_id")
    return payload


def _validate_lease_batch(records: List[dict], lease: Dict[str, Any]) -> None:
    """Reject the entire batch if any row escapes the lease's two bindings."""
    for raw in records or []:
        if not isinstance(raw, dict):
            raise HTTPException(status_code=422, detail="Recording rows must be objects")
        if str(raw.get("client_id") or "") != str(lease["client_id"]):
            raise HTTPException(status_code=403, detail="Render recording client mismatch")
        if str(raw.get("recording_id") or "") != str(lease["recording_id"]):
            raise HTTPException(status_code=403, detail="Render recording id mismatch")


class RecordingBatch(BaseModel):
    records: List[dict] = []
    lease_token: str = ""


@router.get("/config")
async def recordings_config():
    """Return feature availability/configuration, never a shared capture state."""
    rec = get_render_recorder()
    available = _control_available()
    return {
        "available": available,
        "enabled": False,
        "scope": "tab",
        "config": rec.client_config() if available else {},
    }


@router.post("")
async def ingest_recordings(batch: RecordingBatch):
    """Accept a batch only under a valid lease for this document + recording."""
    if not _control_available():
        raise HTTPException(status_code=403, detail="Render Recorder is unavailable")
    lease = _verify_capture_lease(batch.lease_token)
    _validate_lease_batch(batch.records, lease)
    result = get_render_recorder().ingest(batch.records, user_id=str(lease["user_id"]))
    return {"status": "ok", **result}


@router.get("")
async def list_recordings(
    kinds: Optional[str] = Query(None, description="Comma list: snapshot,mutation,lag,js_error,console,network,nav,meta"),
    levels: Optional[str] = Query(None, description="Comma list: info,warning,error"),
    session_id: Optional[str] = Query(None),
    client_id: Optional[str] = Query(None),
    session_seq: Optional[int] = Query(None, description="Exact correlation key match"),
    since_minutes: Optional[float] = Query(None),
    search: Optional[str] = Query(None, description="Substring match on label/detail/url"),
    limit: int = Query(200, ge=1, le=2000),
    _auth: dict = Depends(require_admin),
):
    """Return recent render records, newest first. The (large) html payload is
    omitted here — fetch it per-row via /one."""
    records = await get_render_recorder().query(
        kinds=_split_csv(kinds),
        levels=_split_csv(levels),
        session_id=session_id,
        client_id=client_id,
        session_seq=session_seq,
        since_minutes=since_minutes,
        search=search,
        include_html=False,
        limit=limit,
    )
    return {"records": records, "count": len(records)}


@router.get("/one")
async def get_recording(
    id: str = Query(..., description="Recording id"),
    _auth: dict = Depends(require_admin),
):
    """Return a single record including its full html payload."""
    rows = await get_render_recorder().query(rec_id=id, include_html=True, limit=1)
    return rows[0] if rows else {"detail": "not found"}


@router.get("/enabled")
async def get_recordings_enabled(_auth: dict = Depends(require_admin)):
    """Return control availability/config; live capture state belongs to the tab."""
    rec = get_render_recorder()
    return {
        "available": _control_available(),
        "enabled": False,
        "scope": "tab",
        "config": rec.client_config(),
    }


class EnabledBody(BaseModel):
    enabled: bool
    client_id: str = ""
    recording_id: Optional[str] = None


@router.post("/enabled")
async def set_recordings_enabled(
    body: EnabledBody, _auth: dict = Depends(require_admin)
):
    """Issue/renew one document lease; never mutate app-global capture state."""
    rec = get_render_recorder()
    available = _control_available()
    base = {
        "available": available,
        "enabled": False,
        "scope": "tab",
        "config": rec.client_config(),
    }
    if not body.enabled:
        return base
    if not available:
        raise HTTPException(status_code=409, detail="Render Recorder is unavailable")
    lease = _issue_capture_lease(
        _auth,
        client_id=body.client_id,
        recording_id=body.recording_id,
    )
    return {**base, **lease, "enabled": True}


@router.get("/stats")
async def recordings_stats(_auth: dict = Depends(require_admin)):
    """Recorder health: capture mode, availability, pending writes, durable row/byte counts."""
    stats = await get_render_recorder().stats()
    available = _control_available()
    stats["available"] = available
    return stats


@router.delete("")
async def clear_recordings(
    older_than_minutes: Optional[float] = Query(None, description="Clear records older than N minutes (omit = all)"),
    kinds: Optional[str] = Query(None, description="Comma list — only clear these kinds"),
    session_id: Optional[str] = Query(None, description="Only clear this session"),
    search: Optional[str] = Query(None, description="Only clear records matching this substring"),
    _auth: dict = Depends(require_admin),
):
    """Clear render recordings. No scope → clears everything."""
    result = await get_render_recorder().clear(
        older_than_minutes=older_than_minutes,
        kinds=_split_csv(kinds),
        session_id=session_id,
        search=search,
    )
    return {"status": "ok", **result}

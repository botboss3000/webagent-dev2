"""SSH Control — external-device command execution without Administrator access.

This self-contained ability owns its encrypted named profiles, FastAPI endpoints,
Paramiko transport, and process-lifetime job registry. It deliberately does not
import the deployment SSH provider, Admin Terminal, or any Administrator ability.
"""

from __future__ import annotations

import asyncio
import base64
import hashlib
import io
import ipaddress
import json
import logging
import shlex
import socket
import time
import uuid
from dataclasses import dataclass, field
from datetime import UTC
from typing import Any

from fastapi import APIRouter, HTTPException, Request
from pydantic import BaseModel, Field

logger = logging.getLogger(__name__)

ABILITY_ID = "ssh_control"
VAULT_SERVICE = "ssh_control"
MAX_PROFILES = 20
DEFAULT_COMMAND_TIMEOUT = 120
MAX_COMMAND_TIMEOUT = 900
DEFAULT_JOB_RUNTIME = 21_600
MAX_JOB_RUNTIME = 86_400
MAX_RUNNING_JOBS = 3
MAX_OUTPUT_CHARS = 1_048_576
MAX_POLL_CHARS = 32_768
MAX_COMMAND_CHARS = 65_536
COMPLETED_RETENTION_SECONDS = 900

TOOL_SCHEMAS: dict = {}
# Deletion has an inherent Ask floor. Command/job actions remain destructive via
# descriptor metadata (so Plan blocks them) but intentionally default to Auto.
DESTRUCTIVE: set = {"ssh_delete_connection"}


# ── Generic helpers ──────────────────────────────────────────────────────────

def _json(data: dict) -> str:
    return json.dumps(data, ensure_ascii=False)


def _as_dict(raw: Any) -> dict:
    if isinstance(raw, dict):
        return dict(raw)
    if isinstance(raw, str) and raw.strip():
        try:
            value = json.loads(raw)
            return dict(value) if isinstance(value, dict) else {}
        except (TypeError, ValueError, json.JSONDecodeError):
            return {}
    return {}


def _profile_label(agent_id: str, connection_id: str) -> str:
    return f"agent:{agent_id}:{connection_id}"


def _pack_secrets(values: dict) -> str:
    keys = ("password", "private_key", "key_passphrase", "sudo_password")
    return json.dumps({key: str(values.get(key) or "") for key in keys})


def _unpack_secrets(raw: str) -> dict:
    value = _as_dict(raw)
    return {
        key: str(value.get(key) or "")
        for key in ("password", "private_key", "key_passphrase", "sudo_password")
    }


def _public_profile(config: dict, secrets: dict | None = None) -> dict:
    secrets = secrets or {}
    auth_method = str(config.get("auth_method") or "password")
    configured = bool(
        secrets.get("private_key") if auth_method == "private_key" else secrets.get("password")
    ) if secrets else bool(config.get("configured"))
    return {
        "connection_id": str(config.get("connection_id") or ""),
        "name": str(config.get("name") or "SSH device"),
        "host": str(config.get("host") or ""),
        "port": int(config.get("port") or 22),
        "username": str(config.get("username") or ""),
        "auth_method": auth_method,
        "host_key_fingerprint": str(config.get("host_key_fingerprint") or ""),
        "host_key_type": str(config.get("host_key_type") or ""),
        "configured": configured,
        "has_sudo_password": bool(secrets.get("sudo_password"))
            if secrets else bool(config.get("has_sudo_password")),
        "verified_at": config.get("verified_at"),
    }


async def _list_profiles(user_id: str, agent_id: str, *, include_secrets: bool = False) -> list:
    from app.db import get_db

    rows = await get_db().auth_element_list(user_id, VAULT_SERVICE)
    out = []
    for row in rows or []:
        config = _as_dict(row.get("config"))
        if str(config.get("agent_id") or "") != agent_id:
            continue
        secrets = _unpack_secrets(row.get("secret_ref") or "")
        item = {"config": config, "secrets": secrets, "label": row.get("label")}
        out.append(item if include_secrets else _public_profile(config, secrets))
    out.sort(key=lambda item: str(
        (item.get("config") or {}).get("name") if include_secrets else item.get("name")
    ).lower())
    return out


async def _get_profile(user_id: str, agent_id: str, connection_id: str) -> dict | None:
    from app.db import get_db

    if not user_id or not agent_id or not connection_id:
        return None
    row = await get_db().auth_element_get(
        user_id, VAULT_SERVICE, _profile_label(agent_id, connection_id)
    )
    if not row:
        return None
    config = _as_dict(row.get("config"))
    if config.get("agent_id") != agent_id or config.get("connection_id") != connection_id:
        return None
    return {"config": config, "secrets": _unpack_secrets(row.get("secret_ref") or "")}


async def _save_profile(user_id: str, agent_id: str, config: dict, secrets: dict) -> None:
    from app.db import get_db

    connection_id = str(config["connection_id"])
    await get_db().auth_element_set(
        user_id=user_id,
        service=VAULT_SERVICE,
        label=_profile_label(agent_id, connection_id),
        config=config,
        secret_ref=_pack_secrets(secrets),
    )


async def _delete_profile(user_id: str, agent_id: str, connection_id: str) -> bool:
    from app.db import get_db

    if not await _get_profile(user_id, agent_id, connection_id):
        return False
    return bool(await get_db().auth_element_delete(
        user_id, VAULT_SERVICE, _profile_label(agent_id, connection_id)
    ))


# ── Authorization and ability gates ─────────────────────────────────────────

async def _authorize(user_id: str, agent_id: str, *, require_enabled: bool = True) -> None:
    if not user_id or str(user_id).startswith("anon_"):
        raise HTTPException(status_code=401, detail="Sign in to use SSH Control.")
    if not agent_id:
        raise HTTPException(status_code=400, detail="agent_id is required.")

    from app.db import get_db
    from app.entitlements.resources import (
        ResourceEntitlementError,
        enforce_ability_group,
    )

    db = get_db()
    try:
        roles = await db.get_agent_roles(agent_id)
    except Exception:  # noqa: BLE001 - deny access when any role backend fails
        roles = {"admin_users": [], "member_users": []}
    allowed = bool(await db.is_user_admin(user_id)) or user_id in set(
        (roles.get("admin_users") or []) + (roles.get("member_users") or [])
    )
    if not allowed:
        raise HTTPException(status_code=403, detail="You do not have access to this agent.")
    try:
        await enforce_ability_group(db, user_id, ABILITY_ID)
    except ResourceEntitlementError as exc:
        raise HTTPException(status_code=403, detail=str(exc)) from exc

    if require_enabled:
        rows = await db.get_agent_connections(agent_id)
        enabled = any(
            row.get("connection_type") == ABILITY_ID and bool(row.get("enabled"))
            for row in rows or []
        )
        if not enabled:
            raise HTTPException(
                status_code=403,
                detail="SSH Control is not enabled on this agent.",
            )


async def _api_actor(request: Request, agent_id: str) -> str:
    from app.auth.identity import request_user_id

    user_id = request_user_id(request)
    await _authorize(user_id, agent_id)
    return user_id


async def _tool_gate(user_id: str, agent_id: str) -> str | None:
    try:
        await _authorize(user_id, agent_id)
        return None
    except HTTPException as exc:
        detail = exc.detail if isinstance(exc.detail, str) else "SSH Control is unavailable."
        return _json({"status": "error", "message": detail})


# ── Target validation and Paramiko transport ────────────────────────────────

def _import_paramiko():
    try:
        import paramiko
        return paramiko
    except Exception as exc:
        raise RuntimeError("SSH support requires the installed 'paramiko' package.") from exc


def _ip(value: str) -> ipaddress.IPv4Address | ipaddress.IPv6Address | None:
    try:
        return ipaddress.ip_address(str(value).split("%", 1)[0])
    except ValueError:
        return None


def _local_interface_ips() -> set:
    values = {"127.0.0.1", "::1"}
    names = {socket.gethostname(), socket.getfqdn(), "localhost"}
    for name in names:
        try:
            for item in socket.getaddrinfo(name, None):
                values.add(str(item[4][0]).split("%", 1)[0])
        except OSError as exc:
            logger.debug("Could not resolve local interface name %s: %s", name, exc)
    try:
        import psutil
        for addresses in psutil.net_if_addrs().values():
            for address in addresses:
                if address.family in (socket.AF_INET, socket.AF_INET6):
                    values.add(str(address.address).split("%", 1)[0])
    except Exception as exc:  # noqa: BLE001 - psutil is an optional dependency
        # psutil is optional; hostname resolution + the active route still cover
        # the standard installation without adding another dependency.
        logger.debug("Optional interface enumeration unavailable: %s", exc)
    try:
        sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        try:
            sock.connect(("192.0.2.1", 9))
            values.add(sock.getsockname()[0])
        finally:
            sock.close()
    except OSError as exc:
        logger.debug("Could not inspect the active outbound interface: %s", exc)
    return values


def _resolve_external_target(host: str, port: int) -> str:
    host = str(host or "").strip()
    if not host:
        raise ValueError("Host is required.")
    if host.lower().rstrip(".") in {"localhost", "localhost.localdomain"} or host.lower().endswith(".localhost"):
        raise ValueError("SSH Control cannot connect to the WebAgent host.")
    try:
        port = int(port)
    except Exception as exc:
        raise ValueError("Port must be an integer from 1 to 65535.") from exc
    if not 1 <= port <= 65535:
        raise ValueError("Port must be from 1 to 65535.")

    try:
        infos = socket.getaddrinfo(host, port, type=socket.SOCK_STREAM)
    except socket.gaierror as exc:
        raise ValueError(f"Could not resolve SSH host '{host}'.") from exc
    local = {_ip(value) for value in _local_interface_ips()}
    local.discard(None)
    candidates = []
    for info in infos:
        address = str(info[4][0]).split("%", 1)[0]
        parsed = _ip(address)
        if parsed is None:
            continue
        if parsed.is_loopback or parsed.is_unspecified or parsed.is_link_local or parsed.is_multicast:
            raise ValueError("SSH Control cannot connect to localhost or a local-only address.")
        if parsed in local:
            raise ValueError("SSH Control cannot connect to an address assigned to the WebAgent host.")
        if address not in candidates:
            candidates.append(address)
    if not candidates:
        raise ValueError(f"SSH host '{host}' did not resolve to a usable external address.")
    # Resolve exactly once and connect to the checked numeric address. This keeps
    # a second DNS answer from swapping in localhost between validation and use.
    return candidates[0]


def _fingerprint(key) -> str:
    digest = hashlib.sha256(key.asbytes()).digest()
    return "SHA256:" + base64.b64encode(digest).decode("ascii").rstrip("=")


def _probe_host(host: str, port: int, timeout: int = 15) -> dict:
    paramiko = _import_paramiko()
    address = _resolve_external_target(host, port)
    sock = socket.create_connection((address, int(port)), timeout=timeout)
    transport = None
    try:
        transport = paramiko.Transport(sock)
        transport.banner_timeout = timeout
        transport.start_client(timeout=timeout)
        key = transport.get_remote_server_key()
        if key is None:
            raise RuntimeError("The SSH server did not present a host key.")
        return {
            "address": address,
            "fingerprint": _fingerprint(key),
            "key_type": key.get_name(),
        }
    finally:
        if transport is not None:
            transport.close()
        else:
            sock.close()


def _load_private_key(paramiko, key_text: str, passphrase: str):
    last_error = None
    for name in ("Ed25519Key", "ECDSAKey", "RSAKey", "DSSKey"):
        cls = getattr(paramiko, name, None)
        if cls is None:
            continue
        try:
            return cls.from_private_key(io.StringIO(key_text), password=passphrase or None)
        except Exception as exc:  # noqa: BLE001 - key classes expose different parse exceptions
            last_error = exc
    raise RuntimeError(
        "Could not read that SSH private key; its format or passphrase is invalid."
    ) from last_error


class _PinnedHostKeyPolicy:
    def __init__(self, paramiko, expected: str):
        self.paramiko = paramiko
        self.expected = expected

    def missing_host_key(self, client, hostname, key):
        actual = _fingerprint(key)
        if actual != self.expected:
            raise self.paramiko.SSHException(
                f"SSH host key mismatch: expected {self.expected}, received {actual}."
            )


def _connect(config: dict, secrets: dict, timeout: int = 20):
    paramiko = _import_paramiko()
    host = str(config.get("host") or "")
    port = int(config.get("port") or 22)
    address = _resolve_external_target(host, port)
    expected = str(config.get("host_key_fingerprint") or "")
    if not expected.startswith("SHA256:"):
        raise RuntimeError("This SSH profile has no trusted host-key fingerprint.")

    auth_method = str(config.get("auth_method") or "password")
    pkey = None
    password = None
    if auth_method == "private_key":
        key_text = str(secrets.get("private_key") or "").strip()
        if not key_text:
            raise RuntimeError("This SSH profile has no private key.")
        pkey = _load_private_key(paramiko, key_text, str(secrets.get("key_passphrase") or ""))
    elif auth_method == "password":
        password = str(secrets.get("password") or "")
        if not password:
            raise RuntimeError("This SSH profile has no password.")
    else:
        raise RuntimeError("Unsupported SSH authentication method.")

    client = paramiko.SSHClient()
    client.set_missing_host_key_policy(_PinnedHostKeyPolicy(paramiko, expected))
    try:
        client.connect(
            hostname=address,
            port=port,
            username=str(config.get("username") or ""),
            pkey=pkey,
            password=password,
            timeout=timeout,
            banner_timeout=timeout,
            auth_timeout=timeout,
            allow_agent=False,
            look_for_keys=False,
        )
        return client
    except Exception:
        client.close()
        raise


def _friendly_error(exc: Exception, host: str = "the device") -> str:
    text = str(exc) or exc.__class__.__name__
    low = text.lower()
    if "host key mismatch" in low:
        return text
    if "authentication" in low or ("auth" in low and "fail" in low):
        return f"Login to {host} was rejected; check the saved user and credential."
    if "timed out" in low or "timeout" in low:
        return f"Timed out reaching {host}; check its address, port, and SSH service."
    if any(token in low for token in ("refused", "no route", "unreachable", "getaddrinfo")):
        return f"Could not reach {host}; check its address, port, and SSH service."
    return text


def _command_for(command: str, elevated: bool, sudo_password: str) -> tuple[str, str | None]:
    command = str(command or "").strip()
    if not command:
        raise ValueError("Command is required.")
    if len(command) > MAX_COMMAND_CHARS:
        raise ValueError(f"Command exceeds the {MAX_COMMAND_CHARS}-character limit.")
    if not elevated:
        return command, None
    quoted = shlex.quote(command)
    if sudo_password:
        return f"sudo -S -p '' -- sh -lc {quoted}", sudo_password + "\n"
    return f"sudo -n -- sh -lc {quoted}", None


def _append_tail(buf: bytearray, data: bytes) -> bool:
    buf.extend(data)
    # One-shot commands keep separate stdout/stderr tails. Giving each half of
    # the total budget guarantees their combined response never exceeds 1 MiB.
    stream_limit = MAX_OUTPUT_CHARS // 2
    if len(buf) <= stream_limit:
        return False
    del buf[: len(buf) - stream_limit]
    return True


def _run_sync(config: dict, secrets: dict, command: str, timeout: int, elevated: bool) -> dict:
    started = time.monotonic()
    client = _connect(config, secrets)
    channel = None
    stdout_buf = bytearray()
    stderr_buf = bytearray()
    truncated = False
    timed_out = False
    exit_code = None
    try:
        remote_command, stdin_data = _command_for(
            command, elevated, str(secrets.get("sudo_password") or "")
        )
        transport = client.get_transport()
        if transport is None or not transport.is_active():
            raise RuntimeError("SSH transport closed before command execution.")
        channel = transport.open_session(timeout=20)
        channel.exec_command(remote_command)
        if stdin_data is not None:
            channel.sendall(stdin_data.encode("utf-8"))
            channel.shutdown_write()
        deadline = time.monotonic() + timeout
        while True:
            read_any = False
            while channel.recv_ready():
                read_any = True
                truncated = _append_tail(stdout_buf, channel.recv(32_768)) or truncated
            while channel.recv_stderr_ready():
                read_any = True
                truncated = _append_tail(stderr_buf, channel.recv_stderr(32_768)) or truncated
            if channel.exit_status_ready() and not channel.recv_ready() and not channel.recv_stderr_ready():
                exit_code = channel.recv_exit_status()
                break
            if time.monotonic() >= deadline:
                timed_out = True
                channel.close()
                break
            if not read_any:
                time.sleep(0.05)
        return {
            "status": "timeout" if timed_out else "ok",
            "exit_code": exit_code,
            "stdout": stdout_buf.decode("utf-8", "replace"),
            "stderr": stderr_buf.decode("utf-8", "replace"),
            "timed_out": timed_out,
            "truncated": truncated,
            "duration_ms": int((time.monotonic() - started) * 1000),
        }
    finally:
        if channel is not None:
            channel.close()
        client.close()


# ── Process-lifetime job registry ────────────────────────────────────────────

@dataclass
class _Job:
    job_id: str
    user_id: str
    agent_id: str
    connection_id: str
    connection_name: str
    client: Any
    channel: Any
    started_at: float
    max_runtime: int
    status: str = "running"
    exit_code: int | None = None
    ended_at: float | None = None
    events: list[tuple] = field(default_factory=list)
    next_cursor: int = 0
    base_cursor: int = 0
    truncated: bool = False
    reader_task: asyncio.Task | None = None

    def append(self, stream: str, raw: bytes) -> None:
        text = raw.decode("utf-8", "replace")
        if not text:
            return
        start = self.next_cursor
        self.next_cursor += len(text)
        self.events.append((start, self.next_cursor, stream, text))
        total = sum(len(event[3]) for event in self.events)
        while self.events and total > MAX_OUTPUT_CHARS:
            old = self.events.pop(0)
            total -= len(old[3])
            self.base_cursor = old[1]
            self.truncated = True


_JOBS: dict[str, _Job] = {}
_JOBS_LOCK = asyncio.Lock()
_JOB_START_LOCK = asyncio.Lock()


async def _job_reader(job: _Job) -> None:
    try:
        while job.status == "running":
            read_any = False
            try:
                while job.channel.recv_ready():
                    read_any = True
                    job.append("stdout", job.channel.recv(32_768))
                while job.channel.recv_stderr_ready():
                    read_any = True
                    job.append("stderr", job.channel.recv_stderr(32_768))
                if (job.channel.exit_status_ready()
                        and not job.channel.recv_ready()
                        and not job.channel.recv_stderr_ready()):
                    job.exit_code = job.channel.recv_exit_status()
                    job.status = "completed"
                    break
                if time.monotonic() - job.started_at >= job.max_runtime:
                    job.status = "timed_out"
                    job.channel.close()
                    break
            except Exception as exc:  # noqa: BLE001 - normalize Paramiko channel failures
                if job.status == "running":
                    job.status = "error"
                    job.append("stderr", _friendly_error(exc).encode("utf-8", "replace"))
                break
            if not read_any:
                await asyncio.sleep(0.1)
    finally:
        if job.status == "running":
            job.status = "closed"
        job.ended_at = time.monotonic()
        try:
            job.channel.close()
        except Exception as exc:  # noqa: BLE001 - best-effort cleanup
            logger.debug("SSH job channel cleanup failed: %s", exc)
        try:
            job.client.close()
        except Exception as exc:  # noqa: BLE001 - best-effort cleanup
            logger.debug("SSH job client cleanup failed: %s", exc)


async def _prune_jobs() -> None:
    cutoff = time.monotonic() - COMPLETED_RETENTION_SECONDS
    async with _JOBS_LOCK:
        stale = [job_id for job_id, job in _JOBS.items()
                 if job.ended_at is not None and job.ended_at < cutoff]
        for job_id in stale:
            _JOBS.pop(job_id, None)


def _job_view(job: _Job) -> dict:
    return {
        "job_id": job.job_id,
        "connection_id": job.connection_id,
        "connection_name": job.connection_name,
        "status": job.status,
        "exit_code": job.exit_code,
        "running_seconds": round(
            (job.ended_at or time.monotonic()) - job.started_at, 2
        ),
        "next_cursor": job.next_cursor,
        "output_truncated": job.truncated,
    }


async def _cancel_jobs_for_profile(user_id: str, agent_id: str, connection_id: str) -> None:
    async with _JOBS_LOCK:
        jobs = [job for job in _JOBS.values()
                if job.user_id == user_id and job.agent_id == agent_id
                and job.connection_id == connection_id and job.status == "running"]
    for job in jobs:
        job.status = "cancelled"
        job.ended_at = time.monotonic()
        try:
            job.channel.close()
            job.client.close()
        except Exception as exc:  # noqa: BLE001 - best-effort cancellation
            logger.debug("SSH profile job cancellation cleanup failed: %s", exc)


async def start_background() -> None:
    """Register the shutdown hook; no persistent worker is needed."""


async def stop_background() -> None:
    async with _JOBS_LOCK:
        jobs = list(_JOBS.values())
    for job in jobs:
        if job.status == "running":
            job.status = "cancelled"
            try:
                job.channel.close()
                job.client.close()
            except Exception as exc:  # noqa: BLE001 - best-effort shutdown
                logger.debug("SSH shutdown cleanup failed: %s", exc)
    tasks = [job.reader_task for job in jobs if job.reader_task]
    if tasks:
        await asyncio.gather(*tasks, return_exceptions=True)


# ── Secure browser card API ─────────────────────────────────────────────────

router = APIRouter(prefix="/api/v1/ssh-control", tags=["ssh-control"])


class ProbeRequest(BaseModel):
    agent_id: str
    host: str
    port: int = Field(default=22, ge=1, le=65535)


class SaveConnectionRequest(BaseModel):
    agent_id: str
    connection_id: str | None = None
    name: str = Field(min_length=1, max_length=80)
    host: str = Field(min_length=1, max_length=255)
    port: int = Field(default=22, ge=1, le=65535)
    username: str = Field(min_length=1, max_length=128)
    auth_method: str
    password: str = ""
    private_key: str = ""
    key_passphrase: str = ""
    sudo_password: str = ""
    clear_sudo_password: bool = False
    expected_fingerprint: str


@router.post("/probe")
async def probe_connection(body: ProbeRequest, request: Request):
    await _api_actor(request, body.agent_id)
    try:
        result = await asyncio.to_thread(_probe_host, body.host, body.port)
    except Exception as exc:
        raise HTTPException(status_code=400, detail=_friendly_error(exc, body.host)) from exc
    return {
        "status": "ok",
        "host": body.host,
        "port": body.port,
        "fingerprint": result["fingerprint"],
        "key_type": result["key_type"],
    }


@router.post("/connections")
async def save_connection(body: SaveConnectionRequest, request: Request):
    user_id = await _api_actor(request, body.agent_id)
    auth_method = str(body.auth_method or "").strip().lower()
    if auth_method not in {"password", "private_key"}:
        raise HTTPException(status_code=400, detail="auth_method must be password or private_key.")
    if not str(body.expected_fingerprint or "").startswith("SHA256:"):
        raise HTTPException(status_code=400, detail="A trusted SHA-256 host-key fingerprint is required.")

    existing = None
    if body.connection_id:
        existing = await _get_profile(user_id, body.agent_id, body.connection_id)
        if not existing:
            raise HTTPException(status_code=404, detail="SSH connection not found.")
    elif len(await _list_profiles(user_id, body.agent_id)) >= MAX_PROFILES:
        raise HTTPException(status_code=409, detail=f"At most {MAX_PROFILES} SSH connections may be saved per agent.")

    secrets = dict((existing or {}).get("secrets") or {})
    for key in ("password", "private_key", "key_passphrase", "sudo_password"):
        incoming = str(getattr(body, key) or "")
        if incoming:
            secrets[key] = incoming
    if body.clear_sudo_password:
        secrets["sudo_password"] = ""
    if body.private_key and not body.key_passphrase:
        # A replacement unencrypted key must not inherit the old key's
        # passphrase. Blank passphrases otherwise preserve the saved value.
        secrets["key_passphrase"] = ""
    # Switching auth methods must not silently keep satisfying the new method
    # from a stale field the user did not intentionally submit.
    if existing and auth_method != existing["config"].get("auth_method"):
        required_key = "private_key" if auth_method == "private_key" else "password"
        if not str(getattr(body, required_key) or ""):
            raise HTTPException(status_code=400, detail=f"Enter a new {required_key.replace('_', ' ')} when changing authentication method.")

    connection_id = body.connection_id or str(uuid.uuid4())
    config = {
        "agent_id": body.agent_id,
        "connection_id": connection_id,
        "name": body.name.strip(),
        "host": body.host.strip(),
        "port": body.port,
        "username": body.username.strip(),
        "auth_method": auth_method,
        "host_key_fingerprint": body.expected_fingerprint.strip(),
        "host_key_type": "",
        "verified_at": None,
    }
    try:
        # Re-probe immediately so the key cannot change between the displayed
        # trust step and authentication/save.
        probed = await asyncio.to_thread(_probe_host, config["host"], config["port"])
        if probed["fingerprint"] != config["host_key_fingerprint"]:
            raise RuntimeError(
                "SSH host key changed before save; inspect the new fingerprint and try again."
            )
        client = await asyncio.to_thread(_connect, config, secrets)
        await asyncio.to_thread(client.close)
    except Exception as exc:
        raise HTTPException(status_code=400, detail=_friendly_error(exc, config["host"])) from exc

    from datetime import datetime
    config["host_key_type"] = probed["key_type"]
    config["verified_at"] = datetime.now(UTC).isoformat()
    config["configured"] = True
    config["has_sudo_password"] = bool(secrets.get("sudo_password"))
    await _save_profile(user_id, body.agent_id, config, secrets)
    return {"status": "ok", "connection": _public_profile(config, secrets)}


# ── Agent tools ──────────────────────────────────────────────────────────────

def build_tools(*, user_id: str = "", session_id: str = "", agent_id: str = "",
                agent_template_id: str = "", enabled_providers=None, **_ctx):
    async def ssh_request_connection(name: str, connection_id: str | None = None) -> str:
        """Open a secure card for creating or editing a named SSH connection."""
        denied = await _tool_gate(user_id, agent_id)
        if denied:
            return denied
        existing = None
        if connection_id:
            row = await _get_profile(user_id, agent_id, connection_id)
            if not row:
                return _json({"status": "error", "message": "SSH connection not found."})
            existing = _public_profile(row["config"], row["secrets"])
        return _json({
            "status": "ok",
            "ui": "ssh_connection_form",
            "agent_id": agent_id,
            "session_id": session_id,
            "connection_id": connection_id or "",
            "name": (name or (existing or {}).get("name") or "SSH device")[:80],
            "connection": existing,
            "message": (
                "Use this secure panel to inspect the host-key fingerprint and finish saving. "
                "Credentials go straight to the encrypted vault and are never shown to the agent."
            ),
        })

    async def ssh_list_connections() -> str:
        """List this user and agent's saved SSH devices without secret values."""
        denied = await _tool_gate(user_id, agent_id)
        if denied:
            return denied
        profiles = await _list_profiles(user_id, agent_id)
        return _json({"status": "ok", "count": len(profiles), "connections": profiles})

    async def ssh_test_connection(connection_id: str) -> str:
        """Authenticate to a saved SSH device and verify its pinned host key."""
        denied = await _tool_gate(user_id, agent_id)
        if denied:
            return denied
        row = await _get_profile(user_id, agent_id, connection_id)
        if not row:
            return _json({"status": "error", "message": "SSH connection not found."})
        try:
            client = await asyncio.to_thread(_connect, row["config"], row["secrets"])
            await asyncio.to_thread(client.close)
            return _json({"status": "ok", "connection": _public_profile(row["config"], row["secrets"])})
        except Exception as exc:  # noqa: BLE001 - return a safe agent-facing transport error
            return _json({"status": "error", "message": _friendly_error(exc, row["config"].get("host", "the device"))})

    async def ssh_delete_connection(connection_id: str) -> str:
        """Delete a saved SSH connection and cancel its live jobs."""
        denied = await _tool_gate(user_id, agent_id)
        if denied:
            return denied
        await _cancel_jobs_for_profile(user_id, agent_id, connection_id)
        deleted = await _delete_profile(user_id, agent_id, connection_id)
        return _json({"status": "ok" if deleted else "error", "deleted": deleted, "connection_id": connection_id})

    async def ssh_run_command(connection_id: str, command: str,
                              timeout_seconds: int = DEFAULT_COMMAND_TIMEOUT,
                              elevated: bool = False) -> str:
        """Run one bounded, non-interactive command on a saved SSH device."""
        denied = await _tool_gate(user_id, agent_id)
        if denied:
            return denied
        row = await _get_profile(user_id, agent_id, connection_id)
        if not row:
            return _json({"status": "error", "message": "SSH connection not found."})
        try:
            timeout = max(1, min(int(timeout_seconds), MAX_COMMAND_TIMEOUT))
            result = await asyncio.to_thread(
                _run_sync, row["config"], row["secrets"], command, timeout, bool(elevated)
            )
            result["connection_id"] = connection_id
            result["connection_name"] = row["config"].get("name")
            return _json(result)
        except Exception as exc:  # noqa: BLE001 - return a safe agent-facing command error
            return _json({"status": "error", "message": _friendly_error(exc, row["config"].get("host", "the device"))})

    async def ssh_start_job(connection_id: str, command: str,
                            elevated: bool = False,
                            max_runtime_seconds: int = DEFAULT_JOB_RUNTIME) -> str:
        """Start a process-lifetime background command and return a pollable job ID."""
        denied = await _tool_gate(user_id, agent_id)
        if denied:
            return denied
        await _prune_jobs()
        row = await _get_profile(user_id, agent_id, connection_id)
        if not row:
            return _json({"status": "error", "message": "SSH connection not found."})
        async with _JOB_START_LOCK:
            async with _JOBS_LOCK:
                running = sum(1 for job in _JOBS.values()
                              if job.user_id == user_id and job.agent_id == agent_id
                              and job.status == "running")
            if running >= MAX_RUNNING_JOBS:
                return _json({"status": "error", "message": f"At most {MAX_RUNNING_JOBS} SSH jobs may run at once."})
            try:
                runtime = max(1, min(int(max_runtime_seconds), MAX_JOB_RUNTIME))
                remote_command, stdin_data = _command_for(
                    command, bool(elevated), str(row["secrets"].get("sudo_password") or "")
                )

                def _open_job():
                    client = _connect(row["config"], row["secrets"])
                    try:
                        transport = client.get_transport()
                        channel = transport.open_session(timeout=20)
                        channel.exec_command(remote_command)
                        if stdin_data is not None:
                            channel.sendall(stdin_data.encode("utf-8"))
                            channel.shutdown_write()
                        return client, channel
                    except Exception:
                        client.close()
                        raise

                client, channel = await asyncio.to_thread(_open_job)
                job = _Job(
                    job_id=str(uuid.uuid4()), user_id=user_id, agent_id=agent_id,
                    connection_id=connection_id,
                    connection_name=str(row["config"].get("name") or "SSH device"),
                    client=client, channel=channel, started_at=time.monotonic(),
                    max_runtime=runtime,
                )
                async with _JOBS_LOCK:
                    _JOBS[job.job_id] = job
                job.reader_task = asyncio.create_task(_job_reader(job))
                return _json({"status": "ok", "job": _job_view(job)})
            except Exception as exc:  # noqa: BLE001 - return a safe agent-facing job error
                return _json({"status": "error", "message": _friendly_error(exc, row["config"].get("host", "the device"))})

    async def ssh_poll_job(job_id: str, cursor: int = 0, wait_seconds: float = 0) -> str:
        """Read incremental stdout/stderr and current state from one SSH job."""
        denied = await _tool_gate(user_id, agent_id)
        if denied:
            return denied
        await _prune_jobs()
        async with _JOBS_LOCK:
            job = _JOBS.get(job_id)
        if not job or job.user_id != user_id or job.agent_id != agent_id:
            return _json({"status": "error", "message": "SSH job not found."})
        try:
            cursor = max(0, int(cursor))
            wait = max(0.0, min(float(wait_seconds), 30.0))
        except (TypeError, ValueError):
            cursor, wait = 0, 0.0
        deadline = time.monotonic() + wait
        while job.status == "running" and job.next_cursor <= cursor and time.monotonic() < deadline:
            await asyncio.sleep(min(0.2, max(0.01, deadline - time.monotonic())))
        gap = cursor < job.base_cursor
        pos = max(cursor, job.base_cursor)
        events = []
        used = 0
        next_cursor = pos
        for start, end, stream, text in job.events:
            if end <= pos:
                continue
            chunk = text[max(0, pos - start):]
            if used + len(chunk) > MAX_POLL_CHARS:
                chunk = chunk[: MAX_POLL_CHARS - used]
            if chunk:
                events.append({"stream": stream, "text": chunk})
                used += len(chunk)
                next_cursor = max(next_cursor, max(start, pos) + len(chunk))
            if used >= MAX_POLL_CHARS:
                break
        return _json({
            "status": "ok", "job": _job_view(job), "events": events,
            "next_cursor": next_cursor, "cursor_gap": gap,
        })

    async def ssh_list_jobs(connection_id: str | None = None) -> str:
        """List this user and agent's live and recently completed SSH jobs."""
        denied = await _tool_gate(user_id, agent_id)
        if denied:
            return denied
        await _prune_jobs()
        async with _JOBS_LOCK:
            jobs = [job for job in _JOBS.values()
                    if job.user_id == user_id and job.agent_id == agent_id
                    and (not connection_id or job.connection_id == connection_id)]
        jobs.sort(key=lambda job: job.started_at, reverse=True)
        return _json({"status": "ok", "count": len(jobs), "jobs": [_job_view(job) for job in jobs]})

    async def ssh_cancel_job(job_id: str) -> str:
        """Close a running SSH job channel; detached remote children may survive."""
        denied = await _tool_gate(user_id, agent_id)
        if denied:
            return denied
        async with _JOBS_LOCK:
            job = _JOBS.get(job_id)
        if not job or job.user_id != user_id or job.agent_id != agent_id:
            return _json({"status": "error", "message": "SSH job not found."})
        if job.status != "running":
            return _json({"status": "ok", "cancelled": False, "job": _job_view(job)})
        job.status = "cancelled"
        job.ended_at = time.monotonic()
        try:
            job.channel.close()
            job.client.close()
        except Exception as exc:  # noqa: BLE001 - best-effort cancellation
            logger.debug("SSH job cancellation cleanup failed: %s", exc)
        return _json({
            "status": "ok", "cancelled": True, "job": _job_view(job),
            "warning": "The SSH channel was closed; deliberately detached child processes may continue remotely.",
        })

    TOOL_SCHEMAS.clear()
    TOOL_SCHEMAS.update(_SCHEMAS)
    return {
        "ssh_request_connection": ssh_request_connection,
        "ssh_list_connections": ssh_list_connections,
        "ssh_test_connection": ssh_test_connection,
        "ssh_delete_connection": ssh_delete_connection,
        "ssh_run_command": ssh_run_command,
        "ssh_start_job": ssh_start_job,
        "ssh_poll_job": ssh_poll_job,
        "ssh_list_jobs": ssh_list_jobs,
        "ssh_cancel_job": ssh_cancel_job,
    }


_CONNECTION_ID = {"type": "string", "description": "Opaque id returned by ssh_list_connections."}
_SCHEMAS = {
    "ssh_request_connection": {
        "type": "object",
        "properties": {
            "name": {"type": "string", "description": "Short device name shown on the secure card."},
            "connection_id": {"type": "string", "description": "Existing connection id when editing; omit when creating."},
        },
        "required": ["name"],
    },
    "ssh_list_connections": {"type": "object", "properties": {}, "required": []},
    "ssh_test_connection": {"type": "object", "properties": {"connection_id": _CONNECTION_ID}, "required": ["connection_id"]},
    "ssh_delete_connection": {"type": "object", "properties": {"connection_id": _CONNECTION_ID}, "required": ["connection_id"]},
    "ssh_run_command": {
        "type": "object",
        "properties": {
            "connection_id": _CONNECTION_ID,
            "command": {"type": "string", "description": "Exact non-interactive remote command."},
            "timeout_seconds": {"type": "integer", "minimum": 1, "maximum": MAX_COMMAND_TIMEOUT, "description": "Timeout; default 120 seconds."},
            "elevated": {"type": "boolean", "description": "Run through POSIX sudo using the saved sudo credential or passwordless sudo."},
        },
        "required": ["connection_id", "command"],
    },
    "ssh_start_job": {
        "type": "object",
        "properties": {
            "connection_id": _CONNECTION_ID,
            "command": {"type": "string", "description": "Exact non-interactive remote command."},
            "elevated": {"type": "boolean", "description": "Run through POSIX sudo."},
            "max_runtime_seconds": {"type": "integer", "minimum": 1, "maximum": MAX_JOB_RUNTIME, "description": "Maximum tracked runtime; default six hours."},
        },
        "required": ["connection_id", "command"],
    },
    "ssh_poll_job": {
        "type": "object",
        "properties": {
            "job_id": {"type": "string"},
            "cursor": {"type": "integer", "minimum": 0, "description": "Prior next_cursor; default zero."},
            "wait_seconds": {"type": "number", "minimum": 0, "maximum": 30, "description": "Long-poll until output/state changes."},
        },
        "required": ["job_id"],
    },
    "ssh_list_jobs": {
        "type": "object",
        "properties": {"connection_id": {"type": "string", "description": "Optional device filter."}},
        "required": [],
    },
    "ssh_cancel_job": {
        "type": "object",
        "properties": {"job_id": {"type": "string"}},
        "required": ["job_id"],
    },
}

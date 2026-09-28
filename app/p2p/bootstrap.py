"""Scoped bootstrap and source-authoritative replication for a deployed peer.

This is deliberately separate from the recurring manifest worker.  A new
installation may inherit selected app data, portable JSON configuration and
app-level credentials, then receive updated snapshots from its source. It must
never receive a byte-for-byte copy of ``app.db`` or an encryption root.

Secret values are read through the encrypted DB facade on the source and written
through the same facade on the target.  The target therefore encrypts every
secret with its own KEK/DEK instead of inheriting the source machine's keys.
"""

from __future__ import annotations

import hashlib
import json
import socket
import sqlite3
from pathlib import Path
from typing import Any

from app.p2p.manifest import classify_file
from app.p2p.vault_policy import should_sync_row

PROJECT_ROOT = Path(__file__).resolve().parent.parent.parent
CONFIG_DIR = PROJECT_ROOT / "data" / "config"

# Portable, durable app-plane rows.  Runtime coordination tables such as
# instances/device_presence/device_jobs/background_leader/storage_layout are
# intentionally absent.
PORTABLE_APP_TABLES = (
    "agent_templates",
    "agent_prompt_templates",
    "tools",
    "channel_identities",
    "user_profiles",
    "user_accounts",
)

def _selected(options: dict[str, Any], key: str) -> bool:
    return bool((options or {}).get(key))


def _export_app_rows() -> dict[str, list[dict[str, Any]]]:
    from app.db.storage_layout import get_app_store

    store = get_app_store(initialize=True)
    result: dict[str, list[dict[str, Any]]] = {}
    with store.connection() as conn:
        conn.row_factory = sqlite3.Row
        existing = {
            str(row[0])
            for row in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")
        }
        for table in PORTABLE_APP_TABLES:
            if table not in existing:
                continue
            rows = [dict(row) for row in conn.execute(f'SELECT * FROM "{table}"')]
            if table == "user_accounts":
                # Password hashes are portable login data; active remember tokens
                # are browser-session material and must not authenticate on a new box.
                for row in rows:
                    if "remember_token" in row:
                        row["remember_token"] = ""
            result[table] = rows
    return result


def _portable_configs() -> dict[str, Any]:
    result: dict[str, Any] = {}
    if not CONFIG_DIR.exists():
        return result
    for path in sorted(CONFIG_DIR.glob("*.json")):
        rel = path.relative_to(PROJECT_ROOT).as_posix()
        if classify_file(rel) != "full":
            continue
        try:
            result[path.name] = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            continue
    return result


def _app_vault_keys() -> list[tuple[str, str, str]]:
    from app.db.storage_layout import APP_DB_PATH

    path = APP_DB_PATH.with_name("app_secrets.db")
    if not path.exists():
        return []
    conn = sqlite3.connect(path)
    try:
        rows = conn.execute(
            "SELECT user_id, service, label FROM auth_elements "
            "ORDER BY user_id, service, label"
        ).fetchall()
    except sqlite3.Error:
        return []
    finally:
        conn.close()
    return [
        (str(user_id), str(service), str(label))
        for user_id, service, label in rows
        if should_sync_row(str(user_id), str(service), str(label))
    ]


async def _export_vault_rows(options: dict[str, Any]) -> tuple[list[dict], list[str]]:
    requested: set[tuple[str, str, str]] = set()
    if _selected(options, "app_secrets"):
        requested.update(_app_vault_keys())

    if not requested:
        return [], []

    from app.db import get_db

    db = get_db()
    rows: list[dict] = []
    warnings: list[str] = []
    for user_id, service, label in sorted(requested):
        try:
            row = await db.auth_element_get(user_id, service, label)
        except Exception as exc:  # noqa: BLE001 - report one row, keep the deploy useful
            warnings.append(f"Could not read {service}/{label}: {exc}")
            continue
        if not row:
            continue
        if row.get("_secret_error"):
            warnings.append(f"Skipped {service}/{label}: its secret could not be decrypted.")
            continue
        config = row.get("config") or {}
        if isinstance(config, str):
            try:
                config = json.loads(config or "{}")
            except ValueError:
                config = {}
        rows.append({
            "user_id": user_id,
            "service": service,
            "label": label,
            "config": config,
            # Plain only inside the encrypted, authenticated bootstrap request.
            # apply_payload writes through EncryptedStorageBackend on the target.
            "secret": str(row.get("secret_ref") or ""),
        })
    return rows, warnings


async def build_payload(options: dict[str, Any]) -> dict[str, Any]:
    """Build the selected one-time bootstrap payload on the source instance."""
    vault_rows, warnings = await _export_vault_rows(options or {})
    return {
        "version": 1,
        "options": {k: bool(v) for k, v in (options or {}).items()},
        "app_rows": _export_app_rows() if _selected(options, "app_db") else {},
        "configs": _portable_configs() if _selected(options, "app_configs") else {},
        "vault_rows": vault_rows,
        "warnings": warnings,
    }


def _apply_app_rows(tables: dict[str, list[dict[str, Any]]]) -> int:
    from app.db.storage_layout import get_app_store

    store = get_app_store(initialize=True)
    applied = 0
    with store.connection() as conn:
        for table in PORTABLE_APP_TABLES:
            rows = (tables or {}).get(table) or []
            if not rows:
                continue
            known = {
                str(row[1]) for row in conn.execute(f'PRAGMA table_info("{table}")')
            }
            for row in rows:
                keys = [key for key in row if key in known]
                if not keys:
                    continue
                cols = ", ".join(f'"{key}"' for key in keys)
                marks = ", ".join("?" for _ in keys)
                conn.execute(
                    f'INSERT OR REPLACE INTO "{table}" ({cols}) VALUES ({marks})',
                    tuple(row[key] for key in keys),
                )
                applied += 1
        conn.commit()
    return applied


def _apply_configs(configs: dict[str, Any]) -> int:
    from app.util.config_io import safe_write_json

    applied = 0
    for name, value in (configs or {}).items():
        safe_name = Path(str(name)).name
        if safe_name != name or not safe_name.endswith(".json"):
            continue
        rel = f"data/config/{safe_name}"
        if classify_file(rel) != "full":
            continue
        safe_write_json(CONFIG_DIR / safe_name, value)
        applied += 1
    return applied


async def apply_payload(payload: dict[str, Any]) -> dict[str, Any]:
    """Apply a source bootstrap payload on the new target instance."""
    if int((payload or {}).get("version") or 0) != 1:
        raise ValueError("Unsupported P2P bootstrap payload version")

    app_rows = _apply_app_rows(payload.get("app_rows") or {})
    configs = _apply_configs(payload.get("configs") or {})

    from app.db import get_db

    db = get_db()
    secrets = 0
    for row in payload.get("vault_rows") or []:
        user_id = str(row.get("user_id") or "")
        service = str(row.get("service") or "")
        label = str(row.get("label") or "default")
        if not user_id or not service or not should_sync_row(user_id, service, label):
            continue
        config = row.get("config") if isinstance(row.get("config"), dict) else {}
        await db.auth_element_set(
            user_id=user_id,
            service=service,
            label=label,
            config=config,
            secret_ref=str(row.get("secret") or ""),
        )
        secrets += 1

    return {
        "ok": True,
        "app_rows": app_rows,
        "config_files": configs,
        "secret_rows": secrets,
        "warnings": list(payload.get("warnings") or []),
    }


async def pair_and_push(
    target_url: str,
    source_url: str,
    options: dict[str, Any],
) -> dict[str, Any]:
    """Pair, bootstrap selected data, and register ongoing replica pushes."""
    import httpx

    from app.p2p import identity
    from app.p2p import store as peer_store
    from app.p2p.transport.crypto import local_x25519_public_key_b64
    from app.p2p.transport.http_transport import HttpTransport

    target = str(target_url or "").strip().rstrip("/")
    if not target:
        raise ValueError("The deployed instance did not report a reachable address")
    source = str(source_url or "").strip().rstrip("/")
    handshake = {
        "instance_id": identity.instance_id(),
        "name": socket.gethostname() or "WebAgent source",
        "public_key": identity.public_key_hex(),
        "x25519_public_key": local_x25519_public_key_b64(),
        "url": source,
        "sync_options": {k: bool(v) for k, v in (options or {}).items()},
        "bootstrap_only": True,
    }
    async with httpx.AsyncClient(timeout=30.0, verify=False) as client:
        status_response = await client.get(f"{target}/api/v1/p2p/status")
        if status_response.status_code != 200:
            raise RuntimeError(
                "The deployed WebAgent revision does not expose the P2P service "
                f"(status probe returned HTTP {status_response.status_code}). "
                "Release the scoped P2P bootstrap routes to the deployment repo, "
                "then update or recreate this instance."
            )
        try:
            target_status = status_response.json()
        except Exception as exc:
            raise RuntimeError("The target returned an invalid P2P status response") from exc
        capabilities = target_status.get("capabilities") or {}
        if not capabilities.get("scoped_bootstrap"):
            raise RuntimeError(
                "The deployed WebAgent revision has an older P2P service that does "
                "not support scoped configuration bootstrap. Update the instance "
                "before synchronizing it."
            )
        if (
            str(target_status.get("instance_id") or "") == identity.instance_id()
            or str(target_status.get("public_key") or "") == identity.public_key_hex()
        ):
            raise RuntimeError(
                "The target has a cloned copy of this instance's P2P identity. "
                "Remove data/config/p2p from the deployment repository and rotate "
                "the exposed identity before pairing."
            )
        response = await client.post(f"{target}/api/v1/p2p/handshake", json=handshake)
    if response.status_code != 200:
        raise RuntimeError(
            f"Target handshake returned HTTP {response.status_code}: {response.text[:160]}"
        )
    remote = response.json()
    peer = peer_store.add_peer(
        url=target,
        name=str(remote.get("name") or "New WebAgent instance"),
        public_key_hex=str(remote.get("public_key") or ""),
        remote_instance_id=str(remote.get("instance_id") or ""),
        x25519_public_key=str(remote.get("x25519_public_key") or ""),
        sync_options=options or {},
        bootstrap_only=True,
        push_replica=True,
    )
    payload = await build_payload(options or {})
    transport = HttpTransport()
    if not await transport.connect(peer):
        raise RuntimeError("Could not establish the encrypted P2P channel")
    response_bytes = await transport.send(
        peer["id"],
        "POST",
        "/api/v1/p2p/bootstrap/apply",
        json.dumps(payload, separators=(",", ":")).encode("utf-8"),
    )
    result = json.loads(response_bytes.decode("utf-8") or "{}")
    result["peer_id"] = peer["id"]
    result["target_instance_id"] = str(remote.get("instance_id") or "")
    replica_body = json.dumps(payload, sort_keys=True, separators=(",", ":")).encode("utf-8")
    peer_store.set_sync_state(
        peer["id"],
        status="insync",
        last_replica_hash=hashlib.sha256(replica_body).hexdigest(),
    )
    try:
        from app.p2p.worker import kick
        kick()
    except Exception:
        pass
    return result

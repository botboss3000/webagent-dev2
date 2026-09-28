"""Phase 3 storage/load benchmark with optional real Postgres execution.

Local mode is safe and deterministic. Set WEBAGENT_PHASE3_PROVIDER_DSN and pass
--provider to run the same manifest workload in an isolated temporary Postgres
schema over the real network/TLS path. The provider schema is dropped in a
finally block.
"""

from __future__ import annotations

import argparse
import json
import os
import sqlite3
import statistics
import sys
import tempfile
import time
import uuid
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from app.db.session_manifest import (
    POSTGRES_MANIFEST_DDL,
    compute_session_manifest,
    manifest_from_rows,
)


OUTPUT = ROOT / "docs" / "storage-authority-phase3-benchmark.json"
FIELDS = (
    "id,session_id,parent_id,role,content,tool_name,tool_call_id,channel,"
    "metadata,output,source,from_id,to_id,session_seq,turn_id,turn_seq,"
    "status,created_at"
)


def _percentile(samples: list[float], percentile: float) -> float:
    if not samples:
        return 0.0
    ordered = sorted(samples)
    index = min(len(ordered) - 1, int(round((len(ordered) - 1) * percentile)))
    return ordered[index]


def _summary(samples: list[float]) -> dict:
    return {
        "p50_ms": round(statistics.median(samples), 3) if samples else 0.0,
        "p95_ms": round(_percentile(samples, 0.95), 3),
        "max_ms": round(max(samples), 3) if samples else 0.0,
    }


def _rows(count: int, payload_bytes: int):
    content = "x" * payload_bytes
    for index in range(count):
        yield (
            str(uuid.uuid4()),
            "phase3-session",
            None,
            "user" if index % 2 == 0 else "assistant",
            content,
            None,
            None,
            "benchmark",
            "{}",
            None,
            "benchmark",
            "u",
            "a",
            index + 1,
            f"turn-{index // 2}",
            index % 2,
            "complete",
            datetime.now(timezone.utc).isoformat(),
        )


def run_local(row_count: int, payload_bytes: int) -> dict:
    with tempfile.TemporaryDirectory() as temp:
        path = Path(temp) / "phase3.sqlite"
        conn = sqlite3.connect(path)
        conn.executescript(
            """
            CREATE TABLE interactions (
                id TEXT PRIMARY KEY, session_id TEXT, parent_id TEXT, role TEXT,
                content TEXT, tool_name TEXT, tool_call_id TEXT, channel TEXT,
                metadata TEXT, output TEXT, source TEXT, from_id TEXT, to_id TEXT,
                session_seq INTEGER, turn_id TEXT, turn_seq INTEGER, status TEXT,
                created_at TEXT
            );
            """
        )
        conn.executemany(
            f"INSERT INTO interactions ({FIELDS}) VALUES ({','.join('?' for _ in range(18))})",
            _rows(row_count, payload_bytes),
        )
        conn.commit()

        started = time.perf_counter()
        cold = compute_session_manifest(conn, "phase3-session")
        cold_ms = (time.perf_counter() - started) * 1000
        warm_samples = []
        for _ in range(100):
            started = time.perf_counter()
            compute_session_manifest(conn, "phase3-session")
            warm_samples.append((time.perf_counter() - started) * 1000)

        full_bytes = row_count * payload_bytes
        manifest_bytes = len(json.dumps(cold, separators=(",", ":")).encode("utf-8"))
        conn.close()
    return {
        "provider": "sqlite-local",
        "provider_backed": False,
        "encrypted_transport": False,
        "row_count": row_count,
        "payload_bytes_per_row": payload_bytes,
        "cold_manifest_ms": round(cold_ms, 3),
        "warm_manifest": _summary(warm_samples),
        "estimated_full_transfer_bytes": full_bytes,
        "manifest_transfer_bytes": manifest_bytes,
        "transfer_reduction_ratio": round(1 - manifest_bytes / max(1, full_bytes), 6),
    }


def run_provider(dsn: str, row_count: int, payload_bytes: int) -> dict:
    import psycopg

    schema = f"wa_phase3_{uuid.uuid4().hex[:12]}"
    connect_samples: list[float] = []
    warm_samples: list[float] = []
    started = time.perf_counter()
    conn = psycopg.connect(dsn, autocommit=True, prepare_threshold=None)
    connect_samples.append((time.perf_counter() - started) * 1000)
    encrypted = False
    try:
        encrypted = bool(conn.execute(
            "SELECT ssl FROM pg_stat_ssl WHERE pid=pg_backend_pid()"
        ).fetchone()[0])
        conn.execute(f'CREATE SCHEMA "{schema}"')
        conn.execute(f'SET search_path TO "{schema}"')
        conn.execute(
            """
            CREATE TABLE interactions (
                id TEXT PRIMARY KEY, session_id TEXT, parent_id TEXT, role TEXT,
                content TEXT, tool_name TEXT, tool_call_id TEXT, channel TEXT,
                metadata TEXT, output TEXT, source TEXT, from_id TEXT, to_id TEXT,
                session_seq INTEGER, turn_id TEXT, turn_seq INTEGER, status TEXT,
                created_at TIMESTAMPTZ
            )
            """
        )
        conn.execute(POSTGRES_MANIFEST_DDL)
        placeholders = ",".join("%s" for _ in range(18))
        with conn.cursor() as cursor:
            cursor.executemany(
                f"INSERT INTO interactions ({FIELDS}) VALUES ({placeholders})",
                _rows(row_count, payload_bytes),
            )

        started = time.perf_counter()
        rows = conn.execute(
            f"SELECT {FIELDS} FROM interactions "
            "WHERE session_id=%s ORDER BY COALESCE(session_seq,0),created_at,id",
            ("phase3-session",),
        ).fetchall()
        manifest = manifest_from_rows(rows)
        conn.execute(
            """UPDATE session_manifests SET content_hash=%s,interaction_count=%s,
               max_session_seq=%s,dirty=0 WHERE session_id=%s""",
            (
                manifest["content_hash"],
                manifest["interaction_count"],
                manifest["max_session_seq"],
                "phase3-session",
            ),
        )
        cold_ms = (time.perf_counter() - started) * 1000
        for _ in range(100):
            started = time.perf_counter()
            conn.execute(
                "SELECT authority_revision,content_hash,interaction_count,"
                "max_session_seq,dirty FROM session_manifests WHERE session_id=%s",
                ("phase3-session",),
            ).fetchone()
            warm_samples.append((time.perf_counter() - started) * 1000)
        return {
            "provider": "postgres",
            "provider_backed": True,
            "encrypted_transport": encrypted,
            "row_count": row_count,
            "payload_bytes_per_row": payload_bytes,
            "connect": _summary(connect_samples),
            "cold_manifest_ms": round(cold_ms, 3),
            "warm_manifest": _summary(warm_samples),
        }
    finally:
        try:
            conn.execute("SET search_path TO public")
            conn.execute(f'DROP SCHEMA IF EXISTS "{schema}" CASCADE')
        finally:
            conn.close()


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--provider", action="store_true")
    parser.add_argument("--rows", type=int, default=5000)
    parser.add_argument("--payload-bytes", type=int, default=1024)
    args = parser.parse_args()
    result = {
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "browser_gates_changed": False,
        "workload": run_local(args.rows, args.payload_bytes),
    }
    if args.provider:
        dsn = os.environ.get("WEBAGENT_PHASE3_PROVIDER_DSN", "")
        if not dsn:
            raise SystemExit("WEBAGENT_PHASE3_PROVIDER_DSN is required with --provider")
        result["provider_workload"] = run_provider(
            dsn, args.rows, args.payload_bytes
        )
    else:
        result["provider_workload"] = {
            "status": "not-run",
            "reason": "Pass --provider with WEBAGENT_PHASE3_PROVIDER_DSN",
        }
    OUTPUT.write_text(json.dumps(result, indent=2) + "\n", encoding="utf-8")
    print(json.dumps(result, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

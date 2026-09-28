"""Phase 2 manifest and durable-reservation microbenchmark.

This measures local protocol/storage overhead only. It intentionally excludes
network, browser paint, model latency, and external provider latency.
"""

from __future__ import annotations

import argparse
import json
import sqlite3
import statistics
import sys
import tempfile
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from app.agent import turn_reservations
from app.db.session_manifest import compute_session_manifest


SCHEMA = """
CREATE TABLE interactions (
    id TEXT PRIMARY KEY, session_id TEXT, parent_id TEXT, role TEXT, content TEXT,
    tool_name TEXT, tool_call_id TEXT, channel TEXT, metadata TEXT, output TEXT,
    source TEXT, from_id TEXT, to_id TEXT, session_seq INTEGER, turn_id TEXT,
    turn_seq INTEGER, status TEXT, created_at TEXT
)
"""


def _percentiles(samples: list[float]) -> dict[str, float]:
    ordered = sorted(samples)
    p95_index = max(0, min(len(ordered) - 1, int(len(ordered) * 0.95) - 1))
    return {
        "p50_ms": round(statistics.median(ordered), 4),
        "p95_ms": round(ordered[p95_index], 4),
    }


def _measure(call, iterations: int) -> tuple[list[float], object]:
    samples = []
    value = None
    for _ in range(iterations):
        started = time.perf_counter()
        value = call()
        samples.append((time.perf_counter() - started) * 1000)
    return samples, value


def run(turns: int, chars: int, iterations: int) -> dict:
    with tempfile.TemporaryDirectory() as temp:
        root = Path(temp)
        conn = sqlite3.connect(root / "manifest.sqlite")
        conn.execute(SCHEMA)
        payload = "x" * chars
        rows = []
        for index in range(turns * 2):
            role = "user" if index % 2 == 0 else "assistant"
            rows.append(
                (
                    f"i-{index}", "session", None, role, payload, None, None,
                    "web", "{}", None, role, "u", "a", index + 1,
                    f"turn-{index // 2}", index % 2, "complete",
                    f"2026-01-01T00:00:{index:06d}Z",
                )
            )
        conn.executemany(
            "INSERT INTO interactions VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            rows,
        )
        conn.commit()

        cold_started = time.perf_counter()
        manifest = compute_session_manifest(conn, "session")
        cold_ms = (time.perf_counter() - cold_started) * 1000
        warm_samples, _ = _measure(
            lambda: compute_session_manifest(conn, "session"), iterations
        )
        manifest_wire = json.dumps(
            {"manifest": manifest, "not_modified": True},
            separators=(",", ":"),
        ).encode()
        full_wire = json.dumps(rows, separators=(",", ":"), default=str).encode()

        conn.execute("UPDATE interactions SET content=? WHERE id='i-0'", ("changed",))
        conn.commit()
        rebuild_started = time.perf_counter()
        rebuilt = compute_session_manifest(conn, "session")
        rebuild_ms = (time.perf_counter() - rebuild_started) * 1000
        conn.close()

        original_path = turn_reservations._DB_PATH
        turn_reservations._DB_PATH = root / "reservations.sqlite"
        try:
            counter = 0

            def reserve_and_complete():
                nonlocal counter
                counter += 1
                reservation = turn_reservations.reserve_turn(
                    "u", f"s-{counter}", f"k-{counter}", f"h-{counter}"
                )
                turn_reservations.complete(reservation, {"status": "complete"})

            reservation_samples, _ = _measure(reserve_and_complete, iterations)
        finally:
            turn_reservations._DB_PATH = original_path

    return {
        "fixture": {
            "turns": turns,
            "interactions": turns * 2,
            "characters_per_interaction": chars,
            "iterations": iterations,
        },
        "manifest": {
            "cold_hash_build_ms": round(cold_ms, 4),
            "post_mutation_rebuild_ms": round(rebuild_ms, 4),
            "warm_validation": _percentiles(warm_samples),
            "warm_sqlite_rows_read": 1,
            "cold_sqlite_rows_read": turns * 2,
            "full_transcript_bytes": len(full_wire),
            "validated_manifest_bytes": len(manifest_wire),
            "transfer_reduction_percent": round(
                100 * (1 - len(manifest_wire) / max(1, len(full_wire))), 2
            ),
            "revision_advanced_after_edit": (
                rebuilt["authority_revision"] > manifest["authority_revision"]
            ),
        },
        "durable_turn_reservation": {
            **_percentiles(reservation_samples),
            "sqlite_transactions_per_reservation": 2,
        },
        "scope": (
            "local storage/protocol microbenchmark; excludes network, browser "
            "paint, model latency, encryption overhead, and provider latency"
        ),
    }


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--turns", type=int, default=100)
    parser.add_argument("--chars", type=int, default=500)
    parser.add_argument("--iterations", type=int, default=100)
    parser.add_argument("--output", type=Path)
    args = parser.parse_args()
    rendered = json.dumps(run(args.turns, args.chars, args.iterations), indent=2) + "\n"
    if args.output:
        args.output.parent.mkdir(parents=True, exist_ok=True)
        args.output.write_text(rendered, encoding="utf-8")
    print(rendered, end="")


if __name__ == "__main__":
    main()

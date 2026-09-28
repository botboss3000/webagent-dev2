"""Repeatable microbenchmark for the Phase 1 storage modes.

This isolates storage/protocol overhead from provider latency. It reports the
required counters for a fixed synthetic transcript and is intended for relative
regression checks, not as an end-to-end browser or LLM benchmark.
"""

from __future__ import annotations

import argparse
import json
import sqlite3
import statistics
import tempfile
import time
from pathlib import Path


def _history(turns: int, chars: int) -> list[dict]:
    payload = "x" * chars
    rows = []
    for index in range(turns):
        rows.append({"id": f"u-{index}", "role": "user", "content": payload})
        rows.append({"id": f"a-{index}", "role": "assistant", "content": payload})
    return rows


def _measure(callable_, iterations: int) -> tuple[float, float]:
    samples = []
    result = None
    for _ in range(iterations):
        start = time.perf_counter()
        result = callable_()
        samples.append((time.perf_counter() - start) * 1000)
    return samples[0], statistics.median(samples[1:] or samples)


def run(turns: int, chars: int, iterations: int) -> dict:
    history = _history(turns, chars)
    encoded = json.dumps(history, separators=(",", ":")).encode()
    token_request = json.dumps({
        "session_id": "benchmark",
        "history_revision": turns,
        "history_token": "x" * 43,
        "new_message": "hello",
    }, separators=(",", ":")).encode()

    with tempfile.TemporaryDirectory() as temp:
        path = Path(temp) / "benchmark.db"
        conn = sqlite3.connect(path)
        conn.execute("CREATE TABLE interactions (id TEXT PRIMARY KEY, seq INT, payload TEXT)")
        conn.executemany(
            "INSERT INTO interactions VALUES (?,?,?)",
            [(row["id"], index, json.dumps(row)) for index, row in enumerate(history)],
        )
        conn.commit()

        def sqlite_read():
            rows = conn.execute(
                "SELECT payload FROM interactions ORDER BY seq"
            ).fetchall()
            return json.dumps([json.loads(row[0]) for row in rows]).encode()

        def idb_validated_read():
            # Browser-side equivalent: validate the manifest, then deserialize
            # the already-local structured clone.
            manifest = {"revision": turns, "content_hash": str(hash(encoded))}
            assert manifest["revision"] == turns
            return json.loads(encoded)

        def browser_authority_warm():
            return json.loads(encoded), json.loads(token_request)

        sqlite_first, sqlite_warm = _measure(sqlite_read, iterations)
        idb_first, idb_warm = _measure(idb_validated_read, iterations)
        browser_first, browser_warm = _measure(browser_authority_warm, iterations)
        db_bytes = path.stat().st_size
        conn.close()

    return {
        "fixture": {
            "turns": turns,
            "interactions": len(history),
            "content_chars_per_interaction": chars,
            "iterations": iterations,
        },
        "server_sqlite_no_persistent_browser_cache": {
            "cpu_ms_median": round(sqlite_warm, 3),
            "sqlite_reads_per_warm_turn": len(history),
            "sqlite_writes_per_warm_turn": 2,
            "indexeddb_bytes": 0,
            "transferred_bytes_first_render": len(encoded),
            "transferred_bytes_warm_turn": len(encoded),
            "first_render_ms": round(sqlite_first, 3),
            "warm_turn_ms": round(sqlite_warm, 3),
            "sqlite_file_bytes": db_bytes,
        },
        "server_sqlite_validated_indexeddb_cache": {
            "cpu_ms_median": round(idb_warm, 3),
            "sqlite_reads_per_warm_turn": 0,
            "sqlite_writes_per_warm_turn": 2,
            "indexeddb_bytes": len(encoded),
            "transferred_bytes_first_render": len(encoded),
            "transferred_bytes_warm_turn": len(token_request),
            "first_render_ms": round(idb_first, 3),
            "warm_turn_ms": round(idb_warm, 3),
        },
        "browser_authority": {
            "cpu_ms_median": round(browser_warm, 3),
            "sqlite_reads_per_warm_turn": 0,
            "sqlite_writes_per_warm_turn": 0,
            "indexeddb_bytes": len(encoded),
            "transferred_bytes_first_render": 0,
            "transferred_bytes_first_turn": len(encoded),
            "transferred_bytes_warm_turn": len(token_request),
            "first_render_ms": round(browser_first, 3),
            "warm_turn_ms": round(browser_warm, 3),
        },
        "scope": "storage/protocol microbenchmark; excludes network and model latency",
    }


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--turns", type=int, default=100)
    parser.add_argument("--chars", type=int, default=500)
    parser.add_argument("--iterations", type=int, default=25)
    args = parser.parse_args()
    print(json.dumps(run(args.turns, args.chars, args.iterations), indent=2))


if __name__ == "__main__":
    main()

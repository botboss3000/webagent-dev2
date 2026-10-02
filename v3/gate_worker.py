"""Optional final-reply checker, isolated from the WebAgent database.

Rust sends bounded metadata including a byte count, run identifiers, and
digests, never the reply text. This worker can pass or reject, but cannot run
tools or publish a chat message.
"""
import asyncio
import json
import os
import re
import socket
import struct
import sys
from pathlib import Path

PROTOCOL_VERSION = 1
MODULE_ID = "publication_size_guard_v1"
SOCKET = Path(os.environ.get("V3_GATE_SOCKET", "/run/v3/gate/gate.sock"))
MAX_FRAME = 16 * 1024
MAX_PUBLICATION_BYTES = 8192
APP_UID = 1100
HEX64 = re.compile(r"[0-9a-f]{64}\Z")
REQUEST_FIELDS = {
    "protocol_version", "request_id", "module_id", "checkpoint", "run_id",
    "generation", "policy_revision", "input_digest", "turn", "call_index", "facts",
}


def validate_request(value):
    if not isinstance(value, dict) or set(value) != REQUEST_FIELDS:
        raise ValueError("invalid gate request")
    if type(value["protocol_version"]) is not int or value["protocol_version"] != PROTOCOL_VERSION:
        raise ValueError("unsupported gate protocol")
    if value["module_id"] != MODULE_ID or value["checkpoint"] != "publication":
        raise ValueError("unsupported gate module or checkpoint")
    for field in ("request_id", "policy_revision", "input_digest"):
        if not isinstance(value[field], str) or not HEX64.fullmatch(value[field]):
            raise ValueError("invalid gate digest")
    for field in ("run_id", "generation"):
        item = value[field]
        if not isinstance(item, str) or not 1 <= len(item) <= 128 or any(ord(char) < 32 for char in item):
            raise ValueError("invalid gate identity")
    for field in ("turn", "call_index"):
        if type(value[field]) is not int or not 0 <= value[field] <= 1_000_000:
            raise ValueError("invalid gate position")
    facts = value["facts"]
    if not isinstance(facts, dict) or set(facts) != {"text_bytes"}:
        raise ValueError("invalid gate facts")
    if type(facts["text_bytes"]) is not int or not 0 <= facts["text_bytes"] <= 1_000_000_000:
        raise ValueError("invalid publication size")
    return value


# The first installed module has one rule: reject a reply over 8192 bytes.
# Keep the response tied to the request ID so Rust cannot mix up decisions.
def evaluate(value):
    request = validate_request(value)
    exceeds = request["facts"]["text_bytes"] > MAX_PUBLICATION_BYTES
    return {
        "protocol_version": PROTOCOL_VERSION,
        "request_id": request["request_id"],
        "module_id": MODULE_ID,
        "decision": "reject" if exceeds else "pass",
        "reason_code": "publication_size_exceeded" if exceeds else None,
    }


async def client(reader, writer, expected_uid=APP_UID):
    try:
        peer = writer.get_extra_info("socket").getsockopt(
            socket.SOL_SOCKET, socket.SO_PEERCRED, 12
        )
        if struct.unpack("3i", peer)[1] != expected_uid:
            return
        raw = await asyncio.wait_for(reader.readline(), 1)
        if not raw or len(raw) > MAX_FRAME or not raw.endswith(b"\n"):
            return
        value = json.loads(raw)
        response = evaluate(value)
        encoded = (json.dumps(response, separators=(",", ":")) + "\n").encode()
        if len(encoded) > MAX_FRAME:
            return
        writer.write(encoded)
        await asyncio.wait_for(writer.drain(), 1)
    except (ValueError, json.JSONDecodeError, asyncio.TimeoutError, asyncio.LimitOverrunError):
        pass
    finally:
        writer.close()
        await writer.wait_closed()


async def main():
    if os.getuid() != 1102 or os.getgid() != 1100:
        raise RuntimeError("gate must run as uid 1102, gid 1100")
    if not SOCKET.parent.is_dir() or SOCKET.parent.is_symlink():
        raise RuntimeError("gate socket directory unavailable")
    if SOCKET.exists():
        if not SOCKET.is_socket():
            raise RuntimeError("gate socket path occupied")
        SOCKET.unlink()
    server = await asyncio.start_unix_server(client, str(SOCKET), limit=MAX_FRAME + 1)
    SOCKET.chmod(0o660)
    async with server:
        await server.serve_forever()


if __name__ == "__main__":
    asyncio.run(main())

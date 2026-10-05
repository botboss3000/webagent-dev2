#!/usr/bin/env python3
"""Codex-side client for the protected V3 development build coordinator."""
import argparse
import json
import socket
import sys
import uuid

SOCKET = "/home/ar/webagent-dev2/v3/runtime/build/build.sock"


def call(name, arguments):
    request_id = str(uuid.uuid4())
    request = {"id": request_id, "name": name, "arguments": arguments}
    with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as connection:
        connection.settimeout(75)
        connection.connect(SOCKET)
        connection.sendall(json.dumps(request, separators=(",", ":")).encode() + b"\n")
        with connection.makefile("rb") as stream:
            raw = stream.readline(65537)
    if not raw.endswith(b"\n") or len(raw) > 65536:
        raise RuntimeError("bounded broker response unavailable")
    response = json.loads(raw)
    if response.get("id") != request_id:
        raise RuntimeError("broker response ID mismatch")
    if response.get("status") != "success":
        raise RuntimeError(str(response.get("output", "broker request failed")))
    return response["output"]


def main():
    parser = argparse.ArgumentParser(description="Coordinate V3 development builds with WebAgent")
    commands = parser.add_subparsers(dest="command", required=True)
    commands.add_parser("status")
    commands.add_parser("inputs")
    begin = commands.add_parser("begin")
    begin.add_argument("task_id")
    begin.add_argument("summary")
    finish = commands.add_parser("finish")
    finish.add_argument("task_id")
    finish.add_argument("--expected", required=True, help="inputs_sha256 obtained from inputs")
    abandon = commands.add_parser("abandon")
    abandon.add_argument("task_id")
    job = commands.add_parser("job")
    job.add_argument("job_id")
    args = parser.parse_args()
    if args.command == "inputs":
        result = call("v3_build_status", {})
    elif args.command == "job":
        result = call("v3_worker_job", {"job_id": args.job_id})
    else:
        arguments = {"action": args.command}
        if args.command in ("begin", "finish", "abandon"):
            arguments["task_id"] = args.task_id
        if args.command == "begin":
            arguments["summary"] = args.summary
        if args.command == "finish":
            arguments["expected_inputs_sha256"] = args.expected
        result = call("v3_build_work", arguments)
    print(json.dumps(result, sort_keys=True))


if __name__ == "__main__":
    try:
        main()
    except (OSError, ValueError, RuntimeError) as error:
        print(str(error), file=sys.stderr)
        raise SystemExit(1)

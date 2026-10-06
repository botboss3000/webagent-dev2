#!/usr/bin/env python3
"""Codex-side client for the protected V3 development build coordinator."""
import argparse
import json
import socket
import sys
import time
import uuid

SOCKET = "/home/ar/webagent-dev2/v3/runtime/build/build.sock"
POLL_SECONDS = 15
MAX_WAIT_SECONDS = 30 * 60
TERMINAL = {"completed", "failed", "stale", "cancelled", "uncertain", "interrupted"}
PENDING_EXIT = 3


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


def _excerpt(value, limit=1024):
    """Limit already-scrubbed broker text without splitting a UTF-8 sequence."""
    return str(value or "").encode("utf-8")[-limit:].decode("utf-8", "ignore")


def wait_task(task_id, timeout, request=call, clock=time.monotonic, pause=time.sleep):
    """Read one durable task and its eventual job; never schedule work."""
    result = {"task_id": task_id, "result": "still_pending"}
    try:
        receipt = next((item for item in request("v3_build_work", {"action": "status"})["own"]
                        if item["task_id"] == task_id), None)
    except (OSError, RuntimeError, ValueError, KeyError, TypeError):
        result["reason"] = "broker status unavailable; resume from this task ID"
        return result, PENDING_EXIT
    if receipt is None:
        result.update(result="failed", reason="task receipt unavailable to this caller")
        return result, 1
    job_id = receipt.get("job_id")
    if not job_id:
        result["reason"] = ("waiting for other registered tasks" if receipt["status"] == "closed"
                            else "task has not finished" if receipt["status"] == "open"
                            else "task was abandoned")
        if receipt["status"] == "abandoned":
            result["result"] = "failed"
            return result, 1
        return result, PENDING_EXIT

    result["job_id"] = job_id
    deadline = clock() + timeout
    while True:
        try:
            job = request("v3_worker_job", {"job_id": job_id})
        except (OSError, RuntimeError, ValueError, KeyError, TypeError):
            result["reason"] = "job record unavailable; resume from this task or job ID"
            return result, PENDING_EXIT
        result["status"] = job.get("status", "unknown")
        result["phase"] = job.get("phase", "unknown")
        if result["status"] in TERMINAL:
            result["reason"] = _excerpt(job.get("reason"))
            result["log_excerpt"] = _excerpt(job.get("log"))
            if result["status"] != "completed":
                result["result"] = "failed"
                return result, 1
            try:
                containers = request("v3_build_status", {})["containers"]
            except (OSError, RuntimeError, ValueError, KeyError, TypeError):
                result["reason"] = "container health unavailable; resume from this task or job ID"
                return result, PENDING_EXIT
            result["containers"] = {name: {"state": containers.get(name, {}).get("state"),
                                            "health": containers.get(name, {}).get("health")}
                                    for name in ("api", "loop")}
            if all(entry == {"state": "running", "health": "healthy"}
                   for entry in result["containers"].values()):
                result["result"] = "success"
                return result, 0
            result["result"] = "failed"
            result["reason"] = "completed job but V3 container health check failed"
            return result, 1
        remaining = deadline - clock()
        if remaining <= 0:
            result["reason"] = "wait limit reached; resume from this task or job ID"
            return result, PENDING_EXIT
        pause(min(POLL_SECONDS, remaining))


def main():
    parser = argparse.ArgumentParser(description="Coordinate V3 development builds with WebAgent")
    commands = parser.add_subparsers(dest="command", required=True)
    commands.add_parser("status")
    inputs = commands.add_parser("inputs")
    inputs.add_argument("--task", help="read this task's isolated workspace digest")
    begin = commands.add_parser("begin")
    begin.add_argument("task_id")
    begin.add_argument("summary")
    begin.add_argument("--shared", action="store_true", help="legacy shared checkout coordination")
    finish = commands.add_parser("finish")
    finish.add_argument("task_id")
    finish.add_argument("--expected", required=True, help="inputs_sha256 obtained from inputs")
    abandon = commands.add_parser("abandon")
    abandon.add_argument("task_id")
    job = commands.add_parser("job")
    job.add_argument("job_id")
    wait = commands.add_parser("wait", help="poll a finished task's shared job without submitting work")
    wait.add_argument("task_id")
    wait.add_argument("--timeout", type=int, default=MAX_WAIT_SECONDS,
                      help="seconds to poll an assigned job, 0-1800 (default: 1800)")
    args = parser.parse_args()
    if args.command == "wait":
        if not 0 <= args.timeout <= MAX_WAIT_SECONDS:
            parser.error("--timeout must be between 0 and 1800 seconds")
        result, code = wait_task(args.task_id, args.timeout)
        print(json.dumps(result, sort_keys=True))
        return code
    if args.command == "inputs":
        if args.task:
            own = call("v3_build_work", {"action": "status"})["own"]
            result = next((item for item in own if item["task_id"] == args.task), None)
            if result is None:
                raise ValueError("task receipt unavailable")
        else:
            result = call("v3_build_status", {})
    elif args.command == "job":
        result = call("v3_worker_job", {"job_id": args.job_id})
    else:
        arguments = {"action": args.command}
        if args.command in ("begin", "finish", "abandon"):
            arguments["task_id"] = args.task_id
        if args.command == "begin":
            arguments["summary"] = args.summary
            if not args.shared:
                arguments["mode"] = "isolated"
        if args.command == "finish":
            arguments["expected_inputs_sha256"] = args.expected
        result = call("v3_build_work", arguments)
    print(json.dumps(result, sort_keys=True))
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except (OSError, ValueError, RuntimeError) as error:
        print(str(error), file=sys.stderr)
        raise SystemExit(1)

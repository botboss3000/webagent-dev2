"""Run the Rust agent loop against the real Python worker in a disposable container.

From the development server:
docker run --rm --cap-drop ALL --cap-add CHOWN --cap-add SETUID --cap-add SETGID --cap-add KILL \
  --security-opt no-new-privileges:true --pids-limit 256 --memory 4g --cpus 2 \
  --mount type=bind,src=/home/ar/webagent-dev-v2/v3,dst=/build,readonly \
  --tmpfs /run:rw,nosuid,nodev,size=16m \
  --tmpfs /workspaces:rw,nosuid,nodev,size=32m \
  --workdir /build rust:1.90-bookworm \
  python3 -B /build/tests/run_real_worker_integration.py

No live state, workspace, source-promotion, or Docker socket mount is used.
"""
import json
import os
from pathlib import Path
import subprocess
import sys
import time

SOCKET = Path("/run/v3/worker/tools.sock")
CONTROL = Path("/run/v3/control")
WORKSPACES = Path("/workspaces")
TEST_NAME = "runtime_tests::real_worker_coding_gates_are_end_to_end"


def identity(uid):
    os.setgroups([])
    os.setgid(1100)
    os.setuid(uid)


def prepare():
    if os.getuid() != 0:
        raise RuntimeError("container harness must start as root")
    CONTROL.mkdir(parents=True, exist_ok=True)
    worker_dir = SOCKET.parent
    worker_dir.mkdir(parents=True, exist_ok=True)
    worker_dir.chmod(0o750)
    os.chown(worker_dir, 1101, 1100)
    WORKSPACES.chmod(0o770)
    os.chown(WORKSPACES, 1101, 1100)
    for name, value in (("dispatch.lock", ""), ("shutoff", "0")):
        path = CONTROL / name
        path.write_text(value)
        path.chmod(0o640)
        os.chown(path, 1100, 1100)
    CONTROL.chmod(0o750)
    os.chown(CONTROL, 1100, 1100)


def test_executable():
    env = os.environ.copy()
    env["CARGO_TARGET_DIR"] = "/tmp/v3-real-worker-target"
    env["CARGO_BUILD_JOBS"] = "2"
    build = subprocess.run(
        ["cargo", "test", "--locked", "--no-run", "--message-format=json"],
        cwd="/build", env=env, text=True, stdout=subprocess.PIPE, check=False,
    )
    if build.returncode:
        for line in build.stdout.splitlines():
            record = json.loads(line)
            if record.get("reason") == "compiler-message":
                print(record["message"]["rendered"], file=sys.stderr)
        raise RuntimeError(f"Rust test build failed: {build.returncode}")
    binaries = []
    for line in build.stdout.splitlines():
        record = json.loads(line)
        if record.get("reason") == "compiler-artifact" and record.get("executable"):
            target = record["target"]
            if target["name"] == "webagent-v3" and "bin" in target["kind"]:
                binaries.append(record["executable"])
    if len(binaries) != 1:
        raise RuntimeError(f"expected one Rust test binary, found {len(binaries)}")
    return binaries[0]


def main():
    prepare()
    os.setgid(1100)  # Traverse the worker-owned socket directory as its shared group.
    executable = test_executable()
    worker_env = {
        "PATH": "/usr/local/bin:/usr/bin:/bin",
        "LANG": "C.UTF-8",
        "V3_TOOL_SOCKET": str(SOCKET),
        "V3_WORKSPACE_ROOT": str(WORKSPACES),
    }
    worker = subprocess.Popen(
        ["/usr/bin/python3", "-I", "/build/worker.py"], env=worker_env,
        preexec_fn=lambda: identity(1101),
    )
    try:
        deadline = time.monotonic() + 10
        while not SOCKET.exists():
            if worker.poll() is not None:
                raise RuntimeError(f"worker exited at startup: {worker.returncode}")
            if time.monotonic() >= deadline:
                raise RuntimeError("worker socket did not appear")
            time.sleep(0.05)
        app_env = os.environ.copy()
        app_env.update({
            "V3_REAL_WORKER_SOCKET": str(SOCKET),
            "V3_REAL_WORKSPACE_ROOT": str(WORKSPACES),
            "V3_REAL_CONTROL_DIR": str(CONTROL),
        })
        test = subprocess.run(
            [executable, "--exact", TEST_NAME, "--ignored", "--nocapture"],
            cwd="/build", env=app_env, preexec_fn=lambda: identity(1100),
            check=False,
        )
        if test.returncode:
            raise SystemExit(test.returncode)
        if worker.poll() is not None:
            raise RuntimeError(f"worker died during test: {worker.returncode}")
    finally:
        worker.terminate()
        try:
            worker.wait(timeout=5)
        except subprocess.TimeoutExpired:
            worker.kill()
            worker.wait()


if __name__ == "__main__":
    main()

"""Container bootstrap; root is used only to establish OS identities."""
import ctypes
import os
import signal
import subprocess
import time
from pathlib import Path


def drop(uid):
    os.setgroups([])
    os.setgid(1100)
    os.setuid(uid)
    # PR_SET_NO_NEW_PRIVS: child executables cannot gain privileges.
    if ctypes.CDLL(None).prctl(38, 1, 0, 0, 0) != 0:
        raise RuntimeError("cannot disable privilege escalation")


def directory(path, uid, mode):
    Path(path).mkdir(parents=True, exist_ok=True)
    os.chown(path, uid, 1100)
    os.chmod(path, mode)


directory("/state", 1100, 0o700)
directory("/workspaces", 1101, 0o770)
directory("/run/v3", 0, 0o700)
directory("/run/v3/control", 0, 0o700)
directory("/run/v3/worker", 0, 0o700)
for name, content in [("dispatch.lock", ""), ("shutoff", "1")]:
    path = "/run/v3/control/" + name
    if Path(path).exists():
        os.chown(path, 0, 0)
    Path(path).write_text(content)
    os.chown(path, 1100, 1100)
    os.chmod(path, 0o640)
directory("/run/v3/control", 1100, 0o750)
directory("/run/v3/worker", 1101, 0o750)
directory("/run/v3", 1100, 0o755)
os.setgroups([])
os.setgid(1100)
worker = subprocess.Popen(["/usr/bin/python3", "-I", "/opt/v3/worker.py"],
                          env={"PATH": "/usr/bin:/bin", "LANG": "C.UTF-8",
                               "V3_TOOL_SOCKET": "/run/v3/worker/tools.sock",
                               "V3_WORKSPACE_ROOT": "/workspaces"},
                          preexec_fn=lambda: drop(1101))
for _ in range(100):
    if Path("/run/v3/worker/tools.sock").exists():
        break
    if worker.poll() is not None:
        raise RuntimeError("tool worker failed")
    time.sleep(0.05)
else:
    worker.terminate()
    raise RuntimeError("tool worker startup timed out")
app = subprocess.Popen(["/usr/local/bin/webagent-v3"], preexec_fn=lambda: drop(1100))
# Supervisor itself drops to the application identity; it retains no root privileges.
drop(1100)


def stop(_signum, _frame):
    # App has a different uid; container init delivers SIGTERM to the group.
    app.terminate()


signal.signal(signal.SIGTERM, stop)
while app.poll() is None and worker.poll() is None:
    time.sleep(0.2)
if app.poll() is None:
    app.terminate()
raise SystemExit(app.poll() or worker.poll() or 1)

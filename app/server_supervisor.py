"""Keep one V2 Uvicorn worker alive across explicit application restarts."""
from __future__ import annotations
import logging
import os
import signal
import subprocess
import sys
import time

log = logging.getLogger(__name__)

def supervise(command: list[str], grace: float = 15.0) -> int:
    stopping = False
    restarting = False
    def stop(_signum, _frame):
        nonlocal stopping
        stopping = True
    def restart(_signum, _frame):
        nonlocal restarting
        restarting = True
    old = {sig: signal.signal(sig, handler) for sig, handler in (
        (signal.SIGTERM, stop), (signal.SIGINT, stop), (signal.SIGHUP, restart))}
    worker = None
    def retire():
        nonlocal worker
        if worker is None:
            return
        try:
            os.killpg(worker.pid, signal.SIGTERM)
        except ProcessLookupError:
            pass
        try:
            worker.wait(timeout=grace)
        except subprocess.TimeoutExpired:
            log.warning("worker pid %s did not stop in %.1fs", worker.pid, grace)
        try:
            os.killpg(worker.pid, signal.SIGKILL)
        except ProcessLookupError:
            pass
        worker.wait()
    try:
        while not stopping:
            restarting = False
            worker = subprocess.Popen(command, start_new_session=True)
            log.info("started application worker pid=%s", worker.pid)
            while not stopping and not restarting and worker.poll() is None:
                time.sleep(0.1)
            unexpected = not stopping and not restarting
            if unexpected:
                log.warning("application worker exited with code %s; recovering", worker.returncode)
            retire()
            worker = None
            if unexpected:
                time.sleep(1)
    finally:
        retire()
        for sig, handler in old.items():
            signal.signal(sig, handler)
    return 0

if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO, format="%(asctime)s supervisor %(message)s")
    raise SystemExit(supervise([
        sys.executable, "-m", "uvicorn", "app.v2:app",
        "--host", "0.0.0.0", "--port", "8080", "--workers", "1",
        "--ws", "wsproto", "--timeout-graceful-shutdown", "10",
    ]))

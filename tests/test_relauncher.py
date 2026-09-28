from __future__ import annotations

import tempfile
import threading
import unittest
from pathlib import Path
from unittest.mock import patch

from app import relauncher


class _ImmediateThread:
    def __init__(self, *, target, daemon):
        self._target = target
        self.daemon = daemon

    def start(self):
        self._target()


class RelauncherTests(unittest.TestCase):
    def test_trigger_restart_uses_reload_supervisor_without_exiting_worker(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp) / "webagent"
            app_dir = root / "app"
            app_dir.mkdir(parents=True)
            (root / "run.py").write_text("", encoding="utf-8")
            main_py = app_dir / "main.py"
            main_py.write_text("", encoding="utf-8")

            touched: list[str] = []

            def unexpected(*_args, **_kwargs):
                raise AssertionError("reload-supervised restart must not spawn or exit")

            with (
                patch.object(relauncher, "_project_root", return_value=str(root)),
                patch.object(relauncher.sys, "argv", ["uvicorn", "app.main:app", "--reload"]),
                patch.object(relauncher.subprocess, "Popen", side_effect=unexpected),
                patch.object(relauncher.os, "_exit", side_effect=unexpected),
                patch.object(relauncher.os, "utime", side_effect=lambda path, *_: touched.append(path)),
            ):
                result = relauncher.trigger_restart(delay=0.01)

            self.assertEqual(result["status"], "restarting")
            self.assertTrue(result["auto_restart"])
            self.assertEqual(result["mode"], "reload")
            self.assertEqual(touched, [str(main_py)])

    def test_trigger_restart_reloads_uvicorn_workers_without_exiting_container(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp) / "webagent"
            (root / "app").mkdir(parents=True)
            (root / "run.py").write_text("", encoding="utf-8")

            signals: list[tuple[int, int]] = []

            def unexpected(*_args, **_kwargs):
                raise AssertionError(
                    "multiprocess restart must signal Uvicorn without spawning or exiting"
                )

            with (
                patch.object(relauncher, "_project_root", return_value=str(root)),
                patch.object(relauncher, "_reload_supervisor_active", return_value=False),
                patch.object(
                    relauncher,
                    "_uvicorn_multiprocess_supervisor_pid",
                    return_value=321,
                    create=True,
                ),
                patch.object(relauncher, "auto_restart_available", side_effect=unexpected),
                patch.object(relauncher.subprocess, "Popen", side_effect=unexpected),
                patch.object(threading, "Thread", _ImmediateThread),
                patch.object(relauncher.time, "sleep"),
                patch.object(relauncher.os, "kill",
                             side_effect=lambda pid, sig: signals.append((pid, sig))),
                patch.object(relauncher.os, "killpg", side_effect=unexpected),
                patch.object(relauncher.os, "_exit", side_effect=unexpected),
            ):
                result = relauncher.trigger_restart(delay=0.01)

            self.assertEqual(result["status"], "restarting")
            self.assertEqual(result["mode"], "multiprocess")
            self.assertEqual(signals, [(321, relauncher.signal.SIGHUP)])

    def test_mp_reload_forces_turnover_when_sighup_does_not_retire_workers(self):
        # A wedged supervisor accepts SIGHUP but never retires its workers (it is
        # parked in Process.join()). _mp_reload must then force the turnover by
        # killing the workers — never the supervisor/Tini/container — so the
        # blocked join() returns and Uvicorn respawns a fresh set.
        old_workers = {4001, 4002}
        killed: list[tuple[int, int]] = []

        with (
            patch.object(relauncher, "_IS_WIN", False),
            patch.object(relauncher, "_worker_children", return_value=set(old_workers)),
            patch.object(relauncher, "_await_retirement", return_value=False),
            patch.object(relauncher.time, "sleep"),
            patch.object(relauncher.os, "kill",
                         side_effect=lambda pid, sig: killed.append((pid, sig))),
        ):
            relauncher._mp_reload(321, delay=0.01)

        self.assertEqual(killed[0], (321, relauncher.signal.SIGHUP))
        forced = sorted(pid for pid, sig in killed[1:] if sig == relauncher.signal.SIGKILL)
        self.assertEqual(forced, sorted(old_workers))

    def test_mp_reload_leaves_workers_alone_when_sighup_retires_them(self):
        killed: list[tuple[int, int]] = []

        with (
            patch.object(relauncher, "_IS_WIN", False),
            patch.object(relauncher, "_worker_children", return_value={4001}),
            patch.object(relauncher, "_await_retirement", return_value=True),
            patch.object(relauncher.time, "sleep"),
            patch.object(relauncher.os, "kill",
                         side_effect=lambda pid, sig: killed.append((pid, sig))),
        ):
            relauncher._mp_reload(321, delay=0.01)

        self.assertEqual(killed, [(321, relauncher.signal.SIGHUP)])

    def test_detects_uvicorn_multiprocess_supervisor_pid_conservatively(self):
        argv = ["python", "uvicorn", "app.main:app", "--workers", "4"]
        with (
            patch.object(relauncher, "_IS_WIN", False),
            patch.object(relauncher, "_process_argv", return_value=argv),
            patch.object(relauncher.os, "getppid", return_value=321),
            patch.object(relauncher.os, "getpgid", return_value=321),
            patch.object(relauncher.os, "getpgrp", return_value=321),
        ):
            self.assertEqual(relauncher._uvicorn_multiprocess_supervisor_pid(), 321)
            with patch.object(relauncher, "_worker_count", return_value=1):
                self.assertIsNone(relauncher._uvicorn_multiprocess_supervisor_pid())

    def test_trigger_restart_preserves_single_process_exit(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp) / "webagent"
            (root / "app").mkdir(parents=True)
            (root / "run.py").write_text("", encoding="utf-8")
            exits: list[int] = []

            with (
                patch.object(relauncher, "_project_root", return_value=str(root)),
                patch.object(relauncher, "_reload_supervisor_active", return_value=False),
                patch.object(relauncher, "_uvicorn_multiprocess_supervisor_pid", return_value=None),
                patch.object(relauncher.subprocess, "Popen"),
                patch.object(threading, "Thread", _ImmediateThread),
                patch.object(relauncher.time, "sleep"),
                patch.object(relauncher.os, "_exit",
                             side_effect=lambda code: exits.append(code)),
                patch.object(relauncher.os, "killpg",
                             side_effect=AssertionError("single process must not kill a group")),
            ):
                result = relauncher.trigger_restart(delay=0.01)

            self.assertEqual(result["status"], "restarting")
            self.assertNotIn("mode", result)
            self.assertEqual(exits, [0])


if __name__ == "__main__":
    unittest.main()

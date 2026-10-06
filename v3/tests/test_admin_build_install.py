import hashlib
import importlib.util
import json
import os
from pathlib import Path
import tempfile
import socket
import struct
import threading
import types
import unittest
from unittest.mock import Mock, patch
import uuid

PATH = Path(__file__).resolve().parents[1] / "plugins/abilities/Administrator/v3_build/install.py"
spec = importlib.util.spec_from_file_location("admin_build_install", PATH)
installer = importlib.util.module_from_spec(spec)
spec.loader.exec_module(installer)


class FakePrivileges:
    def __init__(self):
        self.owners = {}
        self.euid = 0
    def uid(self): return self.euid
    def owner(self, info): return self.owners.get(info.st_ino, 1000)
    def chown(self, fd, uid, gid): self.owners[os.fstat(fd).st_ino] = uid


class InstallTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.v3 = self.root / "v3"
        self.source = self.v3 / "plugins/abilities/Administrator/v3_build"
        self.source.mkdir(parents=True)
        self.host = b"CONTEXT = ('v3/src',)\n"
        (self.source / "host.py").write_bytes(self.host)
        (self.source / "install.py").write_bytes(b"# fixture bootstrap\n")
        (self.source / "release.py").write_bytes(b"# fixture release\n")
        self.fake = FakePrivileges()
        self.patches = [patch.object(installer.shutil, "disk_usage", return_value=types.SimpleNamespace(free=5*1024*1024*1024)),
                        patch.object(installer, "PRIVILEGES", self.fake),
                        patch.object(installer, "V3", self.v3), patch.object(installer, "SOURCE", self.source),
                        patch.object(installer, "SERVICE", self.v3 / "host-build-service"),
                        patch.object(installer, "JOBS", self.v3 / "build-jobs"),
                        patch.object(installer, "ARCHIVES", self.v3 / "rollback/administrator")]
        for item in self.patches: item.start()

    def tearDown(self):
        for item in reversed(self.patches): item.stop()
        self.temp.cleanup()

    def test_install_exact_protected_code_and_repeat(self):
        result = installer.install()
        service = self.v3 / "host-build-service"
        self.assertEqual(self.host, (service / "host.py").read_bytes())
        self.assertEqual(hashlib.sha256(self.host).hexdigest(), result["host_sha256"])
        self.assertEqual(0o755, service.stat().st_mode & 0o777)
        self.assertEqual(0o644, (service / "host.py").stat().st_mode & 0o777)
        self.assertEqual(0o600, (service / "installed.json").stat().st_mode & 0o777)
        self.assertEqual(0, self.fake.owner((service / "host.py").stat()))
        self.assertEqual(result, installer.install())

    def test_nonroot_and_precreated_unowned_service_rejected(self):
        self.fake.euid = 1000
        with self.assertRaises(PermissionError): installer.install()
        self.fake.euid = 0
        (self.v3 / "host-build-service").mkdir()
        with self.assertRaisesRegex(PermissionError, "not root-owned"): installer.install()

    def test_symlink_and_hardlink_sources_or_destinations_rejected(self):
        (self.source / "host.py").unlink()
        outside = self.root / "private"
        outside.write_bytes(b"PRIVATE")
        (self.source / "host.py").symlink_to(outside)
        with self.assertRaises(OSError): installer.install()
        (self.source / "host.py").unlink()
        os.link(outside, self.source / "host.py")
        with self.assertRaisesRegex(ValueError, "single-link"): installer.install()
        (self.source / "host.py").unlink()
        (self.source / "host.py").write_bytes(self.host)
        installer.install()
        service = self.v3 / "host-build-service"
        (service / "host.py").unlink()
        (service / "host.py").symlink_to(outside)
        with self.assertRaises(PermissionError): installer.install()
        self.assertEqual(b"PRIVATE", outside.read_bytes())

    def test_component_symlink_directory_is_not_followed(self):
        outside = self.root / "outside"
        outside.mkdir()
        (self.root / "link").symlink_to(outside, target_is_directory=True)
        with self.assertRaises(OSError): installer.directory(self.root / "link")

    def artifact_fixture(self):
        jobs = self.v3 / "build-jobs"
        jobs.mkdir(mode=0o700)
        job = jobs / str(uuid.uuid4())
        job.mkdir(mode=0o700)
        (job / "rollback-images.tar").write_bytes(b"image export fixture")
        config = {role: {"Image": "sha256:" + "a" * 64, "Config": {"Env": ["TOKEN=PRIVATE-FIXTURE"]}, "HostConfig": {}} for role in ("api", "loop")}
        (job / "container-config.json").write_text(json.dumps(config))
        for name in ("rollback-images.tar", "container-config.json"): os.chmod(job / name, 0o600)
        return job

    def test_root_archives_only_two_private_artifacts_and_deduplicates(self):
        job = self.artifact_fixture()
        (job / "session.db").write_bytes(b"SQLite format 3\0PRIVATE")
        result = installer.archive_rollbacks()
        self.assertEqual({"copied": 2, "failed": 0}, result)
        protected = self.v3 / "rollback/administrator" / job.name
        self.assertEqual({"rollback-images.tar", "container-config.json", "manifest.json"}, {p.name for p in protected.iterdir()})
        self.assertEqual(0o700, protected.stat().st_mode & 0o777)
        self.assertEqual(0o600, (protected / "container-config.json").stat().st_mode & 0o777)
        self.assertEqual(0, self.fake.owner((protected / "rollback-images.tar").stat()))
        self.assertNotIn("PRIVATE-FIXTURE", (protected / "manifest.json").read_text())
        self.assertEqual({"copied": 0, "failed": 0}, installer.archive_rollbacks())

    def test_archive_rejects_symlink_hardlink_database_and_disk_exhaustion(self):
        job = self.artifact_fixture()
        source_fd = installer.directory(job)
        destination = self.root / "destination"
        destination.mkdir()
        destination_fd = installer.directory(destination)
        try:
            with patch.object(installer.shutil, "disk_usage", return_value=types.SimpleNamespace(free=0)), self.assertRaisesRegex(ValueError, "space"):
                installer.copy_artifact(source_fd, destination_fd, "rollback-images.tar", 1000)
            (job / "rollback-images.tar").write_bytes(b"SQLite format 3\0PRIVATE")
            with self.assertRaisesRegex(ValueError, "database"):
                installer.copy_artifact(source_fd, destination_fd, "rollback-images.tar", 1000)
            (job / "rollback-images.tar").unlink()
            (job / "rollback-images.tar").symlink_to(job / "container-config.json")
            with self.assertRaises(OSError): installer.copy_artifact(source_fd, destination_fd, "rollback-images.tar", 1000)
            (job / "rollback-images.tar").unlink()
            os.link(job / "container-config.json", job / "rollback-images.tar")
            with self.assertRaises(PermissionError): installer.copy_artifact(source_fd, destination_fd, "rollback-images.tar", 1000)
            self.assertEqual([], list(destination.iterdir()))
        finally:
            os.close(source_fd); os.close(destination_fd)

    def test_snapshot_is_fixed_source_and_two_gate_files(self):
        src = self.v3 / "src"
        src.mkdir()
        (src / "main.rs").write_text("fn main() {}")
        cache = src / "__pycache__"
        cache.mkdir()
        (cache / "generated.pyc").write_bytes(b"bytecode")
        (src / "generated.pyo").write_bytes(b"bytecode")
        gates = self.v3 / "data/config/gates"
        gates.mkdir(parents=True)
        (gates / "message_visibility_v1.json").write_text("{}")
        context = installer.snapshot_context(("v3/src", "v3/data/config/gates/message_visibility_v1.json"))
        self.assertGreater(len(context), 0)
        with installer.tarfile.open(fileobj=installer.io.BytesIO(context)) as archive:
            names = [member.name for member in archive]
        self.assertIn("v3/src/main.rs", names)
        self.assertFalse(any("__pycache__" in name or name.endswith((".pyc", ".pyo")) for name in names))
        for invalid in ("v3/data/session.db", "v3/workspaces/source", "v3/../data"):
            with self.assertRaises(ValueError): installer.snapshot_context((invalid,))
        (src / "secret.db").write_bytes(b"SQLite format 3")
        with self.assertRaisesRegex(ValueError, "database"): installer.snapshot_context(("v3/src",))

    def test_snapshot_protocol_checks_peer_before_reading_source(self):
        stopped = threading.Event()
        class Connection:
            def __init__(self): self.output = b""
            def __enter__(self): return self
            def __exit__(self, *args): pass
            def settimeout(self, value): pass
            def getsockopt(self, *args): return struct.pack("3i", 1, 1101, 1100)
            def recv(self, count): raise AssertionError("unauthorized peer input must not be read")
            def sendall(self, data): self.output += data
        conn = Connection()
        class Listener:
            def settimeout(self, value): pass
            def accept(self): stopped.set(); return conn, None
        with patch.object(installer, "snapshot_context", side_effect=AssertionError("unauthorized snapshot")):
            installer.snapshot_server(Listener(), ("v3/src",), stopped)
        self.assertEqual(struct.pack("!Q", 0), conn.output)

    def test_privilege_drop_groups_before_ids_no_new_privs_fixed_exec(self):
        calls = []
        libc = Mock()
        libc.prctl.side_effect = lambda *args: calls.append(("no_new_privs", args)) or 0
        def record(name): return lambda *args: calls.append((name, args))
        with patch.object(installer.pwd, "getpwnam", return_value=types.SimpleNamespace(pw_uid=1000, pw_gid=1000)), \
             patch.object(installer.os, "initgroups", record("groups")), patch.object(installer.os, "setgid", record("gid")), \
             patch.object(installer.os, "setuid", record("uid")), patch.object(installer.os, "set_inheritable", record("inheritable")), \
             patch.object(installer.os, "execve", record("exec")), patch.object(installer.ctypes, "CDLL", return_value=libc):
            installer._drop_and_exec(42, 43, 44)
        self.assertEqual(["groups", "gid", "uid", "no_new_privs"], [name for name, _ in calls[:4]])
        command = calls[-1][1]
        self.assertEqual("/usr/bin/python3", command[0])
        self.assertIn("/proc/self/fd/42", command[1])
        self.assertEqual({"PATH", "HOME", "LANG"}, set(command[2]))


if __name__ == "__main__": unittest.main()

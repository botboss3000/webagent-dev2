"""Fail-closed Linux sandbox for untrusted workspace subprocesses.

Trusted supervisors retain cancellation and descendant cleanup authority. The
child cannot access another workspace, application state, control sockets, or
signal processes outside its Landlock domain. No credentials enter its env.
"""
import ctypes
import os
from pathlib import Path

class Ruleset(ctypes.Structure):
    _fields_ = [("handled_access_fs", ctypes.c_uint64),
                ("handled_access_net", ctypes.c_uint64),
                ("scoped", ctypes.c_uint64)]

class PathRule(ctypes.Structure):
    _pack_ = 1
    _fields_ = [("allowed_access", ctypes.c_uint64),
                ("parent_fd", ctypes.c_int32)]

READ = (1 << 0) | (1 << 2) | (1 << 3)
# No device-node creation or device ioctl access, including in the workspace.
WRITE = sum(1 << bit for bit in (1, 4, 5, 7, 8, 9, 10, 12, 13, 14))

def restrict(base, read_paths=()):
    base = Path(base).resolve(strict=True)
    libc = ctypes.CDLL(None, use_errno=True)
    def call(number, *args):
        result = libc.syscall(number, *args)
        if result < 0:
            raise OSError(ctypes.get_errno(), "workspace sandbox unavailable")
        return result
    abi = call(444, 0, 0, 1)
    if abi < 6:
        raise RuntimeError("workspace sandbox requires Landlock ABI 6 or newer")
    # Handle every filesystem right supported through ABI 8, deny unlisted
    # paths, and isolate signals and abstract Unix sockets from other domains.
    attr = Ruleset((1 << 16) - 1, 1, 3)
    ruleset = call(444, ctypes.byref(attr), ctypes.sizeof(attr), 0)
    try:
        def allow(path, rights):
            if not os.path.exists(path):
                return
            fd = os.open(path, os.O_PATH | os.O_CLOEXEC)
            try:
                if not os.path.isdir(path):
                    rights &= (1 << 0) | (1 << 1) | (1 << 2) | (1 << 14)
                rule = PathRule(rights, fd)
                call(445, ruleset, 1, ctypes.byref(rule), 0)
            finally:
                os.close(fd)
        for path in ("/usr", "/bin", "/lib", "/lib64", "/opt/rust"):
            allow(path, READ)
        for path in ("/etc/ld.so.cache", "/etc/resolv.conf", "/etc/hosts",
                     "/etc/nsswitch.conf", "/etc/ssl/certs", "/etc/passwd",
                     "/etc/group", "/dev/random", "/dev/urandom"):
            allow(path, READ)
        allow("/dev/null", (1 << 1) | (1 << 2))
        allow(base, READ | WRITE)
        for path in read_paths:
            allow(path, READ)
        # The trusted worker assigns unique workspace preview ports. A child
        # cannot rebind another tenant's port, even during listener replacement.
        class PortRule(ctypes.Structure):
            _fields_ = [("allowed_access", ctypes.c_uint64), ("port", ctypes.c_uint64)]
        raw = os.environ.get("V3_ALLOWED_LISTEN_PORTS", "")
        ports = raw.split(",") if raw else []
        if len(ports) > 4 or any(not p.isdecimal() or not 20000 <= int(p) <= 29999 for p in ports):
            raise ValueError("invalid trusted workspace listen ports")
        # Ephemeral listeners used by test fixtures stay outside the reserved
        # preview range; Linux intersects per-socket ranges with this ceiling.
        lower=int(Path("/proc/sys/net/ipv4/ip_local_port_range").read_text().split()[0])
        if lower<=29999: raise RuntimeError("ephemeral ports overlap workspace previews")
        ephemeral=PortRule(1,0)
        call(445,ruleset,2,ctypes.byref(ephemeral),0)
        for port in ports:
            rule = PortRule(1, int(port))
            call(445, ruleset, 2, ctypes.byref(rule), 0)

        if libc.prctl(38, 1, 0, 0, 0) != 0:
            raise OSError(ctypes.get_errno(), "cannot set no_new_privs")
        call(446, ruleset, 0)
    finally:
        os.close(ruleset)

"""Bounded File Manager operations on the live development checkout.

Rust admits only an authenticated administrator. This worker owns path
confinement and performs filesystem effects without following symlinks.
"""
import base64
import hashlib
import os
import stat
import uuid
from pathlib import Path

ROOT = Path("/live-checkout")
VIRTUAL = "/live-checkout"
MAX_FILE = 1024 * 1024
MAX_ENTRIES = 2000
HIDDEN = {".git", "data", "target", "workspaces", "rollback", "runtime",
          "gate-runtime", "host-build-service", "deployment-secrets", ".cache"}


class Rejected(ValueError):
    pass


def parts(raw, directory=False):
    if not isinstance(raw, str) or len(raw.encode()) > 1024 or "\x00" in raw or "\\" in raw:
        raise Rejected("Invalid File Manager path")
    if raw == VIRTUAL:
        raw = ""
    elif raw.startswith(VIRTUAL + "/"):
        raw = raw[len(VIRTUAL) + 1:]
    if not raw:
        if directory:
            return []
        raise Rejected("A file path is required")
    if raw.startswith("/") or any(item in ("", ".", "..") for item in raw.split("/")):
        raise Rejected("Path must stay inside the development checkout")
    items = raw.split("/")
    if any(item in HIDDEN or item.startswith(".v3-") for item in items):
        raise Rejected("Protected checkout path")
    if items[0] == "v3" and len(items) > 1 and items[1] in HIDDEN:
        raise Rejected("Protected V3 runtime path")
    return items


def display(items):
    return VIRTUAL + ("/" + "/".join(items) if items else "")


def root_fd():
    if not ROOT.is_dir() or ROOT.is_symlink():
        raise Rejected("Development checkout unavailable")
    return os.open(ROOT, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW | os.O_CLOEXEC)


def directory_fd(items):
    fd = root_fd()
    try:
        for item in items:
            next_fd = os.open(item, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW | os.O_CLOEXEC,
                              dir_fd=fd)
            os.close(fd)
            fd = next_fd
        return fd
    except BaseException:
        os.close(fd)
        raise


def parent_fd(raw):
    items = parts(raw)
    return directory_fd(items[:-1]), items[-1]


def regular_bytes(fd):
    before = os.fstat(fd)
    if not stat.S_ISREG(before.st_mode) or before.st_size > MAX_FILE:
        raise Rejected("Only bounded regular files are available")
    data = bytearray()
    while chunk := os.read(fd, 65536):
        data.extend(chunk)
        if len(data) > MAX_FILE:
            raise Rejected("File exceeds the one MiB editor limit")
    after = os.fstat(fd)
    if (before.st_dev, before.st_ino, before.st_size, before.st_mtime_ns) != (
            after.st_dev, after.st_ino, after.st_size, after.st_mtime_ns):
        raise Rejected("File changed during read")
    return bytes(data), before


def read(raw):
    parent, name = parent_fd(raw)
    try:
        fd = os.open(name, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK | os.O_CLOEXEC,
                     dir_fd=parent)
        try:
            content, _ = regular_bytes(fd)
        finally:
            os.close(fd)
    finally:
        os.close(parent)
    try:
        value = content.decode("utf-8")
        binary = False
        encoding = "utf-8"
    except UnicodeDecodeError:
        value = base64.b64encode(content).decode()
        binary = True
        encoding = "base64"
    return {"path": display(parts(raw)), "content": value, "encoding": encoding, "binary": binary,
            "size": len(content), "truncated": False,
            "sha256": hashlib.sha256(content).hexdigest()}


def tree(raw, show_hidden=False):
    if type(show_hidden) is not bool:
        raise Rejected("Invalid hidden-file setting")
    items = parts(raw, directory=True)
    fd = directory_fd(items)
    entries = []
    try:
        with os.scandir(fd) as listing:
            for entry in listing:
                if len(entries) >= MAX_ENTRIES:
                    raise Rejected("Directory exceeds File Manager limit")
                name = entry.name
                if name in HIDDEN or name.startswith(".v3-") or (
                        items == ["v3"] and name in HIDDEN):
                    continue
                if not show_hidden and name.startswith(".") and name not in (".env.example", ".gitignore", ".github"):
                    continue
                info = entry.stat(follow_symlinks=False)
                if stat.S_ISLNK(info.st_mode) or not (stat.S_ISREG(info.st_mode) or stat.S_ISDIR(info.st_mode)):
                    continue
                path = display([*items, name])
                entries.append({"name": name, "path": path, "is_dir": stat.S_ISDIR(info.st_mode),
                                "size": 0 if stat.S_ISDIR(info.st_mode) else info.st_size})
    finally:
        os.close(fd)
    entries.sort(key=lambda value: (not value["is_dir"], value["name"].lower(), value["name"]))
    return {"path": display(items), "parent": display(items[:-1]) if items else None,
            "project_root": VIRTUAL, "entries": entries}


def write(raw, content, encoding, expected):
    if encoding not in ("utf-8", "base64") or not isinstance(content, str):
        raise Rejected("Invalid file content")
    try:
        data = content.encode("utf-8") if encoding == "utf-8" else base64.b64decode(content, validate=True)
    except (UnicodeError, ValueError) as error:
        raise Rejected("Invalid file encoding") from error
    if len(data) > MAX_FILE:
        raise Rejected("File exceeds the one MiB editor limit")
    if not isinstance(expected, str) or (expected and (
            len(expected) != 64 or any(c not in "0123456789abcdef" for c in expected))):
        raise Rejected("Expected file digest required")
    parent, name = parent_fd(raw)
    temp = ".v3-file-" + uuid.uuid4().hex
    try:
        try:
            old_fd = os.open(name, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK | os.O_CLOEXEC,
                             dir_fd=parent)
        except FileNotFoundError:
            old_fd = None
        if old_fd is None:
            if expected:
                raise Rejected("File disappeared; reload before saving")
            mode = 0o660
            old_stat = None
        else:
            try:
                old, old_stat = regular_bytes(old_fd)
            finally:
                os.close(old_fd)
            if hashlib.sha256(old).hexdigest() != expected:
                raise Rejected("File changed; reload before saving")
            mode = stat.S_IMODE(old_stat.st_mode)
        fd = os.open(temp, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW | os.O_CLOEXEC,
                     0o600, dir_fd=parent)
        try:
            with os.fdopen(fd, "wb") as output:
                output.write(data)
                output.flush()
                os.fchmod(output.fileno(), mode)
                os.fsync(output.fileno())
            if old_stat is None:
                os.link(temp, name, src_dir_fd=parent, dst_dir_fd=parent, follow_symlinks=False)
            else:
                current = os.stat(name, dir_fd=parent, follow_symlinks=False)
                if (current.st_dev, current.st_ino, current.st_size, current.st_mtime_ns) != (
                        old_stat.st_dev, old_stat.st_ino, old_stat.st_size, old_stat.st_mtime_ns):
                    raise Rejected("File changed during save")
                os.replace(temp, name, src_dir_fd=parent, dst_dir_fd=parent)
            os.fsync(parent)
        finally:
            try:
                os.unlink(temp, dir_fd=parent)
            except FileNotFoundError:
                pass
    finally:
        os.close(parent)
    return {"path": display(parts(raw)), "size": len(data), "status": "ok",
            "sha256": hashlib.sha256(data).hexdigest()}


def create(raw, kind):
    if kind not in ("file", "dir"):
        raise Rejected("Invalid entry kind")
    parent, name = parent_fd(raw)
    try:
        if kind == "dir":
            os.mkdir(name, 0o770, dir_fd=parent)
        else:
            fd = os.open(name, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW | os.O_CLOEXEC,
                         0o660, dir_fd=parent)
            os.close(fd)
        os.fsync(parent)
    finally:
        os.close(parent)
    return {"path": display(parts(raw)), "kind": kind, "status": "ok"}


def rename(raw, new_raw):
    source, old_name = parent_fd(raw)
    target, new_name = parent_fd(new_raw)
    try:
        if stat.S_ISLNK(os.stat(old_name, dir_fd=source, follow_symlinks=False).st_mode):
            raise Rejected("Symlink entries unavailable")

        # Linux renameat2 with RENAME_NOREPLACE avoids overwriting a raced destination.
        import ctypes
        libc = ctypes.CDLL(None, use_errno=True)
        result = libc.renameat2(source, old_name.encode(), target, new_name.encode(), 1)
        if result:
            raise OSError(ctypes.get_errno(), "Rename failed")
        os.fsync(source)
        if target != source:
            os.fsync(target)
    finally:
        os.close(source)
        os.close(target)
    return {"from": display(parts(raw)), "to": display(parts(new_raw)), "status": "ok"}


def _preflight_delete(parent, name, budget):
    info = os.stat(name, dir_fd=parent, follow_symlinks=False)
    if stat.S_ISREG(info.st_mode):
        return
    if not stat.S_ISDIR(info.st_mode):
        raise Rejected("Only ordinary files and directories can be deleted")
    fd = os.open(name, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW | os.O_CLOEXEC, dir_fd=parent)
    try:
        with os.scandir(fd) as listing:
            children = [entry.name for entry in listing]
        for child in children:
            budget[0] -= 1
            if budget[0] < 0 or child in HIDDEN or child.startswith(".v3-"):
                raise Rejected("Directory contains protected or too many entries")
            _preflight_delete(fd, child, budget)
    finally:
        os.close(fd)


def _delete_at(parent, name, budget):
    info = os.stat(name, dir_fd=parent, follow_symlinks=False)
    if stat.S_ISLNK(info.st_mode):
        raise Rejected("Symlink entries unavailable")
    if stat.S_ISREG(info.st_mode):
        os.unlink(name, dir_fd=parent)
        return
    if not stat.S_ISDIR(info.st_mode):
        raise Rejected("Only ordinary files and directories can be deleted")
    fd = os.open(name, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW | os.O_CLOEXEC, dir_fd=parent)
    try:
        with os.scandir(fd) as listing:
            children = [entry.name for entry in listing]
        for child in children:
            budget[0] -= 1
            if budget[0] < 0 or child in HIDDEN or child.startswith(".v3-"):
                raise Rejected("Directory contains protected or too many entries")
            _delete_at(fd, child, budget)
    finally:
        os.close(fd)
    os.rmdir(name, dir_fd=parent)


def delete(raw):
    parent, name = parent_fd(raw)
    try:
        _preflight_delete(parent, name, [MAX_ENTRIES])
        _delete_at(parent, name, [MAX_ENTRIES])
        os.fsync(parent)
    finally:
        os.close(parent)
    return {"path": display(parts(raw)), "status": "deleted"}


def execute(args):
    if not isinstance(args, dict):
        return {"status": "error", "output": "Invalid File Manager request"}
    action = args.get("action")
    known = {"tree": {"action", "path", "show_hidden"},
             "read": {"action", "path"}, "write": {"action", "path", "content", "encoding", "expected_sha256"},
             "create": {"action", "path", "kind"}, "rename": {"action", "path", "new_path"},
             "delete": {"action", "path"}}
    if action not in known or set(args) - known[action]:
        return {"status": "error", "output": "Unsupported File Manager action"}
    try:
        value = {"tree": lambda: tree(args.get("path", ""), args.get("show_hidden", False)),
                 "read": lambda: read(args["path"]),
                 "write": lambda: write(args["path"], args["content"], args["encoding"], args["expected_sha256"]),
                 "create": lambda: create(args["path"], args["kind"]),
                 "rename": lambda: rename(args["path"], args["new_path"]),
                 "delete": lambda: delete(args["path"])}[action]()
        return {"status": "success", "output": value}
    except (Rejected, OSError, KeyError, TypeError) as error:
        return {"status": "error", "output": str(error) if isinstance(error, Rejected) else "File Manager operation failed"}

async def git_action(args, trusted, git_plugin):
    """Project the live checkout's typed Git operations into File Manager data."""
    adapter = git_plugin.adapter
    if not isinstance(args, dict) or args.get("action") not in {
            "git_status", "git_graph", "git_commit_detail", "git_branches",
            "git_tree_status", "git_line_stats", "git_ignore", "git_tool", "git_commit", "git_settings"}:
        return {"status": "error", "output": "Unsupported File Manager Git action"}
    action = args["action"]
    repo_name = args.get("repo", "__root__")
    if not isinstance(repo_name, str) or repo_name != "__root__" and (
            repo_name.startswith("/") or ".." in repo_name.split("/") or "\\" in repo_name):
        return {"status": "error", "output": "Invalid repository selection"}
    if repo_name != "__root__":
        try:
            parts(repo_name)
        except Rejected:
            return {"status": "error", "output": "Protected repository selection"}
    lease = adapter._SANDBOX_ROOT.set(ROOT.resolve())
    try:
        if action == "git_tool":
            values = {key: value for key, value in args.items() if key != "action"}
            values["repo"] = repo_name
            return await git_plugin.execute("git_tool", values, ROOT, trusted)
        repo = adapter.repository(ROOT, repo_name)
        await adapter.validate(repo)
        if action == "git_status":
            return {"status": "success", "output": await _git_status(adapter, repo, repo_name)}
        if action == "git_tree_status":
            return {"status": "success", "output": await _git_tree_status(adapter, repo)}
        if action == "git_line_stats":
            return {"status": "success", "output": await _git_line_stats(adapter, repo)}
        if action == "git_ignore":
            return {"status": "success", "output": await _git_ignore(adapter, repo, repo_name, args)}
        if action == "git_graph":
            limit = args.get("limit", 80)
            if type(limit) is not int or not 1 <= limit <= 100:
                raise Rejected("Invalid graph limit")
            return {"status": "success", "output": await _git_graph(adapter, repo, limit)}
        if action == "git_commit_detail":
            return {"status": "success", "output": await _git_detail(adapter, repo, args.get("hash"))}
        if action == "git_branches":
            return {"status": "success", "output": await _git_branches(adapter, repo)}
        if action == "git_settings":
            return {"status": "success", "output": await _git_settings(adapter, git_plugin, repo, args, trusted)}
        return {"status": "success", "output": await _git_commit(adapter, repo, args)}
    except adapter.Uncertain as error:
        return {"status": "error", "uncertain": True, "error_kind": "git_effect_uncertain",
                "output": str(error)}
    except (adapter.Rejected, Rejected, ValueError) as error:
        return {"status": "error", "error_kind": "git_rejected", "output": str(error)}
    except Exception:
        return {"status": "error", "error_kind": "git_rejected", "output": "Git Control operation failed"}
    finally:
        adapter._SANDBOX_ROOT.reset(lease)


async def _git_text(adapter, repo, arguments):
    return (await adapter.command(repo, arguments)).decode("utf-8", errors="replace").strip()


async def _git_optional(adapter, repo, arguments):
    try:
        return await _git_text(adapter, repo, arguments)
    except adapter.Rejected:
        return ""


GIT_EXCLUDES = [":(exclude)data", ":(exclude)v3/data", ":(exclude)v3/target",
                ":(exclude)v3/workspaces", ":(exclude)v3/runtime",
                ":(exclude)v3/gate-runtime", ":(exclude)v3/host-build-service",
                ":(exclude)v3/rollback"]


async def _git_live_snapshot(adapter, repo, repo_name):
    import json
    head = await adapter.head(repo)
    pathspec = ["--", ".", *GIT_EXCLUDES] if repo_name == "__root__" else []
    status = (await adapter.command(repo, ["status", "--porcelain=v1",
                                           "--untracked-files=all", *pathspec])).decode().rstrip("\n")
    raw = await adapter.command(repo, ["diff", "--raw", "HEAD", *pathspec])
    untracked = []
    for line in status.splitlines():
        if not line.startswith("?? "):
            continue
        name = line[3:]
        try:
            parts(name)
            data, _ = adapter.read_source(repo, name)
            untracked.append([name, hashlib.sha256(data).hexdigest()])
        except (Rejected, adapter.Rejected):
            untracked.append([name, "unavailable"])
    review = hashlib.sha256(json.dumps([head, status, raw.hex(), untracked],
                                          sort_keys=True).encode()).hexdigest()
    return {"head": head, "status": status, "review_sha256": review}


async def _git_status(adapter, repo, repo_name):
    snap = await _git_live_snapshot(adapter, repo, repo_name)
    branch = await _git_text(adapter, repo, ["symbolic-ref", "--quiet", "--short", "HEAD"])
    remote = await _git_optional(adapter, repo, ["remote", "get-url", "origin"])
    counts = await _git_optional(adapter, repo, ["rev-list", "--left-right", "--count",
                                                 "HEAD...refs/remotes/origin/" + branch])
    ahead, behind = (map(int, counts.split()) if len(counts.split()) == 2 else (0, 0))
    staged, unstaged, untracked = [], [], []
    for line in snap["status"].splitlines():
        if len(line) < 4:
            continue
        name = line[3:]
        try:
            parts(name)
        except Rejected:
            continue
        x, y = line[0], line[1]
        if x == y == "?":
            untracked.append({"path": name, "flag": "??"})
        else:
            if x != " ":
                staged.append({"path": name, "flag": x})
            if y != " ":
                unstaged.append({"path": name, "flag": y})
    return {"branch": branch, "remote_url": remote, "has_remote": bool(remote),
            "ahead": ahead, "behind": behind,
            "file_count": len(staged) + len(unstaged) + len(untracked),
            "staged": staged, "unstaged": unstaged, "untracked": untracked,
            "head": snap["head"], "review_sha256": snap["review_sha256"]}


async def _git_tree_status(adapter, repo):
    pathspec = ["--", ".", *GIT_EXCLUDES]
    tracked = await adapter.command(repo, ["ls-files", "--cached", "-z", *pathspec])
    ignored = await adapter.command(repo, ["ls-files", "--others", "--ignored",
                                           "--exclude-standard", "--directory", "-z", *pathspec])
    def available(raw):
        values = []
        for item in raw.split(b"\x00"):
            if not item:
                continue
            name = item.decode("utf-8", errors="replace").rstrip("/")
            try:
                parts(name)
            except Rejected:
                continue
            values.append(name)
            if len(values) > 10000:
                raise Rejected("Git tracking view exceeds limit")
        return values
    return {"repo_root": VIRTUAL, "is_repo": True, "head": await adapter.head(repo),
            "tracked": available(tracked), "ignored": available(ignored)}


async def _git_line_stats(adapter, repo):
    pathspec = ["--", ".", *GIT_EXCLUDES]
    raw = await adapter.command(repo, ["diff", "--numstat", "--no-renames", "-z",
                                        "HEAD", *pathspec])
    stats = {}
    for row in raw.split(b"\x00"):
        columns = row.decode("utf-8", errors="replace").split("\t", 2)
        if len(columns) != 3 or not columns[0].isdigit() or not columns[1].isdigit():
            continue
        name = columns[2]
        try:
            parts(name)
        except Rejected:
            continue
        stats[name] = {"added": int(columns[0]), "removed": int(columns[1])}
        if len(stats) > MAX_ENTRIES:
            raise Rejected("Git line statistics exceed limit")
    untracked = await adapter.command(repo, ["ls-files", "--others", "--exclude-standard",
                                             "-z", *pathspec])
    for item in untracked.split(b"\x00"):
        if not item:
            continue
        name = item.decode("utf-8", errors="replace")
        try:
            parts(name)
            data, _ = adapter.read_source(repo, name)
        except (Rejected, adapter.Rejected):
            continue
        if b"\x00" not in data:
            stats[name] = {"added": len(data.splitlines()), "removed": 0}
        if len(stats) > MAX_ENTRIES:
            raise Rejected("Git line statistics exceed limit")
    return {"project_root": VIRTUAL, "stats": stats}


async def _git_ignore(adapter, repo, repo_name, args):
    import re
    if repo_name != "__root__" or set(args) != {"action", "repo", "path", "tracked", "expected_head"}:
        raise Rejected("Git tracking changes require the checkout root and current HEAD")
    name = args["path"]
    tracked = args["tracked"]
    if type(tracked) is not bool or not isinstance(name, str):
        raise Rejected("Invalid Git tracking request")
    parts(name)
    if name == ".gitignore" or not all(re.fullmatch(r"[A-Za-z0-9._-]+", item)
                                        for item in name.split("/")):
        raise Rejected("Git tracking toggle requires a simple file path")
    await adapter.fence(repo, args["expected_head"])
    parent, leaf = parent_fd(name)
    try:
        info = os.stat(leaf, dir_fd=parent, follow_symlinks=False)
        if not (stat.S_ISREG(info.st_mode) or stat.S_ISDIR(info.st_mode)):
            raise Rejected("Only regular files and directories can change tracking")
        is_dir = stat.S_ISDIR(info.st_mode)
    finally:
        os.close(parent)
    pattern = "/" + name + ("/" if is_dir else "")
    try:
        old = read(".gitignore")
        if old["binary"]:
            raise Rejected("Git ignore file is not UTF-8 text")
        current, digest = old["content"], old["sha256"]
    except FileNotFoundError:
        current, digest = "", ""
    if tracked:
        updated = "".join(line for line in current.splitlines(keepends=True)
                          if line.rstrip("\r\n") != pattern)
    elif pattern not in [line.rstrip("\r\n") for line in current.splitlines()]:
        updated = current + ("" if not current or current.endswith("\n") else "\n") + pattern + "\n"
    else:
        updated = current
    pathspec = ["--", name]
    if tracked:
        ordinary = await adapter.command(repo, ["ls-files", "--others", "--exclude-standard", "-z", *pathspec])
        ignored = await adapter.command(repo, ["ls-files", "--others", "--ignored", "--exclude-standard",
                                               "-z", *pathspec])
        selected = set(ordinary.split(b"\x00") + ignored.split(b"\x00"))
    else:
        selected = set((await adapter.command(repo, ["ls-files", "--cached", "-z", *pathspec])).split(b"\x00"))
    safe = []
    for item in sorted(selected):
        if not item:
            continue
        rel = item.decode("utf-8", errors="replace")
        if rel != name and not (is_dir and rel.startswith(name + "/")):
            continue
        try:
            parts(rel)
            parent, leaf = parent_fd(rel)
            try:
                info = os.stat(leaf, dir_fd=parent, follow_symlinks=False)
            finally:
                os.close(parent)
            if not stat.S_ISREG(info.st_mode):
                continue
        except (Rejected, OSError):
            continue
        safe.append(rel)
        if len(safe) > MAX_ENTRIES:
            raise Rejected("Git tracking selection exceeds limit")
    try:
        if updated != current:
            write(".gitignore", updated, "utf-8", digest)
        for offset in range(0, len(safe), 64):
            batch = safe[offset:offset + 64]
            argv = (["add", "-f", "--", *batch] if tracked else
                    ["rm", "--cached", "--ignore-unmatch", "--", *batch])
            await adapter.command(repo, argv, mutable=True)
        return await _git_tree_status(adapter, repo)
    except (adapter.Rejected, Rejected, OSError) as error:
        raise adapter.Uncertain("Git tracking may have changed; refresh before retry") from error


async def _git_graph(adapter, repo, limit):
    raw = await _git_text(adapter, repo, ["log", "--all", "--date-order",
        "--format=%H%x1f%P%x1f%D%x1f%an%x1f%ar%x1f%aI%x1f%s", "-" + str(limit)])
    head = await adapter.head(repo)
    branch = await _git_text(adapter, repo, ["symbolic-ref", "--quiet", "--short", "HEAD"])
    pulled = set((await _git_optional(adapter, repo, ["rev-list", "-300", "HEAD"])).splitlines())
    pullable = set((await _git_optional(adapter, repo, ["rev-list", "-300",
        "refs/remotes/origin/" + branch, "^HEAD"])).splitlines())
    unpushed = set((await _git_optional(adapter, repo, ["rev-list", "-300", "HEAD",
        "^refs/remotes/origin/" + branch])).splitlines())
    lanes = []
    result = []
    maximum = 0
    def index(value):
        try:
            return lanes.index(value)
        except ValueError:
            return -1
    def empty():
        try:
            return lanes.index(None)
        except ValueError:
            lanes.append(None)
            return len(lanes) - 1
    for line in raw.splitlines():
        fields = line.split("\x1f")
        if len(fields) != 7:
            continue
        full, parents_text, refs, author, relative, iso, subject = fields
        if not full or len(full) not in (40, 64):
            continue
        parents = [value for value in parents_text.split() if value]
        incoming = list(lanes)
        lane = index(full)
        if lane < 0:
            lane = empty()
        for pos, value in enumerate(lanes):
            if value == full:
                lanes[pos] = None
        parent_lanes = []
        for pos, parent in enumerate(parents):
            existing = index(parent)
            if pos == 0 and lanes[lane] is None and (existing < 0 or existing > lane):
                lanes[lane] = parent
                parent_lanes.append(lane)
            elif existing >= 0:
                parent_lanes.append(existing)
            else:
                slot = empty()
                lanes[slot] = parent
                parent_lanes.append(slot)
        outgoing = list(lanes)
        maximum = max(maximum, lane + 1, len(incoming), len(outgoing))
        result.append({"hash": full[:7], "full_hash": full, "parents": [p[:7] for p in parents],
            "refs": refs, "author": author, "date_relative": relative, "date_iso": iso,
            "message": subject, "lane": lane, "parent_lanes": parent_lanes,
            "lanes_in": [p[:7] if p else None for p in incoming],
            "lanes_out": [p[:7] if p else None for p in outgoing],
            "merge_in_lanes": [i for i, p in enumerate(incoming) if p == full and i != lane],
            "is_head": full == head, "is_pulled": full in pulled,
            "is_pullable": full in pullable, "is_unpushed": full in unpushed})
    raw_refs = await _git_optional(adapter, repo, ["for-each-ref",
        "--format=%(refname:short)%x1f%(objectname)", "refs/heads", "refs/remotes"])
    branches = {}
    for line in raw_refs.splitlines():
        fields = line.split("\x1f")
        if len(fields) == 2:
            branches[fields[0]] = fields[1][:7]
    return {"commits": result, "max_lane": maximum, "branches": branches,
            "current_branch": branch, "head_hash": head}


async def _git_detail(adapter, repo, hash_value):
    import re
    if not isinstance(hash_value, str) or not re.fullmatch("[0-9a-f]{40,64}", hash_value):
        raise Rejected("Full commit hash required")
    raw = (await adapter.command(repo, ["show", "-s", "--no-show-signature",
        "--format=%H%x1f%h%x1f%an%x1f%ae%x1f%aI%x1f%cn%x1f%ce%x1f%cI%x1f%P%x1f%s%x1f%b",
        hash_value])).decode("utf-8", errors="replace").rstrip("\n")
    fields = raw.split("\x1f", 10)
    if len(fields) != 11:
        raise Rejected("Commit detail unavailable")
    names = ["full_hash", "hash", "author", "author_email", "author_date",
             "committer", "committer_email", "commit_date", "parents", "subject", "body"]
    detail = dict(zip(names, fields))
    detail["parents"] = fields[8].split()
    raw_files = await _git_text(adapter, repo, ["show", "--numstat", "--format=",
                                                "--no-ext-diff", "--no-textconv", hash_value])
    files = []
    for line in raw_files.splitlines():
        columns = line.split("\t", 2)
        if len(columns) == 3:
            added, removed, name = columns
            files.append({"path": name, "added": int(added) if added.isdigit() else 0,
                          "removed": int(removed) if removed.isdigit() else 0,
                          "binary": added == "-" and removed == "-"})
    detail["files"] = files
    return detail


async def _git_branches(adapter, repo):
    async def listing(prefix):
        raw = await _git_optional(adapter, repo, ["for-each-ref",
            "--format=%(refname:short)%x1f%(objectname)", prefix])
        return dict(line.split("\x1f", 1) for line in raw.splitlines() if "\x1f" in line)
    local = await listing("refs/heads")
    remote_raw = await listing("refs/remotes/origin")
    remote = {name.removeprefix("origin/"): value for name, value in remote_raw.items()
              if name != "origin/HEAD"}
    current = await _git_text(adapter, repo, ["symbolic-ref", "--quiet", "--short", "HEAD"])
    branches = [{"name": name, "hash": (local.get(name) or remote.get(name) or "")[:7],
                 "local": name in local, "remote": name in remote}
                for name in sorted(set(local) | set(remote))]
    return {"current": current, "branches": branches}


async def _git_commit(adapter, repo, args):
    expected_head = args.get("expected_head")
    expected_review = args.get("expected_review_sha256")
    paths = adapter.paths(repo, args.get("paths"))
    message = args.get("message")
    if not isinstance(message, str) or not message.strip() or len(message.encode()) > 2000:
        raise Rejected("Commit message required")
    if adapter.secret(message.encode()):
        raise Rejected("Commit message contains possible credentials")
    await adapter.fence(repo, expected_head)
    if (await _git_live_snapshot(adapter, repo, args.get("repo", "__root__")))["review_sha256"] != expected_review:
        raise Rejected("Review changed; refresh before committing")
    for name in paths:
        parts(name)
        content, _ = adapter.read_source(repo, name, True)
        if content is not None and adapter.secret(content):
            raise Rejected("Source contains possible credentials")
    staged = (await adapter.command(repo, ["diff", "--cached", "--name-only", "-z"])).split(b"\x00")
    if any(name.decode() not in paths for name in staged if name):
        raise Rejected("Unselected staged paths present")
    author = await _git_optional(adapter, repo, ["config", "--local", "user.name"]) or "WebAgent v3"
    email = await _git_optional(adapter, repo, ["config", "--local", "user.email"]) or "webagent-v3@localhost"
    await adapter.command(repo, ["add", "--", *paths], mutable=True)
    await adapter.command(repo, ["-c", "user.name=" + author,
        "-c", "user.email=" + email, "commit", "--no-verify",
        "-m", message, "--", *paths], mutable=True)
    return {"status": "committed", "title": message.splitlines()[0],
            "hash": (await adapter.head(repo))[:7], "push": {"attempted": False}}

async def _git_settings(adapter, git_plugin, repo, args, trusted):
    allowed = {"action", "repo", "expected_head", "remote_url",
               "git_user_name", "git_user_email"}
    if set(args) - allowed:
        raise Rejected("Invalid repository settings")
    await adapter.fence(repo, args.get("expected_head"))
    requested = args.get("remote_url")
    if requested is not None:
        if not isinstance(requested, str):
            raise Rejected("Invalid remote URL")
        old = await _git_optional(adapter, repo, ["remote", "get-url", "origin"])
        if requested:
            value = git_plugin.normalized(requested)
            granted = {record["url"] for record in git_plugin.connections(trusted)}
            if value not in granted:
                raise Rejected("Remote URL is not granted by V3 repository connections")
            if old:
                if old != value:
                    await adapter.command(repo, ["remote", "set-url", "origin", value], mutable=True)
            else:
                await adapter.command(repo, ["remote", "add", "origin", value], mutable=True)
        elif old:
            await adapter.command(repo, ["remote", "remove", "origin"], mutable=True)
    for key, config in (("git_user_name", "user.name"), ("git_user_email", "user.email")):
        value = args.get(key)
        if value is None or value == "":
            continue
        if not isinstance(value, str) or len(value.encode()) > 128 or any(
                ord(char) < 32 or ord(char) == 127 for char in value):
            raise Rejected("Invalid Git author identity")
        await adapter.command(repo, ["config", "--local", config, value], mutable=True)
    return {"status": "ok", "message": "Repository settings saved"}

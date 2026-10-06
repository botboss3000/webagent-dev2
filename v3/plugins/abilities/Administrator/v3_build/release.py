"""Frozen task contexts and versioned development release mounts."""
import io
import os
from pathlib import Path
import stat
import tarfile
import uuid


def entries(blob, validate):
    validate(blob)
    result = {}
    with tarfile.open(fileobj=io.BytesIO(blob), mode="r:") as archive:
        for member in archive:
            if member.isfile():
                result[member.name.removeprefix("./")] = (
                    member.mode & 0o777, archive.extractfile(member).read())
    return result


def archive(files, validate):
    output = io.BytesIO()
    with tarfile.open(fileobj=output, mode="w") as stream:
        for name, (mode, body) in sorted(files.items()):
            info = tarfile.TarInfo(name)
            info.size, info.mode = len(body), mode
            stream.addfile(info, io.BytesIO(body))
    blob = output.getvalue()
    validate(blob)
    return blob


def extract(root, blob, validate):
    if root.exists():
        raise ValueError("release snapshot already exists")
    root.mkdir(parents=True, mode=0o700)
    for name, (mode, body) in entries(blob, validate).items():
        path = root / name
        path.parent.mkdir(parents=True, exist_ok=True)
        with path.open("xb") as output:
            output.write(body)
        path.chmod(mode)


def snapshot(root, allowlist, validate):
    files = {}
    for allowed in allowlist:
        path = root / allowed
        if not path.exists():
            continue
        paths = [path] if path.is_file() else path.rglob("*")
        for entry in paths:
            if entry.is_symlink():
                raise ValueError("source workspace symlink forbidden")
            if entry.is_dir():
                continue
            if not entry.is_file():
                raise ValueError("source workspace special file forbidden")
            name = entry.relative_to(root).as_posix()
            if "__pycache__" in entry.parts or name.endswith((".pyc", ".pyo")):
                continue
            info = entry.stat()
            if info.st_size > 16 * 1024 * 1024 or len(files) > 20000:
                raise ValueError("source workspace input bound exceeded")
            files[name] = (info.st_mode & 0o777, entry.read_bytes())
    return archive(files, validate)


def candidate(base_blob, task_blob, live_blob, validate):
    base, task, live = (entries(value, validate) for value in
                        (base_blob, task_blob, live_blob))
    changed = [name for name in sorted(set(base) | set(task))
               if base.get(name) != task.get(name)]
    if not changed or len(changed) > 256 or any(
            not name.startswith("v3/") or name.startswith("v3/data/")
            for name in changed):
        raise ValueError("isolated task has no bounded V3 source changes")
    merged = dict(live)
    for name in changed:
        if live.get(name) not in (base.get(name), task.get(name)):
            raise ValueError("source conflict: " + name)
        if name in task:
            merged[name] = task[name]
        else:
            merged.pop(name, None)
    return archive(merged, validate), changed


def release_mounts(previous, root, asset_root=None):
    """Pin code mounts to a frozen version, preserving state and runtime mounts."""
    mapping = {
        "/live-v3": "v3",
        "/opt/v3/config": "v3/config",
        "/opt/v3/ui": "v3/ui",
        "/opt/v3/plugins/abilities": "v3/plugins/abilities",
        "/opt/v3/shared-ui/shared/css": "ui/shared/css",
        "/opt/v3/shared-ui/main-panel/agents": "ui/main-panel/agents",
        "/opt/v3/shared-ui/vendor/fonts": "ui/vendor/fonts",
        "/opt/v3/shared-ui/vendor/lucide": "ui/vendor/lucide",
        "/opt/v3/shared-ui/favicon.svg": "ui/favicon.svg",
    }
    host = dict(previous)
    mounts = []
    seen = set()
    api = any(item.get("Target") == "/live-checkout"
              for item in host.get("Mounts") or [])
    legacy_assets = {"/opt/v3/ui", "/opt/v3/shared-ui/shared/css",
                     "/opt/v3/shared-ui/main-panel/agents",
                     "/opt/v3/shared-ui/vendor/fonts",
                     "/opt/v3/shared-ui/vendor/lucide",
                     "/opt/v3/shared-ui/favicon.svg"}
    for mount in host.get("Mounts") or []:
        mount = dict(mount)
        target = mount.get("Target")
        if api and asset_root is not None and target in legacy_assets:
            continue
        if api and asset_root is not None and target == "/opt/v3/assets":
            mount["Source"] = str(asset_root)
            mount["ReadOnly"] = True
            seen.add(target)
        elif target in mapping:
            source = root / mapping[target]
            if not source.exists() or source.is_symlink():
                raise ValueError("release mount input unavailable: " + str(target))
            mount["Source"] = str(source)
            mount["ReadOnly"] = True
            seen.add(target)
        mounts.append(mount)
    if api and asset_root is not None and "/opt/v3/assets" not in seen:
        if not asset_root.is_dir() or asset_root.is_symlink():
            raise ValueError("stable asset mount unavailable")
        mounts.append({"Type": "bind", "Source": str(asset_root),
                       "Target": "/opt/v3/assets", "ReadOnly": True})
        seen.add("/opt/v3/assets")
    required = ({"/live-v3", "/opt/v3/assets" if asset_root is not None else "/opt/v3/ui"} if api
                else {"/opt/v3/plugins/abilities"})
    if not required <= seen:
        raise ValueError("required release mounts absent")
    host["Mounts"] = mounts
    return host

def promote_checkout(root, before_blob, after_blob, changed, validate):
    """Copy a healthy release's changed files into the shared source checkout."""
    before, after = entries(before_blob, validate), entries(after_blob, validate)
    root = Path(root).resolve()
    actions = []
    for name in changed:
        if not name.startswith("v3/") or name.startswith("v3/data/"):
            raise ValueError("checkout promotion path outside V3 source")
        path = root / name
        for parent in (path, *path.parents):
            if parent == root:
                break
            if parent.is_symlink():
                raise ValueError("checkout promotion symlink forbidden")
        if path.exists():
            if not path.is_file():
                raise ValueError("checkout promotion special file forbidden")
            info = path.stat()
            current = (info.st_mode & 0o777, path.read_bytes())
        else:
            current = None
        current_body = current[1] if current is not None else None
        before_body = before[name][1] if name in before else None
        after_body = after[name][1] if name in after else None
        if current_body not in (before_body, after_body):
            raise ValueError("checkout changed during release: " + name)
        if current != after.get(name):
            actions.append((path, after.get(name)))
    for path, target in actions:
        if target is None:
            path.unlink()
            continue
        mode, body = target
        path.parent.mkdir(parents=True, exist_ok=True)
        temporary = path.with_name(path.name + ".release-" + uuid.uuid4().hex)
        try:
            with temporary.open("xb") as output:
                output.write(body)
                output.flush()
                os.fsync(output.fileno())
            temporary.chmod(mode)
            os.replace(temporary, path)
        finally:
            temporary.unlink(missing_ok=True)
    return len(actions)


def checkout_matches(root, after_blob, changed, validate):
    """Reconcile a completed deployment without writing the shared checkout."""
    after = entries(after_blob, validate)
    root = Path(root).resolve()
    for name in changed:
        if not name.startswith("v3/") or name.startswith("v3/data/"):
            return False
        path = root / name
        for parent in (path, *path.parents):
            if parent == root:
                break
            if parent.is_symlink():
                return False
        if path.exists():
            if not path.is_file():
                return False
            info = path.stat()
            current = (info.st_mode & 0o777, path.read_bytes())
        else:
            current = None
        if current != after.get(name):
            return False
    return True


def stage_assets(root, job_id, blob, validate):
    """Freeze the tested source, including served UI and workspace templates."""
    uuid.UUID(job_id)
    files = entries(blob, validate)
    if "v3/ui/index.html" not in files:
        raise ValueError("UI release has no application entrypoint")
    if root.is_symlink():
        raise ValueError("asset root symlink forbidden")
    root.mkdir(parents=True, exist_ok=True, mode=0o755)
    root.chmod(0o755)
    revisions = root / "releases"
    if revisions.is_symlink():
        raise ValueError("asset revisions symlink forbidden")
    revisions.mkdir(exist_ok=True, mode=0o755)
    revisions.chmod(0o755)
    destination = revisions / job_id
    if destination.exists():
        existing = entries(snapshot(destination, ("v3", "ui"), lambda _: None),
                           lambda _: None)
        if existing != files:
            raise ValueError("frozen UI asset release changed")
    else:
        extract(destination, archive(files, lambda _: None), lambda _: None)
        destination.chmod(0o755)
    return destination


def current_asset_id(root):
    pointer = root / "current"
    if not pointer.exists() and not pointer.is_symlink():
        return None
    if not pointer.is_symlink():
        raise ValueError("asset pointer is not a symlink")
    target = os.readlink(pointer)
    if not target.startswith("releases/") or "/" in target[len("releases/"):]:
        raise ValueError("asset pointer target invalid")
    job_id = target[len("releases/"):]
    uuid.UUID(job_id)
    if not (root / target).is_dir():
        raise ValueError("asset pointer target unavailable")
    return job_id


def activate_assets(root, job_id):
    """Atomically select a complete immutable UI snapshot."""
    uuid.UUID(job_id)
    target = root / "releases" / job_id
    if not target.is_dir() or target.is_symlink():
        raise ValueError("asset release unavailable")
    previous = current_asset_id(root)
    temporary = root / ("current-" + uuid.uuid4().hex)
    try:
        os.symlink("releases/" + job_id, temporary)
        os.replace(temporary, root / "current")
        directory = os.open(root, os.O_DIRECTORY)
        try:
            os.fsync(directory)
        finally:
            os.close(directory)
    finally:
        temporary.unlink(missing_ok=True)
    return previous

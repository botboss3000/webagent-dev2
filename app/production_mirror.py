"""Production mirror — regenerate a *trimmed* copy of this dev repo into a
separate sibling **production** repo and push it to its own GitHub remote.

Git can't push "everything except folder X": every commit carries the whole
tree. So the production repo is its OWN git repo — separate folder, separate
remote, separate history — that this module *regenerates on demand* from the dev
tree minus the dev-only folders the admin marked in the File Explorer. It owns
two things:

- **The shared config store** (``data/config/production-mirror.json``): the
  production folder path, the production remote URL, the **exclude list** (repo-
  relative folders that never ship), and the last-release record. The production
  **GitHub key** is a secret, so it is NOT kept here — it lives in the encrypted
  vault (``auth_elements``, service ``production_mirror``), with the shared
  Git-page token as a fallback when none is stored (:func:`save_vault_token` /
  :func:`get_vault_token` / :func:`_resolve_token`).
- **The release engine**, in two composable halves so the work can run as one
  click *or* split into two:
  - :func:`copy_events` (the "Sync to production" step) — a one-way sync: list the
    dev project's real files via ``git ls-files`` (so ``.gitignore`` is honoured
    automatically and no runtime junk/secrets leak), drop the excluded folders,
    mirror what's left into the production repo (wiping everything but its ``.git``
    first so *deletions* propagate), secret-scan the staged diff, then **commit
    locally**. :func:`diff_status` previews that same comparison read-only, so the
    UI can show whether dev and production differ before the sync runs.
  - :func:`push_events` (the "Push to GitHub" step) — push that local commit to
    the production remote and record the release.
  - :func:`release_events` (the Git page's one-click "Release") — simply runs
    copy then push back-to-back, so all three paths share one engine.

The Git Control page (``app/api/github.py``) exposes thin admin endpoints over
this module; the File Explorer (``ui/shared/js/files.js``) reads and writes the
exclude list through the same endpoints — so both pages share one source of
truth. Nothing here is wired into the agent core; it is a self-contained
admin-tools extension that reuses the editions trim philosophy.
"""

from __future__ import annotations

import filecmp
import logging
import os
import re
import shutil
import subprocess
from datetime import datetime, timezone
from pathlib import Path

from app.util.config_io import read_json, safe_write_json
from app.git_executor import run_git_job
from app.runtime_mode import data_root

logger = logging.getLogger(__name__)

# ── Project + config locations ──────────────────────────────────────────────
_PROJECT_ROOT = Path(__file__).resolve().parent.parent
_CONFIG_PATH = data_root() / "config" / "production-mirror.json"
# The GitHub token is the same single shared credential the Git page uses.
_TOKEN_FILE = data_root() / "config" / "provider.json"

# Same secret guards the dev commit path uses (mirrored so a stripped tree is
# scanned with identical rules before it is ever pushed).
_SECRET_PATTERNS = [
    (r'ghp_[A-Za-z0-9]{36}', 'GitHub personal access token'),
    (r'sk-or-v1-[A-Za-z0-9]{32,}', 'OpenRouter/OpenAI API key'),
    (r'sk-[A-Za-z0-9]{32,}', 'API key pattern'),
    (r'AKIA[0-9A-Z]{16}', 'AWS access key'),
    (r'-----BEGIN (?:RSA |EC )?PRIVATE KEY-----', 'Private key'),
]


# ── Token: production vault first, shared Git-page token as fallback ─────────
# The address (remote URL) and folder live in production-mirror.json (plaintext,
# gitignored). The GitHub **key** is a secret, so it lives in the encrypted vault
# (``auth_elements.secret_ref`` — per-tenant Fernet when field encryption is on),
# the SAME store the deploy panel uses, keyed by service ``production_mirror``. If
# no production key is stored we fall back to the shared Git-page token from
# ``provider.json`` so existing setups keep working with no key entered.
_VAULT_USER = "admin"
_VAULT_SERVICE = "production_mirror"
_VAULT_LABEL = "default"


def _get_token() -> str:
    """The shared Git-page token — the fallback used when no production-specific key
    is stored in the vault. It now lives in the encrypted vault (service
    ``deploy_github_token``); ``git_repos.builtin_token()`` returns the vault-primed
    value, with the ``provider.json`` mirror as a last resort."""
    try:
        from app import git_repos
        tok = git_repos.builtin_token()
        if tok:
            return tok
    except Exception:  # noqa: BLE001
        pass
    try:
        if _TOKEN_FILE.is_file():
            import json
            data = json.loads(_TOKEN_FILE.read_text(encoding="utf-8"))
            return data.get("github_token", "")
    except Exception:
        pass
    return ""


async def get_vault_token() -> str:
    """The production-specific GitHub key from the encrypted vault, or ''."""
    try:
        from app.db import get_db
        elem = await get_db().auth_element_get(_VAULT_USER, _VAULT_SERVICE, _VAULT_LABEL)
    except Exception as e:  # noqa: BLE001
        logger.debug("production mirror: vault token read failed: %s", e)
        return ""
    return str((elem or {}).get("secret_ref") or "")


async def save_vault_token(token: str) -> bool:
    """Store the production GitHub key in the encrypted vault. A BLANK token
    LEAVES the stored secret unchanged (the UI never echoes a key back to edit
    it), matching the deploy-credential vault convention."""
    token = (token or "").strip()
    if not token:
        return True
    try:
        from app.db import get_db
        await get_db().auth_element_set(
            user_id=_VAULT_USER, service=_VAULT_SERVICE,
            config={}, secret_ref=token, label=_VAULT_LABEL,
        )
        return True
    except Exception as e:  # noqa: BLE001
        logger.warning("production mirror: vault token save failed: %s", e)
        return False


async def clear_vault_token() -> bool:
    """Forget the stored production key (revert to the shared fallback)."""
    try:
        from app.db import get_db
        return bool(await get_db().auth_element_delete(_VAULT_USER, _VAULT_SERVICE, _VAULT_LABEL))
    except Exception as e:  # noqa: BLE001
        logger.warning("production mirror: vault token clear failed: %s", e)
        return False


async def token_is_set() -> bool:
    """True when a usable key exists (production vault OR shared fallback)."""
    return bool((await get_vault_token()) or _get_token())


async def _resolve_token() -> str:
    """The key to authenticate clone/push with: production vault first, the
    shared ``provider.json`` token second."""
    return (await get_vault_token()) or _get_token()


def _git_env() -> dict:
    """Environment for git calls. Always disables interactive credential prompts
    so a missing/blank key fails fast instead of hanging headless; also injects
    the shared token as a belt-and-suspenders fallback to the tokenized URL
    (which is the real auth path here)."""
    env = os.environ.copy()
    env["GIT_ASKPASS"] = ""
    env["GIT_TERMINAL_PROMPT"] = "0"
    token = _get_token()
    if token:
        env["GIT_USERNAME"] = "token"
        env["GIT_PASSWORD"] = token
    return env


# Disable the host credential helper for EVERY production-mirror git call.
# Auth here always rides on an in-memory tokenized clone/push URL (see
# `_tokenize_url`), so the machine helper is never needed — and leaving it
# active was actively harmful. On a tokenized push the host helper (on Windows,
# Git Credential Manager) silently STORES the credential as an
# `x-access-token@github.com` account; github.com then has two saved accounts
# and GCM pops a blocking "Select an account" dialog on the next git operation
# that a headless server can't answer. An empty `credential.helper=` value
# resets the helper list, so git neither consults nor writes the host store.
# Same fix as `app/api/github._run_git`.
_NO_CRED_HELPER = ["-c", "credential.helper="]


def _git(args: list[str], cwd: Path, timeout: int = 120) -> tuple[str, str, int]:
    """Run a git command in *cwd*. Returns (stdout, stderr, returncode)."""
    try:
        proc = subprocess.run(
            ["git"] + _NO_CRED_HELPER + args,
            cwd=str(cwd),
            capture_output=True,
            text=True,
            # Force UTF-8 decoding (with replacement) instead of the Windows
            # locale codepage (cp1252). git emits UTF-8 — clone/progress output
            # and non-ASCII paths contain bytes cp1252 can't decode, which
            # otherwise crashes subprocess's reader thread mid-clone.
            encoding="utf-8",
            errors="replace",
            env=_git_env(),
            timeout=timeout,
        )
        return proc.stdout, proc.stderr, proc.returncode
    except subprocess.TimeoutExpired:
        return "", "git command timed out", -1
    except FileNotFoundError:
        return "", "git not found on this system", -1
    except Exception as e:  # noqa: BLE001
        return "", str(e), -1


# ── Config store ────────────────────────────────────────────────────────────
def default_prod_folder() -> str:
    """The default sibling production folder next to this repo.

    Derived from the dev folder's own name so it sits in the same parent: a
    ``…-dev`` (or ``…-development``) repo maps to a ``…-prod`` sibling (e.g.
    ``webagent-dev`` → ``webagent-prod``); anything else just gets ``-prod``
    appended. Admins can override the path in the File Explorer's production
    folder field.
    """
    dev_name = _PROJECT_ROOT.name
    low = dev_name.lower()
    if low.endswith("-dev"):
        prod_name = dev_name[:-len("-dev")] + "-prod"
    elif low.endswith("-development"):
        prod_name = dev_name[:-len("-development")] + "-prod"
    else:
        prod_name = dev_name + "-prod"
    return str((_PROJECT_ROOT.parent / prod_name).resolve()).replace("\\", "/")


def _normalize_remote_url(url: str) -> str:
    """Accept the friendly ``owner/repo`` shorthand admins type in the Production
    repo field and expand it to a full GitHub HTTPS URL; leave an already-complete
    URL (``https://…`` or ``git@…``) untouched. Trailing slashes are trimmed and a
    ``.git`` suffix is added to the shorthand so the engine always gets a clonable
    URL (e.g. ``botboss3000/webagent`` → ``https://github.com/botboss3000/webagent.git``)."""
    u = (url or "").strip().rstrip("/")
    if not u:
        return ""
    if "://" in u or u.startswith("git@"):
        return u
    if re.match(r"^[A-Za-z0-9._-]+/[A-Za-z0-9._-]+$", u):
        if not u.endswith(".git"):
            u += ".git"
        return f"https://github.com/{u}"
    return u


def load_config() -> dict:
    """Read the production-mirror config, filling in defaults. Never raises."""
    data = read_json(_CONFIG_PATH, {}) or {}
    excludes = data.get("exclude_paths") or []
    # Keep stored excludes normalized + de-duped + sorted for a stable UI.
    norm = sorted({_norm_rel(p) for p in excludes if _norm_rel(p)})
    return {
        "prod_folder": (data.get("prod_folder") or default_prod_folder()),
        "prod_remote_url": data.get("prod_remote_url") or "",
        "exclude_paths": norm,
        "auto_release_on_push": bool(data.get("auto_release_on_push") or False),
        "last_release": data.get("last_release") or None,
    }


def save_config(updates: dict) -> dict:
    """Merge *updates* into the stored config and persist atomically."""
    cfg = load_config()
    cfg.update({k: v for k, v in updates.items() if v is not None})
    if "exclude_paths" in updates and updates["exclude_paths"] is not None:
        cfg["exclude_paths"] = sorted({_norm_rel(p) for p in updates["exclude_paths"] if _norm_rel(p)})
    # Expand an owner/repo shorthand to a full URL whenever the remote is set.
    if "prod_remote_url" in updates and updates["prod_remote_url"] is not None:
        cfg["prod_remote_url"] = _normalize_remote_url(updates["prod_remote_url"])
    safe_write_json(_CONFIG_PATH, cfg)
    return cfg


# ── Path helpers ────────────────────────────────────────────────────────────
def _norm_rel(path_str: str) -> str:
    """Normalize a path to a repo-relative POSIX string, or '' if it falls
    outside the repo. Accepts an absolute path (as the File Explorer sends) or
    an already-relative one."""
    if not path_str:
        return ""
    p = str(path_str).replace("\\", "/").strip().strip("/")
    if not p:
        return ""
    try:
        candidate = Path(path_str)
        if candidate.is_absolute():
            rel = candidate.resolve().relative_to(_PROJECT_ROOT)
            return str(rel).replace("\\", "/").strip("/")
    except Exception:
        return ""
    # Already a relative path — reject any traversal outside the repo.
    if ".." in p.split("/"):
        return ""
    return p


def set_excluded(path_str: str, excluded: bool) -> dict:
    """Add or remove one folder from the exclude list. Returns the new config."""
    rel = _norm_rel(path_str)
    if not rel:
        raise ValueError("Path is outside the project and cannot be excluded.")
    cfg = load_config()
    current = set(cfg["exclude_paths"])
    if excluded:
        current.add(rel)
    else:
        current.discard(rel)
    return save_config({"exclude_paths": sorted(current)})


def set_excluded_bulk(add=None, remove=None) -> dict:
    """Apply several exclude-list changes in one write. *remove* paths are
    discarded first, then *add* paths are added, so a path appearing in both ends
    up added. Used by the File Explorer's folder checkbox: ticking a partially-
    checked folder *includes the whole subtree* (remove the folder + every
    excluded descendant), unticking it *excludes the whole folder* (add the
    folder, drop the now-redundant per-descendant marks) — all atomically, so the
    Git page never sees a half-applied list. Paths outside the repo are skipped."""
    cfg = load_config()
    current = set(cfg["exclude_paths"])
    for p in (remove or []):
        rel = _norm_rel(p)
        if rel:
            current.discard(rel)
    for p in (add or []):
        rel = _norm_rel(p)
        if rel:
            current.add(rel)
    return save_config({"exclude_paths": sorted(current)})


def _is_under_excluded(rel: str, excludes: set[str]) -> bool:
    """True when *rel* is one of the excluded folders or lives inside one."""
    for ex in excludes:
        if rel == ex or rel.startswith(ex + "/"):
            return True
    return False


# ── File listing (git-aware: honours .gitignore) ────────────────────────────
def _list_project_files() -> list[str]:
    """Every real project file as a repo-relative POSIX path.

    Uses ``git ls-files --cached --others --exclude-standard`` so the set is
    exactly what git considers part of the project: tracked files PLUS untracked
    files that are not ``.gitignore``d — and nothing that is ignored (runtime
    DBs, secrets, caches, temp). That is precisely what should travel to a
    GitHub-published mirror.
    """
    out, _, rc = _git(
        ["ls-files", "--cached", "--others", "--exclude-standard", "-z"],
        cwd=_PROJECT_ROOT,
        timeout=60,
    )
    if rc != 0:
        return []
    files = [seg.replace("\\", "/").strip("/") for seg in out.split("\0") if seg.strip()]
    # De-dup (a file can appear once) and keep stable order.
    seen: set[str] = set()
    result: list[str] = []
    for f in files:
        if f not in seen:
            seen.add(f)
            result.append(f)
    return result


def _dev_branch() -> str:
    out, _, rc = _git(["rev-parse", "--abbrev-ref", "HEAD"], cwd=_PROJECT_ROOT, timeout=10)
    branch = out.strip()
    return branch if (rc == 0 and branch and branch != "HEAD") else "main"


def _dev_head_short() -> str:
    out, _, rc = _git(["rev-parse", "--short", "HEAD"], cwd=_PROJECT_ROOT, timeout=10)
    return out.strip() if rc == 0 else ""


def _dev_identity() -> tuple[str, str]:
    name, _, _ = _git(["config", "user.name"], cwd=_PROJECT_ROOT, timeout=10)
    email, _, _ = _git(["config", "user.email"], cwd=_PROJECT_ROOT, timeout=10)
    return (name.strip() or "WebAgent", email.strip() or "webagent@local")


# ── Status (for the Git page's Production section) ──────────────────────────
def production_status() -> dict:
    """Lightweight status used by the Production section of the Git page."""
    cfg = load_config()
    dest = Path(cfg["prod_folder"]) if cfg["prod_folder"] else None
    folder_exists = bool(dest and dest.exists())
    is_repo = bool(dest and (dest / ".git").exists())
    dev_commit = _dev_head_short()
    last = cfg.get("last_release") or {}
    last_commit = last.get("dev_commit") or ""
    # "Has changes" when dev moved since the last release, when nothing has been
    # released yet, or when the exclude list changed since the last release.
    last_excludes = last.get("exclude_paths")
    excludes_changed = (last_excludes is not None and sorted(last_excludes) != cfg["exclude_paths"])
    has_changes = (not last) or (dev_commit and dev_commit != last_commit) or excludes_changed
    return {
        "configured": bool(cfg["prod_remote_url"]),
        "prod_folder": cfg["prod_folder"],
        "prod_remote_url": cfg["prod_remote_url"],
        "exclude_paths": cfg["exclude_paths"],
        "exclude_count": len(cfg["exclude_paths"]),
        "auto_release_on_push": cfg["auto_release_on_push"],
        "folder_exists": folder_exists,
        "is_repo": is_repo,
        "dev_branch": _dev_branch(),
        "dev_commit": dev_commit,
        "last_release": last or None,
        "has_changes": bool(has_changes),
    }


def diff_status() -> dict:
    """Read-only comparison of the dev **shipping set** against the production
    folder's current contents — exactly what a one-way *Sync* would change, but
    without touching anything. Powers the File Explorer's More-menu line that
    tells the admin whether dev and production differ before they sync.

    Returns counts of files that would be **added** (shipped from dev, missing in
    production), **updated** (present in both but different bytes) and **removed**
    (in production but no longer shipped). ``in_sync`` is true when all three are
    zero. ``first_sync`` is true when production hasn't been built yet (no repo
    there), in which case every shipping file counts as an addition.
    """
    cfg = load_config()
    dest = Path(cfg["prod_folder"]) if cfg.get("prod_folder") else None
    remote_url = (cfg.get("prod_remote_url") or "").strip()
    excludes = set(cfg["exclude_paths"])

    all_files = _list_project_files()
    keep = [f for f in all_files if not _is_under_excluded(f, excludes)]
    keep_set = set(keep)
    base = {
        "configured": bool(remote_url),
        "prod_folder": cfg["prod_folder"],
        "shipping_count": len(keep),
        "excluded_count": len(all_files) - len(keep),
    }

    is_repo = bool(dest and (dest / ".git").exists())
    folder_exists = bool(dest and dest.exists())
    if not dest or not folder_exists or not is_repo:
        # Nothing materialized yet → the first sync writes the whole shipping set.
        return {**base, "folder_exists": folder_exists, "is_repo": is_repo,
                "first_sync": True, "in_sync": False,
                "added": len(keep), "updated": 0, "removed": 0,
                "total_changes": len(keep)}

    # Current production files (everything but the .git folder).
    prod_files: set[str] = set()
    for root, dirs, files in os.walk(dest):
        if Path(root) == dest and ".git" in dirs:
            dirs.remove(".git")
        for fn in files:
            rel = str((Path(root) / fn).relative_to(dest)).replace("\\", "/")
            prod_files.add(rel)

    added = updated = 0
    for rel in keep_set:
        if rel not in prod_files:
            added += 1
            continue
        src = str(_PROJECT_ROOT / rel)
        dst = str(dest / rel)
        try:
            # Fast path: matching size + mtime (a one-way sync copies with
            # ``copy2``, preserving mtime) means identical — skip the read. Only
            # when the stat signatures differ do we read both files to confirm a
            # real content change, so a stat-only mismatch (e.g. a freshly cloned
            # prod tree) isn't miscounted as an update.
            if filecmp.cmp(src, dst, shallow=True):
                continue
            if not filecmp.cmp(src, dst, shallow=False):
                updated += 1
        except Exception:  # noqa: BLE001
            updated += 1
    removed = len(prod_files - keep_set)
    total = added + updated + removed
    return {**base, "folder_exists": True, "is_repo": True, "first_sync": False,
            "in_sync": total == 0, "added": added, "updated": updated,
            "removed": removed, "total_changes": total}


# ── The release engine ──────────────────────────────────────────────────────
def _wipe_except_git(dest: Path) -> None:
    """Remove everything in *dest* except its own .git folder, so files deleted
    (or newly excluded) in dev disappear from the mirror."""
    for child in dest.iterdir():
        if child.name == ".git":
            continue
        try:
            if child.is_dir() and not child.is_symlink():
                shutil.rmtree(child, ignore_errors=True)
            else:
                child.unlink(missing_ok=True)
        except Exception as e:  # noqa: BLE001
            logger.warning("production mirror: could not remove %s: %s", child, e)


def _copy_files(files: list[str], dest: Path) -> int:
    """Copy each repo-relative file from the dev root into *dest*. Returns the
    number copied."""
    copied = 0
    for rel in files:
        src = _PROJECT_ROOT / rel
        if not src.is_file():
            continue
        dst = dest / rel
        try:
            dst.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(src, dst)
            copied += 1
        except Exception as e:  # noqa: BLE001
            logger.warning("production mirror: could not copy %s: %s", rel, e)
    return copied


def _set_identity(dest: Path) -> None:
    name, email = _dev_identity()
    _git(["config", "user.name", name], cwd=dest, timeout=10)
    _git(["config", "user.email", email], cwd=dest, timeout=10)


def _ensure_repo(dest: Path, remote_url: str, token: str) -> str:
    """Make *dest* a git repo wired to *remote_url* and return the branch to
    commit/push.

    On first setup this is non-destructive: if the remote already has history we
    **clone** it so the mirror's trimmed tree commits *on top* of the existing
    repo (a normal fast-forward push, nothing overwritten); only a genuinely
    empty remote gets a fresh ``git init``. *token* (resolved from the vault, or
    the shared fallback) is used for the clone but the stored ``origin`` is reset
    to the clean URL afterwards.
    """
    auth_url = _tokenize_url(remote_url, token)

    # Already set up — just keep origin pointed at the clean URL.
    if (dest / ".git").exists():
        _git(["remote", "set-url", "origin", remote_url], cwd=dest, timeout=10)
        b, _, rc = _git(["rev-parse", "--abbrev-ref", "HEAD"], cwd=dest, timeout=10)
        return b.strip() if (rc == 0 and b.strip() and b.strip() != "HEAD") else _dev_branch()

    # First setup: does the remote already have branches?
    refs_out, _, rc = _git(["ls-remote", "--heads", auth_url], cwd=_PROJECT_ROOT, timeout=60)
    remote_has_history = (rc == 0 and bool(refs_out.strip()))

    dest.mkdir(parents=True, exist_ok=True)
    dest_empty = not any(dest.iterdir())

    if remote_has_history and dest_empty:
        # Clone so we share history → the release is a fast-forward, not a clobber.
        _git(["clone", auth_url, str(dest)], cwd=str(dest.parent), timeout=600)
        _git(["remote", "set-url", "origin", remote_url], cwd=dest, timeout=10)
        _set_identity(dest)
        b, _, rc2 = _git(["rev-parse", "--abbrev-ref", "HEAD"], cwd=dest, timeout=10)
        return b.strip() if (rc2 == 0 and b.strip()) else _dev_branch()

    # Empty remote (or a non-empty local folder we can't clone into) → fresh init.
    branch = _dev_branch()
    _git(["init"], cwd=dest, timeout=30)
    _git(["symbolic-ref", "HEAD", f"refs/heads/{branch}"], cwd=dest, timeout=10)
    _set_identity(dest)
    _, _, rc3 = _git(["remote", "get-url", "origin"], cwd=dest, timeout=10)
    if rc3 != 0:
        _git(["remote", "add", "origin", remote_url], cwd=dest, timeout=10)
    else:
        _git(["remote", "set-url", "origin", remote_url], cwd=dest, timeout=10)
    return branch


def _scan_for_secrets(diff_text: str) -> str | None:
    for pattern, label in _SECRET_PATTERNS:
        if re.search(pattern, diff_text or ""):
            return label
    return None


def _tokenize_url(url: str, token: str) -> str:
    """Embed the shared token into an HTTPS GitHub URL for a one-off
    authenticated push — WITHOUT writing it into the mirror's .git/config.

    The production repo's stored ``origin`` stays the clean URL (so nothing
    secret is persisted on disk); we only push to the tokenized form in memory.
    SSH or already-credentialed URLs are returned unchanged.
    """
    if not token or not url or not url.startswith("https://"):
        return url
    rest = url[len("https://"):]
    host_part = rest.split("/", 1)[0]
    if "@" in host_part:  # strip any creds already embedded by the user
        rest = rest.split("@", 1)[1]
    return f"https://x-access-token:{token}@{rest}"


def _sanitize(text: str, token: str) -> str:
    """Never surface the token in an error message echoed back from git."""
    if token and text:
        return text.replace(token, "***")
    return text or ""


def _push(dest: Path, branch: str, remote_url: str, token: str) -> tuple[str, str, int]:
    """Push the mirror's current HEAD to *branch* on the production remote.

    Pushes to an explicit (in-memory tokenized) URL rather than the named
    ``origin`` so authentication never depends on a configured credential
    helper, and the token is never written to the mirror's git config. Only this
    tool ever writes the mirror, so upstream tracking isn't needed. *token* is the
    resolved production key (vault first, shared fallback)."""
    push_url = _tokenize_url(remote_url, token)
    out, err, code = _git(
        ["push", push_url, f"HEAD:refs/heads/{branch}"], cwd=dest, timeout=300
    )
    return _sanitize(out, token), _sanitize(err, token), code


async def _auto_commit_message(dest: Path, staged_diff: str,
                               fallback: str) -> tuple[str, str]:
    """Auto-write the production commit message with the SAME lightweight LLM
    writer the dev commit path (the ⭐ commit&push button) uses, fed the production
    repo's already-staged diff. Returns ``(message, source)``: ``source`` is
    ``"llm"`` when the model produced the note, or ``"fallback"`` when we keep the
    deterministic release line (writer unavailable, empty reply, timeout, or no
    diff). The import is lazy so this module never depends on the API layer at load
    time (``app/api/github.py`` already imports this module)."""
    import asyncio

    try:
        stat_out, _, _ = await run_git_job(
            lambda: _git(["diff", "--cached", "--stat"], cwd=dest, timeout=60))
        from app.api.github import _generate_commit_message
        msg, source, _detail = await _generate_commit_message(stat_out, staged_diff, [])
        if source == "llm" and msg.strip():
            return msg.strip(), "llm"
    except Exception:  # noqa: BLE001
        logger.warning("production mirror: auto commit-message writer failed", exc_info=True)
    return fallback, "fallback"


async def copy_events(message: str = ""):
    """Async generator: build the trimmed production tree in the sister folder and
    **commit it locally — WITHOUT pushing**. This is the first half of a release;
    the File Explorer's "Copy to production" runs it on its own so the admin can
    stage what ships, review it, then push separately.

    Phases: preparing → scanning(file listing) → copying → safety(secret scan)
    → message(auto-write the note when blank) → committing → done. Terminal
    statuses: ``copied`` (a local commit is staged
    in the production folder, ready to push), ``nothing`` (production already
    matches dev — nothing new to commit), ``blocked`` (a secret was found before
    committing), ``error``. The terminal event is always
    ``{"phase": "done", "result": {...}}`` so the UI can rely on it.
    """
    import asyncio

    async def _to_thread(fn, *a, **k):
        return await run_git_job(fn, *a, **k)

    cfg = load_config()
    remote_url = (cfg.get("prod_remote_url") or "").strip()
    dest = Path(cfg["prod_folder"]) if cfg.get("prod_folder") else None
    excludes = set(cfg["exclude_paths"])
    branch = _dev_branch()

    yield {"phase": "preparing", "prod_folder": str(dest) if dest else "", "branch": branch}
    await asyncio.sleep(0)

    # ── Validate config ──
    # The remote URL is needed even for copy: the first copy CLONES an existing
    # production repo (so the commit is a fast-forward, never a clobber), and the
    # mirror's origin is wired from it.
    if not remote_url:
        yield {"phase": "done", "result": {"status": "error",
               "message": "No production GitHub remote URL is set yet. Set it on the Production panel first."}}
        return
    if not dest:
        yield {"phase": "done", "result": {"status": "error",
               "message": "No production folder is set."}}
        return
    if dest.resolve() == _PROJECT_ROOT.resolve():
        yield {"phase": "done", "result": {"status": "error",
               "message": "The production folder must be different from the dev repo."}}
        return

    # Resolve the auth key once (production vault first, shared token fallback);
    # the first copy clones the existing remote, which needs it.
    token = await _resolve_token()

    # ── List the dev project's real files (git-aware) ──
    yield {"phase": "scanning"}
    await asyncio.sleep(0)
    all_files = await _to_thread(_list_project_files)
    if not all_files:
        yield {"phase": "done", "result": {"status": "error",
               "message": "Could not list the dev project's files (is this a git repo?)."}}
        return
    keep = [f for f in all_files if not _is_under_excluded(f, excludes)]
    dropped = len(all_files) - len(keep)

    # ── Create / sync the production repo, mirror the trimmed tree ──
    # First run has no .git in the dest yet → _ensure_repo CLONES the existing
    # remote (can take ~20s); flag it so the UI can say so instead of looking hung.
    first_setup = not (dest / ".git").exists()
    yield {"phase": "copying", "file_count": len(keep), "excluded_count": dropped,
           "first_setup": first_setup}
    await asyncio.sleep(0)

    def _materialize() -> tuple[int, str]:
        dest.mkdir(parents=True, exist_ok=True)
        prod_branch = _ensure_repo(dest, remote_url, token)
        _wipe_except_git(dest)
        return _copy_files(keep, dest), prod_branch

    try:
        copied, branch = await _to_thread(_materialize)
    except Exception as e:  # noqa: BLE001
        logger.exception("production mirror: materialize failed")
        yield {"phase": "done", "result": {"status": "error",
               "message": f"Could not build the production copy: {e}"}}
        return

    # ── Stage + secret-scan the staged diff before anything is committed ──
    yield {"phase": "safety"}
    await asyncio.sleep(0)
    await _to_thread(lambda: _git(["add", "-A"], cwd=dest, timeout=120))
    diff_out, _, _ = await _to_thread(lambda: _git(["diff", "--cached"], cwd=dest, timeout=120))
    secret = _scan_for_secrets(diff_out)
    if secret:
        yield {"phase": "done", "result": {"status": "blocked",
               "message": (f"Possible {secret} found in the trimmed tree — copy aborted before committing. "
                           "Remove it from the dev working tree (or exclude the folder) and retry.")}}
        return

    # ── Nothing to copy? ──
    status_out, _, _ = await _to_thread(lambda: _git(["status", "--porcelain"], cwd=dest, timeout=60))
    if not status_out.strip():
        yield {"phase": "done", "result": {"status": "nothing",
               "message": "Production folder already matches dev — nothing to sync."}}
        return

    # ── Compose the commit message (mirrors the Git page's ⭐ commit&push) ──
    # A blank note means "auto-write one": we reuse the SAME lightweight LLM writer
    # the dev commit path uses, fed the production repo's already-staged diff, so a
    # release gets a real conventional-commit summary of what actually changed. On
    # any writer failure we keep the deterministic "Release from dev…" line. The
    # message / message_ready / message_fallback phases mirror the ⭐ exactly.
    dev_short = _dev_head_short()
    ex_note = (", ".join(sorted(excludes)) if excludes else "none")
    # ASCII hyphen, not an em-dash: git-on-Windows stores a literal "—" in the
    # commit arg double-encoded, which shows up as "â€”" in the commit history.
    deterministic = (f"Release from dev {dev_short or '(uncommitted)'} - {copied} files\n\n"
                     f"Generated production mirror. Excluded folders: {ex_note}.")
    if message and message.strip():
        commit_msg = message.strip()
    else:
        yield {"phase": "message"}
        await asyncio.sleep(0)
        commit_msg, msg_source = await _auto_commit_message(dest, diff_out, deterministic)
        if msg_source == "llm":
            yield {"phase": "message_ready", "title": commit_msg.splitlines()[0]}
        else:
            yield {"phase": "message_fallback"}

    # ── Commit (locally only — no push) ──
    yield {"phase": "committing"}
    await asyncio.sleep(0)
    _, c_err, c_rc = await _to_thread(lambda: _git(["commit", "-m", commit_msg], cwd=dest, timeout=120))
    if c_rc != 0:
        yield {"phase": "done", "result": {"status": "error",
               "message": f"Commit failed: {c_err.strip() or 'unknown error'}"}}
        return

    yield {"phase": "done", "result": {
        "status": "copied",
        "message": (f"Synced {copied} files to the production folder "
                    f"({dropped} excluded) and committed locally. Push to publish."),
        "dev_commit": dev_short,
        "files": copied,
        "excluded_count": dropped,
        "branch": branch,
    }}


async def push_events():
    """Async generator: **push** the production folder's current commit to its
    GitHub remote. This is the second half of a release; the File Explorer's
    "Push to GitHub" runs it on its own, after a Copy has staged a local commit.

    Phases: preparing → pushing → done. Terminal statuses: ``pushed`` (sent to
    GitHub), ``nothing`` (GitHub already has this commit), ``error`` (including
    "nothing has been copied yet"). Records ``last_release`` on a real push.
    """
    import asyncio

    async def _to_thread(fn, *a, **k):
        return await run_git_job(fn, *a, **k)

    cfg = load_config()
    remote_url = (cfg.get("prod_remote_url") or "").strip()
    dest = Path(cfg["prod_folder"]) if cfg.get("prod_folder") else None
    excludes = set(cfg["exclude_paths"])

    yield {"phase": "preparing", "prod_folder": str(dest) if dest else ""}
    await asyncio.sleep(0)

    if not remote_url:
        yield {"phase": "done", "result": {"status": "error",
               "message": "No production GitHub remote URL is set yet. Set it on the Production panel first."}}
        return
    if not dest or not (dest / ".git").exists():
        yield {"phase": "done", "result": {"status": "error",
               "message": "Nothing has been copied to the production folder yet — run Copy first."}}
        return
    # A repo with no commit can't be pushed.
    head_out, _, head_rc = await _to_thread(lambda: _git(["rev-parse", "HEAD"], cwd=dest, timeout=10))
    if head_rc != 0 or not head_out.strip():
        yield {"phase": "done", "result": {"status": "error",
               "message": "Nothing has been committed to the production folder yet — run Copy first."}}
        return

    # Resolve the auth key (production vault first, shared token fallback).
    token = await _resolve_token()

    # Normalize origin to the clean URL + learn the branch to push.
    branch = await _to_thread(lambda: _ensure_repo(dest, remote_url, token))

    # ── Push ──
    yield {"phase": "pushing"}
    await asyncio.sleep(0)
    p_out, p_err, p_rc = await _to_thread(lambda: _push(dest, branch, remote_url, token))
    if p_rc != 0:
        yield {"phase": "done", "result": {"status": "error",
               "message": f"Push failed: {(p_err or p_out).strip() or 'unknown error'}"}}
        return

    up_to_date = "up-to-date" in (p_out + p_err).lower() or "up to date" in (p_out + p_err).lower()

    # ── Record the release ──
    dev_short = _dev_head_short()
    record = {
        "at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "dev_commit": dev_short,
        "excluded_count": len(excludes),
        "exclude_paths": sorted(excludes),
        "branch": branch,
    }
    try:
        save_config({"last_release": record})
    except Exception:  # noqa: BLE001
        logger.warning("production mirror: could not persist last_release")

    if up_to_date:
        yield {"phase": "done", "result": {"status": "nothing",
               "message": "Production GitHub is already up to date — nothing new to push."}}
        return

    yield {"phase": "done", "result": {
        "status": "pushed",
        "message": "Pushed the production folder to GitHub.",
        "dev_commit": dev_short,
        "branch": branch,
    }}


async def release_events(message: str = ""):
    """Full one-click release = copy (build + commit locally) then push. Kept as
    the Git page's "Release to production" button; it now **composes**
    :func:`copy_events` and :func:`push_events` so the one-shot and the split
    Copy/Push paths share exactly one engine.

    Phases: preparing → scanning → copying → safety → committing → pushing → done.
    Terminal statuses (unchanged for the Git page): ``released``, ``nothing``,
    ``blocked``, ``error``.
    """
    # ── First half: copy + local commit ──
    copy_res = None
    async for ev in copy_events(message):
        if ev.get("phase") == "done":
            res = ev.get("result") or {}
            if res.get("status") != "copied":
                # nothing / blocked / error — forward verbatim and stop (these
                # statuses already map correctly on the Git page).
                yield ev
                return
            copy_res = res
            break
        yield ev

    # ── Second half: push ──
    async for ev in push_events():
        phase = ev.get("phase")
        if phase == "preparing":
            continue  # don't re-show "Preparing" partway through a release
        if phase == "done":
            res = ev.get("result") or {}
            st = res.get("status")
            if st in ("pushed", "nothing"):
                files = (copy_res or {}).get("files", 0)
                dropped = (copy_res or {}).get("excluded_count", 0)
                yield {"phase": "done", "result": {
                    "status": "released",
                    "message": (f"Released {files} files to production "
                                f"({dropped} excluded) and pushed to GitHub."),
                    "dev_commit": (copy_res or {}).get("dev_commit", ""),
                    "files": files,
                    "excluded_count": dropped,
                }}
            else:
                yield ev  # error
            return
        yield ev  # "pushing"

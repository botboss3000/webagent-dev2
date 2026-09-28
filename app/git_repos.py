"""Git repos registry — let the Git Control page manage MORE than one repo.

The Git Control page (``app/api/github.py``) was hard-wired to this app's own
folder: every git action (status, commit graph, commit, push, pull, branches) ran
in the project root with the single shared GitHub token. This module is the small,
self-contained registry that lets the page also point at **other local git repos**,
each with its own GitHub remote URL and its own token, and show *that* repo's
working-tree changes + commit graph instead.

Two kinds of entry:

- **The built-in WebAgent repo** is always present and is *synthesized on the fly*
  (never persisted): its folder is the project root, its ``origin`` is read live
  from git, and its shared GitHub token comes from the **encrypted vault** (primed
  by ``github.py`` — see :func:`_shared_token`), with ``provider.json`` kept as a
  plaintext local mirror. It can't be edited or removed here.
- **Added repos** are stored in ``data/config/git-repos.json`` (gitignored, exactly
  like the shared token) with their own ``folder`` / ``remote_url`` / ``token``.

``github.py`` calls :func:`resolve_active` at the top of each UI request to learn
which folder + token to run git with, and re-points its one git helper there. The
agent's Git Control ability deliberately does **not** read this registry — it pins
back to the project root — so an agent never commits to whatever repo the user
happens to be browsing on the page.

Nothing here is wired into the agent core; it is a self-contained admin-tools
extension, mirroring ``app/production_mirror.py``.
"""

from __future__ import annotations

import json
import logging
import os
import subprocess
import time
import uuid
from pathlib import Path

from app.util.config_io import read_json, safe_write_json
from app.runtime_mode import data_root

logger = logging.getLogger(__name__)

# ── Project + config locations ──────────────────────────────────────────────
_APP_ROOT = Path(__file__).resolve().parent.parent
_WORKSPACE_REPO = Path(os.environ.get(
    "WEBAGENT_WORKSPACE_REPO", "/workspace/webagent"
))
_PROJECT_ROOT = _WORKSPACE_REPO if (_WORKSPACE_REPO / ".git").is_dir() else _APP_ROOT
_CONFIG_PATH = data_root() / "config" / "git-repos.json"
# The GitHub token for the built-in repo is the same single shared credential the
# Git page has always used (provider.json), so the default repo is unchanged.
_TOKEN_FILE = data_root() / "config" / "provider.json"

# The built-in (this-app) repo's fixed id + label.
BUILTIN_ID = "webagent"
BUILTIN_LABEL = "WebAgent (this app)"


# ── Helpers ─────────────────────────────────────────────────────────────────
# Vault-authoritative override for the built-in repo's shared GitHub token.
# The shared token now lives in the ENCRYPTED VAULT (app/deploy/credentials.py,
# service ``deploy_github_token``) so it is durable and travels to every device
# signed into the same vault — the Deploy panel already used that key, so the Git
# page and Deploy now share ONE token. ``github.py`` reads the vault (async) and
# primes this in-memory override at startup + before each token-sensitive request;
# every sync git path here then resolves the vault token without an await.
# ``provider.json`` stays a plaintext LOCAL mirror/fallback (resilience + the sync
# fast-path). When the override is set (even to "") it WINS over provider.json.
_SHARED_TOKEN_OVERRIDE: str | None = None


def set_shared_token_override(token: str | None) -> None:
    """Set (or clear with ``None``) the in-memory shared-token override primed from
    the vault. Called by ``github.py`` after it reads the vault."""
    global _SHARED_TOKEN_OVERRIDE
    _SHARED_TOKEN_OVERRIDE = token


def builtin_token() -> str:
    """Public accessor for the built-in repo's shared GitHub token (vault override
    if primed, else the provider.json mirror)."""
    return _shared_token()


def _shared_token() -> str:
    """The single shared GitHub token for the built-in repo. Prefers the
    vault-primed override; falls back to the ``provider.json`` mirror when the
    override has not been primed yet (e.g. before the first vault read)."""
    if _SHARED_TOKEN_OVERRIDE is not None:
        return _SHARED_TOKEN_OVERRIDE
    try:
        if _TOKEN_FILE.is_file():
            data = json.loads(_TOKEN_FILE.read_text(encoding="utf-8"))
            return data.get("github_token", "") or ""
    except Exception:  # noqa: BLE001
        pass
    return ""


def _project_root_str() -> str:
    return str(_PROJECT_ROOT).replace("\\", "/")


def _norm_folder(path_str: str) -> str:
    """Absolute, forward-slashed folder path (or '' when blank/unresolvable)."""
    s = (path_str or "").strip().strip('"')
    if not s:
        return ""
    try:
        return str(Path(s).resolve()).replace("\\", "/")
    except Exception:  # noqa: BLE001
        return s.replace("\\", "/")


def _git(args: list[str], cwd: Path, timeout: int = 10) -> tuple[str, str, int]:
    """Run a *read-only, local* git command in *cwd* (no token needed). Returns
    (stdout, stderr, returncode)."""
    try:
        proc = subprocess.run(
            ["git"] + list(args),
            cwd=str(cwd),
            capture_output=True,
            text=True,
            timeout=timeout,
        )
        return proc.stdout, proc.stderr, proc.returncode
    except FileNotFoundError:
        return "", "git not found on this system", -1
    except subprocess.TimeoutExpired:
        return "", "git command timed out", -1
    except Exception as e:  # noqa: BLE001
        return "", str(e), -1


# ── Config store (added repos only — the built-in is synthesized) ───────────
def load_config() -> dict:
    """Read the registry, normalised. Never raises. Shape:
    ``{"active_id": str, "repos": [{id, label, folder, remote_url, token}]}``."""
    data = read_json(_CONFIG_PATH, {}) or {}
    repos: list[dict] = []
    for r in (data.get("repos") or []):
        if not isinstance(r, dict):
            continue
        rid = str(r.get("id") or "").strip()
        folder = _norm_folder(r.get("folder") or "")
        if not rid or not folder:
            continue
        repos.append({
            "id": rid,
            "label": (str(r.get("label") or "").strip() or folder.rsplit("/", 1)[-1]),
            "folder": folder,
            "remote_url": str(r.get("remote_url") or "").strip(),
            "token": str(r.get("token") or ""),
            "git_user_name": str(r.get("git_user_name") or "").strip(),
            "git_user_email": str(r.get("git_user_email") or "").strip(),
        })
    active_id = str(data.get("active_id") or BUILTIN_ID).strip() or BUILTIN_ID
    # A selection pointing at a now-removed repo falls back to the built-in.
    if active_id != BUILTIN_ID and not any(r["id"] == active_id for r in repos):
        active_id = BUILTIN_ID
    return {"active_id": active_id, "repos": repos}


def _save(cfg: dict) -> dict:
    safe_write_json(_CONFIG_PATH, {
        "active_id": cfg.get("active_id") or BUILTIN_ID,
        "repos": cfg.get("repos") or [],
    })
    return cfg


# The built-in repo's origin URL rarely changes, yet _builtin_entry() runs on
# EVERY UI request (resolve_active → get_active_full → _all_full) and again on
# each 5s Git Control poll — each time spawning a `git remote get-url origin`.
# resolve_active() doesn't even use the URL (only folder + token), so this spawn
# was pure overhead on the hot path. Cache it briefly; a changed remote (set via
# the Edit form → invalidate_origin_cache) shows immediately, otherwise within
# _ORIGIN_TTL seconds.
_ORIGIN_CACHE: str = ""
_ORIGIN_CACHE_TS: float = 0.0
_ORIGIN_TTL = 30.0


def invalidate_origin_cache() -> None:
    """Force the next read to re-query git — call after changing origin's URL."""
    global _ORIGIN_CACHE_TS
    _ORIGIN_CACHE_TS = 0.0


def _builtin_origin() -> str:
    """Origin URL of this app's repo, cached for _ORIGIN_TTL seconds."""
    global _ORIGIN_CACHE, _ORIGIN_CACHE_TS
    now = time.monotonic()
    if _ORIGIN_CACHE_TS and (now - _ORIGIN_CACHE_TS) < _ORIGIN_TTL:
        return _ORIGIN_CACHE
    origin = ""
    try:
        out, _, rc = _git(["remote", "get-url", "origin"], cwd=_PROJECT_ROOT)
        if rc == 0:
            origin = out.strip()
    except Exception:  # noqa: BLE001
        pass
    _ORIGIN_CACHE = origin
    _ORIGIN_CACHE_TS = now
    return origin


def _builtin_entry() -> dict:
    """The always-present WebAgent repo: project root, live origin + shared token."""
    origin = _builtin_origin()
    name, _, _ = _git(["config", "user.name"], cwd=_PROJECT_ROOT)
    email, _, _ = _git(["config", "user.email"], cwd=_PROJECT_ROOT)
    return {
        "id": BUILTIN_ID,
        "label": BUILTIN_LABEL,
        "folder": _project_root_str(),
        "remote_url": origin,
        "token": _shared_token(),
        "builtin": True,
        "git_user_name": name.strip(),
        "git_user_email": email.strip(),
    }


def _all_full() -> list[dict]:
    """Every repo (built-in first) WITH its token — for internal resolution."""
    cfg = load_config()
    return [_builtin_entry()] + [{**r, "builtin": False} for r in cfg["repos"]]


# ── Public surface ──────────────────────────────────────────────────────────
def list_repos() -> list[dict]:
    """All repos, WebAgent first, each tagged ``builtin`` + ``active``. The raw
    token is never returned — only a ``has_token`` flag — so the client can't read
    a stored key back out."""
    active = load_config()["active_id"]
    out: list[dict] = []
    for r in _all_full():
        out.append({
            "id": r["id"],
            "label": r["label"],
            "folder": r["folder"],
            "remote_url": r.get("remote_url", ""),
            "has_token": bool(r.get("token")),
            "builtin": bool(r.get("builtin")),
            "active": r["id"] == active,
            "git_user_name": r.get("git_user_name", ""),
            "git_user_email": r.get("git_user_email", ""),
        })
    return out


def get_active_full() -> dict:
    """The active repo's full entry (with token), built-in if nothing selected."""
    active = load_config()["active_id"]
    for r in _all_full():
        if r["id"] == active:
            return r
    return _builtin_entry()


def resolve_active() -> tuple[Path, str]:
    """``(repo folder, token)`` for the active repo — what ``github.py`` points git
    at. Falls back to the project root + shared token on any inconsistency."""
    r = get_active_full()
    folder = r.get("folder") or _project_root_str()
    return Path(folder), (r.get("token") or "")


def validate_folder(path: str) -> dict:
    """Check *path* is the top of a git work tree; return its branch + origin so
    the Add form can pre-fill them and warn early on a non-repo folder."""
    folder = _norm_folder(path)
    p = Path(folder) if folder else None
    if not p or not p.exists():
        return {"is_repo": False, "exists": False, "folder": folder,
                "branch": "", "origin": "", "message": "Folder not found."}
    if not p.is_dir():
        return {"is_repo": False, "exists": True, "folder": folder,
                "branch": "", "origin": "", "message": "That path is not a folder."}
    out, _, rc = _git(["rev-parse", "--is-inside-work-tree"], cwd=p)
    if not (rc == 0 and out.strip() == "true"):
        return {"is_repo": False, "exists": True, "folder": folder,
                "branch": "", "origin": "",
                "message": "That folder is not a git repository (no .git found)."}
    branch_out, _, _ = _git(["rev-parse", "--abbrev-ref", "HEAD"], cwd=p)
    origin_out, _, _ = _git(["remote", "get-url", "origin"], cwd=p)
    return {"is_repo": True, "exists": True, "folder": folder,
            "branch": branch_out.strip(), "origin": origin_out.strip(), "message": "ok"}


def set_active(repo_id: str) -> dict:
    """Select which repo the Git page operates on."""
    rid = (repo_id or "").strip() or BUILTIN_ID
    cfg = load_config()
    valid = {BUILTIN_ID} | {r["id"] for r in cfg["repos"]}
    if rid not in valid:
        raise ValueError("Unknown repository.")
    cfg["active_id"] = rid
    _save(cfg)
    return cfg


def add_repo(label: str = "", folder: str = "", remote_url: str = "",
             token: str = "", git_user_name: str = "",
             git_user_email: str = "") -> dict:
    """Register a new local repo. Validates the folder is a git repo and isn't the
    built-in project root. Returns the new entry (token stripped)."""
    folder_n = _norm_folder(folder)
    if not folder_n:
        raise ValueError("A local folder is required.")
    if folder_n == _project_root_str():
        raise ValueError("That's this app's own repo — it's already the default.")
    info = validate_folder(folder_n)
    if not info["is_repo"]:
        raise ValueError(info.get("message") or "That folder is not a git repository.")
    cfg = load_config()
    if any(r["folder"] == folder_n for r in cfg["repos"]):
        raise ValueError("That folder is already in the list.")
    entry = {
        "id": uuid.uuid4().hex[:12],
        "label": (label or "").strip() or folder_n.rsplit("/", 1)[-1],
        "folder": folder_n,
        "remote_url": (remote_url or "").strip() or info.get("origin", ""),
        "token": token or "",
        "git_user_name": (git_user_name or "").strip(),
        "git_user_email": (git_user_email or "").strip(),
    }
    cfg["repos"].append(entry)
    _save(cfg)
    return {**entry, "token": "", "has_token": bool(entry["token"]), "builtin": False}


def update_repo(repo_id: str, *, label=None, folder=None, remote_url=None,
                token=None, git_user_name=None,
                git_user_email=None) -> dict:
    """Edit a registered repo. A blank/None *token* keeps the existing one. The
    built-in repo can't be edited here."""
    rid = (repo_id or "").strip()
    if rid == BUILTIN_ID:
        raise ValueError("The built-in WebAgent repo can't be edited here.")
    cfg = load_config()
    for r in cfg["repos"]:
        if r["id"] != rid:
            continue
        if label is not None and label.strip():
            r["label"] = label.strip()
        if folder is not None:
            fn = _norm_folder(folder)
            if fn and fn != r["folder"]:
                info = validate_folder(fn)
                if not info["is_repo"]:
                    raise ValueError(info.get("message") or "That folder is not a git repository.")
                r["folder"] = fn
        if remote_url is not None:
            r["remote_url"] = remote_url.strip()
        if token:  # blank = keep existing key
            r["token"] = token
        if git_user_name is not None:
            r["git_user_name"] = git_user_name.strip()
        if git_user_email is not None:
            r["git_user_email"] = git_user_email.strip()
        _save(cfg)
        return {**r, "token": "", "has_token": bool(r["token"]), "builtin": False}
    raise ValueError("Unknown repository.")


def remove_repo(repo_id: str) -> dict:
    """Forget a registered repo. The built-in repo can't be removed."""
    rid = (repo_id or "").strip()
    if rid == BUILTIN_ID:
        raise ValueError("The built-in WebAgent repo can't be removed.")
    cfg = load_config()
    before = len(cfg["repos"])
    cfg["repos"] = [r for r in cfg["repos"] if r["id"] != rid]
    if len(cfg["repos"]) == before:
        raise ValueError("Unknown repository.")
    if cfg["active_id"] == rid:
        cfg["active_id"] = BUILTIN_ID
    _save(cfg)
    return cfg

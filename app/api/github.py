"""GitHub / Git integration API for WebAgent.

Provides endpoints for:
- Repo status (branch, remote, unstaged/staged files)
- Commit log
- Stage all + commit
- Push to remote
- Pull from remote
- Store / check GitHub token (for HTTPS auth)

Note: the GitHub token is a single shared credential (the repo is shared).
Unlike LLM keys, it's NOT per-user. It now lives in the ENCRYPTED VAULT
(``app/deploy/credentials.py``, service ``deploy_github_token`` — the same key the
Deploy panel uses) so it is durable and travels to every device signed into the
same vault. ``provider.json`` is kept only as a plaintext LOCAL mirror/fallback for
the synchronous git fast-path; ``_prime_shared_token_from_vault`` makes the vault
authoritative (and migrates a legacy provider.json-only token into it once).
"""

import asyncio
import base64
import functools
import logging
import os
import re
import signal
import subprocess
import threading
import time
import json
import uuid
from pathlib import Path
from fastapi import APIRouter, HTTPException, Query, Request
from fastapi.responses import StreamingResponse
from pydantic import BaseModel, Field
from app.auth.identity import request_user_id
from app.db import get_db
from app import production_mirror
from app import git_repos
from app.git_executor import run_git_job as _run_git_job
from app.runtime_mode import data_root

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api/v1/github")

# ── Find project root (parent of app/) ──
_PROJECT_ROOT = Path(__file__).resolve().parent.parent.parent

# ── Token storage (single shared token in provider.json) ──
_TOKEN_FILE = data_root() / "config" / "provider.json"


def _get_token() -> str:
    """Read stored GitHub token from provider.json."""
    try:
        if _TOKEN_FILE.is_file():
            data = json.loads(_TOKEN_FILE.read_text(encoding="utf-8"))
            return data.get("github_token", "")
    except Exception:
        pass
    return ""


def _save_token(token: str) -> None:
    """Write GitHub token to provider.json (the local mirror of the vault key)."""
    try:
        data = {}
        if _TOKEN_FILE.is_file():
            data = json.loads(_TOKEN_FILE.read_text(encoding="utf-8"))
        data["github_token"] = token
        _TOKEN_FILE.write_text(json.dumps(data, indent=2), encoding="utf-8")
    except Exception as e:
        logger.error("Failed to save GitHub token: %s", e)
        raise HTTPException(status_code=500, detail=f"Failed to save token: {e}")


def _mirror_token(token: str) -> None:
    """Best-effort local mirror write — never raises (used off the HTTP path)."""
    try:
        _save_token(token)
    except Exception as e:  # noqa: BLE001
        logger.debug("could not mirror github token to provider.json: %s", e)


# ── Vault-backed shared token (durable + shared across devices) ──────────────
# The built-in repo's shared GitHub token lives in the encrypted vault (service
# ``deploy_github_token``, shared with the Deploy panel). These helpers make the
# vault the source of truth and keep the sync git paths (git_repos override +
# provider.json mirror + _TOKEN_CACHE) primed from it. The repo's CLEAN address is
# stored in the SAME vault row (non-secret) so a device — or a new deployment —
# sharing the vault gets the URL + token SEPARATELY, never a tokenized URL.

def _clean_repo_url(raw: str) -> str:
    """A git remote URL with any embedded credentials stripped and SSH form turned
    into https — safe to store/share as the repo address (the token lives
    separately). Blank/unparseable → ''."""
    u = (raw or "").strip()
    if not u:
        return ""
    m = re.match(r"^[\w.+-]+@([^:/]+):(.+)$", u)     # git@host:owner/repo → https
    if m:
        u = f"https://{m.group(1)}/{m.group(2)}"
    u = re.sub(r"^(https?://)[^/@]+@", r"\1", u)      # strip https://user[:tok]@host
    return u


async def _save_repo_url_to_vault(raw: str) -> None:
    """Mirror the built-in repo's clean address into the shared vault row. Cheap:
    the credentials layer skips the write when the URL is unchanged. Never raises."""
    try:
        clean = _clean_repo_url(raw)
        if clean:
            from app.deploy import credentials as dc
            await dc.save_github_repo_url(clean)
    except Exception as e:  # noqa: BLE001
        logger.debug("could not save repo url to vault: %s", e)


async def _prime_shared_token_from_vault() -> str:
    """Resolve the shared GitHub token from the vault and prime every sync git path.

    Vault-authoritative: whatever the vault holds wins (so a token saved on another
    device signed into the same vault shows up here). If the vault is empty but a
    legacy plaintext token still sits in provider.json, migrate it into the vault
    once. Safe to call anywhere — it never raises."""
    try:
        from app.deploy import credentials as dc
        try:
            tok = await dc.get_github_token()
        except Exception as e:  # noqa: BLE001
            logger.debug("vault github token read failed: %s", e)
            tok = ""
        if not tok:
            legacy = _get_token()
            if legacy:
                try:
                    if await dc.save_github_token(legacy):
                        logger.info("Migrated legacy Git-page token into the vault.")
                except Exception as e:  # noqa: BLE001
                    logger.warning("could not migrate Git-page token to vault: %s", e)
                tok = legacy
        git_repos.set_shared_token_override(tok)
        _cache_token(tok)
        if tok and tok != _get_token():
            _mirror_token(tok)   # keep the local fallback in step with the vault
        # Also record this app's own repo address (clean) in the shared vault row,
        # so a device / new deployment sharing the vault knows the repo URL too.
        origin = await _run_git_job(git_repos._builtin_origin)
        await _save_repo_url_to_vault(origin)
        return tok
    except Exception as e:  # noqa: BLE001
        logger.debug("prime shared token failed: %s", e)
        return ""


async def _save_shared_token(token: str) -> None:
    """Persist the shared GitHub token to the vault (durable + shared) and prime the
    local sync paths. A blank token clears it everywhere."""
    from app.deploy import credentials as dc
    token = (token or "").strip()
    if not token:
        try:
            await dc.clear_github_token()
        except Exception as e:  # noqa: BLE001
            logger.warning("could not clear vault github token: %s", e)
        git_repos.set_shared_token_override("")
        _cache_token("")
        _mirror_token("")
        return
    try:
        await dc.save_github_token(token)
    except Exception as e:  # noqa: BLE001
        logger.warning("could not save github token to vault: %s", e)
    git_repos.set_shared_token_override(token)
    _cache_token(token)
    _mirror_token(token)


# ── Token cache (set before sync git calls) ──
_TOKEN_CACHE: str = ""
# Git user identity for the active repo — injected as -c args so commits
# work without a global git config (fixes "Author identity unknown" errors).
_GIT_USER_NAME: str = ""
_GIT_USER_EMAIL: str = ""


def _cache_token(token: str) -> None:
    global _TOKEN_CACHE
    _TOKEN_CACHE = token


# ── Active repo (which folder the Git page operates on) ──
# The Git Control page can point at OTHER local repos besides this app's own —
# see app/git_repos.py. `_ACTIVE_REPO` is the folder every git call below runs in;
# it is re-resolved from the saved selection at the top of each UI request via
# `_use_active_repo()` and defaults to this app's own root. The agent's Git Control
# ability pins it back here (`_pin_to_project_root`) so an agent never follows the
# page's selection. Same single-process global convention as `_TOKEN_CACHE`.
_ACTIVE_REPO: Path = _PROJECT_ROOT

# Git Control status/graph requests poll concurrently, and several nominally
# read-only Git commands briefly take Git's optional index lock. Serialize every
# Git subprocess in this server so a commit's `git add` can never race one of
# those refreshes. RLock is intentional: helpers such as _git_push call _run_git
# more than once on the same worker thread.
_GIT_COMMAND_LOCK = threading.RLock()

# Git is an independent contention domain. Never put Git subprocess work on
# asyncio's shared default executor: a slow fetch plus lock waiters can otherwise
# occupy every worker needed by SQLite, schema, and ordinary request handlers.
_GIT_READ_CACHE: dict[tuple, tuple[float, dict]] = {}
_GIT_READ_REFRESHES: dict[tuple, asyncio.Task] = {}

# StreamingResponse cancels its response iterator when a browser, reverse proxy,
# or tunnel drops the body stream. The Git operation must not share that lifetime:
# a disconnect just after `git commit` used to cancel the generator before
# `git push`, leaving a real local commit while the UI reported only the browser's
# opaque "Error in input stream". Keep each operation in its own task and retain
# completed records briefly so a reconnecting client can recover the result.
_COMMIT_OPERATIONS: dict[str, dict] = {}
_COMMIT_OPERATION_TTL = 300.0


def _git_read_cache_key(kind: str, *variant) -> tuple:
    try:
        repo_id = git_repos.load_config().get("active_id") or str(_PROJECT_ROOT)
    except Exception:  # noqa: BLE001
        repo_id = str(_PROJECT_ROOT)
    return (kind, str(repo_id), *variant)


def _with_git_cache_meta(payload: dict, *, stale: bool, refreshing: bool,
                         age_s: float) -> dict:
    result = dict(payload)
    result["_cache"] = {
        "stale": bool(stale),
        "refreshing": bool(refreshing),
        "age_ms": max(0, round(age_s * 1000)),
    }
    return result


async def _cached_git_read(key: tuple, loader, *, wait: bool = False) -> dict:
    """Serve the last good read immediately and coalesce its refresh."""
    now = time.monotonic()
    cached = _GIT_READ_CACHE.get(key)
    task = _GIT_READ_REFRESHES.get(key)
    if task is None or task.done():
        async def _refresh():
            try:
                value = await _run_git_job(loader)
                _GIT_READ_CACHE[key] = (time.monotonic(), value)
                return value
            finally:
                current = asyncio.current_task()
                if _GIT_READ_REFRESHES.get(key) is current:
                    _GIT_READ_REFRESHES.pop(key, None)

        task = asyncio.create_task(_refresh(), name=f"git-read:{key[0]}")
        _GIT_READ_REFRESHES[key] = task

    if cached is not None and not wait:
        cached_at, payload = cached

        def _consume(done: asyncio.Task) -> None:
            if not done.cancelled():
                try:
                    done.exception()
                except Exception:
                    pass

        task.add_done_callback(_consume)
        return _with_git_cache_meta(
            payload, stale=True, refreshing=True, age_s=now - cached_at
        )

    value = await task
    return _with_git_cache_meta(value, stale=False, refreshing=False, age_s=0)


def _use_active_repo() -> None:
    """Point subsequent git calls at the user's currently selected repo + key + git
    user identity. Call at the top of each UI endpoint.
    Falls back to this app's repo + shared token on any error so the page never
    breaks if the registry is missing or a folder went away."""
    global _ACTIVE_REPO, _GIT_USER_NAME, _GIT_USER_EMAIL
    try:
        root, token = git_repos.resolve_active()
        _ACTIVE_REPO = root if (root and root.exists()) else _PROJECT_ROOT
        _cache_token(token or _get_token())
        # Cache git user identity from the active repo entry
        full = git_repos.get_active_full()
        _GIT_USER_NAME = full.get("git_user_name", "")
        _GIT_USER_EMAIL = full.get("git_user_email", "")
    except Exception:  # noqa: BLE001
        _ACTIVE_REPO = _PROJECT_ROOT
        _cache_token(git_repos.builtin_token() or _get_token())
        _GIT_USER_NAME = ""
        _GIT_USER_EMAIL = ""


def _pin_to_project_root() -> None:
    """Force subsequent git calls back to this app's own repo + shared key. Used by
    the agent Git Control ability so agent git is never re-pointed by whatever repo
    the user has selected on the Git Control page."""
    global _ACTIVE_REPO, _GIT_USER_NAME, _GIT_USER_EMAIL
    _ACTIVE_REPO = _PROJECT_ROOT
    # Prefer the vault-primed shared token (set at startup / before remote ops);
    # fall back to the provider.json mirror if it hasn't been primed yet.
    _cache_token(git_repos.builtin_token() or _get_token())
    # Clear identity so _run_git reads the repo's actual config (not a stale -c override)
    _GIT_USER_NAME, _GIT_USER_EMAIL = "", ""
    name_out, _, _ = _run_git(["config", "user.name"], timeout=5)
    email_out, _, _ = _run_git(["config", "user.email"], timeout=5)
    _GIT_USER_NAME = name_out.strip()
    _GIT_USER_EMAIL = email_out.strip()


def _index_lock_signature(repo: Path | None = None) -> tuple[int, int, int, int] | None:
    """Return an identity for a repo's index lock, if one exists."""
    target = (repo or _ACTIVE_REPO) / ".git" / "index.lock"
    try:
        stat = target.stat()
        return stat.st_dev, stat.st_ino, stat.st_mtime_ns, stat.st_size
    except OSError:
        return None


def _terminate_git_process_tree(proc: subprocess.Popen) -> None:
    """Stop a timed-out Git command and every helper/child it spawned.

    ``subprocess.run(..., timeout=...)`` kills only the process it started. On
    Windows, Git commonly delegates work to another ``git.exe``; that child can
    survive the timeout and keep ``.git/index.lock`` open (or strand it when it
    exits). Start Git in its own process group and tear down the complete tree.
    """
    try:
        if os.name == "nt":
            subprocess.run(
                ["taskkill", "/PID", str(proc.pid), "/T", "/F"],
                capture_output=True,
                timeout=10,
                check=False,
            )
        else:
            os.killpg(proc.pid, signal.SIGKILL)
    except (OSError, subprocess.SubprocessError):
        try:
            proc.kill()
        except OSError:
            pass


def _cleanup_timed_out_index_lock(
        args: list[str], before: tuple[int, int, int, int] | None,
        repo: Path | None = None) -> None:
    """Remove only an index lock created or changed by our timed-out command."""
    lock_path = (repo or _ACTIVE_REPO) / ".git" / "index.lock"
    after = _index_lock_signature(repo)
    if after is None or after == before:
        return
    try:
        lock_path.unlink()
        command = args[0] if args else "command"
        logger.warning("Removed index lock stranded by timed-out git %s", command)
    except OSError as exc:
        logger.warning("Could not remove timed-out git index lock: %s", exc)


def _git_lock_contended(err: str) -> bool:
    """True when a git error means another process currently holds index.lock."""
    low = (err or "").lower()
    return "index.lock" in low and "unable to create" in low


def _sweep_orphaned_index_lock(max_age_s: float = 180.0) -> bool:
    """Remove .git/index.lock only when it is provably orphaned.

    The process-wide lock serializes git calls INSIDE this server, but the same
    working tree is shared with external tools (the Codex desktop app, parallel
    MCP agent processes, a user's terminal). If one of them is killed mid-write
    it can strand index.lock forever — and the old signature-based cleanup only
    removed locks our OWN timed-out commands left behind. Sweep only when the
    lock is older than any legitimate git writer could hold it (the add/commit
    budget here is 120s). On Windows, atomically rename the stale candidate
    before deleting it: an active Git writer keeps the file open without delete
    sharing, so the rename fails safely. Checking for *any* git.exe process was
    too broad -- an unrelated repository could keep this lock stranded forever.
    """
    lock_path = _ACTIVE_REPO / ".git" / "index.lock"
    try:
        st = lock_path.stat()
    except OSError:
        return False
    if time.time() - st.st_mtime < max_age_s:
        return False
    if os.name == "nt":
        claimed = lock_path.with_name(
            f"index.lock.orphaned-{os.getpid()}-{threading.get_ident()}")
        try:
            lock_path.replace(claimed)
        except OSError:
            # A live Git process still owns the handle, or the candidate changed.
            return False
        try:
            claimed.unlink()
        except OSError:
            # Keep Git unblocked even if antivirus/indexing briefly prevents the
            # quarantine file itself from being removed.
            pass
        logger.warning(
            "Removed orphaned .git/index.lock (age %.0fs)", time.time() - st.st_mtime)
        return True
    try:
        check = subprocess.run(
            ["pgrep", "-x", "git"], capture_output=True, timeout=10, check=False)
    except (OSError, subprocess.SubprocessError):
        return False
    if check.returncode == 0:
        return False
    try:
        lock_path.unlink()
        logger.warning(
            "Removed orphaned .git/index.lock (age %.0fs)", time.time() - st.st_mtime)
        return True
    except OSError:
        return False


def _run_git_retry_on_lock(
        args: list[str], timeout: int = 120,
        retries: int = 4, base_delay: float = 0.5) -> tuple[str, str, int]:
    """Run a git writer, retrying briefly when another process holds index.lock.

    Losing the lock race fails INSTANTLY with 'Unable to create .../index.lock:
    File exists' — no wait, no partial state (the commit/add never started). A
    short backoff loop lets the concurrent winner finish, then we proceed, so a
    commit racing the Codex app or a parallel agent no longer surfaces as a hard
    'git commit failed' error."""
    out, err, code = "", "", -1
    for attempt in range(retries + 1):
        out, err, code = _run_git(args, timeout=timeout)
        if code != 0 and _git_lock_contended(err):
            # Put recovery beside the retry so every writer using this helper is
            # protected; callers should not need to remember a separate sweep.
            if _sweep_orphaned_index_lock():
                continue
            time.sleep(base_delay * (attempt + 1))
            continue
        return out, err, code
    return out, err, code


def _run_git(args: list[str], timeout: int = 15,
             cwd: Path | None = None) -> tuple[str, str, int]:
    """Run one Git subprocess without overlapping another WebAgent Git call."""
    with _GIT_COMMAND_LOCK:
        return _run_git_locked(args, timeout, cwd)


def _run_git_locked(args: list[str], timeout: int,
                    cwd: Path | None = None) -> tuple[str, str, int]:
    """Run a git command in a repo (the active repo, or ``cwd`` if given).
    Returns (stdout, stderr, returncode).

    Auth is injected DIRECTLY: the stored GitHub token is carried as an HTTP
    Basic-auth header (`-c http.extraHeader=…`) and the machine credential helper
    is disabled for this call (`-c credential.helper=`). This is the fix for the
    "commit & push stuck on Starting…" hang. The old code set GIT_USERNAME /
    GIT_PASSWORD, **which git does not read** — so a push fell through to whatever
    credential helper the host has (on Windows, Git Credential Manager). From this
    headless server process (no interactive desktop) that helper tries to pop a
    prompt nobody can answer, so `git push`/`pull` HANGS until the timeout and the
    UI sits forever on "Starting…". Injecting the header + clearing the helper
    means git authenticates with OUR token and, on a bad/again-missing token,
    fails INSTANTLY with a readable error instead of blocking. GIT_TERMINAL_PROMPT
    stays 0 as a second backstop against any interactive prompt.
    """
    env = os.environ.copy()
    env["GIT_TERMINAL_PROMPT"] = "0"
    # Read-only commands such as `git status` normally refresh cached stat data
    # in the index. The Git Control page polls those commands, so a cancelled
    # or timed-out poll could strand index.lock before a commit even started.
    # Required index writers (add/commit/etc.) still lock normally; this disables
    # only Git's optional background refresh writes.
    env["GIT_OPTIONAL_LOCKS"] = "0"
    config_args: list[str] = []
    # Inject git user identity so commits never fail with "Author identity unknown"
    # even when no global/user git config is set on the host.
    if _GIT_USER_NAME:
        config_args += ["-c", f"user.name={_GIT_USER_NAME}"]
    if _GIT_USER_EMAIL:
        config_args += ["-c", f"user.email={_GIT_USER_EMAIL}"]
    token = _TOKEN_CACHE or _get_token()
    if token:
        # An empty credential.helper value is git's documented way to reset the
        # helper list (so the host's Credential Manager is bypassed); the token
        # then rides as a one-shot Basic header — never persisted to .git/config.
        basic = base64.b64encode(f"x-access-token:{token}".encode()).decode()
        config_args = [
            "-c", "credential.helper=",
            "-c", f"http.extraHeader=Authorization: Basic {basic}",
        ]
    repo = cwd or _ACTIVE_REPO
    lock_before = _index_lock_signature(repo)
    popen_kwargs = {}
    if os.name == "nt":
        popen_kwargs["creationflags"] = subprocess.CREATE_NEW_PROCESS_GROUP
    else:
        popen_kwargs["start_new_session"] = True
    try:
        proc = subprocess.Popen(
            ["git"] + config_args + args,
            cwd=str(repo),
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            env=env,
            **popen_kwargs,
        )
        stdout, stderr = proc.communicate(timeout=timeout)
        return stdout, stderr, proc.returncode
    except subprocess.TimeoutExpired:
        _terminate_git_process_tree(proc)
        try:
            proc.communicate(timeout=5)
        except (subprocess.TimeoutExpired, OSError):
            try:
                proc.kill()
                proc.communicate(timeout=5)
            except (subprocess.SubprocessError, OSError):
                pass
        _cleanup_timed_out_index_lock(args, lock_before, repo)
        return "", "git command timed out", -1
    except FileNotFoundError:
        return "", "git not found on this system", -1
    except Exception as e:
        return "", str(e), -1


# ── Coalesced remote fetch ──────────────────────────────────────────
# Both /status and /log-graph refresh remote refs before reading. Opening the
# Git Control page hits both, so it used to fire TWO back-to-back network
# fetches — and the graph's was a heavy `git fetch --all` across every remote.
# This guard runs at most ONE `git fetch origin` per _FETCH_TTL seconds: the
# first read in a window pays the network cost, and the sibling read + the 5s
# poll ticks inside the window reuse those just-updated refs instead of piling
# on more fetches. The timestamp is stamped BEFORE the (slow) call so a
# concurrent request in the same window skips rather than racing a second fetch.
_LAST_FETCH_TS: float = 0.0
_FETCH_LOCK = threading.Lock()
_FETCH_TTL = 8.0  # seconds


def _maybe_fetch(timeout: int = 20) -> None:
    """Refresh origin's refs, but no more than once every _FETCH_TTL seconds."""
    global _LAST_FETCH_TS
    now = time.monotonic()
    with _FETCH_LOCK:
        if now - _LAST_FETCH_TS < _FETCH_TTL:
            return
        _LAST_FETCH_TS = now
    _run_git(["fetch", "--quiet", "origin"], timeout=timeout)


def _git_push(timeout: int = 120) -> tuple[str, str, int]:
    """Push the current branch, self-healing a missing upstream link.

    A plain `git push` fails when the local branch has no upstream set —
    e.g. after a history rewrite / force-push drops the tracking link —
    reporting "has no upstream branch". When that's the failure we retry
    once with `--set-upstream origin <branch>`, so the push lands AND the
    link is restored for next time. This is the same effect as the git
    config `push.autoSetupRemote=true`, but applied in-code so it works
    regardless of the user's global git settings. Returns the
    (stdout, stderr, returncode) of whichever push actually ran.
    """
    out, err, code = _run_git(["push"], timeout=timeout)
    if code != 0 and "no upstream branch" in (err or "").lower():
        branch_out, _, br_code = _run_git(["rev-parse", "--abbrev-ref", "HEAD"], timeout=5)
        branch = branch_out.strip()
        if br_code == 0 and branch and branch != "HEAD":
            out, err, code = _run_git(
                ["push", "--set-upstream", "origin", branch], timeout=timeout
            )
    return out, err, code


def _push_failure_hint(stderr: str, stdout: str) -> str:
    """Turn a raw ``git push`` failure into an actionable, plain-language message.

    The raw git error ("remote: Write access to repository not granted", "fatal:
    Authentication failed", "git command timed out", …) is accurate but doesn't
    tell a non-git user WHAT to do. We keep the raw text (nothing hidden) and
    append a specific next step based on which failure it is — so a push that
    can't succeed says so immediately and clearly instead of leaving the button
    spinning. Matched newest-cause-first so the most specific hint wins."""
    raw = (stderr or stdout or "").strip()
    low = raw.lower()
    if not (_TOKEN_CACHE or _get_token()):
        hint = ("No GitHub token is set for this repository. Add one in the "
                "Git Control sidebar, then try again.")
    elif "timed out" in low:
        hint = ("The push didn't finish in time — the machine is usually busy "
                "or the connection to GitHub is slow. Check your connection "
                "and try again; the stored token is still configured.")
    elif ("refusing to allow a personal access token" in low
          and (".github/workflows/" in low or "workflow" in low)):
        hint = ("GitHub accepted the token, but it cannot create or update GitHub "
                "Actions workflow files. Update the saved GitHub key for this "
                "repository: a fine-grained token needs both 'Contents: Read and "
                "write' and 'Workflows: Read and write'; a classic token needs "
                "both the 'repo' and 'workflow' scopes. Then replace the saved key "
                "and press Push — do not create another commit.")
    elif ("write access" in low or "not granted" in low or "403" in low
          or ("permission" in low and "denied" in low)):
        hint = ("Your saved GitHub token doesn't have WRITE access to this "
                "repository. On GitHub, grant the token 'Contents: Read and "
                "write' for this repo (or paste a token that has it in the "
                "Git Control sidebar).")
    elif ("authentication failed" in low or "could not read" in low
          or "invalid username or password" in low or "401" in low):
        hint = ("GitHub rejected the token. Update the GitHub token in the "
                "Git Control sidebar.")
    elif "repository not found" in low or "not found" in low or "404" in low:
        hint = ("The remote repository wasn't found, or your token can't see it. "
                "Check the remote URL and the token's repository access.")
    else:
        hint = ""
    if hint:
        return f"{raw}\n\n{hint}" if raw else hint
    return raw or "git push failed"


# ── Request models ──

# ── Admin access control ──
# AuthMiddleware not active in main.py, so we read Authorization header directly.

_ADMIN_USER_ID = "admin"


def _get_user_id_from_request(request: Request) -> str:
    """Extract the caller's user_id (JWT, else open-mode bootstrap admin).

    Delegates to the shared request-based chokepoint so Git Control
    (commit/push, etc.) works over a Cloudflare Tunnel — where the browser can't
    mint a JWT — via the same open-mode fallback every other admin gate uses.
    See app.auth.identity.request_user_id.
    """
    return request_user_id(request)


def _require_admin(request: Request):
    """Check that the requesting user is admin. Raises 403 if not."""
    user_id = _get_user_id_from_request(request)
    if user_id != _ADMIN_USER_ID:
        raise HTTPException(
            status_code=403,
            detail="Restricted to admin users only.",
        )


# ── Request models ──

class CommitPushRequest(BaseModel):
    # All optional: a blank message means "auto-write one from the diff".
    message: str = Field("", max_length=2000)
    skip_push: bool = False
    include_untracked: bool = True
    # When true the endpoint streams granular progress events (NDJSON) instead of
    # returning a single result dict — drives the ⭐ button's live step display.
    stream: bool = False


class TokenRequest(BaseModel):
    token: str = Field(..., min_length=1)


class RemoteUrlRequest(BaseModel):
    url: str = Field(..., min_length=1, max_length=500)


class BranchRequest(BaseModel):
    branch: str = Field(..., min_length=1, max_length=200)


# Production-mirror request models (the "Release to production" flow).
class ProdConfigRequest(BaseModel):
    prod_folder: str | None = Field(None, max_length=1000)
    prod_remote_url: str | None = Field(None, max_length=500)
    auto_release_on_push: bool | None = None
    # The GitHub key is a secret → stored in the encrypted vault, NOT the local
    # config JSON. A blank string leaves the stored key unchanged (the UI never
    # echoes it back); only a non-empty value updates it.
    github_token: str | None = Field(None, max_length=500)


class ProdExcludeRequest(BaseModel):
    path: str = Field(..., min_length=1, max_length=1000)
    excluded: bool = True


class GitIgnoreRequest(BaseModel):
    path: str = Field(..., min_length=1, max_length=1000)
    tracked: bool = True


class ProdExcludeBulkRequest(BaseModel):
    add: list[str] = Field(default_factory=list, max_length=5000)
    remove: list[str] = Field(default_factory=list, max_length=5000)


class ProdReleaseRequest(BaseModel):
    message: str = Field("", max_length=2000)
    stream: bool = False
    # Optional destination override sent by the File Explorer's folder field. When
    # present it is persisted to the config before the copy/push runs, so the
    # action always uses the freshest folder even if the field's debounced
    # auto-save hasn't landed yet.
    prod_folder: str | None = Field(None, max_length=1000)
    # Same backstop for the production repo (GitHub remote) field — an owner/repo
    # shorthand the engine expands to a full URL on save.
    prod_remote_url: str | None = Field(None, max_length=500)
    # And for the GitHub key field — persisted to the encrypted vault (blank =
    # keep) before the copy/push runs.
    github_token: str | None = Field(None, max_length=500)


# Multi-repo registry request models (the Git Control "repo selector").
class RepoAddRequest(BaseModel):
    label: str = Field("", max_length=200)
    folder: str = Field(..., min_length=1, max_length=1000)
    remote_url: str = Field("", max_length=500)
    token: str = Field("", max_length=500)
    git_user_name: str = Field("", max_length=200)
    git_user_email: str = Field("", max_length=200)


class RepoUpdateRequest(BaseModel):
    label: str | None = Field(None, max_length=200)
    folder: str | None = Field(None, max_length=1000)
    remote_url: str | None = Field(None, max_length=500)
    token: str | None = Field(None, max_length=500)
    git_user_name: str | None = Field(None, max_length=200)
    git_user_email: str | None = Field(None, max_length=200)


class RepoSelectRequest(BaseModel):
    id: str = Field(..., min_length=1, max_length=64)


class PathScopedChangesRequest(BaseModel):
    """A deliberately small, chat-panel scoped set of repository paths."""
    session_id: str = Field(..., min_length=1, max_length=200)
    paths: list[str] = Field(..., min_length=1, max_length=500)
    message: str = Field("", max_length=2000)


class FileCheckoutModeRequest(BaseModel):
    session_id: str = Field(..., min_length=1, max_length=200)
    enabled: bool


# ── Endpoints ──

@router.get("/check-access")
async def check_access(request: Request):
    """Check if the current user has admin access to the GitHub features."""
    user_id = _get_user_id_from_request(request)
    return {"is_admin": user_id == _ADMIN_USER_ID}


@router.get("/status")
async def get_status(request: Request, fetch: bool = True, wait: bool = False):
    """Return repo status: branch, remote, file status, ahead/behind info.

    `fetch=1` (default) refreshes remote refs from origin first so ahead/behind
    is accurate, at the cost of a network round-trip. `fetch=0` skips that and
    returns the last-known local state instantly — used for the fast first paint
    of the Git Control panel, which then refreshes with `fetch=1` in the
    background.

    The read runs a chain of blocking git subprocesses, so it is offloaded to a
    worker thread (`asyncio.to_thread`) — otherwise it freezes the server's
    single event loop for its whole duration, stalling every other request.
    """
    key = _git_read_cache_key("status")
    return await _cached_git_read(key, functools.partial(_status_payload, fetch), wait=wait)


def _status_payload(fetch: bool) -> dict:
    # Point git at the user's selected repo (+ its key) before running commands
    _use_active_repo()

    # 0. Refresh remote refs so ahead/behind reflects what's actually on origin.
    # Without this, the cached refs are whatever was last fetched/pulled on this
    # machine, so the page reports "in sync" even when origin has new commits.
    # Coalesced (see _maybe_fetch): the sibling /log-graph read and the 5s poll
    # reuse this one fetch instead of each firing their own. Skipped entirely on
    # a local-only fast read (fetch=0).
    if fetch:
        _maybe_fetch()

    # 1. Branch name
    branch_out, _, rc = _run_git(["rev-parse", "--abbrev-ref", "HEAD"])
    if rc != 0:
        raise HTTPException(status_code=500, detail="Not a git repository or no HEAD")
    branch = branch_out.strip()

    # 2. Remote URL
    remote_out, _, _ = _run_git(["remote", "get-url", "origin"])
    remote_url = remote_out.strip()

    # 3. Git status (short format)
    exclude_runtime = _ACTIVE_REPO.resolve() == _PROJECT_ROOT.resolve()
    source_pathspec = ["--", ".", *_RUNTIME_PATHSPECS] if exclude_runtime else []
    status_out, _, _ = _run_git(["status", "--short", *source_pathspec])
    status_lines = [line.rstrip() for line in status_out.split("\n") if line.strip()]

    # 4. Parse status into staged / unstaged / untracked
    staged = []
    unstaged = []
    untracked = []
    for line in status_lines:
        index_flag = line[0] if len(line) > 0 else " "
        worktree_flag = line[1] if len(line) > 1 else " "
        path = line[3:] if len(line) > 3 else line
        if index_flag != " " and index_flag != "?":
            staged.append({"path": path, "flag": index_flag})
        if worktree_flag != " " and worktree_flag != "?":
            unstaged.append({"path": path, "flag": worktree_flag})
        if index_flag == "?" and worktree_flag == "?":
            untracked.append({"path": path})

    # 5. Recent commits — pull from origin/<branch> if remote exists, else HEAD.
    # Format: full-hash | short-hash | author-name | author-email | relative-date
    #         | iso-date | refs (decorations) | subject
    # Separator chosen to be unlikely in any field.
    _SEP = "\x1f"
    _log_format = _SEP.join(["%H", "%h", "%an", "%ae", "%ar", "%aI", "%D", "%s"])
    log_ref = f"origin/{branch}" if remote_url else "HEAD"
    # Show commits from remote tip back; also include local-only commits ahead of
    # remote by walking HEAD too (use --branches with explicit range fallback).
    # Simplest: get unique commits from both HEAD and origin/<branch>, newest first.
    log_revs = ["HEAD", log_ref] if remote_url else ["HEAD"]
    log_out, _, rc_log = _run_git(
        ["log", f"--format={_log_format}", "-30"] + log_revs,
        timeout=10,
    )

    # 5a. Build set of commits reachable from HEAD (= already pulled to this VM).
    head_hash = ""
    head_out, _, _ = _run_git(["rev-parse", "HEAD"], timeout=5)
    if head_out.strip():
        head_hash = head_out.strip()
    pulled_set: set[str] = set()
    rev_out, _, _ = _run_git(["rev-list", "-100", "HEAD"], timeout=10)
    if rev_out.strip():
        pulled_set = {h.strip() for h in rev_out.strip().split("\n") if h.strip()}

    commits = []
    seen_hashes: set[str] = set()
    if rc_log == 0:
        for line in log_out.split("\n"):
            line = line.rstrip("\n")
            if not line:
                continue
            parts = line.split(_SEP)
            if len(parts) < 8:
                continue
            full_hash, short_hash, author, email, date_rel, date_iso, refs, subject = parts[:8]
            if full_hash in seen_hashes:
                continue
            seen_hashes.add(full_hash)
            commits.append({
                "hash": short_hash,
                "full_hash": full_hash,
                "author": author,
                "author_email": email,
                "date_relative": date_rel,
                "date_iso": date_iso,
                "refs": refs,
                "message": subject,
                "is_pulled": full_hash in pulled_set,
                "is_head": full_hash == head_hash,
            })

    # 6. Ahead/behind
    ahead_behind_out, _, _ = _run_git(
        ["rev-list", "--left-right", "--count", f"{branch}...origin/{branch}"],
        timeout=10,
    )
    ahead, behind = 0, 0
    ab_parts = ahead_behind_out.strip().split()
    if len(ab_parts) == 2:
        ahead = int(ab_parts[0])
        behind = int(ab_parts[1])

    # 7. Has remote
    has_remote = bool(remote_url)

    # 8. Last commit
    last_commit_out, _, _ = _run_git(
        ["log", "-1", "--format=%H|%an|%ae|%ar|%s"],
        timeout=5,
    )
    last_commit = {}
    if last_commit_out.strip():
        parts = last_commit_out.strip().split("|", 4)
        if len(parts) == 5:
            last_commit = {
                "hash": parts[0],
                "author": parts[1],
                "author_email": parts[2],
                "date_relative": parts[3],
                "message": parts[4],
            }

    # 9. File count total
    file_count = len(staged) + len(unstaged) + len(untracked)

    # Match the Explorer's +/- badges while respecting Git Control's active
    # repository selection (which may point somewhere other than this project).
    line_stats = _working_tree_line_stats(exclude_runtime=exclude_runtime)
    for files in (staged, unstaged, untracked):
        for file in files:
            stat = line_stats.get(file["path"])
            if stat:
                file.update(stat)

    return {
        "branch": branch,
        "remote_url": remote_url,
        "has_remote": has_remote,
        "ahead": ahead,
        "behind": behind,
        "file_count": file_count,
        "staged": staged,
        "unstaged": unstaged,
        "untracked": untracked,
        "commits": commits,
        "last_commit": last_commit,
    }


def _safe_changed_paths(paths: list[str]) -> list[str]:
    """Validate repo-relative paths supplied by the chat change panel."""
    clean: list[str] = []
    seen: set[str] = set()
    root = _ACTIVE_REPO.resolve()
    for raw in paths:
        path = (raw or "").replace("\\", "/").strip().lstrip("/")
        if not path or path in seen:
            continue
        candidate = (root / path).resolve()
        if candidate != root and root not in candidate.parents:
            raise HTTPException(status_code=400, detail=f"Invalid repository path: {raw}")
        clean.append(path)
        seen.add(path)
    if not clean:
        raise HTTPException(status_code=400, detail="Select at least one changed file.")
    return clean


def _chat_changes_payload(allowed_paths: set[str] | None = None) -> dict:
    """Working-tree files and line counts, always for this WebAgent checkout."""
    _pin_to_project_root()
    source_pathspec = ["--", ".", *_RUNTIME_PATHSPECS]
    status_out, status_err, rc = _run_git(
        ["-c", "core.quotePath=false", "status", "--porcelain", "--untracked-files=all", *source_pathspec],
        timeout=20,
    )
    if rc != 0:
        raise HTTPException(status_code=500, detail=status_err.strip() or "git status failed")
    stats = _working_tree_line_stats(exclude_runtime=True)
    files = []
    for line in status_out.splitlines():
        if len(line) < 4:
            continue
        path = line[3:].strip().strip('"')
        if not path:
            continue
        # Porcelain renames are `old -> new`; the new path is the one the user sees.
        if " -> " in path:
            path = path.rsplit(" -> ", 1)[-1]
        if allowed_paths is not None and path not in allowed_paths:
            continue
        stat = stats.get(path, {})
        files.append({"path": path, "status": line[:2],
                      "added": stat.get("added", 0), "removed": stat.get("removed", 0)})
    return {"files": files}


@router.get("/chat-changes")
async def get_chat_changes(
    request: Request,
    session_id: str = Query(..., min_length=1, max_length=200),
):
    """Dirty paths attributed to one chat session."""
    _require_admin(request)
    user_id = _get_user_id_from_request(request)
    db = get_db()
    try:
        await db.assert_session_owned(user_id, session_id)
    except PermissionError as exc:
        raise HTTPException(status_code=403, detail=str(exc)) from exc
    claims = set(await db.get_session_change_claims(session_id))
    payload = await _run_git_job(_chat_changes_payload, claims)
    dirty = {file["path"] for file in payload["files"]}
    stale = sorted(claims - dirty)
    if stale:
        await db.clear_session_change_claims(session_id, stale)
    owners = await db.get_session_change_owners(sorted(dirty))
    from app.agent.file_checkout import file_checkout_status
    checkout_rows = await file_checkout_status(
        session_id, include_user_leases=True, user_id=user_id
    )
    get_checkout_enabled = getattr(db, "get_session_file_checkout_enabled", None)
    try:
        checkout_enabled = bool(await get_checkout_enabled(session_id)) if get_checkout_enabled else True
    except Exception:
        checkout_enabled = True
    checkout_by_path = {
        row["path"]: row for row in checkout_rows
        if row.get("active") or row.get("status") in {
            "checked_in", "stale", "conflict", "violation", "validated", "changed",
            "observed", "recheck_required", "checked_out_elsewhere",
        }
    }
    files_by_path = {file["path"]: file for file in payload["files"]}
    for path, checkout in checkout_by_path.items():
        if path not in files_by_path:
            row = {"path": path, "status": "", "added": 0, "removed": 0}
            payload["files"].append(row)
            files_by_path[path] = row
    for file in payload["files"]:
        other_sessions = [
            owner for owner in owners.get(file["path"], [])
            if owner != session_id
        ]
        checkout = checkout_by_path.get(file["path"]) or {}
        file["conflict"] = bool(other_sessions) or checkout.get("status") == "conflict"
        file["other_sessions"] = other_sessions
        checkout_status = checkout.get("status")
        if file["conflict"]:
            lifecycle = "Conflict"
        elif checkout_status == "recheck_required":
            lifecycle = "Read required"
        elif checkout_status == "checked_out_elsewhere":
            lifecycle = "Checked out elsewhere"
        elif checkout_status == "observed":
            lifecycle = "Read"
        elif checkout_status == "violation":
            lifecycle = "Violation"
        elif checkout_status == "stale":
            lifecycle = "Stale"
        elif checkout.get("active") and checkout.get("validated"):
            lifecycle = "Validated"
        elif checkout.get("active") and (checkout.get("modified") or file["path"] in dirty):
            lifecycle = "Changed"
        elif checkout.get("active"):
            lifecycle = "Checked out"
        elif checkout_status == "checked_in" and file["path"] in dirty:
            lifecycle = "Checked in"
        else:
            lifecycle = "Changed"
        file.update({
            "checkout_status": lifecycle,
            "checked_out": bool(checkout.get("active")),
            "checkout_owner": checkout.get("owner"),
            "manual_checkin_allowed": bool(checkout.get("manual_checkin_allowed")),
            "checkout_expires_at": checkout.get("expires_at"),
            "validated": bool(checkout.get("validated")),
            "validation_note": checkout.get("validation_note"),
            "violation": checkout.get("violation"),
            "observed_fingerprint": checkout.get("observed_fingerprint"),
            "expected_fingerprint": checkout.get("expected_fingerprint"),
        })
    payload["files"].sort(key=lambda row: row["path"])
    payload["file_checkout_enabled"] = checkout_enabled
    return payload


async def _owned_action_paths(
    req: PathScopedChangesRequest, request: Request
) -> tuple[object, list[str]]:
    user_id = _get_user_id_from_request(request)
    db = get_db()
    try:
        await db.assert_session_owned(user_id, req.session_id)
    except PermissionError as exc:
        raise HTTPException(status_code=403, detail=str(exc)) from exc
    paths = _safe_changed_paths(req.paths)
    owned = set(await db.get_session_change_claims(req.session_id))
    foreign = [path for path in paths if path not in owned]
    if foreign:
        raise HTTPException(
            status_code=409,
            detail=f"Paths are not owned by this chat session: {', '.join(foreign)}",
        )
    owners = await db.get_session_change_owners(paths)
    conflicted = [
        path for path in paths
        if any(owner != req.session_id for owner in owners.get(path, []))
    ]
    if conflicted:
        raise HTTPException(
            status_code=409,
            detail=f"Paths were also changed by another session: {', '.join(conflicted)}",
        )
    return db, paths


@router.post("/chat-changes/checkin")
async def manually_checkin_chat_checkouts(req: PathScopedChangesRequest, request: Request):
    """Release another same-user session's file lease from the Updates panel."""
    _require_admin(request)
    user_id = _get_user_id_from_request(request)
    db = get_db()
    try:
        await db.assert_session_owned(user_id, req.session_id)
    except PermissionError as exc:
        raise HTTPException(status_code=403, detail=str(exc)) from exc
    from app.agent.file_checkout import CheckoutError, manual_checkin_files
    try:
        files = await manual_checkin_files(req.session_id, user_id, _safe_changed_paths(req.paths))
    except CheckoutError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    return {"status": "checked_in", "files": files}


@router.post("/chat-changes/checkout-mode")
async def set_chat_checkout_mode(req: FileCheckoutModeRequest, request: Request):
    """Enable or disable checkout enforcement for this chat session only."""
    _require_admin(request)
    user_id = _get_user_id_from_request(request)
    db = get_db()
    try:
        await db.assert_session_owned(user_id, req.session_id)
    except PermissionError as exc:
        raise HTTPException(status_code=403, detail=str(exc)) from exc
    setter = getattr(db, "set_session_file_checkout_enabled", None)
    if not setter:
        raise HTTPException(status_code=503, detail="Session checkout settings are unavailable.")
    enabled = await setter(req.session_id, req.enabled)
    released = 0
    if not enabled:
        from app.agent.file_checkout import release_session_checkouts
        released = await release_session_checkouts(
            req.session_id, "Checkout protection disabled for this session"
        )
    return {
        "file_checkout_enabled": bool(enabled),
        "released_checkouts": released,
    }


@router.post("/chat-changes/undo")
async def undo_chat_changes(req: PathScopedChangesRequest, request: Request):
    """Discard exactly the paths the current chat session marked as its changes."""
    _require_admin(request)
    db, paths = await _owned_action_paths(req, request)
    def _undo():
        _pin_to_project_root()
        porcelain, _, _ = _run_git(["status", "--porcelain", "--untracked-files=all", "--"] + paths, timeout=20)
        untracked = {
            line[3:].strip().strip('"') for line in porcelain.splitlines()
            if line.startswith("?? ") and len(line) > 3
        }
        tracked = [path for path in paths if path not in untracked]
        # Restore tracked files (index and working tree). Missing paths are fine:
        # they are untracked files and are removed in the second pass.
        if tracked:
            out, err, restore_rc = _run_git(["restore", "--staged", "--worktree", "--"] + tracked, timeout=30)
            if restore_rc != 0:
                raise HTTPException(status_code=500, detail=err.strip() or out.strip() or "git restore failed")
        for path in untracked:
            candidate = _ACTIVE_REPO / path
            if candidate.exists() and not (candidate / ".git").exists():
                if candidate.is_dir():
                    import shutil
                    shutil.rmtree(candidate)
                else:
                    candidate.unlink()
        return {"status": "undone", "paths": paths}
    result = await _run_git_job(_undo)
    await db.clear_session_change_claims(req.session_id, paths)
    return result


@router.post("/chat-changes/commit")
async def commit_chat_changes(req: PathScopedChangesRequest, request: Request):
    """Commit only the supplied chat-session paths; unrelated index entries stay out."""
    _require_admin(request)
    db, paths = await _owned_action_paths(req, request)
    def _commit():
        _pin_to_project_root()
        status_out, status_err, status_rc = _run_git(["status", "--porcelain", "--"] + paths, timeout=20)
        if status_rc != 0:
            raise HTTPException(status_code=500, detail=status_err.strip() or "git status failed")
        if not status_out.strip():
            return {"status": "nothing_to_commit", "message": "Those files no longer have changes."}
        diff_out, _, _ = _run_git(["diff", "HEAD", "--"] + paths, timeout=30)
        for pattern, label in _SECRET_PATTERNS:
            if re.search(pattern, diff_out or ""):
                raise HTTPException(status_code=400, detail=f"Possible {label} found in the selected changes.")
        _sweep_orphaned_index_lock()
        _, add_err, add_rc = _run_git_retry_on_lock(
            ["add", "--"] + paths, timeout=120)
        if add_rc != 0:
            raise HTTPException(status_code=500, detail=add_err.strip() or "git add failed")
        message = req.message.strip() or _fallback_commit_message("", paths)
        msg_file = data_root() / "tmp" / "_chat_commit_msg.txt"
        msg_file.parent.mkdir(parents=True, exist_ok=True)
        msg_file.write_text(message, encoding="utf-8")
        try:
            _, commit_err, commit_rc = _run_git_retry_on_lock(
                ["commit", "--only", "-F", str(msg_file), "--"] + paths,
                timeout=120)
        finally:
            try: msg_file.unlink()
            except OSError: pass
        if commit_rc != 0:
            raise HTTPException(status_code=500, detail=commit_err.strip() or "git commit failed")
        head, _, _ = _run_git(["rev-parse", "--short", "HEAD"], timeout=10)
        return {"status": "committed", "paths": paths, "hash": head.strip(), "title": message.splitlines()[0]}
    result = await _run_git_job(_commit)
    await db.clear_session_change_claims(req.session_id, paths)
    return result


@router.get("/log-graph")
async def get_log_graph(
    request: Request, limit: int = 80, fetch: bool = True, wait: bool = False
):
    """Return a commit graph across **all** local + remote branches.

    Output is shaped so the frontend can draw a VS Code style graph:
      - `commits` is a list newest-first; each row carries its `lane`
        (column index for the dot) and `routes` describing the line
        segments connecting it down to the next row.
      - `max_lane` is the total number of columns used (graph width).
      - `branches` is a map of refname -> short hash so the frontend can
        render branch tip labels.

    The endpoint is read-only and shows commits from every ref, including
    branches that aren't `main`, so users can see merges and side branches
    just like the VS Code Git Control graph.

    `fetch=1` (default) refreshes remote refs first so origin/* tips are live;
    `fetch=0` skips the network round-trip and graphs the last-known local refs
    instantly (used for the panel's fast first paint).

    Offloaded to a worker thread (`asyncio.to_thread`): the graph walk is a chain
    of blocking git subprocesses that would otherwise freeze the event loop.
    """
    clamped = max(1, min(int(limit), 500))
    key = _git_read_cache_key("log-graph", clamped)
    loader = functools.partial(_log_graph_payload, clamped, fetch)
    return await _cached_git_read(key, loader, wait=wait)


def _log_graph_payload(limit: int, fetch: bool) -> dict:
    _use_active_repo()

    # Refresh remote refs so origin/* branch tips reflect what's on GitHub.
    # Coalesced + scoped to `origin` (was `--all`, which fetched every remote):
    # the sibling /status read usually satisfies this within _FETCH_TTL, so the
    # page's open fires a single origin fetch instead of two. Skipped on a
    # local-only fast read (fetch=0).
    if fetch:
        _maybe_fetch()

    # Clamp limit to a sane window — big graphs become unreadable anyway.
    try:
        limit = max(1, min(int(limit), 500))
    except Exception:
        limit = 80

    # 1. Walk every ref's history and collect commits with parents.
    # %H = full sha, %P = parent shas (space-separated), %D = refnames,
    # %an = author, %ar = relative date, %aI = iso date, %s = subject
    _SEP = "\x1f"
    fmt = _SEP.join(["%H", "%P", "%D", "%an", "%ar", "%aI", "%s"])
    raw_out, _, rc = _run_git(
        ["log", "--all", "--date-order", f"--format={fmt}", f"-{limit}"],
        timeout=20,
    )
    if rc != 0:
        raise HTTPException(status_code=500, detail="git log failed")

    raw_commits = []
    for line in raw_out.split("\n"):
        line = line.rstrip("\n")
        if not line:
            continue
        parts = line.split(_SEP)
        if len(parts) < 7:
            continue
        full_hash, parents_str, refs, author, date_rel, date_iso, subject = parts[:7]
        parents = [p for p in parents_str.split(" ") if p]
        raw_commits.append({
            "full_hash": full_hash,
            "hash": full_hash[:7],
            "parents": parents,
            "refs": refs,
            "author": author,
            "date_relative": date_rel,
            "date_iso": date_iso,
            "message": subject,
        })

    # 2. HEAD hash + commits already pulled (= reachable from HEAD).
    head_hash = ""
    h_out, _, _ = _run_git(["rev-parse", "HEAD"], timeout=5)
    if h_out.strip():
        head_hash = h_out.strip()
    pulled_set: set[str] = set()
    rev_out, _, _ = _run_git(["rev-list", "-300", "HEAD"], timeout=10)
    if rev_out.strip():
        pulled_set = {h.strip() for h in rev_out.strip().split("\n") if h.strip()}

    # 2b. Commits that `git pull` would actually bring in — i.e. reachable
    # from origin/<current-branch> but not yet in HEAD. Commits on other
    # remote branches are NOT pullable from the current branch and should
    # not be marked with the "↓ not pulled" badge.
    pullable_set: set[str] = set()
    cur_branch_out, _, _ = _run_git(["rev-parse", "--abbrev-ref", "HEAD"], timeout=5)
    cur_branch = cur_branch_out.strip()
    if cur_branch and cur_branch != "HEAD":
        pull_out, _, pull_rc = _run_git(
            ["rev-list", "-300", f"origin/{cur_branch}", "^HEAD"],
            timeout=10,
        )
        if pull_rc == 0 and pull_out.strip():
            pullable_set = {h.strip() for h in pull_out.strip().split("\n") if h.strip()}

    # 2c. Commits committed locally but NOT yet on the remote — reachable from
    # HEAD but not from origin/<current-branch>. These are the "ahead" commits a
    # push would send; the frontend draws them with a dotted connector + green
    # halo so they read as not-yet-on-origin. If origin/<branch> doesn't resolve
    # (no upstream yet), the rev-list fails and we mark nothing — a safe default.
    unpushed_set: set[str] = set()
    if cur_branch and cur_branch != "HEAD":
        ahead_out, _, ahead_rc = _run_git(
            ["rev-list", "-300", "HEAD", f"^origin/{cur_branch}"],
            timeout=10,
        )
        if ahead_rc == 0 and ahead_out.strip():
            unpushed_set = {h.strip() for h in ahead_out.strip().split("\n") if h.strip()}

    # 3. Compute lanes (column positions) for the graph.
    # `active_lanes[i]` holds the hash of the commit expected to land in
    # lane `i` next (placed there by a child commit above). Walking the
    # log newest-first, for each commit:
    #   - snapshot lanes_in (what's coming into this row from above);
    #   - find its dot lane (first matching active slot, or open a new one
    #     if no child placed it — i.e. it's a branch tip);
    #   - free every active slot pointing at this commit (multiple
    #     children draw lines down to the same parent);
    #   - place its parents into lanes — first parent re-uses the dot's
    #     own lane when free (keeps the trunk straight), extras take new
    #     or recycled empty slots.
    active_lanes: list[str | None] = []
    graph_commits = []
    max_lane = 0

    def _first_index(hash_: str) -> int:
        for i, h in enumerate(active_lanes):
            if h == hash_:
                return i
        return -1

    def _open_empty_slot() -> int:
        for i, h in enumerate(active_lanes):
            if h is None:
                return i
        active_lanes.append(None)
        return len(active_lanes) - 1

    for c in raw_commits:
        # Snapshot BEFORE any mutation — these are the lines entering the
        # top edge of this row from rows above.
        lanes_in = list(active_lanes)

        lane = _first_index(c["full_hash"])
        if lane < 0:
            # Tip commit: no child placed it, claim an empty slot.
            lane = _open_empty_slot()

        # Free every slot waiting on this commit.
        for i, h in enumerate(active_lanes):
            if h == c["full_hash"]:
                active_lanes[i] = None

        # Assign parents to lanes.
        parent_lanes: list[int] = []
        for idx, p in enumerate(c["parents"]):
            existing = _first_index(p)
            if idx == 0 and active_lanes[lane] is None and (existing < 0 or existing > lane):
                # First parent = the trunk continuing straight down. Keep it in
                # this commit's OWN lane. If a side-branch row above already
                # parked the parent in a HIGHER lane (existing > lane), leave
                # that reservation in place too: the higher lane then carries a
                # merge line that bends into the parent's own row, while the
                # trunk stays put instead of veering right into the side
                # branch's column. Without this, the leftmost (e.g. main) trunk
                # would kink into a side lane the moment they share an ancestor.
                # (active_lanes[lane] is always free here — the dot's lane was
                # vacated just above.) A parent already sitting in a LOWER lane
                # (existing < lane) is the trunk we merge back into, so we fall
                # through and bend left toward it.
                active_lanes[lane] = p
                parent_lanes.append(lane)
            elif existing >= 0:
                parent_lanes.append(existing)
            else:
                slot = _open_empty_slot()
                active_lanes[slot] = p
                parent_lanes.append(slot)

        lanes_out = list(active_lanes)
        # Width = farthest non-empty lane this row touches.
        width_in = max((i + 1 for i, h in enumerate(lanes_in) if h is not None), default=0)
        width_out = max((i + 1 for i, h in enumerate(lanes_out) if h is not None), default=0)
        row_width = max(width_in, width_out, lane + 1)
        if row_width > max_lane:
            max_lane = row_width

        graph_commits.append({
            "hash": c["hash"],
            "full_hash": c["full_hash"],
            "parents": [p[:7] for p in c["parents"]],
            "refs": c["refs"],
            "author": c["author"],
            "date_relative": c["date_relative"],
            "date_iso": c["date_iso"],
            "message": c["message"],
            "lane": lane,
            "parent_lanes": parent_lanes,
            # Each element is the short hash the lane was tracking, or null
            # if the lane was empty. Frontend just checks truthy/falsy.
            "lanes_in":  [h[:7] if h else None for h in lanes_in],
            "lanes_out": [h[:7] if h else None for h in lanes_out],
            # When a child placed our hash in a non-dot lane (multi-child
            # commit) the frontend bends that line into our dot. Mark them.
            "merge_in_lanes": [i for i, h in enumerate(lanes_in)
                               if h == c["full_hash"] and i != lane],
            "is_head": c["full_hash"] == head_hash,
            "is_pulled": c["full_hash"] in pulled_set,
            "is_pullable": c["full_hash"] in pullable_set,
            "is_unpushed": c["full_hash"] in unpushed_set,
        })

    # 4. Map every ref name to its short hash for branch-tip badges.
    branches: dict[str, str] = {}
    ref_out, _, _ = _run_git(
        ["for-each-ref", "--format=%(refname:short)\x1f%(objectname)", "refs/heads", "refs/remotes"],
        timeout=10,
    )
    for line in ref_out.split("\n"):
        line = line.strip()
        if not line:
            continue
        bits = line.split("\x1f")
        if len(bits) == 2:
            branches[bits[0]] = bits[1][:7]

    return {
        "commits": graph_commits,
        "max_lane": max_lane,
        "branches": branches,
        "current_branch": cur_branch,
        "head_hash": head_hash,
    }


@router.get("/commit/{commit_hash}")
async def get_commit_detail(commit_hash: str, request: Request):
    """Return full info about a single commit (body, files, diff stat)."""
    # Sanity-check hash format to avoid passing arbitrary args to git
    if not commit_hash or len(commit_hash) > 64 or not all(c in "0123456789abcdefABCDEF" for c in commit_hash):
        raise HTTPException(status_code=400, detail="Invalid commit hash")

    return await _run_git_job(_commit_detail_payload, commit_hash)


def _commit_detail_payload(commit_hash: str) -> dict:

    _use_active_repo()

    # Metadata + full body
    _SEP = "\x1f"
    fmt = _SEP.join(["%H", "%h", "%an", "%ae", "%ai", "%cn", "%ce", "%ci", "%P", "%s", "%b"])
    meta_out, meta_err, rc_meta = _run_git(
        ["show", "-s", f"--format={fmt}", commit_hash],
        timeout=10,
    )
    if rc_meta != 0:
        raise HTTPException(status_code=404, detail=meta_err.strip() or "Commit not found")
    parts = meta_out.rstrip("\n").split(_SEP)
    if len(parts) < 11:
        raise HTTPException(status_code=500, detail="Unexpected git show output")
    full_hash, short_hash, an, ae, ad, cn, ce, cd, parents, subject, body = parts[:11]

    # File-level diff stat (numstat: added, removed, path per line)
    stat_out, _, _ = _run_git(
        ["show", "--numstat", "--format=", commit_hash],
        timeout=15,
    )
    files = []
    for line in stat_out.split("\n"):
        line = line.strip()
        if not line:
            continue
        cols = line.split("\t")
        if len(cols) >= 3:
            added = cols[0]
            removed = cols[1]
            path = cols[2]
            files.append({
                "path": path,
                "added": 0 if added == "-" else int(added),
                "removed": 0 if removed == "-" else int(removed),
                "binary": (added == "-" and removed == "-"),
            })

    return {
        "hash": short_hash,
        "full_hash": full_hash,
        "author": an,
        "author_email": ae,
        "author_date": ad,
        "committer": cn,
        "committer_email": ce,
        "commit_date": cd,
        "parents": [p for p in parents.split(" ") if p],
        "subject": subject,
        "body": body,
        "files": files,
    }


def _count_new_file_lines(path: Path, max_bytes: int = 2_000_000) -> int | None:
    """Line count of a brand-new (untracked) file, for its +N badge.

    `git diff` doesn't see untracked files, so for those we count the lines
    ourselves and treat them all as additions. Returns None — meaning "show no
    count" — for anything that shouldn't get a number: a binary file (a NUL byte
    near the top), a file too big to bother reading, or one that's gone /
    unreadable. An empty file is 0."""
    try:
        if not path.is_file() or path.stat().st_size > max_bytes:
            return None
        data = path.read_bytes()
    except Exception:  # noqa: BLE001
        return None
    if b"\x00" in data[:8192]:
        return None
    if not data:
        return 0
    # Trailing newline shouldn't count as an extra (empty) line.
    return data.count(b"\n") + (0 if data.endswith(b"\n") else 1)


def _working_tree_line_stats(*, exclude_runtime: bool = False) -> dict[str, dict[str, int]]:
    """Return per-file +/- counts for the repository selected for git commands."""
    stats: dict[str, dict[str, int]] = {}
    pathspec = ["--", ".", *_RUNTIME_PATHSPECS] if exclude_runtime else []
    out, _, rc = _run_git(
        ["-c", "core.quotePath=false", "diff", "--numstat", "--no-renames", "HEAD", *pathspec],
        timeout=20,
    )
    if rc == 0:
        for line in out.split("\n"):
            cols = line.rstrip("\n").split("\t")
            if len(cols) < 3:
                continue
            added, removed, path = cols[0], cols[1], cols[2]
            if added == "-" and removed == "-":
                continue
            stats[path] = {
                "added": int(added) if added.isdigit() else 0,
                "removed": int(removed) if removed.isdigit() else 0,
            }

    st_out, _, _ = _run_git(
        ["-c", "core.quotePath=false", "status", "--porcelain", "--untracked-files=all", *pathspec],
        timeout=20,
    )
    for processed, line in enumerate(st_out.split("\n")):
        if processed >= 4000:
            break
        if not line.startswith("?? "):
            continue
        rel = line[3:].strip().strip('"')
        if not rel:
            continue
        added = _count_new_file_lines(_ACTIVE_REPO / rel)
        if added is not None:
            stats[rel] = {"added": added, "removed": 0}
    return stats


_LINE_STATS_CACHE_TTL_SECONDS = 10.0
_line_stats_cache_lock = threading.Lock()
_line_stats_cache_at = 0.0
_line_stats_cache: dict | None = None
_RUNTIME_PATHSPECS = (
    ":(exclude)data/agent_data/**",
    ":(exclude)data/user_data/**",
    ":(exclude)logs/**",
    ":(exclude)data/backups/**",
    ":(exclude)data/engine_state/**",
    ":(exclude).ast-index/**",
    ":(exclude,glob)**/*.db",
    ":(exclude,glob)**/*.db-wal",
    ":(exclude,glob)**/*.db-shm",
)


def _project_line_stats_payload() -> dict:
    """Compute Explorer badges without blocking the server event loop."""
    global _line_stats_cache, _line_stats_cache_at
    now = time.monotonic()
    if _line_stats_cache is not None and now - _line_stats_cache_at < _LINE_STATS_CACHE_TTL_SECONDS:
        return _line_stats_cache

    with _line_stats_cache_lock:
        now = time.monotonic()
        if _line_stats_cache is not None and now - _line_stats_cache_at < _LINE_STATS_CACHE_TTL_SECONDS:
            return _line_stats_cache

        stats: dict[str, dict[str, int]] = {}
        diff = subprocess.run(
            ["git", "-c", "core.quotePath=false", "diff", "--numstat", "--no-renames",
             "HEAD", "--", ".", *_RUNTIME_PATHSPECS],
            cwd=str(_PROJECT_ROOT), capture_output=True, text=True, timeout=20,
        )
        if diff.returncode == 0:
            for line in diff.stdout.splitlines():
                cols = line.split("\t")
                if len(cols) < 3 or (cols[0] == "-" and cols[1] == "-"):
                    continue
                stats[cols[2]] = {
                    "added": int(cols[0]) if cols[0].isdigit() else 0,
                    "removed": int(cols[1]) if cols[1].isdigit() else 0,
                }

        status = subprocess.run(
            ["git", "-c", "core.quotePath=false", "status", "--porcelain",
             "--untracked-files=all", "--", ".", *_RUNTIME_PATHSPECS],
            cwd=str(_PROJECT_ROOT), capture_output=True, text=True, timeout=20,
        )
        for processed, line in enumerate(status.stdout.splitlines()):
            if processed >= 4000:
                break
            if not line.startswith("?? "):
                continue
            rel = line[3:].strip().strip('"')
            added = _count_new_file_lines(_PROJECT_ROOT / rel) if rel else None
            if added is not None:
                stats[rel] = {"added": added, "removed": 0}

        payload = {
            "project_root": str(_PROJECT_ROOT).replace("\\", "/"),
            "stats": stats,
        }
        _line_stats_cache = payload
        _line_stats_cache_at = time.monotonic()
        return payload


@router.get("/line-stats")
async def get_line_stats(request: Request):
    """Per-file +/- line counts for everything changed since the last commit.

    Powers the green +N / red -N badges in the File Explorer tree. The returned
    map is keyed by **repo-relative path** and covers:
      • tracked edits — staged AND unstaged together, via `git diff --numstat
        HEAD`, so a badge reflects the whole working tree vs the last commit;
      • untracked files — counted as all-additions (a brand-new file reads +N).
    `--no-renames` keeps a rename as a delete (old path) + add (new path), so
    each side gets a clean per-path count instead of an `old => new` line to
    parse. `core.quotePath=false` stops git octal-escaping non-ASCII names so the
    paths match the tree's.

    Pinned to THIS app's own repo (the file tree's root) so the counts always
    line up with the tree even when Git Control has another repo selected.
    Read-only — the Files page is already admin-gated."""
    return await _run_git_job(_project_line_stats_payload)

    stats: dict[str, dict[str, int]] = {}

    # 1. Tracked changes vs HEAD (staged + unstaged combined). On a fresh repo
    #    with no commit yet this fails — tolerated; we still report untracked.
    out, _, rc = _run_git(
        ["-c", "core.quotePath=false", "diff", "--numstat", "--no-renames", "HEAD"],
        timeout=20,
    )
    if rc == 0:
        for line in out.split("\n"):
            line = line.rstrip("\n")
            if not line:
                continue
            cols = line.split("\t")
            if len(cols) < 3:
                continue
            added, removed, path = cols[0], cols[1], cols[2]
            if added == "-" and removed == "-":
                continue  # binary — no meaningful line count
            stats[path] = {
                "added": int(added) if added.isdigit() else 0,
                "removed": int(removed) if removed.isdigit() else 0,
            }

    # 2. Untracked files — every line is an addition. `-uall` lists files inside
    #    untracked directories individually (not just the directory name). Capped
    #    so a huge accidentally-untracked tree can't make this read forever.
    st_out, _, _ = _run_git(
        ["-c", "core.quotePath=false", "status", "--porcelain", "--untracked-files=all"],
        timeout=20,
    )
    root = _ACTIVE_REPO
    processed = 0
    for line in st_out.split("\n"):
        if not line.startswith("?? "):
            continue
        if processed >= 4000:
            break
        rel = line[3:].strip().strip('"')
        if not rel:
            continue
        added = _count_new_file_lines(root / rel)
        if added is not None:
            stats[rel] = {"added": added, "removed": 0}
        processed += 1

    return {"project_root": str(root).replace("\\", "/"), "stats": stats}


def _git_status_sets() -> tuple[list[str], list[str], bool]:
    """Run ls-files + status --ignored in the pinned repo and return
    (tracked, ignored, is_repo) as repo-relative paths. Ignored dirs are
    collapsed (status already emits the directory itself, not every file).

    One nuance: a path untracked via `git rm --cached` shows as a staged
    deletion ("D ") rather than an "!!" ignored entry until the deletion is
    committed — even though .gitignore now matches it. Those paths (and any
    ancestor folder whose rule makes them ignored) are folded into `ignored`
    so the tree dims them immediately instead of after the next commit."""
    tracked: list[str] = []
    ignored: list[str] = []
    is_repo = False

    out, _, rc = _run_git(
        ["-c", "core.quotePath=false", "ls-files"],
        timeout=20,
    )
    if rc == 0:
        is_repo = True
        for line in out.splitlines():
            rel = line.rstrip().strip('"')
            if rel:
                tracked.append(rel)

    out, _, rc = _run_git(
        ["-c", "core.quotePath=false", "status", "--porcelain", "--ignored"],
        timeout=20,
    )
    if rc == 0:
        is_repo = True
        for line in out.splitlines():
            if not line.startswith("!! "):
                continue
            rel = line[3:].strip().strip('"').rstrip("/")
            if rel:
                ignored.append(rel)

        # Fold staged deletions that are now check-ignored into the ignored
        # set (see the docstring). Capped so a huge rm --cached -r can't make
        # this read hang; the untracked side of the fold still works per-file.
        staged_del = []
        for line in out.splitlines():
            if line.startswith("D ") and len(line) > 3:
                staged_del.append(line[3:].strip().strip('"'))
        if staged_del and len(staged_del) <= 2000:
            cands = set(staged_del)
            for p in staged_del:
                parts = p.split("/")
                cands.update("/".join(parts[:i]) for i in range(1, len(parts)))
            chk_out, _, _ = _run_git(
                ["check-ignore", "--"] + sorted(cands),
                timeout=20,
            )
            if chk_out:
                ignored += [ln.strip() for ln in chk_out.splitlines() if ln.strip()]

    return tracked, ignored, is_repo


# ── Git tracked/ignored toggle (git-view checkboxes) ─────────────────────────
# Checking/unchecking a box in the File Explorer's git view edits the ACTUAL
# ignore rules and the git index, so the box state matches what git will do on
# the next commit:
#   • tracked=false → append an anchored rule to the root .gitignore, then
#     `git rm --cached` (untrack but keep the working file — never deleted);
#   • tracked=true  → drop exact .gitignore / info-exclude rules naming the
#     path, re-include any ignored ancestor directories via negation, then
#     `git add` (force-add as a backstop for patterns we can't rewrite).
# Only .gitignore files, .git/info/exclude and the index are touched.

def _repo_rel_safe(rel: str) -> str:
    """Normalise a user-supplied repo-relative path and reject anything that
    escapes the repo (`..`, absolute paths, drive letters)."""
    rel = (rel or "").strip().replace("\\", "/").lstrip("/")
    if not rel or rel in {".", ".."}:
        raise ValueError("path must be a repo-relative file or folder")
    if rel.startswith("/") or rel.startswith("../") or ".." in rel.split("/"):
        raise ValueError("path must stay inside the repo")
    if re.match(r"^[A-Za-z]:", rel):
        raise ValueError("path must stay inside the repo")
    return rel.rstrip("/")


def _path_ancestors(rel: str) -> list[str]:
    parts = rel.split("/")
    return ["/".join(parts[:i]) for i in range(1, len(parts))]


def _gitignore_files_for(rel: str) -> list[Path]:
    """Every .gitignore that can match the path: the repo root's plus one in
    each ancestor directory (a nested .gitignore can hold a matching rule)."""
    files = [_ACTIVE_REPO / ".gitignore"]
    cur = _ACTIVE_REPO
    for part in rel.split("/")[:-1]:
        cur = cur / part
        files.append(cur / ".gitignore")
    return files


def _info_exclude() -> Path:
    return _ACTIVE_REPO / ".git" / "info" / "exclude"


def _remove_ignore_rules(rel: str, is_dir: bool) -> None:
    """Delete exact .gitignore / info-exclude patterns naming this path (with
    or without a leading `!`, `rel`, `/rel`, and `rel/` variants). Glob rules
    like `*.log` are left alone — those get overridden by a negation instead."""
    pats = {rel, "/" + rel, "!" + rel, "!/" + rel}
    if is_dir:
        pats |= {rel + "/", "/" + rel + "/", "!" + rel + "/", "!/" + rel + "/"}
    for gf in _gitignore_files_for(rel) + [_info_exclude()]:
        if not gf.is_file():
            continue
        try:
            lines = gf.read_text(encoding="utf-8").splitlines()
        except (OSError, UnicodeDecodeError):
            continue
        kept = [ln for ln in lines if ln.strip() not in pats]
        if len(kept) != len(lines):
            try:
                gf.write_text("\n".join(kept) + ("\n" if kept else ""), encoding="utf-8")
            except OSError:
                pass


def _append_gitignore(gf: Path, lines: list[str]) -> None:
    """Append patterns to an ignore file, creating it if needed and skipping
    lines that are already present (idempotent)."""
    try:
        gf.parent.mkdir(parents=True, exist_ok=True)
        existing = gf.read_text(encoding="utf-8") if gf.is_file() else ""
        have = {ln.strip() for ln in existing.splitlines() if ln.strip()}
        add = [ln for ln in lines if ln not in have]
        if not add:
            return
        if existing and not existing.endswith("\n"):
            existing += "\n"
        gf.write_text(existing + "\n".join(add) + "\n", encoding="utf-8")
    except OSError:
        pass


def _nearest_gitignore(rel: str) -> Path:
    """The deepest existing .gitignore in the path's ancestor chain (a
    negation must live there to override a nested rule), else the root one."""
    files = _gitignore_files_for(rel)
    for gf in reversed(files):
        if gf.is_file():
            return gf
    return files[0]


def _make_tracked(rel: str, is_dir: bool) -> None:
    """Un-ignore + stage a path (or a whole folder subtree)."""
    target = rel + ("/" if is_dir else "")
    _remove_ignore_rules(rel, is_dir)
    # Re-check after removal: only add negations when a rule still matches
    # (a glob in another source, or an ignored ancestor directory).
    _, ignored_now, _ = _git_status_sets()
    ignored_set = set(ignored_now)
    if target.rstrip("/") in ignored_set or any(a in ignored_set for a in _path_ancestors(rel)):
        lines = ["!" + a + "/" for a in _path_ancestors(rel) if a in ignored_set]
        lines.append("!" + target)
        _append_gitignore(_nearest_gitignore(rel), lines)
    out, err, rc = _run_git(["add", "--", target], timeout=20)
    if rc != 0:
        # Backstop: force-add when some pattern we couldn't rewrite still
        # matches (e.g. a rule in core.excludesFile or a glob elsewhere).
        _run_git(["add", "-f", "--", target], timeout=20)


def _make_ignored(rel: str, is_dir: bool) -> None:
    """Add an ignore rule + untrack a path (or a whole folder subtree)."""
    _append_gitignore(_ACTIVE_REPO / ".gitignore",
                      ["/" + rel + ("/" if is_dir else "")])
    target = rel + ("/" if is_dir else "")
    _run_git(["rm", "--cached", "-r", "--", target], timeout=20)


@router.get("/git-status")
async def get_git_status(request: Request):
    """Tracked vs ignored paths for the File Explorer's git view.

    Returns two repo-relative path sets for the app's own repo (pinned like
    /line-stats so the marks always line up with the tree even when Source
    Control has another repo selected):

      • tracked — every path git tracks (`git ls-files`);
      • ignored — every path git ignores (`git status --porcelain --ignored`),
        with whole ignored directories collapsed to the directory itself so a
        huge ignored folder (node_modules, .venv, …) stays a single entry.

    Read-only — the Files page is already admin-gated."""
    def _payload():
        _pin_to_project_root()
        tracked, ignored, is_repo = _git_status_sets()
        return tracked, ignored, is_repo, str(_ACTIVE_REPO).replace("\\", "/")

    tracked, ignored, is_repo, repo_root = await _run_git_job(_payload)
    return {
        "repo_root": repo_root,
        "is_repo": is_repo,
        "tracked": tracked,
        "ignored": ignored,
    }


@router.post("/git-ignore")
async def set_git_ignore(req: GitIgnoreRequest, request: Request):
    """Flip one path's tracked/ignored state in the File Explorer's git view.

    `tracked=true`  → un-ignore + stage the path (remove exact .gitignore /
                      info-exclude rules, negate ignored ancestors, `git add`).
    `tracked=false` → append an anchored rule to the root .gitignore and
                      `git rm --cached` it (the working file is never deleted).

    Folders are whole-subtree: ignoring a folder adds `folder/` and untracks it
    recursively; tracking a folder stages everything under it that isn't
    matched by some other rule. Returns the fresh tracked/ignored sets so the
    tree can repaint without a reload.

    Read/write — the Files page is already admin-gated; only .gitignore files,
    .git/info/exclude and the git index are touched, never working-tree files.
    """
    _require_admin(request)
    def _update():
        _pin_to_project_root()
        try:
            rel = _repo_rel_safe(req.path)
        except ValueError as e:
            raise HTTPException(status_code=400, detail=str(e)) from e
        is_dir = (_ACTIVE_REPO / rel).is_dir()
        try:
            if req.tracked:
                _make_tracked(rel, is_dir)
            else:
                _make_ignored(rel, is_dir)
        except Exception as e:  # noqa: BLE001
            logger.warning("git-ignore toggle failed for %s: %s", rel, e)
            raise HTTPException(status_code=500,
                                detail=f"Could not update git ignore state: {e}") from e
        tracked, ignored, _ = _git_status_sets()
        return tracked, ignored, str(_ACTIVE_REPO).replace("\\", "/")

    tracked, ignored, repo_root = await _run_git_job(_update)
    return {
        "status": "ok",
        "repo_root": repo_root,
        "tracked": tracked,
        "ignored": ignored,
    }


@router.post("/push")
async def push_to_remote(request: Request):
    """Push commits to the remote."""
    _require_admin(request)
    await _prime_shared_token_from_vault()   # refresh the shared key from the vault
    await _run_git_job(_use_active_repo)

    # Run the push OFF the event loop with the same generous budget as
    # /commit-and-push (120s). On a busy machine (concurrent agent streams,
    # git subprocesses starved for CPU) a plain `git push` can take far longer
    # than the old 30s cap, and the old synchronous call blocked the entire
    # event loop for that duration — stalling every other request while it ran.
    stdout, stderr, rc = await _run_git_job(_git_push, 120)
    if rc != 0:
        raise HTTPException(status_code=500, detail=_push_failure_hint(stderr, stdout))

    return {
        "status": "pushed",
        "message": "Push successful.",
        "output": stdout.strip(),
    }


# ── One-shot: stage → (auto-write message) → commit → push ──────────────────
#
# Shared core for the Source-Control ⭐ button (POST /commit-and-push below) AND
# the Git Control ability's `commit_and_push` agent tool (plugins/admin/
# source_tools.py delegates here). One source of truth so the instant UI path
# and the agent path can never drift in safety checks, message style, or flow.

# Obvious secret shapes we refuse to commit. Not exhaustive — a backstop, not a
# substitute for .gitignore / pre-commit scanning.
_SECRET_PATTERNS = [
    (r'ghp_[A-Za-z0-9]{36}', 'GitHub personal access token'),
    (r'sk-or-v1-[A-Za-z0-9]{32,}', 'OpenRouter/OpenAI API key'),
    (r'sk-[A-Za-z0-9]{32,}', 'API key pattern'),
    (r'AKIA[0-9A-Z]{16}', 'AWS access key'),
    (r'-----BEGIN (?:RSA |EC )?PRIVATE KEY-----', 'Private key'),
]


# How long we'll wait for the auto commit-message before giving up and using a
# generic fallback note. Kept short on purpose: the ⭐ button must feel instant,
# and the message is a nice-to-have (the user can always type their own).
_COMMIT_LLM_TIMEOUT = 20.0


def _commit_llm_client():
    """Lazy LLM client for commit-message writing — same provider config the
    chat/compaction path uses (LLM_BASE_URL / LLM_API_KEY, OpenRouter default).

    Bounded for responsiveness: a short timeout and **no automatic client-side
    retries**. The openai client retries twice by default with backoff, which
    turned a single slow/flaky call into a 90s+ stall — the real reason the ⭐
    button appeared to hang. With max_retries=0 a bad call fails once and
    returns immediately; the caller (`_generate_commit_message`) bounds the whole
    sub-call with asyncio.wait_for and drops to a deterministic fallback on any
    failure, so flakiness never re-introduces the stall."""
    base_url = (
        os.environ.get("LLM_BASE_URL")
        or os.environ.get("OPENROUTER_BASE_URL")
        or ""
    )
    api_key = os.environ.get("LLM_API_KEY") or os.environ.get("OPENROUTER_API_KEY") or ""
    try:
        from openai import AsyncOpenAI
        return AsyncOpenAI(base_url=base_url, api_key=api_key,
                           timeout=_COMMIT_LLM_TIMEOUT, max_retries=0)
    except ImportError:  # pragma: no cover
        from app.openai_compat import AsyncOpenAI
        return AsyncOpenAI(base_url=base_url, api_key=api_key,
                           timeout=_COMMIT_LLM_TIMEOUT)


def _changed_files(stat_out: str, untracked: list[str]) -> list[str]:
    """Pull the changed-file paths out of a `git diff --stat` block (each file
    line is ` path | N +++/---`; the trailing ` N files changed …` summary line
    has no `|` so it's skipped) and append the untracked paths, de-duplicated and
    order-preserving."""
    files: list[str] = []
    for ln in (stat_out or "").splitlines():
        if "|" in ln:
            path = ln.split("|", 1)[0].strip()
            if path:
                files.append(path)
    files.extend(untracked or [])
    seen: set[str] = set()
    uniq: list[str] = []
    for f in files:
        if f not in seen:
            seen.add(f)
            uniq.append(f)
    return uniq


def _top_level_groups(files: list[str]) -> list[str]:
    """The top-level *area* for each path — the first path segment, or the bare
    filename for a root-level file — de-duplicated and order-preserving. Lets a
    many-file fallback name the areas that changed (`app, ui, docs`) when the
    individual paths are too long to list inside a 72-char title."""
    groups: list[str] = []
    seen: set[str] = set()
    for f in files:
        norm = f.replace("\\", "/").strip("/")
        head = norm.split("/", 1)[0] if "/" in norm else norm
        if head and head not in seen:
            seen.add(head)
            groups.append(head)
    return groups


def _fallback_commit_message(stat_out: str, untracked: list[str]) -> str:
    """A deterministic, *informative* commit message used whenever the LLM
    sub-call can't produce one (empty reply, timeout, API error).

    The title degrades gracefully so it stays meaningful instead of collapsing
    straight to a content-free count (the bug the user hit, where 3 long paths
    overflowed 72 chars and the title became a bare 'chore: update N files'):
      1. name the first few files outright (3 → 2), if they fit;
      2. else summarise by area — top-level dir/file — e.g. 'app, ui, docs';
      3. only as an absolute last resort, the bare count.
    The body always lists the actual files regardless of which title was chosen.
    """
    files = _changed_files(stat_out, untracked)
    if not files:
        return "chore: staged changes"
    if len(files) == 1:
        return f"chore: update {files[0]}"

    title = ""
    # 1) Name the first few files outright, if they fit the 72-char title.
    for n in (3, 2):
        if n > len(files):
            continue
        shown = ", ".join(files[:n])
        extra = len(files) - n
        candidate = f"chore: update {shown}"
        if extra > 0:
            candidate += f" and {extra} more file{'s' if extra != 1 else ''}"
        if len(candidate) <= 72:
            title = candidate
            break
    # 2) Paths too long to name — summarise by area instead of a bare count.
    if not title:
        groups = _top_level_groups(files)
        for g in (3, 2, 1):
            shown = ", ".join(groups[:g])
            candidate = f"chore: update {shown} ({len(files)} files)"
            if len(candidate) <= 72:
                title = candidate
                break
    # 3) Absolute last resort.
    if not title:
        title = f"chore: update {len(files)} files"

    body_files = files[:20]
    body = "\n".join(f"- {f}" for f in body_files)
    if len(files) > len(body_files):
        body += f"\n- ... and {len(files) - len(body_files)} more"
    return f"{title}\n\n{body}"


_STAT_MAX_FILE_LINES = 60


def _bound_stat(stat_out: str, max_lines: int = _STAT_MAX_FILE_LINES) -> str:
    """Cap a ``git diff --stat`` block so a huge changeset can't bury the real diff
    under a wall of filenames.

    A normal dev commit touches a handful of files, but a production-mirror release
    stages the WHOLE trimmed tree (~1000 files). Left un-capped, the ``--stat`` list
    becomes a ~50KB block of paths that dwarfs the 6KB diff window, pushing the
    commit-message sub-call past the model's context / its short timeout — so every
    release fell back to the deterministic "Release from dev…" line. Keep the first
    *max_lines* per-file lines plus git's trailing ``N files changed …`` summary,
    replacing the omitted files with a ``... (+K more files)`` marker. Small commits
    (≤ max_lines files) pass through unchanged."""
    lines = (stat_out or "").strip().splitlines()
    file_lines = [ln for ln in lines if "|" in ln]
    summary = [ln for ln in lines if "|" not in ln and ln.strip()]
    if len(file_lines) <= max_lines:
        return (stat_out or "").strip()
    kept = file_lines[:max_lines]
    omitted = len(file_lines) - len(kept)
    out = "\n".join(kept)
    out += f"\n  ... (+{omitted} more files)"
    if summary:
        out += "\n" + "\n".join(summary)
    return out


async def _generate_commit_message(stat_out: str, full_diff: str,
                                   untracked: list[str]) -> tuple[str, str, str]:
    """Write a conventional-commit message from the diff via a lightweight LLM
    sub-call. Returns ``(message, source, detail)``: ``source`` is ``"llm"`` when
    the model produced the note, or ``"fallback"`` (with ``detail`` = why) when it
    dropped to the deterministic file-list summary. The fallback is still a real,
    informative message — ``source`` just lets the caller TELL the user the
    auto-writer was skipped (the ⭐ progress UI shows "auto-message unavailable")."""
    fallback = _fallback_commit_message(stat_out, untracked)
    model = (
        os.environ.get("LLM_MODEL")
        or os.environ.get("OPENROUTER_MODEL")
        or ""
    )
    stat_window = _bound_stat(stat_out)
    diff_window = full_diff[:6000] if full_diff else "(no diff)"
    if full_diff and len(full_diff) > 6000:
        diff_window += f"\n... (truncated, {len(full_diff)} total bytes)"
    summary = (
        f"Changed files:\n{stat_window or 'N/A'}\n\n"
        "Untracked:\n"
        + ("\n".join(f"  {f}" for f in untracked) if untracked else "  (none)")
        + f"\n\nDiff:\n{diff_window}"
    )
    # One bounded attempt. The ⭐ button must feel instant, so the whole sub-call
    # is capped with asyncio.wait_for and we fall straight through to the
    # deterministic file-list fallback on ANY failure — timeout, empty reply, or a
    # connection blip. (No retry loop: retrying risks the very stall the timeout
    # exists to prevent, and the fallback is already a real, informative message.)
    try:
        client = _commit_llm_client()
        resp = await asyncio.wait_for(
            client.chat.completions.create(
                model=model,
                messages=[
                    {"role": "system", "content": (
                        "You generate concise git commit messages from a diff. "
                        "Output ONLY:\n"
                        "Line 1: conventional commit title (max 72 chars)\n"
                        "Line 2: blank\n"
                        "Line 3+: brief bullet-point body\n"
                        "Types: feat, fix, chore, refactor, docs, style, test, perf, ci"
                    )},
                    {"role": "user", "content": f"Generate a commit message for:\n\n{summary}"},
                ],
                max_tokens=300,
                temperature=0.3,
            ),
            timeout=_COMMIT_LLM_TIMEOUT,
        )
        try:
            _u = getattr(resp, "usage", None)
            if _u:
                from plugins.billing.usage import record_background_usage
                await record_background_usage(
                    model=model,
                    input_tokens=getattr(_u, "prompt_tokens", 0) or 0,
                    output_tokens=getattr(_u, "completion_tokens", 0) or 0,
                    label="commit",
                )
        except Exception:
            pass
        content = (resp.choices[0].message.content or "").strip()
        if content:
            return content, "llm", ""
        logger.warning("LLM commit-message: empty reply (model=%s) — using file-list fallback", model)
        return fallback, "fallback", "empty reply"
    except Exception as e:
        reason = str(e) or type(e).__name__
        logger.warning("LLM commit-message sub-call failed (%s) — using file-list fallback", reason)
        return fallback, "fallback", reason


async def _commit_and_push_events(message: str = "", *, skip_push: bool = False,
                                  include_untracked: bool = True):
    """Run the one-shot stage → commit → push and **yield a progress event before
    each phase**, so the UI can show granular, *honest* status (analysing →
    safety-check → writing message → committing → pushing) rather than one opaque
    spinner.

    Each event is a dict with a ``phase`` key; the FINAL event is always
    ``{"phase": "done", "result": <dict>}`` carrying the same structured result
    (status one of committed / nothing_to_commit / blocked / error) that callers
    expect. Phase labels are deliberately generic — the frontend owns the
    user-facing wording.

    This is the single source of truth: ``commit_and_push_repo`` (the agent tool +
    non-streaming endpoint) just drains it for the final result, and the streaming
    endpoint forwards every event — so the ⭐ button and the agent can't drift.

    The caller MUST point git at the right repo first: the UI route calls
    `_use_active_repo()` (the user's selected repo + key), the agent ability calls
    `_pin_to_project_root()` (always this app). We deliberately don't reset the
    token here so a non-default repo's key isn't clobbered back to the shared one.
    """
    # Every git step below runs through `asyncio.to_thread` so the blocking
    # `subprocess.run` happens on a worker thread, NOT the event loop. This is what
    # makes the NDJSON progress actually STREAM: a yielded phase event is only
    # flushed to the client when the loop gets a turn, and a synchronous git call
    # on the loop would hold it hostage for the whole commit+push — so the user saw
    # "Starting…" and then nothing until the (often slow) push finished. Running git
    # off-loop lets each step's event reach the browser the moment it's produced.
    async def _git(args, timeout=15):
        return await _run_git_job(_run_git, args, timeout=timeout)

    async def _git_retry(args, timeout=120):
        return await _run_git_job(_run_git_retry_on_lock, args, timeout=timeout)

    # 1. Repo state — bail early if clean.
    yield {"phase": "analyzing"}
    await asyncio.sleep(0)  # hand the loop a turn so the event flushes now
    status_out, status_err, status_code = await _git(["status", "--porcelain"], timeout=30)
    if status_code != 0:
        yield {"phase": "done", "result": {
            "status": "error", "message": f"git status failed: {status_err.strip()}"}}
        return
    if not status_out.strip():
        yield {"phase": "done", "result": {
            "status": "nothing_to_commit",
            "message": "Nothing to commit — working tree is clean."}}
        return

    # 2. Diffs (stat for the summary, full diff for the secret scan + LLM).
    #    Diff against HEAD, not the bare working tree, so ALREADY-STAGED changes
    #    are included — otherwise a pre-staged tree looks empty here and the
    #    auto-message/secret-scan see "(no diff)" while the commit below still
    #    captures everything. Fall back to the working-tree diff if there is no
    #    HEAD yet (brand-new repo with no commits).
    stat_out, _, stat_rc = await _git(["diff", "HEAD", "--stat"], timeout=30)
    full_diff, _, diff_rc = await _git(["diff", "HEAD"], timeout=30)
    if stat_rc != 0:
        stat_out, _, _ = await _git(["diff", "--stat"], timeout=30)
    if diff_rc != 0:
        full_diff, _, _ = await _git(["diff"], timeout=30)
    untracked = [ln.strip()[3:] for ln in status_out.strip().splitlines()
                 if ln.strip().startswith("?? ")]
    file_count = len(status_out.strip().splitlines())

    # 3. Safety: refuse to commit obvious secrets.
    yield {"phase": "scanning", "file_count": file_count}
    await asyncio.sleep(0)
    for pattern, label in _SECRET_PATTERNS:
        if re.search(pattern, full_diff or ""):
            yield {"phase": "done", "result": {
                "status": "blocked",
                "message": (f"Possible {label} found in the diff — commit aborted. "
                            "Remove it from the working tree and retry.")}}
            return

    # 4. Commit message: use the caller's, else auto-write one.
    if message and message.strip():
        commit_msg = message.strip()
        yield {"phase": "message", "source": "user",
               "title": commit_msg.split("\n")[0].strip()}
        await asyncio.sleep(0)
    else:
        yield {"phase": "message", "source": "auto"}
        await asyncio.sleep(0)
        commit_msg, msg_source, msg_detail = await _generate_commit_message(
            stat_out, full_diff, untracked)
        title = commit_msg.split("\n")[0].strip()
        if msg_source == "fallback":
            # Non-fatal: the LLM auto-writer couldn't produce a note, so we used
            # the deterministic file-list summary. Tell the user what happened.
            yield {"phase": "message_fallback", "title": title, "detail": msg_detail}
        else:
            yield {"phase": "message_ready", "title": title}
        await asyncio.sleep(0)

    # 5. Stage.
    yield {"phase": "committing"}
    await asyncio.sleep(0)
    # Large working trees (especially runtime DB/screenshot folders) can take
    # substantially longer than a status/diff read. Cutting this off at 30s was
    # the source of orphaned Windows git children and recurring index.lock errors.
    # Sweep any lock stranded by an external/crashed git process, then retry
    # briefly if another process (Codex app, parallel agent) wins the lock race.
    await _run_git_job(_sweep_orphaned_index_lock)
    _, add_err, add_code = await _git_retry(
        ["add", "-A" if include_untracked else "-u"], timeout=120)
    if add_code != 0:
        yield {"phase": "done", "result": {
            "status": "error", "message": f"git add failed: {add_err.strip()}"}}
        return

    # 6. Commit (via -F file so a multi-line body survives).
    msg_file = data_root() / "tmp" / "_commit_msg.txt"
    msg_file.parent.mkdir(parents=True, exist_ok=True)
    msg_file.write_text(commit_msg, encoding="utf-8")
    # Hooks are part of `git commit` and may legitimately take a while. Give them
    # the same budget as a network push. A subprocess timeout can also race with
    # commit completion, so remember HEAD and reconcile it before reporting an
    # error; otherwise a completed commit gets stranded without its push.
    before_head, _, _ = await _git(["rev-parse", "HEAD"], timeout=10)
    _, commit_err, commit_code = await _git_retry(
        ["commit", "-F", str(msg_file)], timeout=120)
    try:
        msg_file.unlink()
    except OSError:
        pass
    if commit_code == -1 and commit_err.strip() == "git command timed out":
        after_head, _, after_code = await _git(["rev-parse", "HEAD"], timeout=10)
        if after_code == 0 and after_head.strip() != before_head.strip():
            commit_code = 0
    if commit_code != 0:
        yield {"phase": "done", "result": {
            "status": "error", "message": f"git commit failed: {commit_err.strip()}"}}
        return

    head_out, _, _ = await _git(["rev-parse", "HEAD"], timeout=10)
    commit_hash = head_out.strip()[:12]
    commit_title = commit_msg.split("\n")[0].strip()

    # 7. Push (unless asked to hold off).
    if skip_push:
        push = {"attempted": False, "ok": None, "detail": "Skipped (commit only)."}
    else:
        yield {"phase": "pushing"}
        await asyncio.sleep(0)
        push_out, push_err, push_code = await _run_git_job(_git_push, timeout=120)
        push = {"attempted": True, "ok": push_code == 0,
                "detail": "Push successful." if push_code == 0
                else _push_failure_hint(push_err, push_out)}

    yield {"phase": "done", "result": {
        "status": "committed",
        "message_used": commit_msg,
        "title": commit_title,
        "hash": commit_hash,
        "push": push,
        "stat": stat_out.strip(),
        "file_count": file_count,
    }}


async def commit_and_push_repo(message: str = "", *, skip_push: bool = False,
                               include_untracked: bool = True) -> dict:
    """Stage all → (auto-write message if blank) → commit → push, in one shot.

    Returns a structured dict (status one of: committed / nothing_to_commit /
    blocked / error). Never raises for ordinary git failures — the caller decides
    how to surface them. Thin drain over ``_commit_and_push_events`` (the single
    source of truth): used by the Git Control agent tool and the non-streaming
    endpoint; the streaming endpoint forwards the generator's events directly.
    """
    result = {"status": "error", "message": "commit+push produced no result"}
    async for ev in _commit_and_push_events(
            message, skip_push=skip_push, include_untracked=include_untracked):
        if ev.get("phase") == "done":
            result = ev.get("result", result)
    return result


def _prune_commit_operations() -> None:
    """Drop completed reconnect records after their short recovery window."""
    now = time.monotonic()
    for operation_id, record in list(_COMMIT_OPERATIONS.items()):
        finished_at = record.get("finished_at")
        if finished_at is not None and now - finished_at > _COMMIT_OPERATION_TTL:
            _COMMIT_OPERATIONS.pop(operation_id, None)


def _start_commit_operation(message: str, *, skip_push: bool,
                            include_untracked: bool) -> tuple[str, dict]:
    """Start a commit job whose lifetime is independent of its HTTP stream."""
    _prune_commit_operations()
    operation_id = uuid.uuid4().hex
    record = {
        "queue": asyncio.Queue(),
        "last_event": None,
        "result": None,
        "finished_at": None,
        "task": None,
    }
    _COMMIT_OPERATIONS[operation_id] = record

    async def _run() -> None:
        try:
            async for event in _commit_and_push_events(
                    message, skip_push=skip_push,
                    include_untracked=include_untracked):
                record["last_event"] = event
                if event.get("phase") == "done":
                    record["result"] = event.get("result")
                await record["queue"].put(event)
        except asyncio.CancelledError:
            event = {"phase": "done", "result": {
                "status": "error",
                "message": ("Commit cancelled. A Git command that was already "
                            "running may still have completed; refresh Source "
                            "Control to verify the repository state."),
            }}
            record["last_event"] = event
            record["result"] = event["result"]
            await record["queue"].put(event)
        except Exception as e:  # noqa: BLE001
            logger.exception("commit-and-push operation crashed")
            event = {"phase": "done", "result": {
                "status": "error", "message": str(e) or "commit+push failed"}}
            record["last_event"] = event
            record["result"] = event["result"]
            await record["queue"].put(event)
        finally:
            record["finished_at"] = time.monotonic()
            await record["queue"].put(None)

    record["task"] = asyncio.create_task(
        _run(), name=f"git-commit-push:{operation_id[:8]}")
    return operation_id, record


@router.get("/commit-and-push/{operation_id}/status")
async def commit_and_push_status(operation_id: str, request: Request):
    """Return recovery state after the original NDJSON connection was lost."""
    _require_admin(request)
    _prune_commit_operations()
    record = _COMMIT_OPERATIONS.get(operation_id)
    if record is None:
        raise HTTPException(status_code=404, detail="Commit operation not found")
    task = record["task"]
    return {
        "status": "done" if task.done() else "running",
        "last_event": record.get("last_event"),
        "result": record.get("result"),
    }


@router.post("/commit-and-push/{operation_id}/cancel")
async def cancel_commit_and_push(operation_id: str, request: Request):
    """Cancel an explicitly user-aborted detached commit operation."""
    _require_admin(request)
    record = _COMMIT_OPERATIONS.get(operation_id)
    if record is None:
        raise HTTPException(status_code=404, detail="Commit operation not found")
    task = record["task"]
    if not task.done():
        task.cancel()
    return {"status": "cancel_requested" if not task.done() else "done"}


@router.post("/commit-and-push")
async def commit_and_push(req: CommitPushRequest, request: Request):
    """One-shot: stage all → auto-write a commit message (unless one is supplied)
    → commit → push. Backs the Source-Control ⭐ button so a click commits and
    pushes immediately instead of handing the job to a chat agent.

    With ``stream: true`` it returns a newline-delimited JSON (NDJSON) stream of
    progress events — ``{"phase": "analyzing"|"scanning"|"message"|"committing"|
    "pushing", …}`` and a final ``{"phase": "done", "result": {…}}`` — so the ⭐
    button can show granular live status. Without it, it runs to completion and
    returns the single result dict (back-compat; also the agent-tool behaviour).
    Errors in the streaming path arrive INSIDE the final ``done`` event (the UI
    shows them inline) rather than as an HTTP error code.
    """
    _require_admin(request)
    # Commit + push the repo the user currently has selected (with its own key).
    await _prime_shared_token_from_vault()   # refresh the shared key from the vault
    await _run_git_job(_use_active_repo)

    if req.stream:
        operation_id, operation = _start_commit_operation(
            req.message or "",
            skip_push=req.skip_push,
            include_untracked=req.include_untracked,
        )

        async def _event_stream():
            # Do not cancel the operation task if this observer disappears. The
            # browser can reconnect through /status; only the explicit /cancel
            # endpoint is allowed to stop the job.
            while True:
                ev = await operation["queue"].get()
                if ev is None:
                    break
                yield json.dumps(ev) + "\n"
        # no-cache + X-Accel-Buffering tell intermediaries (nginx, tunnels) NOT to
        # buffer the response, so the per-phase NDJSON lines arrive live instead of
        # all-at-once at the end.
        return StreamingResponse(
            _event_stream(),
            media_type="application/x-ndjson",
            headers={
                "Cache-Control": "no-cache, no-transform",
                "X-Accel-Buffering": "no",
                "X-Git-Operation-ID": operation_id,
            },
        )

    result = await commit_and_push_repo(
        req.message or "",
        skip_push=req.skip_push,
        include_untracked=req.include_untracked,
    )
    status = result.get("status")
    if status == "blocked":
        raise HTTPException(status_code=400, detail=result.get("message"))
    if status == "error":
        raise HTTPException(status_code=500, detail=result.get("message"))
    return result


def _is_backend_file(path: str) -> bool:
    """Heuristic: does this path touch code that requires a server restart?

    Anything under app/ or scripts/, plus root-level Python files, the
    requirements/lock files, and the FastAPI entrypoint. Frontend assets
    (ui/, index.html, README, docs) are picked up by a browser refresh.
    """
    if not path:
        return False
    p = path.strip()
    backend_dirs = ("app/", "data/", "scripts/", "migrations/", "tests/")
    if p.startswith(backend_dirs):
        return True
    backend_root_files = (
        "run.py", "pyproject.toml", "uv.lock", "requirements.txt",
    )
    return p in backend_root_files


def _head_hash() -> str:
    out, _, _ = _run_git(["rev-parse", "HEAD"], timeout=5)
    return out.strip()


def _files_changed_between(old: str, new: str) -> list[str]:
    """git diff --name-only old..new — empty list on error/no diff."""
    if not old or not new or old == new:
        return []
    out, _, rc = _run_git(["diff", "--name-only", f"{old}..{new}"], timeout=10)
    if rc != 0:
        return []
    return [line.strip() for line in out.split("\n") if line.strip()]


async def pull_repo() -> dict:
    """Fetch + fast-forward the CURRENTLY-ACTIVE repo (caller decides which repo is
    active via ``_use_active_repo`` / ``_pin_to_project_root`` first).

    The single source of truth for a pull: used by the ``/pull`` UI route AND the
    cross-device ``git_pull`` fleet action (app/devices/actions.py), so both behave
    identically. Returns a structured dict (status: pulled / error) and NEVER raises
    for an ordinary git failure — the caller decides how to surface it (the route
    turns an error into an HTTP 500; the device action lets the error land on the
    job row for the Instances page to poll).
    """
    return await _run_git_job(_pull_repo_payload)


def _pull_repo_payload() -> dict:
    before = _head_hash()
    stdout, stderr, rc = _run_git(["pull"], timeout=30)
    if rc != 0:
        detail = stderr.strip()
        if "Authentication failed" in stderr or "could not read" in stderr:
            detail += "\n\nSet your GitHub token in the File Manager sidebar (Git Control)."
        return {"status": "error", "message": detail or "git pull failed"}
    after = _head_hash()

    files = _files_changed_between(before, after)
    backend_changed = any(_is_backend_file(f) for f in files)

    return {
        "status": "pulled",
        "message": "Pull successful." if before != after else "Already up to date.",
        "output": stdout.strip(),
        "before": before[:7],
        "after": after[:7],
        "files_changed": files,
        "backend_changed": backend_changed,
    }


@router.post("/pull")
async def pull_from_remote(request: Request):
    """Pull from remote. Returns whether backend files changed so the
    caller can decide to restart the server.
    """
    _require_admin(request)
    await _prime_shared_token_from_vault()   # refresh the shared key from the vault
    await _run_git_job(_use_active_repo)

    result = await pull_repo()
    if result.get("status") == "error":
        raise HTTPException(status_code=500, detail=result.get("message"))
    return result


@router.get("/branches")
async def list_branches(request: Request):
    """List all branches the user can switch to or merge.

    Returns local branches plus remote-tracking branches (origin/*). For
    each, includes the short hash and whether a local checkout exists.
    """
    _require_admin(request)

    return await _run_git_job(_list_branches_payload)


def _list_branches_payload() -> dict:

    # Local branches
    local_out, _, _ = _run_git(
        ["for-each-ref", "--format=%(refname:short)\x1f%(objectname)", "refs/heads"],
        timeout=10,
    )
    local: dict[str, str] = {}
    for line in local_out.split("\n"):
        bits = line.strip().split("\x1f")
        if len(bits) == 2:
            local[bits[0]] = bits[1]

    # Remote branches
    remote_out, _, _ = _run_git(
        ["for-each-ref", "--format=%(refname:short)\x1f%(objectname)", "refs/remotes/origin"],
        timeout=10,
    )
    remotes: dict[str, str] = {}
    for line in remote_out.split("\n"):
        bits = line.strip().split("\x1f")
        if len(bits) == 2 and bits[0] != "origin/HEAD":
            # Strip the leading "origin/" to get the bare branch name
            bare = bits[0][len("origin/"):] if bits[0].startswith("origin/") else bits[0]
            remotes[bare] = bits[1]

    # Build the union
    names = sorted(set(local.keys()) | set(remotes.keys()))
    branches = []
    for name in names:
        branches.append({
            "name": name,
            "hash": (local.get(name) or remotes.get(name, ""))[:7],
            "local": name in local,
            "remote": name in remotes,
        })

    cur_out, _, _ = _run_git(["rev-parse", "--abbrev-ref", "HEAD"], timeout=5)
    return {"current": cur_out.strip(), "branches": branches}


def _working_tree_dirty() -> tuple[bool, list[str]]:
    """Returns (dirty, list-of-paths). Empty list when clean."""
    out, _, _ = _run_git(["status", "--porcelain"], timeout=10)
    paths = [line[3:] if len(line) > 3 else line for line in out.split("\n") if line.strip()]
    return (bool(paths), paths)


@router.post("/checkout")
async def checkout_branch(req: BranchRequest, request: Request):
    """Switch the working tree to a different branch.

    Refuses if the working tree has uncommitted changes (would clobber).
    If the requested branch only exists on the remote, creates a local
    tracking branch from origin/<name>.
    """
    _require_admin(request)

    branch = req.branch.strip()
    if not branch:
        raise HTTPException(status_code=400, detail="Branch name required.")

    return await _run_git_job(_checkout_branch_payload, branch)


def _checkout_branch_payload(branch: str) -> dict:
    _use_active_repo()

    dirty, dirty_paths = _working_tree_dirty()
    if dirty:
        raise HTTPException(
            status_code=409,
            detail=(
                "Working tree has uncommitted changes. Commit or discard them before switching branches.\n"
                + "\n".join(dirty_paths[:10])
                + ("\n…" if len(dirty_paths) > 10 else "")
            ),
        )

    # Local branch exists → straight checkout.
    # Otherwise, create a tracking branch from origin/<name>.
    _, _, rc_local = _run_git(["show-ref", "--verify", f"refs/heads/{branch}"], timeout=5)

    before = _head_hash()
    if rc_local == 0:
        stdout, stderr, rc = _run_git(["checkout", branch], timeout=20)
    else:
        # Verify the remote exists before trying
        _, _, rc_remote = _run_git(["show-ref", "--verify", f"refs/remotes/origin/{branch}"], timeout=5)
        if rc_remote != 0:
            raise HTTPException(status_code=404, detail=f"Branch '{branch}' not found locally or on origin.")
        stdout, stderr, rc = _run_git(["checkout", "-b", branch, f"origin/{branch}"], timeout=20)

    if rc != 0:
        raise HTTPException(status_code=500, detail=stderr.strip() or "git checkout failed")

    after = _head_hash()
    files = _files_changed_between(before, after)
    backend_changed = any(_is_backend_file(f) for f in files)

    return {
        "status": "checked-out",
        "branch": branch,
        "message": f"Switched to {branch}.",
        "files_changed": files,
        "backend_changed": backend_changed,
    }


@router.post("/merge")
async def merge_branch(req: BranchRequest, request: Request):
    """Merge another branch into the current branch.

    Aborts on conflict (no half-merged state left behind) and reports the
    conflicting files so the caller can show them.
    """
    _require_admin(request)

    branch = req.branch.strip()
    if not branch:
        raise HTTPException(status_code=400, detail="Branch name required.")

    return await _run_git_job(_merge_branch_payload, branch)


def _merge_branch_payload(branch: str) -> dict:
    _use_active_repo()

    dirty, dirty_paths = _working_tree_dirty()
    if dirty:
        raise HTTPException(
            status_code=409,
            detail=(
                "Working tree has uncommitted changes. Commit or discard them before merging.\n"
                + "\n".join(dirty_paths[:10])
                + ("\n…" if len(dirty_paths) > 10 else "")
            ),
        )

    # If the branch only exists on the remote, merge origin/<name> directly.
    _, _, rc_local = _run_git(["show-ref", "--verify", f"refs/heads/{branch}"], timeout=5)
    if rc_local == 0:
        merge_ref = branch
    else:
        _, _, rc_remote = _run_git(["show-ref", "--verify", f"refs/remotes/origin/{branch}"], timeout=5)
        if rc_remote != 0:
            raise HTTPException(status_code=404, detail=f"Branch '{branch}' not found locally or on origin.")
        merge_ref = f"origin/{branch}"

    before = _head_hash()
    stdout, stderr, rc = _run_git(
        ["merge", "--no-edit", merge_ref],
        timeout=30,
    )
    if rc != 0:
        # Try to roll back so we don't leave a partial merge for them to clean up
        _run_git(["merge", "--abort"], timeout=10)
        # Detect conflicts (this catches the common reason)
        conflict_out, _, _ = _run_git(["diff", "--name-only", "--diff-filter=U"], timeout=5)
        conflicts = [l.strip() for l in conflict_out.split("\n") if l.strip()]
        detail = stderr.strip() or stdout.strip() or "merge failed"
        if conflicts:
            detail = "Merge conflicts in:\n" + "\n".join(conflicts) + "\n\nMerge aborted — resolve conflicts in a terminal and try again."
        raise HTTPException(status_code=409, detail=detail)

    after = _head_hash()
    files = _files_changed_between(before, after)
    backend_changed = any(_is_backend_file(f) for f in files)

    return {
        "status": "merged",
        "branch": branch,
        "message": (
            f"Merged {branch} into HEAD." if before != after
            else f"Already up to date with {branch}."
        ),
        "output": stdout.strip(),
        "before": before[:7],
        "after": after[:7],
        "files_changed": files,
        "backend_changed": backend_changed,
    }


@router.post("/token")
async def set_token(req: TokenRequest, request: Request):
    """Store a GitHub personal access token for HTTPS auth.
    Single shared token — same for all users (the repo is shared).
    """
    _require_admin(request)
    await _save_shared_token(req.token)   # vault = source of truth (+ local mirror)
    logger.info("GitHub token saved")
    return {"status": "ok", "message": "GitHub token saved."}


@router.get("/token-status")
async def token_status():
    """Check if a GitHub token is configured. Primes from the vault first, so a
    token stored on another device sharing the vault is reflected here."""
    token = await _prime_shared_token_from_vault()
    return {
        "configured": bool(token),
        "masked": f"{token[:4]}****" if len(token) > 8 else ("****" if token else ""),
    }


@router.post("/remote-url")
# WHEN-REMOTE-URL-EDIT: Sets the git remote origin URL. Called from source
# control sidebar when the user double-clicks or long-presses the remote URL
# display and submits a new value. Runs `git remote set-url origin <url>`.
# Nav: ui/js/files-git.js wireEvents() → commitEditRemote() → POST this endpoint.
async def set_remote_url(req: RemoteUrlRequest, request: Request):
    """Change the git remote URL for origin (on the active repo)."""
    _require_admin(request)
    def _set_url():
        _use_active_repo()
        return _run_git(["remote", "set-url", "origin", req.url], timeout=10)

    stdout, stderr, rc = await _run_git_job(_set_url)
    if rc != 0:
        raise HTTPException(status_code=500, detail=stderr.strip() or "Failed to set remote URL")
    git_repos.invalidate_origin_cache()   # the built-in repo's cached origin just changed
    # Keep the registry's stored remote in sync when a non-built-in repo is active,
    # so the saved entry and the repo's git config don't drift apart. When the
    # built-in repo is active, mirror the new clean address into the shared vault.
    try:
        active = await _run_git_job(git_repos.get_active_full)
        if not active.get("builtin"):
            git_repos.update_repo(active["id"], remote_url=req.url)
        else:
            await _save_repo_url_to_vault(req.url)
    except Exception:  # noqa: BLE001
        pass
    logger.info("Remote origin URL changed to %s", req.url)
    return {"status": "ok", "message": "Remote URL updated.", "remote_url": req.url}


# ── Repositories (the Git Control multi-repo selector) ────────────────────
# The Git page can manage MORE than this app's own repo: point it at any local git
# folder paired with its own GitHub remote + key, and the Changes list + commit
# graph reflect THAT repo. The built-in WebAgent entry is always present and can't
# be removed; selecting it behaves exactly like before. Heavy lifting lives in
# app/git_repos.py. Selecting a repo re-points every git action on the page via
# `_use_active_repo()` (above). Route order matters: the literal `/repos/select`
# must precede the `/repos/{repo_id}` param route so "select" isn't swallowed as an id.

@router.get("/repos")
async def list_git_repos(request: Request):
    """All configured repos (WebAgent first), each with active/builtin flags. The
    raw key is never returned — only a ``has_token`` flag."""
    _require_admin(request)
    return {"repos": await _run_git_job(git_repos.list_repos)}


@router.get("/repos/validate")
async def validate_git_repo(request: Request, path: str = ""):
    """Check a folder is a git repo; return its branch + origin so the Add form can
    pre-fill them and warn early on a non-repo folder."""
    _require_admin(request)
    return await _run_git_job(git_repos.validate_folder, path)


@router.post("/repos")
async def add_git_repo(req: RepoAddRequest, request: Request):
    """Register a new local repo (folder + GitHub remote + key)."""
    _require_admin(request)
    try:
        entry = await _run_git_job(
            git_repos.add_repo,
            label=req.label, folder=req.folder,
            remote_url=req.remote_url, token=req.token,
            git_user_name=req.git_user_name,
            git_user_email=req.git_user_email,
        )
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))
    return {"status": "ok", "repo": entry,
            "repos": await _run_git_job(git_repos.list_repos)}


@router.post("/repos/select")
async def select_git_repo(req: RepoSelectRequest, request: Request):
    """Choose which repo the Git page operates on."""
    _require_admin(request)
    try:
        await _run_git_job(git_repos.set_active, req.id)
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))
    # Re-point immediately so a follow-up status/graph call uses the new repo.
    await _run_git_job(_use_active_repo)
    return {"status": "ok", "repos": await _run_git_job(git_repos.list_repos)}


def _set_builtin_origin(url: str) -> tuple[str, str, int]:
    _pin_to_project_root()
    stdout, stderr, rc = _run_git(["remote", "set-url", "origin", url], timeout=10)
    if rc != 0 and "no such remote" in (stderr or "").lower():
        stdout, stderr, rc = _run_git(["remote", "add", "origin", url], timeout=10)
    if rc != 0:
        _use_active_repo()
    return stdout, stderr, rc


def _set_builtin_identity(name: str, email: str) -> None:
    _pin_to_project_root()
    if name:
        _run_git(["config", "user.name", name], timeout=10)
    if email:
        _run_git(["config", "user.email", email], timeout=10)
    _use_active_repo()


@router.post("/repos/{repo_id}")
async def update_git_repo(repo_id: str, req: RepoUpdateRequest, request: Request):
    """Edit a repo. A blank token keeps the existing key.

    The built-in WebAgent repo is a special case: its folder is pinned to this
    app's own root and can't change, but its GitHub address (git ``origin``) and
    its shared GitHub key (now stored in the vault) ARE editable here — the same two
    things the standalone remote-URL / key fields already change, surfaced through
    the repo selector's Edit form for parity with added repos.
    """
    _require_admin(request)
    if repo_id == git_repos.BUILTIN_ID:
        # Edit the GitHub address (origin) on this app's own repo, regardless of
        # which repo is currently selected on the page.
        if req.remote_url is not None and req.remote_url.strip():
            stdout, stderr, rc = await _run_git_job(
                _set_builtin_origin, req.remote_url.strip()
            )
            if rc != 0:
                raise HTTPException(status_code=400,
                                    detail=stderr.strip() or "Failed to set remote URL")
            git_repos.invalidate_origin_cache()   # built-in origin just changed
            await _save_repo_url_to_vault(req.remote_url)   # share the new address via the vault
        # Save the shared GitHub key to the vault (blank = keep the existing one).
        if req.token:
            await _save_shared_token(req.token)
        # Set git user name/email on the built-in repo so commits don't fail.
        name = (req.git_user_name or "").strip() if req.git_user_name is not None else ""
        email = (req.git_user_email or "").strip() if req.git_user_email is not None else ""
        if name or email:
            await _run_git_job(_set_builtin_identity, name, email)
        else:
            await _run_git_job(_use_active_repo)
        return {"status": "ok", "repos": await _run_git_job(git_repos.list_repos)}
    try:
        entry = await _run_git_job(
            git_repos.update_repo,
            repo_id, label=req.label, folder=req.folder,
            remote_url=req.remote_url, token=req.token,
            git_user_name=req.git_user_name,
            git_user_email=req.git_user_email,
        )
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))
    return {"status": "ok", "repo": entry,
            "repos": await _run_git_job(git_repos.list_repos)}


@router.delete("/repos/{repo_id}")
async def delete_git_repo(repo_id: str, request: Request):
    """Forget a registered repo (the built-in WebAgent repo can't be removed)."""
    _require_admin(request)
    try:
        await _run_git_job(git_repos.remove_repo, repo_id)
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))
    await _run_git_job(_use_active_repo)
    return {"status": "ok", "repos": await _run_git_job(git_repos.list_repos)}


# ── Production mirror (dual-repo "Release to production") ────────────────────
# This dev repo can publish a TRIMMED copy of itself — everything except the
# folders an admin marked dev-only in the File Explorer — to a SEPARATE sibling
# production repo with its own GitHub remote. Git can't push "everything except
# folder X" (a commit carries the whole tree), so production is its own repo with
# its own history, regenerated on demand. The heavy lifting lives in
# app/production_mirror.py; these endpoints are the thin admin surface. The File
# Explorer reads/writes the exclude list through the same endpoints, so the Git
# page and the explorer share one source of truth.

@router.get("/production/config")
async def get_production_config(request: Request):
    """Production folder path, remote URL, exclude list, last-release record, plus
    a ``token_set`` flag (whether a GitHub key is stored — never the key itself)."""
    _require_admin(request)
    cfg = production_mirror.load_config()
    cfg["token_set"] = await production_mirror.token_is_set()
    return cfg


@router.post("/production/config")
async def set_production_config(req: ProdConfigRequest, request: Request):
    """Update the production folder path / remote URL / auto-release toggle, and
    (separately) the GitHub key into the encrypted vault. A blank key is ignored
    so it's never wiped by a save that didn't touch it."""
    _require_admin(request)
    cfg = production_mirror.save_config({
        "prod_folder": req.prod_folder,
        "prod_remote_url": req.prod_remote_url,
        "auto_release_on_push": req.auto_release_on_push,
    })
    if req.github_token:
        await production_mirror.save_vault_token(req.github_token)
    token_set = await production_mirror.token_is_set()
    return {"status": "ok", "config": cfg, "token_set": token_set}


@router.get("/production/exclude")
async def get_production_exclude(request: Request):
    """The exclude list (repo-relative folders) + the project root, so the File
    Explorer can map them to the absolute paths its rows carry."""
    _require_admin(request)
    cfg = production_mirror.load_config()
    return {
        "exclude_paths": cfg["exclude_paths"],
        "project_root": str(_PROJECT_ROOT).replace("\\", "/"),
    }


@router.post("/production/exclude")
async def set_production_exclude(req: ProdExcludeRequest, request: Request):
    """Mark one folder dev-only (or include it again). Shared by both pages."""
    _require_admin(request)
    try:
        cfg = production_mirror.set_excluded(req.path, req.excluded)
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))
    return {"status": "ok", "exclude_paths": cfg["exclude_paths"]}


@router.post("/production/exclude-bulk")
async def set_production_exclude_bulk(req: ProdExcludeBulkRequest, request: Request):
    """Apply many exclude-list changes at once (add + remove). Used by the File
    Explorer's tri-state folder checkbox to include/exclude a whole subtree in a
    single atomic write."""
    _require_admin(request)
    cfg = production_mirror.set_excluded_bulk(add=req.add, remove=req.remove)
    return {"status": "ok", "exclude_paths": cfg["exclude_paths"]}


@router.get("/production/status")
async def get_production_status(request: Request):
    """Lightweight status for the Production section of the Git page."""
    _require_admin(request)
    return await _run_git_job(production_mirror.production_status)


@router.get("/production/diff")
async def get_production_diff(request: Request):
    """How dev and the production folder differ right now — the added/updated/
    removed counts a one-way Sync would apply. Read-only; powers the File
    Explorer's More-menu 'in sync / N changes' line. Walks + byte-compares the
    shipping set, so it runs in a worker thread to keep the event loop free."""
    _require_admin(request)
    import asyncio
    return await _run_git_job(production_mirror.diff_status)


@router.post("/production/release")
async def release_to_production(req: ProdReleaseRequest, request: Request):
    """Regenerate the trimmed copy in the production folder, commit, and push it
    to the production GitHub remote. Streams NDJSON progress when stream=true."""
    _require_admin(request)

    if req.stream:
        async def _event_stream():
            try:
                async for ev in production_mirror.release_events(req.message or ""):
                    yield json.dumps(ev) + "\n"
            except Exception as e:  # noqa: BLE001
                logger.exception("release-to-production stream crashed")
                yield json.dumps({"phase": "done", "result": {
                    "status": "error",
                    "message": str(e) or "release failed"}}) + "\n"
        return StreamingResponse(
            _event_stream(),
            media_type="application/x-ndjson",
            headers={"Cache-Control": "no-cache, no-transform", "X-Accel-Buffering": "no"},
        )

    result = {"status": "error", "message": "release produced no result"}
    async for ev in production_mirror.release_events(req.message or ""):
        if ev.get("phase") == "done":
            result = ev.get("result", result)
    return result


async def _persist_prod_overrides(req: "ProdReleaseRequest") -> None:
    """Persist the folder + remote + key a Sync/Push request carries (the File
    Explorer's More-menu fields) before the action runs, so it always uses the
    freshest values even if the field's debounced auto-save hasn't landed yet.
    The folder + remote go to the local config; the GitHub key (if any) goes to
    the encrypted vault."""
    overrides = {}
    if req.prod_folder:
        overrides["prod_folder"] = req.prod_folder
    if req.prod_remote_url:
        overrides["prod_remote_url"] = req.prod_remote_url
    if overrides:
        production_mirror.save_config(overrides)
    if req.github_token:
        await production_mirror.save_vault_token(req.github_token)


@router.post("/production/copy")
async def copy_to_production(req: ProdReleaseRequest, request: Request):
    """First half of a release: build the trimmed copy in the production folder
    and commit it there LOCALLY (no push). The File Explorer's "Sync to
    production" calls this. Streams NDJSON progress when stream=true."""
    _require_admin(request)
    await _persist_prod_overrides(req)

    if req.stream:
        async def _event_stream():
            try:
                async for ev in production_mirror.copy_events(req.message or ""):
                    yield json.dumps(ev) + "\n"
            except Exception as e:  # noqa: BLE001
                logger.exception("copy-to-production stream crashed")
                yield json.dumps({"phase": "done", "result": {
                    "status": "error",
                    "message": str(e) or "copy failed"}}) + "\n"
        return StreamingResponse(
            _event_stream(),
            media_type="application/x-ndjson",
            headers={"Cache-Control": "no-cache, no-transform", "X-Accel-Buffering": "no"},
        )

    result = {"status": "error", "message": "copy produced no result"}
    async for ev in production_mirror.copy_events(req.message or ""):
        if ev.get("phase") == "done":
            result = ev.get("result", result)
    return result


@router.post("/production/push")
async def push_to_production(req: ProdReleaseRequest, request: Request):
    """Second half of a release: push the production folder's local commit to its
    GitHub remote. The File Explorer's "Push to GitHub" calls this. Streams NDJSON
    progress when stream=true. (The request's ``message`` is ignored here.)"""
    _require_admin(request)
    await _persist_prod_overrides(req)

    if req.stream:
        async def _event_stream():
            try:
                async for ev in production_mirror.push_events():
                    yield json.dumps(ev) + "\n"
            except Exception as e:  # noqa: BLE001
                logger.exception("push-to-production stream crashed")
                yield json.dumps({"phase": "done", "result": {
                    "status": "error",
                    "message": str(e) or "push failed"}}) + "\n"
        return StreamingResponse(
            _event_stream(),
            media_type="application/x-ndjson",
            headers={"Cache-Control": "no-cache, no-transform", "X-Accel-Buffering": "no"},
        )

    result = {"status": "error", "message": "push produced no result"}
    async for ev in production_mirror.push_events():
        if ev.get("phase") == "done":
            result = ev.get("result", result)
    return result

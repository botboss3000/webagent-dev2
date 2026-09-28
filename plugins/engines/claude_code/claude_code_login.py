"""
In-app sign-in for the Local Claude Code engine.

Lets an admin link their Claude account from the Claude agent's own config card —
no terminal needed — and stores a LONG-LIVED token so the agent keeps working
instead of the standalone CLI login expiring (the recurring 401 cause).

How it works (mirrors what `claude setup-token` does in a terminal, driven here):
  1. start_login() spawns `claude setup-token` inside a real pseudo-terminal
     (WinPty — setup-token refuses to run without one) made deliberately WIDE so
     the authorize URL is printed on a single, un-wrapped line. Claude auto-opens
     the browser AND prints the link; we scrape the link and KEEP that exact
     process alive (it holds the one-time PKCE secret the pasted code is bound to),
     keyed by a login_id.
  2. The user authorizes in their browser, copies the short code Claude shows, and
     submit_code() writes it back into that same live process, which exchanges it
     for the long-lived token and prints it. We capture the token, store it via the
     app's secrets backend (encrypted if a vault is configured), and kill the PTY.
  3. The engine adapter (plugins/engines/claude_code/claude_code.py) reads the stored token
     and injects it as CLAUDE_CODE_OAUTH_TOKEN on every turn, so Claude uses this
     long-lived login regardless of the keychain's daily-expiring OAuth.

Windows-only in practice (this host): if pywinpty is unavailable the sign-in
returns a friendly "not supported here" rather than raising.

NOTE: this module deliberately exposes NO ENGINE_ID / stream(), so the engine
auto-discovery in app/agent/loop.py never treats it as an engine.
Breadcrumbs: app/api/claude_auth.py (the HTTP routes that call this),
ui/main-panel/agents/js/claude-agent.js (the Sign in button + code field).
REMOVE-WHEN: the Local Claude Code engine (claude_code) is dropped.
"""

import asyncio
import json
import logging
import os
import re
import shutil
import threading
import time
import uuid
from typing import Any, Dict, List, Optional, Tuple

logger = logging.getLogger(__name__)

# Secrets-store key the long-lived token lives under (single Claude login per
# install — every Local Claude Code agent shares it). Because the secrets backend
# is the app's shared vault, this login is visible to EVERY device that shares the
# database — sign in once, all devices see it.
_SECRET_KEY = "claude_code_oauth_token"
# Companion marker recording HOW the saved token was obtained:
#   "setup-token" = the durable, long-lived in-app sign-in token (never overwritten)
#   "cli-harvest" = a copy of this device's own standalone CLI login (shorter-lived,
#                   so it is kept refreshed from that device while its login is valid)
_SECRET_SRC_KEY = "claude_code_oauth_token_src"

# Abandoned sign-ins (URL fetched, code never submitted) are reaped after this.
_LOGIN_TTL = 300.0

_URL_RE = re.compile(r"https?://[^\s\"'<>]*oauth/authorize[^\s\"'<>]*")
# setup-token long-lived tokens look like sk-ant-oat01-… ; match the family.
_TOKEN_RE = re.compile(r"sk-ant-[A-Za-z0-9._\-]{20,}")
# Strip the TUI's ANSI/OSC control sequences so our regexes see plain text.
_ANSI_RE = re.compile(
    r"\x1b\][^\x07\x1b]*(?:\x07|\x1b\\)"      # OSC … BEL/ST
    r"|\x1b[\[\]][0-9;?]*[A-Za-z]"            # CSI
    r"|\x1b[=>NOP()][AB0]?"                   # misc two/three-byte
)


# ── Live-login registry ─────────────────────────────────────────────────────

class _Login:
    """One in-flight setup-token process + the output a reader thread accrues."""
    def __init__(self, proc: object) -> None:
        self.proc = proc
        self.buf: List[str] = []
        self.lock = threading.Lock()
        self.created = time.time()
        self.alive = True


_logins: Dict[str, _Login] = {}
_reg_lock = threading.Lock()


def _clean(s: str) -> str:
    s = _ANSI_RE.sub("", s)
    return "".join(c for c in s if c >= " " or c in "\n\t")


def _text(login: _Login) -> str:
    with login.lock:
        raw = "".join(login.buf)
    return _clean(raw)


def _reader(login: _Login) -> None:
    """Drain the PTY into login.buf until the process ends/closes."""
    p = login.proc
    while True:
        try:
            chunk = p.read(4096)        # type: ignore[attr-defined]
        except EOFError:
            break
        except Exception:
            break
        if chunk:
            with login.lock:
                login.buf.append(chunk)
        else:
            time.sleep(0.05)
    login.alive = False


def _kill(login: _Login) -> None:
    try:
        login.proc.terminate(force=True)   # type: ignore[attr-defined]
    except Exception:
        pass


def _drop(login_id: str) -> None:
    with _reg_lock:
        login = _logins.pop(login_id, None)
    if login:
        _kill(login)


def _reap() -> None:
    now = time.time()
    with _reg_lock:
        dead = [lid for lid, lg in _logins.items() if now - lg.created > _LOGIN_TTL]
        victims = [_logins.pop(lid) for lid in dead]
    for lg in victims:
        _kill(lg)


# ── claude resolution + spawn (blocking; run via asyncio.to_thread) ──────────

def _resolve_claude() -> Tuple[Optional[List[str]], Optional[str]]:
    """argv prefix to spawn claude with, or (None, error). On Windows `claude` is
    a .CMD shim that just calls bin/claude.exe — spawn that real exe directly (a
    .CMD needs cmd.exe, which mangles quoting under WinPty)."""
    claude = shutil.which("claude")
    if not claude:
        return None, "the `claude` command was not found on PATH"
    if claude.lower().endswith((".cmd", ".bat")):
        cand = os.path.join(os.path.dirname(claude), "node_modules",
                            "@anthropic-ai", "claude-code", "bin", "claude.exe")
        if os.path.isfile(cand):
            return [cand], None
        return ["cmd.exe", "/c", claude], None
    return [claude], None


def _spawn_blocking() -> Tuple[Optional[_Login], Optional[str]]:
    try:
        from winpty import PtyProcess
    except Exception:
        return None, ("the terminal backend (pywinpty) isn't available on this "
                      "machine, so in-app Claude sign-in can't run here")
    argv, err = _resolve_claude()
    if err:
        return None, err
    try:
        proc = PtyProcess.spawn(list(argv) + ["setup-token"])
        try:
            proc.setwinsize(50, 1000)   # wide → authorize URL won't soft-wrap
        except Exception:
            pass
    except Exception as e:
        return None, f"couldn't launch the Claude sign-in ({e})"
    login = _Login(proc)
    threading.Thread(target=_reader, args=(login,), daemon=True).start()
    return login, None


# ── Public API (async) ──────────────────────────────────────────────────────

async def start_login() -> dict:
    """Begin sign-in: spawn setup-token, return the authorize link + a login_id.
    The process is held alive until submit_code()/cancel_login()/reap."""
    _reap()
    login, err = await asyncio.to_thread(_spawn_blocking)
    if err or login is None:
        return {"ok": False, "error": err or "couldn't start sign-in"}

    url: Optional[str] = None
    browser = False
    for _ in range(110):                 # ~22s for the URL to appear
        txt = _text(login)
        if "opening browser" in txt.lower():
            browser = True
        m = _URL_RE.search(txt)
        if m:
            url = m.group(0)
            break
        if not login.alive:
            break
        await asyncio.sleep(0.2)

    if not url:
        _kill(login)
        return {"ok": False,
                "error": "Claude's sign-in didn't return a link in time.",
                "detail": _text(login)[-400:]}

    login_id = uuid.uuid4().hex
    with _reg_lock:
        _logins[login_id] = login
    return {"ok": True, "login_id": login_id, "authorize_url": url,
            "browser_opened": browser}


async def submit_code(login_id: str, code: str) -> dict:
    """Finish sign-in: feed the pasted code to the held process, capture the
    long-lived token, store it, and tear the process down."""
    with _reg_lock:
        login = _logins.get(login_id)
    if not login:
        return {"ok": False,
                "error": "This sign-in timed out. Click “Sign in to Claude” again."}
    code = (code or "").strip()
    if not code:
        return {"ok": False, "error": "Paste the code Claude showed you first."}

    def _write() -> bool:
        try:
            login.proc.write(code + "\r")   # type: ignore[attr-defined]
            return True
        except Exception:
            return False

    if not await asyncio.to_thread(_write):
        _drop(login_id)
        return {"ok": False, "error": "Couldn't hand the code to the sign-in."}

    token: Optional[str] = None
    for _ in range(200):                 # ~40s for the exchange to print a token
        txt = _text(login)
        m = _TOKEN_RE.search(txt)
        if m:
            token = m.group(0)
            break
        if not login.alive:
            m = _TOKEN_RE.search(_text(login))
            if m:
                token = m.group(0)
            break
        await asyncio.sleep(0.2)

    final_txt = _text(login)
    _drop(login_id)

    if token:
        try:
            await _store_token(token)
        except Exception as e:
            return {"ok": False, "error": f"Signed in, but saving the token failed: {e}"}
        return {"ok": True,
                "message": "Signed in to Claude — this long-lived login is saved, "
                           "so the agent should keep working from now on."}

    low = final_txt.lower()
    if any(k in low for k in ("success", "logged in", "you can now", "saved", "complete")):
        # Claude reported success but didn't print a token we could capture; it
        # stored its own credential locally. Copy that local login up into the
        # SHARED vault so this sign-in still reaches the user's other devices.
        shared = await _harvest_cli_token_to_vault()
        return {"ok": True,
                "message": "Signed in to Claude." + (
                    " This login is now shared with all your devices."
                    if shared else
                    " (Saved on this device — sign in on your other devices too.)"),
                "note": "no_token_captured"}
    return {"ok": False, "error": "Claude didn't accept that code.",
            "detail": final_txt.strip()[-500:]}


async def cancel_login(login_id: str) -> dict:
    _drop(login_id)
    return {"ok": True}


# ── Token storage + status ───────────────────────────────────────────────────

async def _store_token(token: str, source: str = "setup-token") -> None:
    from app.secrets import get_secrets
    s = get_secrets()
    await s.set(_SECRET_KEY, token)
    # Record where the token came from so auth_status knows whether it may refresh
    # it (a harvested CLI token) or must leave it alone (a durable in-app token).
    try:
        await s.set(_SECRET_SRC_KEY, source)
    except Exception:
        pass


async def _stored_token_source() -> str:
    try:
        from app.secrets import get_secrets
        return (await get_secrets().get(_SECRET_SRC_KEY)) or ""
    except Exception:
        return ""


async def get_stored_oauth_token() -> Optional[str]:
    """The saved long-lived token (for the adapter to inject), or None."""
    try:
        from app.secrets import get_secrets
        val = await get_secrets().get(_SECRET_KEY)
        return val or None
    except Exception:
        return None


async def _harvest_cli_token_to_vault(source: str = "cli-harvest") -> bool:
    """Copy THIS device's own standalone Claude login (from ~/.claude/.credentials.json)
    into the SHARED vault so every other device that shares the database sees the same
    sign-in. Best-effort; returns True if a token was stored.

    NOTE: the CLI's own OAuth token is shorter-lived than the in-app "Sign in to
    Claude" (setup-token) token — this only BRIDGES an existing terminal/desktop
    login across devices; the durable cross-device login still comes from the
    in-app button. Callers gate WHEN to call this; the store itself is
    unconditional so it can also refresh a previously-harvested token."""
    try:
        tok = await asyncio.to_thread(_cli_access_token)
        if not tok:
            return False
        await _store_token(tok, source)
        return True
    except Exception:
        return False


async def sign_out() -> dict:
    """Forget the in-app token (does not touch claude's own keychain login)."""
    try:
        from app.secrets import get_secrets
        s = get_secrets()
        await s.delete(_SECRET_KEY)
        try:
            await s.delete(_SECRET_SRC_KEY)
        except Exception:
            pass
    except Exception as e:
        return {"ok": False, "error": str(e)}
    return {"ok": True}


def _cli_login_state() -> dict:
    """Best-effort read of the standalone CLI's own stored login — informational,
    so the UI can show 'signed in' even when the link was done in a terminal."""
    path = os.path.join(os.path.expanduser("~"), ".claude", ".credentials.json")
    try:
        with open(path, "r", encoding="utf-8") as f:
            data = json.load(f)
        oa = data.get("claudeAiOauth") or {}
        exp = oa.get("expiresAt")
        exp_s = (exp / 1000.0) if isinstance(exp, (int, float)) and exp > 1e12 else exp
        valid = bool(exp_s) and float(exp_s) > time.time()
        return {"present": bool(oa), "valid": bool(valid)}
    except Exception:
        return {"present": False, "valid": False}


# ── Plan usage (best-effort, undocumented) ──────────────────────────────────
# The 5-hour and weekly rolling-limit utilisation shown by Claude Code's own
# `/usage` command. The CLI does NOT emit it in its automation output, so we ask
# Anthropic's OAuth usage endpoint directly with the local login's access token.
# This endpoint is UNDOCUMENTED and may change/disappear — every failure path
# returns {"available": False} so the footer panel just hides the section. The
# URL is overridable via CLAUDE_CODE_USAGE_URL so it can be corrected without a
# code change once the real shape is confirmed.

_USAGE_URL = os.environ.get(
    "CLAUDE_CODE_USAGE_URL", "https://api.anthropic.com/api/oauth/usage")
_USAGE_CACHE: Dict[str, Any] = {"at": 0.0, "data": None}
_USAGE_TTL = 60.0  # seconds — don't hammer the endpoint on rapid panel opens


def _cli_access_token() -> Optional[str]:
    """The CLI's own (unexpired) OAuth access token from ~/.claude/.credentials.json
    — the credential that works against claude.ai/anthropic OAuth endpoints."""
    path = os.path.join(os.path.expanduser("~"), ".claude", ".credentials.json")
    try:
        with open(path, "r", encoding="utf-8") as f:
            data = json.load(f)
        oa = data.get("claudeAiOauth") or {}
        tok = oa.get("accessToken")
        exp = oa.get("expiresAt")
        exp_s = (exp / 1000.0) if isinstance(exp, (int, float)) and exp > 1e12 else exp
        if tok and (not exp_s or float(exp_s) > time.time()):
            return str(tok)
    except Exception:
        pass
    return None


def _usage_window(block: Any) -> Optional[dict]:
    """Normalise one usage window to {percent, resets_at} from a few shapes
    (utilization fraction/percent, used+limit, or used+remaining)."""
    if isinstance(block, (int, float)):
        block = {"utilization": block}
    if not isinstance(block, dict):
        return None
    pct = None
    for k in ("utilization", "percent", "percent_used", "used_pct", "usage"):
        v = block.get(k)
        if isinstance(v, (int, float)):
            pct = float(v) * 100.0 if float(v) <= 1.0 else float(v)
            break
    if pct is None and isinstance(block.get("limit"), (int, float)):
        try:
            lim = float(block["limit"])
            if lim > 0:
                pct = float(block.get("used") or 0) / lim * 100.0
        except Exception:
            pct = None
    if pct is None and isinstance(block.get("remaining"), (int, float)) \
            and isinstance(block.get("used"), (int, float)):
        tot = float(block["remaining"]) + float(block["used"])
        pct = (float(block["used"]) / tot * 100.0) if tot else None
    resets = (block.get("resets_at") or block.get("reset_at")
              or block.get("resetsAt") or block.get("reset"))
    if pct is None and not resets:
        return None
    return {"percent": round(max(0.0, min(100.0, pct)), 1) if pct is not None else None,
            "resets_at": resets}


def _find_usage_window(obj: Any, needles: Tuple[str, ...]) -> Optional[dict]:
    """Depth-first search for the first key matching a needle whose value is a
    usable usage window — tolerant of the endpoint's exact field naming."""
    if isinstance(obj, dict):
        for k, v in obj.items():
            if any(n in str(k).lower() for n in needles):
                w = _usage_window(v)
                if w:
                    return w
        for v in obj.values():
            w = _find_usage_window(v, needles)
            if w:
                return w
    elif isinstance(obj, list):
        for v in obj:
            w = _find_usage_window(v, needles)
            if w:
                return w
    return None


async def get_plan_usage() -> dict:
    """Best-effort Claude subscription usage (5-hour + weekly rolling windows).
    Returns {"available": False} on ANY failure so the UI hides the section."""
    now = time.time()
    cached = _USAGE_CACHE.get("data")
    if cached is not None and (now - _USAGE_CACHE.get("at", 0.0)) < _USAGE_TTL:
        return cached
    out = {"available": False}
    token = _cli_access_token() or (await get_stored_oauth_token())
    if not token:
        return out
    try:
        import httpx
        headers = {
            "Authorization": f"Bearer {token}",
            "anthropic-beta": "oauth-2025-04-20",
            "anthropic-version": "2023-06-01",
            "Content-Type": "application/json",
        }
        async with httpx.AsyncClient(timeout=6.0) as client:
            resp = await client.get(_USAGE_URL, headers=headers)
        if resp.status_code != 200:
            return out
        data = resp.json()
    except Exception:
        return out
    five = _find_usage_window(data, ("five_hour", "five-hour", "fivehour",
                                     "5h", "5_hour", "hour"))
    weekly = _find_usage_window(data, ("seven_day", "seven-day", "sevenday",
                                       "7d", "7_day", "week"))
    if not five and not weekly:
        return out
    out = {"available": True, "five_hour": five, "weekly": weekly}
    _USAGE_CACHE["data"] = out
    _USAGE_CACHE["at"] = now
    return out


async def auth_status() -> dict:
    """For the config UI: is the agent able to sign in?  token_saved = our stored
    long-lived token; cli = claude's own login state; signed_in = either works.
    default_folder = the app's own project directory, which the config card pre-fills
    as the Working folder (and the engine falls back to when none is set)."""
    saved = bool(await get_stored_oauth_token())
    cli = await asyncio.to_thread(_cli_login_state)
    # Keep the SHARED vault in step with this device's own Claude login, so a sign-in
    # done on ONE device is visible (and usable) on every device that shares the DB:
    #   • nothing saved yet, but this box is logged in → copy it up (share it)
    #   • the saved token came from that copy-up path   → refresh it while this box's
    #     login is valid (a harvested CLI token is shorter-lived than the in-app one)
    # A durable in-app "Sign in to Claude" token (source "setup-token") is left alone.
    if cli.get("valid"):
        try:
            if not saved:
                if await _harvest_cli_token_to_vault():
                    saved = True
            elif (await _stored_token_source()) == "cli-harvest":
                await _harvest_cli_token_to_vault()
        except Exception:
            pass
    try:
        from app.util.paths import project_root
        default_folder = project_root()
    except Exception:
        default_folder = ""
    return {"token_saved": saved, "cli": cli,
            "signed_in": bool(saved or cli.get("valid")),
            "default_folder": default_folder}

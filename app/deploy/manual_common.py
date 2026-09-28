"""Shared helpers for MANUAL deploy targets (Windows, macOS, …).

A "manual" deploy target installs WebAgent onto a device the admin already owns by
handing back a single copy-paste command — no cloud account, nothing billable
(see ``app/deploy/base.BaseDeployProvider.manual``). Every such target needs the
SAME pre-command logic before its platform-specific shell line: tidy the repo URL
the admin typed, and — for a private repo — splice an access token into the clone
URL so the device can fetch it. That logic lives here so each platform provider
(``app/deploy/providers/windows.py``, ``app/deploy/providers/macos.py``) only has
to describe its own one-liner.

The Linux/Termux target (``app/deploy/providers/termux.py``) predates this module
and keeps its own copy of the same helpers; the browser mirrors all of them in
``ui/main-panel/app/settings/data-settings/deploy.js`` so the command box renders
LIVE even before the server is restarted.
"""

from __future__ import annotations

from typing import Any, Dict

# When the admin hasn't typed a repository, the command installs the STANDARD
# WebAgent repository — so the box always shows a command that is ready to run as
# is. The admin only overrides this for their own fork. Mirror of deploy.js
# `_MC_DEFAULT_REPO`.
DEFAULT_REPO = "https://github.com/botboss3000/webagent"
# Still shown when a PRIVATE repo is chosen but no token is typed yet, so the
# command renders its shape; an obvious "fill me in" token.
PLACEHOLDER_TOKEN = "YOUR_ACCESS_TOKEN"

# Characters that would break the password out of the command's single-quoted
# (POSIX) / quoted (PowerShell) embedding. A password with any of these is NOT
# spliced in — we fall back to first-visitor setup and nudge. Mirror of deploy.js
# `_MC_BAD_PW`.
BAD_PW = "'\"" + "`" + "$\\\n\r"

# Default install folder per platform — where the repo is cloned and run from when
# the admin doesn't choose one. POSIX expands ``$HOME`` inside the double quotes we
# wrap it in; Windows expands ``$env:USERPROFILE`` in PowerShell. Mirror of
# deploy.js `_MC_DEFAULT_DIR_POSIX` / `_MC_DEFAULT_DIR_WINDOWS`.
DEFAULT_DIR_POSIX = "$HOME/webagent"
DEFAULT_DIR_WINDOWS = "$env:USERPROFILE\\webagent"

# Characters that never legitimately appear in a repo URL / token but would break
# (or worse, inject into) the one-liner if spliced in. Used to reject risky input.
BAD_URL = "'\";\n\r\\ &|`$(){}<>"
BAD_TOKEN = "'\";\n\r\\ &|`$(){}<>@/ "
# Characters that would break the one-liner's quoting / structure if they appeared
# in an install folder. Looser than BAD_URL because a real path legitimately
# contains spaces, ``\``, ``:``, ``$`` (``$HOME``) and ``%`` (``%USERPROFILE%``) —
# we only reject shell metacharacters. Mirror of deploy.js `_MC_BAD_DIR`.
BAD_DIR = "\"'" + "`" + ";\n\r|&<>(){}*?"


def _safe(value: str, fallback: str) -> str:
    """Tidy a repo/branch the admin typed. The command runs on the admin's own
    machine, but a URL/branch never legitimately contains shell-breaking
    characters, so fall back to the default rather than splice anything risky.
    """
    v = (value or "").strip() or fallback
    if any(c in v for c in BAD_URL):
        return fallback
    return v


def strip_scheme(url: str) -> str:
    for s in ("https://", "http://"):
        if url.startswith(s):
            return url[len(s):]
    return url


def resolve_dir(install_dir: str, default: str) -> str:
    """Tidy the install folder the admin typed. Blank → the platform default;
    anything with a shell metacharacter (which a real path never needs) → also the
    default, so a stray character can't break the one-liner. Mirror of deploy.js
    `_mcResolveDir`."""
    d = (install_dir or "").strip()
    if not d:
        return default
    if any(c in d for c in BAD_DIR):
        return default
    return d


def resolve_admin(admin_password: str = "") -> Dict[str, Any]:
    """Resolve the Deploy panel's optional "Admin password" for a manual install —
    shared by every platform provider (only the shell prefix that carries it into
    the setup script differs per platform). NEVER errors (the row renders live).

    Returns ``{prewire, password, warning}``:
      * password left BLANK (the default) → ``prewire`` False, no shell prefix: the
        first visitor to the installed app sets the password via the setup page
        (which is open until an admin exists, so this works from another device on
        the same network too — how a phone install is usually reached).
      * password TYPED → ``prewire`` True: the setup script bakes
        ``BOOTSTRAP_ADMIN_PASSWORD`` into the install's ``.env`` (``password``
        carries it) so the admin is created on first boot. An unsafe password
        (characters that would break the command's quoting) is refused — we fall
        back to first-visitor setup and warn rather than splice something risky.

    Mirror of deploy.js ``_mcResolveAdmin``.
    """
    pw = (admin_password or "").strip()
    if not pw:
        return {"prewire": False, "password": "", "warning": ""}
    if any(c in pw for c in BAD_PW):
        return {"prewire": False, "password": "",
                "warning": ("That password contains characters that can't be placed in the "
                            "command safely — use letters, digits and simple punctuation, or "
                            "leave it blank to let the first visitor set it instead.")}
    warning = "" if len(pw) >= 6 else "The admin password should be at least 6 characters."
    return {"prewire": True, "password": pw, "warning": warning}


def safe_bootstrap_code(bootstrap_code: str = "") -> str:
    """Sanity-gate a WABOOT1 setup code before it is spliced into a shell command.

    A real code is ``WABOOT1.<b64url>.<token>`` — base64url characters and dots
    only. If anything else shows up (a quote, whitespace, a shell metacharacter)
    we refuse it rather than let it break out of the single-quoted embedding — the
    command falls back to a bare install (no ``WA_BOOTSTRAP_CODE``). Defensive: the
    generator never produces such a string, but a pasted command runs on a device
    we don't control."""
    c = (bootstrap_code or "").strip()
    if not c:
        return ""
    if any(ch in c for ch in "'\"\n\r \t`$\\|&;<>(){}"):
        return ""
    return c


def env_prefix_posix(admin: Dict[str, Any], bootstrap_code: str = "") -> str:
    """The leading ``VAR='val' `` env assignments carried into a POSIX setup script:
    the optional pre-set admin password (``WA_ADMIN_PW``) and, when configuration is
    embedded, the locked setup code (``WA_BOOTSTRAP_CODE``). Empty when neither is
    set. With no code this is byte-identical to the old admin-only prefix, so the
    browser's live command mirror still matches the bare case."""
    parts = []
    if admin.get("prewire"):
        parts.append("WA_ADMIN_PW='" + admin["password"] + "'")
    code = safe_bootstrap_code(bootstrap_code)
    if code:
        parts.append("WA_BOOTSTRAP_CODE='" + code + "'")
    return (" ".join(parts) + " ") if parts else ""


def env_prefix_windows(admin: Dict[str, Any], bootstrap_code: str = "") -> str:
    """PowerShell counterpart of :func:`env_prefix_posix`: leading
    ``$env:VAR='val'; `` assignments the child powershell inherits. Empty when
    neither is set; byte-identical to the old admin-only prefix with no code."""
    parts = []
    if admin.get("prewire"):
        parts.append("$env:WA_ADMIN_PW='" + admin["password"] + "';")
    code = safe_bootstrap_code(bootstrap_code)
    if code:
        parts.append("$env:WA_BOOTSTRAP_CODE='" + code + "';")
    return (" ".join(parts) + " ") if parts else ""


def resolve_clone(github_url: str, visibility: str = "public", token: str = "") -> Dict[str, Any]:
    """Work out the clone target from the admin's inputs — shared by every manual
    platform provider (only the surrounding shell command differs per platform).

    NEVER errors: the row shows the command live as the admin types. A blank
    repository means "install the standard WebAgent repository", so the command is
    ALWAYS ready to run as is — the admin only types a URL to install their own
    fork. A private repo with no token yet still renders the command's shape via an
    obvious fill-in token, flagged so the row can nudge the admin to finish it.

    Returns ``{repo, clone_url, clean_url, private, default_repo,
    placeholder_token, warning}``. ``clone_url`` is what goes into ``git clone`` —
    for a PRIVATE repo the access token is embedded (``https://TOKEN@host/...``) so
    the device can fetch it; the token is never stored. ``clean_url`` is the same
    address WITHOUT any token — what the deployed box's git ``origin`` should be
    reset to after cloning, so the PAT is never persisted in its ``.git/config``.
    """
    typed = (github_url or "").strip()
    default_repo = not typed
    warning = ""
    if default_repo:
        repo = DEFAULT_REPO
    elif any(c in typed for c in BAD_URL):
        # A typo'd / risky URL: fall back to the standard repo rather than splice
        # something that could break the command, and say so.
        repo = DEFAULT_REPO
        default_repo = True
        warning = ("That repository address isn't a valid URL — using the standard "
                   "WebAgent repository instead.")
    else:
        repo = typed
    private = str(visibility or "public").lower() == "private"

    clone_url = repo
    placeholder_token = False
    if private:
        tok = (token or "").strip()
        if tok and any(c in tok for c in BAD_TOKEN):
            warning = "That token contains characters that aren't valid in a GitHub token."
            tok = ""
        if not tok:
            tok = PLACEHOLDER_TOKEN
            placeholder_token = True
        # repo is always https-shaped here (a real https URL or the default), so
        # the splice reads naturally.
        clone_url = "https://" + tok + "@" + strip_scheme(repo)

    # The clean address (no token) — safe to persist as the box's git origin. Drop
    # any ``userinfo@`` in the authority so an already-tokenized input can't leak.
    rest = strip_scheme(repo)
    at, slash = rest.find("@"), rest.find("/")
    if at != -1 and (slash == -1 or at < slash):
        rest = rest[at + 1:]
    clean_url = ("https://" + rest) if repo else repo
    return {"repo": repo, "clone_url": clone_url, "clean_url": clean_url,
            "private": private, "default_repo": default_repo,
            "placeholder_token": placeholder_token, "warning": warning}

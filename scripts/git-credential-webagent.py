#!/usr/bin/env python3
"""Git credential helper for WebAgent.

Lets plain ``git fetch`` / ``pull`` / ``push`` against github.com authenticate
with the app's shared GitHub token — the same encrypted-vault credential
(``app/deploy/credentials.py``, service ``deploy_github_token``) that the
Source Control and Deploy panels use. Without this, CLI git on a headless box
falls through to an interactive prompt (or the host's Credential Manager) and
hangs or fails with "could not read Username".

Git invokes a credential helper with an action argument (``get`` / ``store`` /
``erase``) and reads a ``key=value`` request on stdin:
``protocol=https``, ``host=github.com``, blank line. For ``get`` we answer
with ``username=x-access-token`` + ``password=<token>`` ONLY for github.com
over HTTPS, so git never prompts and the token is never written by git.

Token resolution order (same sources the app uses):
  1. The encrypted vault via ``app.deploy.credentials.get_github_token()`` —
     the durable, DB-backed credential (source of truth).
  2. ``data/config/provider.json`` — the app's own plaintext mirror/fallback
     for sync git paths, if the vault is unreachable from this subprocess.

``store`` / ``erase`` are no-ops: the token is deliberately never persisted by
git itself (matching ``app/api/github.py``'s one-shot-header design).

Register with:  git config --global credential.helper \
    "!/opt/webagent/.venv/bin/python3 /opt/webagent/scripts/git-credential-webagent.py"
"""

import asyncio
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent


def _vault_token() -> str:
    """The shared GitHub token from the encrypted vault ('' if unavailable)."""
    try:
        sys.path.insert(0, str(ROOT))
        from app.deploy import credentials

        return str(asyncio.run(credentials.get_github_token()) or "")
    except Exception:
        return ""


def _mirror_token() -> str:
    """Fallback: the app's plaintext provider.json mirror of the vault key."""
    try:
        p = ROOT / "data" / "config" / "provider.json"
        if p.is_file():
            data = json.loads(p.read_text(encoding="utf-8"))
            return str(data.get("github_token") or "")
    except Exception:
        pass
    return ""


def main() -> int:
    action = sys.argv[1] if len(sys.argv) > 1 else "get"
    if action != "get":
        return 0  # store/erase: never persist the token
    payload = sys.stdin.read() if not sys.stdin.isatty() else ""
    host, protocol = "", ""
    for line in payload.splitlines():
        key, _, value = line.partition("=")
        if key == "host":
            host = value.strip()
        elif key == "protocol":
            protocol = value.strip()
    # Only answer for github.com over HTTPS; anything else → no credentials
    # (let git keep its normal behavior for other hosts).
    if host != "github.com" or protocol != "https":
        return 0
    token = _vault_token() or _mirror_token()
    if not token:
        return 0
    sys.stdout.write(f"username=x-access-token\npassword={token}\n")
    return 0


if __name__ == "__main__":
    sys.exit(main())

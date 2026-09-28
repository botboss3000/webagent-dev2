"""Deploy target: a Mac.

Like the Linux/Termux target (``app/deploy/providers/termux.py``), there is no
machine to CREATE here — the "server" is a Mac the admin already owns. So this
target never connects anywhere: it GENERATES one command the admin pastes into
**Terminal** once. The command makes sure git is present (triggering the macOS
Command Line Tools install if needed), clones the repo to ``$HOME/webagent``, then
runs the repo's ``deploy/macos-setup.sh`` — which builds a venv, installs the
Playwright-free dependency set (``req_no_playwright.txt``), and installs a
**launchd** agent so WebAgent runs in the background, restarts itself if it stops,
and starts again when the user logs in.

This is a "manual" target (``manual = True``): no cloud key, nothing billable,
nothing to tear down remotely. The flag tells the Deploy panel to render this as a
bespoke row (its own GitHub-URL + public/private + token fields, live command +
QR) instead of the cloud-provider dropdown. The shared clone-URL logic lives in
``app/deploy/manual_common.py``; the browser mirrors ``build_command`` in
``deploy.js`` so the box renders live before any server restart.
"""

from __future__ import annotations

from typing import Any, AsyncIterator, Dict, List, Tuple

from app.deploy import manual_common as mc
from app.deploy.base import BaseDeployProvider, done, ev
from app.deploy.bootstrap import DEFAULT_BRANCH, DEFAULT_REPO_URL

FEATURE = {
    "id": "macos",
    "display_name": "macOS",
    "category": "deploy",
    "status": "beta",
    "summary": "Install WebAgent on a Mac by pasting one command into Terminal.",
    "requires": ["A Mac (macOS)", "~2 GB free storage"],
}

# The Terminal one-liner. ``__CLONE__`` / ``__BRANCH__`` / ``__DIR__`` are
# substituted below (NOT str.format — the script is full of `$var`, `{}`, quotes
# that would collide). If the folder already holds a clone it re-points its origin
# at the chosen repo and lets macos-setup.sh pull (a graceful update); only a
# missing folder is cloned fresh. Pure ASCII so it survives any terminal / QR.
# Keep BYTE-IDENTICAL to deploy.js `_buildMac`.
_TEMPLATE = (
    "set -e; D=\"__DIR__\"; "
    "if ! command -v git >/dev/null 2>&1; then "
    "echo 'Installing the Command Line Tools (a dialog may appear)...'; "
    "xcode-select --install 2>/dev/null || true; "
    "echo 'If a dialog appeared, finish it, then paste this command again.'; fi; "
    "{ if [ -d \"$D/.git\" ]; then git -C \"$D\" remote set-url origin __CLONE__; "
    "else git clone --depth 1 --branch __BRANCH__ __CLONE__ \"$D\"; fi; } && "
    # __ADMIN__ carries the optional pre-set admin password into macos-setup.sh as a
    # leading env assignment (WA_ADMIN_PW); it is empty when left to the first visitor.
    "__ADMIN__bash \"$D/deploy/macos-setup.sh\""
)

# Run-only command: start the server when WebAgent is ALREADY installed — no
# clone, no rebuild. Static (no repo URL / token). (Re)loads the launchd agent the
# installer wrote (which also starts it at login); falls back to a kickstart if
# it's already loaded. Folder-independent (launchd), so it takes no directory.
# Keep BYTE-IDENTICAL to deploy.js `_runMac`.
RUN_COMMAND = (
    "launchctl load -w \"$HOME/Library/LaunchAgents/com.webagent.server.plist\" 2>/dev/null "
    "|| launchctl kickstart -k \"gui/$(id -u)/com.webagent.server\""
)


class MacProvider(BaseDeployProvider):
    id = "macos"
    display_name = "macOS"
    icon = "laptop"
    summary = ("Install WebAgent on a Mac. You copy one command and paste it into "
               "Terminal — there is no cloud account and nothing to pay for.")
    requires = [
        "A Mac running macOS",
        "About 2 GB of free storage",
        "A network connection, if you want to reach the app from another device",
    ]
    # No cloud key, nothing billable, install is copy-paste — the panel adapts.
    manual = True

    # The Deploy panel renders a BESPOKE row for this target (its own fields +
    # live command + QR), not the generic provider form, so these are empty — the
    # row drives `build_command()` via POST /admin/deploy/command. The non-secret
    # github_url + visibility persist via the store; the token is transient.
    config_fields: List[Dict[str, Any]] = []
    credential_fields: List[Dict[str, Any]] = []
    credential_required: List[str] = []

    def available(self) -> Tuple[bool, str]:
        return True, ""

    # ── command generation (the single source of truth) ──
    def build_command(self, github_url: str, visibility: str = "public",
                      token: str = "", branch: str = "", install_dir: str = "",
                      admin_password: str = "", bootstrap_code: str = "") -> Dict[str, Any]:
        """Build everything the Mac row needs. ALWAYS succeeds (never
        ``{ok: False}``) — see ``manual_common.resolve_clone`` for why. Returns
        ``{ok, command, run_command, clone_display, steps, instructions,
        reach_note, private, default_repo, placeholder_token, warning, prewire}``."""
        r = mc.resolve_clone(github_url, visibility, token)
        branch = mc._safe(branch, DEFAULT_BRANCH)
        directory = mc.resolve_dir(install_dir, mc.DEFAULT_DIR_POSIX)
        # Optional pre-set admin password → a leading env assignment before the
        # setup script (POSIX); empty when blank. BYTE-IDENTICAL to deploy.js `_buildMac`.
        a = mc.resolve_admin(admin_password)
        # __ADMIN__ carries the pre-set admin password AND, when configuration is
        # embedded, the locked WA_BOOTSTRAP_CODE that macos-setup.sh writes to
        # bootstrap.json. Byte-identical to deploy.js `_buildMac` in the bare case.
        admin_prefix = mc.env_prefix_posix(a, bootstrap_code)
        command = (_TEMPLATE.replace("__CLONE__", r["clone_url"])
                   .replace("__BRANCH__", branch).replace("__DIR__", directory)
                   .replace("__ADMIN__", admin_prefix))
        # A display copy that hides the token (for any logging / non-QR display).
        clone_display = command.replace(r["clone_url"], r["repo"]) if r["private"] else command

        steps = [
            "Open Terminal: press Cmd+Space, type 'Terminal', and open it.",
            "Paste the command and press Enter. (The first time, macOS may ask to install the "
            "Command Line Tools — allow it, then paste the command again.)",
            "The first run takes a few minutes while it installs everything.",
            "When it finishes, open http://localhost:8080 on this Mac, or http://THIS-MAC-IP:8080 "
            "from another device on the same network.",
        ]
        instructions = (
            "WebAgent installs into a folder in your home directory and runs in the background via "
            "launchd — it starts automatically when you log in and restarts itself if it stops. To "
            "stop it: 'launchctl unload ~/Library/LaunchAgents/com.webagent.server.plist'. To start "
            "it again: 'launchctl load -w ~/Library/LaunchAgents/com.webagent.server.plist'.")
        reach_note = "http://localhost:8080 on this Mac · http://THIS-MAC-IP:8080 on the same network"
        warning = " ".join(w for w in (r["warning"], a["warning"]) if w)
        return {"ok": True, "command": command, "clone_display": clone_display,
                "run_command": RUN_COMMAND, "install_dir": directory,
                "steps": steps, "instructions": instructions, "reach_note": reach_note,
                "private": r["private"], "default_repo": r["default_repo"],
                "placeholder_token": r["placeholder_token"], "warning": warning,
                "prewire": a["prewire"]}

    # ── test (nothing to connect to) ──
    async def test(self, config: Dict[str, Any], creds: Dict[str, Any]) -> Dict[str, Any]:
        return {"ok": True,
                "detail": "macOS installs by pasting a command into Terminal — there is nothing to test."}

    # ── deploy = stream the (public) command, for the generic contract ──
    async def deploy(self, config: Dict[str, Any], creds: Dict[str, Any]) -> AsyncIterator[Dict[str, Any]]:
        plan = self.build_command(config.get("github_url") or DEFAULT_REPO_URL,
                                  config.get("visibility") or "public")
        yield ev("Command ready — paste it into Terminal on the Mac.", phase="ready", level="ok")
        yield done({"ok": True, "server": "", "state": "manual",
                    "command": plan["command"], "instructions": plan["instructions"],
                    "public_url": "", "message": "Install command generated."})

    async def destroy(self, config: Dict[str, Any], creds: Dict[str, Any],
                      record: Dict[str, Any]) -> AsyncIterator[Dict[str, Any]]:
        yield done({"ok": True, "deleted": False,
                    "message": "WebAgent runs on your own Mac — stop it with: "
                               "launchctl unload ~/Library/LaunchAgents/com.webagent.server.plist"})


PROVIDER = MacProvider()

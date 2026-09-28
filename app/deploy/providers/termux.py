"""Deploy target: a Linux computer, or an Android phone/tablet via Termux.

Unlike the cloud targets, there is no machine to CREATE here — the "server" is a
device the admin already owns (a Linux box, or an Android device running the free
Termux app), which is normally NOT reachable from this server. So this target
never connects anywhere: clicking *Deploy* GENERATES one copy-paste command the
admin runs once in a terminal (or in Termux).

Termux is essentially a small Linux userland on Android, so ONE command works in
both places — it detects which it is on. The only Termux-specific "customization"
is that a phone can't reliably build WebAgent's heavy native dependencies
(sqlcipher, compiled wheels), so on Termux the command installs everything inside
an Ubuntu ``proot-distro`` sandbox; on a plain Linux box it installs straight onto
the system. Both paths live in the repo's ``deploy/termux-setup.sh``, which builds
the venv + installs the Playwright-free dependency set (``req_no_playwright.txt``)
and keeps the server alive in the background with auto-restart (plus a wake-lock on
a phone, via ``start_server_termux.sh``).

This is a "manual" target (``manual = True``): no cloud key, nothing billable,
nothing to tear down remotely. The flag tells the Deploy panel to drop the
cloud-key section + the billable-resource confirm dialog, and to render the
generated command in a copy-to-clipboard box instead of a "last server" line.
"""

from __future__ import annotations

from typing import Any, AsyncIterator, Dict, List, Tuple

from app.deploy import manual_common as mc
from app.deploy.base import BaseDeployProvider, done, ev
from app.deploy.bootstrap import DEFAULT_BRANCH, DEFAULT_REPO_URL

FEATURE = {
    "id": "termux",
    "display_name": "Linux / Termux",
    "category": "deploy",
    "status": "beta",
    "summary": "Install WebAgent on a Linux computer, or on an Android phone/tablet via Termux.",
    "requires": ["A Linux computer, or the free Termux app on Android", "~2 GB free storage"],
}

_PORT = 8080  # run.py is fixed to :8080

def _run_command(directory: str) -> str:
    """Start the server when WebAgent is ALREADY installed on the device — no
    clone, no rebuild. Detects however the install set things up and starts it the
    matching way: Termux → the proot keep-alive launcher; a systemd Linux box → the
    webagent.service (folder-independent); otherwise the nohup keep-alive loop in
    the install folder. Keep BYTE-IDENTICAL to deploy.js `_runTermux`."""
    return (
        'if [ -n "$TERMUX_VERSION" ] || [ -d /data/data/com.termux ]; then '
        'bash "' + directory + '/start_server_termux.sh"; '
        'elif command -v systemctl >/dev/null 2>&1 && systemctl list-unit-files webagent.service >/dev/null 2>&1; then '
        'sudo systemctl start webagent; '
        'else bash "' + directory + '/deploy/start_server_linux.sh" "' + directory + '"; fi'
    )


class TermuxProvider(BaseDeployProvider):
    id = "termux"
    display_name = "Linux / Termux"
    icon = "terminal"
    summary = ("Install WebAgent on a Linux computer, or on an Android phone or tablet "
               "using the free Termux app. You copy one command and paste it into a "
               "terminal (or Termux) — there is no cloud account and nothing to pay for.")
    requires = [
        "A Linux computer, or the free Termux app on an Android device",
        "About 2 GB of free storage",
        "A network connection, if you want to reach the app from another device",
    ]
    # No cloud key, nothing billable, install is copy-paste — the panel adapts.
    manual = True

    # The Deploy panel renders a BESPOKE row for Termux (its own GitHub-URL +
    # public/private + token fields, terminal command + QR), not the generic
    # provider form, so these lists are intentionally empty — the row drives
    # `build_command()` below via POST /admin/deploy/command (generic over every
    # manual target). The non-secret github_url + visibility persist via the
    # store; the token is transient.
    config_fields: List[Dict[str, Any]] = []
    credential_fields: List[Dict[str, Any]] = []      # no cloud key
    credential_required: List[str] = []

    def available(self) -> Tuple[bool, str]:
        return True, ""

    # ── command generation (the single source of truth) ──
    def build_command(self, github_url: str, visibility: str = "public",
                      token: str = "", branch: str = "", install_dir: str = "",
                      admin_password: str = "", bootstrap_code: str = "") -> Dict[str, Any]:
        """Build everything the phone row needs from the admin's inputs.

        ALWAYS succeeds (never ``{ok: False}``): the row shows this command live as
        the admin types. A blank repository installs the STANDARD WebAgent
        repository (so the command is ready to run as is); a private repo with no
        token yet renders the command's shape via an obvious fill-in token, flagged
        so the row can nudge. The install folder defaults to ``$HOME/webagent`` and
        can be overridden.

        Returns ``{ok: True, command, run_command, install_dir, clone_display,
        steps, instructions, reach_note, private, default_repo, placeholder_token,
        warning}``. The command installs git, then — if the folder already holds a
        clone — re-points its origin at the chosen repo and lets
        ``deploy/termux-setup.sh`` pull it (a graceful update); otherwise it clones
        fresh. The setup script then does the Ubuntu/venv build + keep-alive. For a
        PRIVATE repo the access token is embedded in the clone URL so the device can
        fetch it; the token is never stored here.
        """
        r = mc.resolve_clone(github_url, visibility, token)
        clone_url = r["clone_url"]
        branch = mc._safe(branch, DEFAULT_BRANCH)
        directory = mc.resolve_dir(install_dir, mc.DEFAULT_DIR_POSIX)

        # The optional pre-set admin password, carried into the setup script via a
        # leading env assignment (POSIX). Blank → no prefix at all (the first visitor
        # sets the password). Keep BYTE-IDENTICAL to deploy.js `_buildTermux`.
        a = mc.resolve_admin(admin_password)
        # The env prefix carries the optional pre-set admin password AND, when the
        # "Include this app's configuration" toggle is on, a password-locked WABOOT1
        # setup code (WA_BOOTSTRAP_CODE) that termux-setup.sh writes to bootstrap.json
        # for first-boot provisioning. The code is base64url + dots (no shell
        # metacharacters), so single-quoting is safe. Only the BARE case (no code)
        # must stay byte-identical to deploy.js `_buildTermux`; the embedded case is
        # server-built only (the browser has no access to the secrets).
        admin_prefix = mc.env_prefix_posix(a, bootstrap_code)

        # ONE command for both Termux and plain Linux: install git with whatever
        # package manager is present (Termux's `pkg`, or apt/dnf/pacman with sudo
        # on a Linux box — sudo only when not already root). If the folder already
        # holds a clone, re-point its origin at the chosen repo (the setup script
        # then pulls) — a graceful update; otherwise clone fresh. Then hand off to
        # the setup script, which detects Termux vs Linux. `:` is a no-op for the
        # "git already installed" case. Keep BYTE-IDENTICAL to deploy.js `_buildTermux`.
        command = (
            'SUDO=; [ "$(id -u 2>/dev/null)" = 0 ] || SUDO=sudo; '
            f'D="{directory}"; '
            'if command -v git >/dev/null 2>&1; then :; '
            'elif command -v pkg >/dev/null 2>&1; then pkg install -y git; '
            'elif command -v apt-get >/dev/null 2>&1; then $SUDO apt-get update && $SUDO apt-get install -y git; '
            'elif command -v dnf >/dev/null 2>&1; then $SUDO dnf install -y git; '
            'elif command -v pacman >/dev/null 2>&1; then $SUDO pacman -Sy --noconfirm git; fi; '
            f'{{ if [ -d "$D/.git" ]; then git -C "$D" remote set-url origin {clone_url}; '
            f'else git clone --depth 1 --branch {branch} {clone_url} "$D"; fi; }} && '
            + admin_prefix + 'bash "$D/deploy/termux-setup.sh"'
        )
        # A display copy that hides the token (for any logging / non-QR display).
        clone_display = command.replace(clone_url, r["repo"]) if r["private"] else command
        private = r["private"]
        placeholder_token = r["placeholder_token"]
        # One warning line for the row: repo issue first, else the admin-password one.
        warning = " ".join(w for w in (r["warning"], a["warning"]) if w)

        steps = [
            "On a phone: install the free Termux app, then open it. On a Linux computer: open a terminal.",
            "Scan the QR code or paste the command, then press Enter.",
            "The first run takes a few minutes while it installs everything (on a phone it also "
            "sets up a small Ubuntu environment).",
            "When it finishes, open http://localhost:8080 on that device, or http://DEVICE-IP:8080 "
            "from another device on the same network (the script prints the address).",
        ]
        instructions = (
            "On a phone the command installs WebAgent inside a small Ubuntu environment (the reliable "
            "way to run the full app on Android); on a Linux computer it installs straight onto the "
            "system. Either way it keeps running in the background and restarts itself if it stops. On "
            "a Linux computer it also restarts automatically after a reboot; on a phone, install the free "
            "Termux:Boot add-on to start it on boot. On a phone it also installs the Server Manager — "
            "type 'webagent' in Termux to inspect, "
            "restart or diagnose the install. To stop it later: on a phone paste 'proot-distro login "
            "ubuntu -- pkill -f run.py', on Linux paste 'pkill -f run.py'.")
        reach_note = "http://localhost:8080 on the device · http://DEVICE-IP:8080 on the same network"
        return {"ok": True, "command": command, "clone_display": clone_display,
                "run_command": _run_command(directory), "install_dir": directory,
                "steps": steps, "instructions": instructions, "reach_note": reach_note,
                "private": private, "default_repo": r["default_repo"],
                "placeholder_token": placeholder_token, "warning": warning,
                "prewire": a["prewire"]}

    # ── test (nothing to connect to) ──
    async def test(self, config: Dict[str, Any], creds: Dict[str, Any]) -> Dict[str, Any]:
        return {"ok": True,
                "detail": "Termux installs by pasting a command on the phone — there is nothing to test."}

    # ── deploy = stream the (public) command, for the generic contract ──
    async def deploy(self, config: Dict[str, Any], creds: Dict[str, Any]) -> AsyncIterator[Dict[str, Any]]:
        plan = self.build_command(config.get("github_url") or DEFAULT_REPO_URL,
                                  config.get("visibility") or "public")
        if not plan.get("ok"):
            yield done({"ok": False, "message": plan.get("error", "Could not build the command.")})
            return
        yield ev("Command ready — copy it into Termux on the phone.", phase="ready", level="ok")
        yield done({"ok": True, "server": "", "state": "manual",
                    "command": plan["command"], "instructions": plan["instructions"],
                    "public_url": "", "message": "Install command generated."})

    async def destroy(self, config: Dict[str, Any], creds: Dict[str, Any],
                      record: Dict[str, Any]) -> AsyncIterator[Dict[str, Any]]:
        yield done({"ok": True, "deleted": False,
                    "message": "Termux runs on your own device — stop it with: proot-distro login ubuntu -- pkill -f run.py"})


PROVIDER = TermuxProvider()

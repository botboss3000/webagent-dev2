"""Deploy target: a Windows PC.

Like the Linux/Termux target (``app/deploy/providers/termux.py``), there is no
machine to CREATE here — the "server" is a PC the admin already owns. So this
target never connects anywhere: it GENERATES one command the admin pastes into
**PowerShell** once. The command installs git if missing (via winget), clones the
repo to ``%USERPROFILE%\\webagent``, then runs the repo's
``deploy/windows-setup.ps1`` — which installs ``uv`` (Astral's Python toolchain),
syncs the dependencies, and registers a **Scheduled Task** so WebAgent runs in the
background, restarts itself if it stops, and starts again when the user logs in.

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
    "id": "windows",
    "display_name": "Windows",
    "category": "deploy",
    "status": "beta",
    "summary": "Install WebAgent on a Windows PC by pasting one command into PowerShell.",
    "requires": ["A Windows 10 or 11 PC", "~2 GB free storage"],
}

# The PowerShell one-liner. ``__CLONE__`` / ``__BRANCH__`` / ``__DIR__`` are
# substituted below (NOT str.format — the script is full of `$var`, `{}`, quotes
# that would collide). If the folder already holds a clone it re-points its origin
# at the chosen repo and lets windows-setup.ps1 pull (a graceful update); only a
# missing folder is cloned fresh. Keep BYTE-IDENTICAL to deploy.js `_buildWindows`.
_TEMPLATE = (
    "$ErrorActionPreference='Stop'; "
    "$repo='__CLONE__'; $dir=\"__DIR__\"; "
    "if(-not(Get-Command git -EA SilentlyContinue)){Write-Host 'Installing Git...'; "
    "try{winget install --id Git.Git -e --source winget "
    "--accept-package-agreements --accept-source-agreements --silent}catch{}; "
    "$env:Path=[Environment]::GetEnvironmentVariable('Path','Machine')+';'"
    "+[Environment]::GetEnvironmentVariable('Path','User')}; "
    "if(-not(Get-Command git -EA SilentlyContinue)){"
    "Write-Host 'Git is required. Install it from https://git-scm.com/download/win then run this again.'; "
    "return}; "
    "if(Test-Path \"$dir\\.git\"){git -C \"$dir\" remote set-url origin $repo}"
    "else{git clone --depth 1 --branch __BRANCH__ $repo \"$dir\"}; "
    # __ADMIN__ carries the optional pre-set admin password into windows-setup.ps1
    # as an env var ($env:WA_ADMIN_PW) the child powershell inherits; empty when blank.
    "__ADMIN__"
    "powershell -NoProfile -ExecutionPolicy Bypass -File \"$dir\\deploy\\windows-setup.ps1\""
)


def _run_command(directory: str) -> str:
    """Start the server when WebAgent is ALREADY installed — no clone, no rebuild.
    Prefers the “WebAgent” Scheduled Task the installer registers (which is
    folder-independent); falls back to the keep-alive ps1 in the install folder if
    the task isn't there. Keep BYTE-IDENTICAL to deploy.js `_runWindows`."""
    return (
        "if(Get-ScheduledTask -TaskName WebAgent -EA SilentlyContinue){Start-ScheduledTask -TaskName WebAgent}"
        "else{powershell -NoProfile -ExecutionPolicy Bypass -File \""
        + directory + "\\deploy\\start_server_windows.ps1\"}"
    )


class WindowsProvider(BaseDeployProvider):
    id = "windows"
    display_name = "Windows"
    icon = "monitor"
    summary = ("Install WebAgent on a Windows PC. You copy one command and paste it "
               "into PowerShell — there is no cloud account and nothing to pay for.")
    requires = [
        "A Windows 10 or 11 PC",
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
        """Build everything the Windows row needs. ALWAYS succeeds (never
        ``{ok: False}``) — see ``manual_common.resolve_clone`` for why. Returns
        ``{ok, command, run_command, clone_display, steps, instructions,
        reach_note, private, default_repo, placeholder_token, warning, prewire}``."""
        r = mc.resolve_clone(github_url, visibility, token)
        branch = mc._safe(branch, DEFAULT_BRANCH)
        directory = mc.resolve_dir(install_dir, mc.DEFAULT_DIR_WINDOWS)
        # Optional pre-set admin password → an env assignment before the setup script
        # (PowerShell); empty when blank. BYTE-IDENTICAL to deploy.js `_buildWindows`.
        a = mc.resolve_admin(admin_password)
        # __ADMIN__ carries the pre-set admin password AND, when configuration is
        # embedded, the locked WA_BOOTSTRAP_CODE that windows-setup.ps1 writes to
        # bootstrap.json. Byte-identical to deploy.js `_buildWindows` in the bare case.
        admin_ps = mc.env_prefix_windows(a, bootstrap_code)
        command = (_TEMPLATE.replace("__CLONE__", r["clone_url"])
                   .replace("__BRANCH__", branch).replace("__DIR__", directory)
                   .replace("__ADMIN__", admin_ps))
        # A display copy that hides the token (for any logging / non-QR display).
        clone_display = command.replace(r["clone_url"], r["repo"]) if r["private"] else command

        steps = [
            "Open PowerShell: click Start, type 'PowerShell', and open it.",
            "Paste the command and press Enter. (If Windows offers to install Git, allow it.)",
            "The first run takes a few minutes while it downloads Python and installs everything.",
            "When it finishes, open http://localhost:8080 on this PC, or http://THIS-PC-IP:8080 "
            "from another device on the same network.",
        ]
        instructions = (
            "WebAgent installs into a folder in your user profile and runs in the background as a "
            "Scheduled Task named 'WebAgent' — it starts automatically when you log in and restarts "
            "itself if it stops. To stop it, run in PowerShell: 'Stop-ScheduledTask -TaskName WebAgent'. "
            "To stop it starting on login: 'Unregister-ScheduledTask -TaskName WebAgent -Confirm:$false'.")
        reach_note = "http://localhost:8080 on this PC · http://THIS-PC-IP:8080 on the same network"
        warning = " ".join(w for w in (r["warning"], a["warning"]) if w)
        return {"ok": True, "command": command, "clone_display": clone_display,
                "run_command": _run_command(directory), "install_dir": directory,
                "steps": steps, "instructions": instructions, "reach_note": reach_note,
                "private": r["private"], "default_repo": r["default_repo"],
                "placeholder_token": r["placeholder_token"], "warning": warning,
                "prewire": a["prewire"]}

    # ── test (nothing to connect to) ──
    async def test(self, config: Dict[str, Any], creds: Dict[str, Any]) -> Dict[str, Any]:
        return {"ok": True,
                "detail": "Windows installs by pasting a command into PowerShell — there is nothing to test."}

    # ── deploy = stream the (public) command, for the generic contract ──
    async def deploy(self, config: Dict[str, Any], creds: Dict[str, Any]) -> AsyncIterator[Dict[str, Any]]:
        plan = self.build_command(config.get("github_url") or DEFAULT_REPO_URL,
                                  config.get("visibility") or "public")
        yield ev("Command ready — paste it into PowerShell on the PC.", phase="ready", level="ok")
        yield done({"ok": True, "server": "", "state": "manual",
                    "command": plan["command"], "instructions": plan["instructions"],
                    "public_url": "", "message": "Install command generated."})

    async def destroy(self, config: Dict[str, Any], creds: Dict[str, Any],
                      record: Dict[str, Any]) -> AsyncIterator[Dict[str, Any]]:
        yield done({"ok": True, "deleted": False,
                    "message": "WebAgent runs on your own PC — stop it with: Stop-ScheduledTask -TaskName WebAgent"})


PROVIDER = WindowsProvider()

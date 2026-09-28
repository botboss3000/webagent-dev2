"""Deploy target: an EXISTING Linux VM you already own, reached over SSH.

The cloud targets (``google_vm``) CREATE a machine and hand it the shared install
recipe as a boot ``startup-script``. This target creates nothing: the admin
already has a running Ubuntu/Debian server (a VM at some IP, a rented box, a
Raspberry Pi on the LAN) and just wants WebAgent installed onto it. So instead of
a cloud API, it opens an SSH connection, uploads the SAME bootstrap
(``app/deploy/bootstrap.py``) the cloud targets use, runs it under root, and
streams the install log back into the panel live — the machine ends up with the
identical Caddy + systemd + uvicorn stack a fresh cloud VM would get.

Because it acts on a machine the admin already owns (nothing billable is created
and nothing is deleted on tear-down), it sets ``creates_server = False`` — the
panel then softens its Deploy / Tear-down confirmations accordingly. It is NOT a
"manual" target: it really connects and does the work with a live log, rather
than printing a copy-paste command.

Auth is EITHER an SSH private key (optionally passphrase-protected) OR a
password — whichever the admin provides. Root is used directly; a non-root user
escalates with ``sudo`` (the password is fed to ``sudo -S`` over the channel, so
it never appears in the process list). All three secrets live in the encrypted
vault like every other deploy key; by default they are KEPT after a deploy (not
auto-forgotten) so a later tear-down / re-install can reconnect.

Needs the optional ``paramiko`` SSH library. If it isn't installed, ``available``
reports that clearly and the other targets are unaffected (the exact case the
base contract's ``available`` hook exists for).
"""

from __future__ import annotations

import asyncio
import io
import logging
import secrets as _secrets
import time
from typing import Any, AsyncIterator, Dict, List, Optional, Tuple

from app.deploy.base import BaseDeployProvider, done, ev
from app.deploy.bootstrap import (
    DEFAULT_BRANCH, DEFAULT_REPO_URL, build_install_script,
)
from app.deploy.manual_common import resolve_clone

logger = logging.getLogger(__name__)

FEATURE = {
    "id": "ssh_vm",
    "display_name": "Existing server (SSH)",
    "category": "deploy",
    "status": "beta",
    "summary": "Install WebAgent onto a Linux server you already have, over SSH.",
    "requires": ["An Ubuntu/Debian server reachable over SSH", "root or a sudo login"],
}

# Where the install log lands on the box (the bootstrap writes here) and where we
# drop the script before running it.
_LOG_PATH = "/var/log/webagent-bootstrap.log"
_DONE_MARK = "WebAgent bootstrap complete"      # printed on success (last line)
_FAIL_MARK = "BOOTSTRAP FAILED"                 # printed by the script's ERR trap

# How long to watch the install before handing the admin back to the holding page.
_INSTALL_WAIT_SECONDS = 780      # ~13 min (a small VM installs in 3–8)
_POLL_SECONDS = 4


def _import_paramiko():
    """Import paramiko or return None (so ``available`` can report it cleanly)."""
    try:
        import paramiko  # noqa: F401
        return paramiko
    except Exception:      # ImportError, or a broken partial install
        return None


class SSHVMProvider(BaseDeployProvider):
    id = "ssh_vm"
    display_name = "Existing server (SSH)"
    icon = "server"
    summary = ("Install WebAgent onto a Linux server you already have — a cloud VM, a "
               "rented box, or a machine on your network — by connecting to it over SSH. "
               "Nothing is created or billed; it installs onto the machine you point it at.")
    requires = [
        "An Ubuntu or Debian server you can reach over SSH",
        "Its address, plus an SSH login that is root or can use sudo",
        "An SSH private key or the login password",
    ]
    # Acts on a machine the admin already owns: nothing billable is created and
    # nothing is deleted on tear-down. The panel reads this to soften its Deploy /
    # Tear-down confirm dialogs (no "billable server" / "permanently deletes").
    creates_server = False
    # No cloud API to enumerate, so it never appears on the Cloud VMs page.
    supports_instances = False
    # The panel shows a "Saved servers" picker for this target: each server you
    # enter can be kept (with its login in the vault) and re-selected later, and
    # every Google VM you create is auto-added here. See app/deploy/manager.py.
    saved_servers = True

    config_fields = [
        {"key": "host", "label": "Server address (IP or hostname)", "type": "text",
         "required": True, "placeholder": "203.0.113.10  or  server.example.com",
         "tip": "Where the server lives on the network — the public IP address, or a "
                "hostname that resolves to it. This is the machine WebAgent will be "
                "installed onto; it must already exist and be switched on."},
        {"key": "ssh_user", "label": "SSH username", "type": "text",
         "default": "root", "placeholder": "root",
         "tip": "The account to log in as over SSH. 'root' works directly. Any other "
                "user must be able to run sudo (enter that user's password below so it "
                "can install system packages)."},
        {"key": "ssh_port", "label": "SSH port", "type": "number",
         "default": 22, "placeholder": "22",
         "tip": "The port the server's SSH service listens on. Almost always 22 — only "
                "change it if you know your server uses a different one."},
        {"key": "repo_url", "label": "Code to install (git repository)", "type": "text",
         "default": DEFAULT_REPO_URL, "placeholder": DEFAULT_REPO_URL,
         "tip": "Leave this as-is to install the standard WebAgent. Only change it if "
                "you maintain your own copy (a fork) and want the server to run that."},
        # public / private. Supplied by the shared "Repo details" bar in the New-
        # deployment panel (hidden from this cloud form); a private repo also needs a
        # github_token below so the server can clone it. Non-secret, so it lives here.
        {"key": "visibility", "label": "Repository access", "type": "select",
         "default": "public",
         "options": [
             {"value": "public", "label": "Public — anyone can clone it"},
             {"value": "private", "label": "Private — needs an access token"},
         ]},
        {"key": "branch", "label": "Version (git branch)", "type": "text",
         "default": DEFAULT_BRANCH,
         "tip": "Which line of the code to install. 'main' is the stable release — "
                "leave it unless you specifically need a different branch."},
        {"key": "domain", "label": "Web address (optional)", "type": "text",
         "placeholder": "leave blank to reach it at the server's IP",
         "tip": "Optional. If you own a domain (e.g. app.yourcompany.com), enter it and "
                "point its DNS 'A' record at this server's IP — the server then gets a "
                "free HTTPS certificate automatically. Leave blank to reach the app at "
                "the server's raw IP over plain http."},
        {"key": "forget_keys", "label": "Forget SSH login after installing", "type": "checkbox",
         "default": False,
         "tip": "Off by default for this target: keeping the SSH login lets you re-run "
                "the install or stop the app later without re-entering it. Turn it on if "
                "you'd rather the app hold no standing access to your server — you'll "
                "then re-enter the key/password for any later action."},
    ]
    credential_fields = [
        {"key": "private_key", "label": "SSH private key", "type": "textarea", "secret": True,
         "placeholder": "-----BEGIN OPENSSH PRIVATE KEY-----\n… paste the whole key …",
         "tip": "Paste an SSH private key that can log into the server (the PRIVATE half "
                "of a key whose public half is in the server's authorized_keys). Leave "
                "blank if you'll sign in with a password instead. Stored encrypted."},
        {"key": "key_passphrase", "label": "Key passphrase (if any)", "type": "password", "secret": True,
         "placeholder": "only if your key is passphrase-protected",
         "tip": "Only needed if the private key above is itself protected by a "
                "passphrase. Leave blank otherwise."},
        {"key": "password", "label": "Login password", "type": "password", "secret": True,
         "placeholder": "the SSH login's password",
         "tip": "The password for the SSH user. Provide EITHER this or a private key "
                "above. For a non-root user this password is also used for sudo, so the "
                "install can set up system services. Stored encrypted."},
        # OPTIONAL GitHub access token for a PRIVATE repository + an OPTIONAL pre-set
        # admin password — both supplied by the shared "Repo details" bar (hidden from
        # this cloud form). Secrets, so they ride the encrypted vault (and honour the
        # "Forget SSH login after installing" toggle).
        {"key": "github_token", "label": "GitHub access token (private repo)",
         "type": "password", "secret": True, "required": False,
         "placeholder": "only for a private repository",
         "tip": "Only needed if the repository above is private. Used once so the server "
                "can clone it, then handled like the other secrets here."},
        {"key": "admin_password", "label": "Admin password (optional)",
         "type": "password", "secret": True, "required": False,
         "placeholder": "leave blank — first visitor sets it",
         "tip": "Optional — leave it blank to install the app as-is: the first person to "
                "open its address then picks the password on the setup page. Or type one "
                "(6+ characters) to pre-set it, so the admin is ready on first boot."},
    ]
    # Auth is key OR password, so no single field is strictly required — deploy /
    # test validate that at least one was given and report clearly if not.
    credential_required: List[str] = []

    # ── capability ──
    def available(self) -> Tuple[bool, str]:
        if _import_paramiko() is None:
            return (False, "Installing over SSH needs the 'paramiko' package on the "
                           "server. Add it (pip install paramiko) and restart WebAgent, "
                           "or use one of the other deploy targets.")
        return True, ""

    # ── low-level SSH helpers (sync; run via asyncio.to_thread) ──
    def _load_key(self, paramiko, key_text: str, passphrase: str):
        """Parse a pasted private key, trying each key type (Ed25519 / ECDSA / RSA /
        DSS) since the header alone doesn't always say which. Returns a PKey or
        raises a friendly error."""
        pw = passphrase or None
        last_err: Optional[Exception] = None
        # The pasted key's header doesn't reliably say which type it is, so try each
        # in turn. Built defensively via getattr because older/obsolete types (e.g.
        # DSSKey) were dropped in newer paramiko.
        candidates = [getattr(paramiko, n, None) for n in
                      ("Ed25519Key", "ECDSAKey", "RSAKey", "DSSKey")]
        for cls in [c for c in candidates if c is not None]:
            try:
                return cls.from_private_key(io.StringIO(key_text), password=pw)
            except Exception as e:      # wrong type or wrong passphrase — try the next
                last_err = e
        raise RuntimeError(
            "Could not read that SSH private key" +
            (" (a passphrase may be required or wrong)." if not pw else " with that passphrase.")
        ) from last_err

    def _connect(self, cfg: Dict[str, Any], creds: Dict[str, Any]):
        """Open an SSHClient (sync). Auto-accepts the host key (first contact with a
        server we were explicitly told to deploy to)."""
        paramiko = _import_paramiko()
        if paramiko is None:
            raise RuntimeError("The 'paramiko' SSH package is not installed on the server.")
        host = (cfg.get("host") or "").strip()
        user = (cfg.get("ssh_user") or "root").strip() or "root"
        try:
            port = int(str(cfg.get("ssh_port") or 22).strip() or 22)
        except ValueError:
            port = 22
        key_text = (creds.get("private_key") or "").strip()
        password = (creds.get("password") or "").strip()
        passphrase = (creds.get("key_passphrase") or "").strip()

        pkey = self._load_key(paramiko, key_text, passphrase) if key_text else None

        client = paramiko.SSHClient()
        client.set_missing_host_key_policy(paramiko.AutoAddPolicy())
        client.connect(
            hostname=host, port=port, username=user,
            pkey=pkey, password=password or None,
            timeout=20, banner_timeout=25, auth_timeout=25,
            allow_agent=False, look_for_keys=False,
        )
        return client

    @staticmethod
    def _run(client, cmd: str, timeout: int = 40, stdin_data: Optional[str] = None
             ) -> Tuple[int, str, str]:
        """Run one command (sync). Optionally feed ``stdin_data`` (e.g. a sudo
        password to ``sudo -S``). Returns (exit_code, stdout, stderr)."""
        stdin, stdout, stderr = client.exec_command(cmd, timeout=timeout)
        if stdin_data is not None:
            try:
                stdin.write(stdin_data)
                stdin.flush()
                stdin.channel.shutdown_write()
            except Exception:
                pass
        out = stdout.read().decode("utf-8", "replace")
        err = stderr.read().decode("utf-8", "replace")
        rc = stdout.channel.recv_exit_status()
        return rc, out, err

    @staticmethod
    def _put(client, remote_path: str, content: str) -> None:
        """Upload a text file over SFTP and make it executable (sync)."""
        sftp = client.open_sftp()
        try:
            with sftp.open(remote_path, "w") as f:
                f.write(content)
            sftp.chmod(remote_path, 0o700)
        finally:
            sftp.close()

    def _sudo_prefix(self, is_root: bool, password: str) -> Tuple[str, Optional[str]]:
        """The command prefix + stdin for a privileged run: nothing as root, else
        ``sudo -S`` fed the password (or ``sudo -n`` for passwordless sudo)."""
        if is_root:
            return "", None
        if password:
            return "sudo -S -p '' ", password + "\n"
        return "sudo -n ", None

    # ── test ──
    async def test(self, config: Dict[str, Any], creds: Dict[str, Any]) -> Dict[str, Any]:
        if _import_paramiko() is None:
            return {"ok": False, "detail": "The 'paramiko' SSH package is not installed on the server."}
        host = (config.get("host") or "").strip()
        if not host:
            return {"ok": False, "detail": "Enter the server's address first."}
        if not (creds.get("private_key") or creds.get("password")):
            return {"ok": False, "detail": "Add an SSH private key or a login password first."}
        try:
            client = await asyncio.to_thread(self._connect, config, creds)
        except Exception as e:
            return {"ok": False, "detail": f"Could not connect: {e}"}
        try:
            user = (config.get("ssh_user") or "root").strip() or "root"
            is_root = user == "root"
            # Confirm it's an apt-based OS and that we can gain root.
            rc, out, _ = await asyncio.to_thread(
                self._run, client, "command -v apt-get >/dev/null 2>&1 && echo APT || echo NOAPT")
            apt_ok = "APT" in out
            sudo_ok = True
            sudo_detail = ""
            if not is_root:
                prefix, stdin_data = self._sudo_prefix(False, (creds.get("password") or "").strip())
                rc2, _, err2 = await asyncio.to_thread(
                    self._run, client, prefix + "true", 30, stdin_data)
                sudo_ok = rc2 == 0
                if not sudo_ok:
                    sudo_detail = " (connected, but this user cannot sudo — give the login password, or use root)"
            if not apt_ok:
                return {"ok": False, "detail": "Connected, but this server isn't Ubuntu/Debian (no apt) — this installer needs an apt-based system."}
            if not sudo_ok:
                return {"ok": False, "detail": "Connected" + sudo_detail + "."}
            return {"ok": True, "detail": f"Connected to {host} — ready to install."}
        except Exception as e:
            return {"ok": False, "detail": str(e)}
        finally:
            try:
                client.close()
            except Exception:
                pass

    # ── deploy ──
    async def deploy(self, config: Dict[str, Any], creds: Dict[str, Any]) -> AsyncIterator[Dict[str, Any]]:
        if _import_paramiko() is None:
            yield done({"ok": False, "message": "The 'paramiko' SSH package is not installed on the server."})
            return
        host = (config.get("host") or "").strip()
        user = (config.get("ssh_user") or "root").strip() or "root"
        repo = (config.get("repo_url") or DEFAULT_REPO_URL).strip() or DEFAULT_REPO_URL
        branch = (config.get("branch") or DEFAULT_BRANCH).strip() or DEFAULT_BRANCH
        domain = (config.get("domain") or "").strip()
        password = (creds.get("password") or "").strip()
        is_root = user == "root"

        # Resolve the clone URL from the shared repo details — for a PRIVATE repo the
        # access token is embedded so the server can fetch it (reuses the manual-target
        # helper). The optional pre-set admin password is baked into the install's .env.
        visibility = str(config.get("visibility") or "public").strip().lower()
        git_token = (creds.get("github_token") or "").strip()
        admin_password = (creds.get("admin_password") or "").strip()
        _clone = resolve_clone(repo, visibility, git_token)
        clone_url = _clone["clone_url"]        # may embed the token (private repo)
        origin_url = _clone["clean_url"]       # clean address the box's origin resets to

        if not host:
            yield done({"ok": False, "message": "No server address set."})
            return
        if not (creds.get("private_key") or password):
            yield done({"ok": False, "message": "Add an SSH private key or a login password first."})
            return
        if visibility == "private" and not git_token:
            yield done({"ok": False, "message": "That repository is private — add a GitHub access token in Repo details, or set the repository to Public."})
            return
        if admin_password and len(admin_password) < 6:
            yield done({"ok": False, "message": "The admin password must be at least 6 characters (or leave it blank to set it on first visit)."})
            return

        yield ev(f"Connecting to {user}@{host} over SSH…", phase="connect")
        try:
            client = await asyncio.to_thread(self._connect, config, creds)
        except Exception as e:
            yield done({"ok": False, "message": f"Could not connect: {e}"})
            return

        try:
            # ── Preflight: apt-based OS + a working path to root ──
            yield ev("Checking the server…", phase="preflight")
            _, out, _ = await asyncio.to_thread(
                self._run, client, "command -v apt-get >/dev/null 2>&1 && echo APT || echo NOAPT")
            if "APT" not in out:
                yield done({"ok": False, "message": "This server isn't Ubuntu/Debian (no apt). The installer needs an apt-based system."})
                return

            prefix, sudo_stdin = self._sudo_prefix(is_root, password)
            if not is_root:
                # Validate (and, for a password, cache) sudo up front so the detached
                # install can run without prompting.
                rc, _, err = await asyncio.to_thread(
                    self._run, client, prefix + ("-v" if password else "true"), 30, sudo_stdin)
                if rc != 0:
                    yield done({"ok": False, "message": "The SSH user can't use sudo. Provide the login password, or connect as root."})
                    return

            # ── Upload the shared install recipe and launch it detached ──
            yield ev("Uploading the install script…", phase="upload")
            script = build_install_script(repo_url=clone_url, origin_url=origin_url, branch=branch,
                                          domain=domain, port=8080,
                                          admin_password=admin_password,
                                          # Optional pre-loaded config bundle — the manager
                                          # gathered + encrypted it into config["_bootstrap_code"]
                                          # (the "Include this app's configuration" toggle); blank when off.
                                          bootstrap_code=config.get("_bootstrap_code", ""))
            remote_script = f"/tmp/webagent-bootstrap-{int(time.time())}-{_secrets.token_hex(3)}.sh"
            await asyncio.to_thread(self._put, client, remote_script, script)

            # nohup + a new session so the install keeps running if the SSH session
            # drops; the script self-redirects its output to the log we then tail.
            run_prefix = "" if is_root else "sudo -n "
            launch = (f"rm -f {_LOG_PATH}; nohup {run_prefix}bash {remote_script} "
                      f">/dev/null 2>&1 </dev/null & echo STARTED")
            rc, out, err = await asyncio.to_thread(self._run, client, launch, 30)
            if "STARTED" not in out:
                yield done({"ok": False, "message": f"Couldn't start the installer: {(err or out or 'unknown error').strip()[:200]}"})
                return
            yield ev("Installer started. Setting up WebAgent (this takes a few minutes)…", phase="installing", level="ok")

            # ── Tail the install log, streaming each NEW line into the panel, until
            #    it reports completion / failure or we stop waiting. The install runs
            #    detached (nohup), so a timeout here never stops it — the holding page
            #    carries on installing. We re-read the whole (small) log each poll and
            #    emit only the lines we haven't shown yet. ──
            deadline = time.time() + _INSTALL_WAIT_SECONDS
            emitted = 0
            state = "timeout"
            while time.time() < deadline:
                await asyncio.sleep(_POLL_SECONDS)
                try:
                    _, out, _ = await asyncio.to_thread(
                        self._run, client, f"cat {_LOG_PATH} 2>/dev/null || true", 30)
                except Exception:
                    continue          # a transient read hiccup — try again next poll
                lines = out.splitlines()
                for line in lines[emitted:]:
                    s = line.rstrip()
                    if s and _DONE_MARK not in s and _FAIL_MARK not in s:
                        yield ev(s, phase="log")
                emitted = len(lines)
                if _FAIL_MARK in out:
                    state = "failed"
                    break
                if _DONE_MARK in out:
                    state = "done"
                    break

        except Exception as e:
            logger.exception("ssh_vm deploy failed")
            yield done({"ok": False, "message": f"Deploy failed: {e}"})
            return
        finally:
            try:
                client.close()
            except Exception:
                pass

        public_url = (f"https://{domain}" if domain else f"http://{host}")
        if state == "failed":
            yield done({"ok": False, "server": host, "ip": host,
                        "message": f"The install reported a problem on {host}. The lines above "
                                   f"(and {_LOG_PATH} on the server) show what went wrong."})
            return
        if state == "timeout":
            yield ev("Still installing — it's taking longer than usual.", phase="installing", level="warn")
            yield done({
                "ok": True, "server": host, "ip": host, "public_url": public_url,
                "state": "installing",
                "message": f"WebAgent is still installing on {host}. Open {public_url} — the page shows progress and becomes the app when it's ready.",
            })
            return

        yield ev("WebAgent is installed and running.", phase="done", level="ok")
        if domain:
            yield ev(f"Point {domain}'s DNS 'A' record at {host} so HTTPS can be issued.", phase="dns", level="warn")
        yield done({
            "ok": True, "server": host, "ip": host, "public_url": public_url,
            "state": "running",
            "message": f"WebAgent is live on {host}.",
        })

    # ── destroy (non-destructive: stop the app; the machine is untouched) ──
    async def destroy(self, config: Dict[str, Any], creds: Dict[str, Any],
                      record: Dict[str, Any]) -> AsyncIterator[Dict[str, Any]]:
        if _import_paramiko() is None:
            yield done({"ok": False, "message": "The 'paramiko' SSH package is not installed on the server."})
            return
        host = (record.get("server") or config.get("host") or "").strip()
        user = (config.get("ssh_user") or "root").strip() or "root"
        password = (creds.get("password") or "").strip()
        is_root = user == "root"
        if not host:
            yield done({"ok": False, "message": "No recorded server to act on."})
            return
        if not (creds.get("private_key") or password):
            yield done({"ok": False, "message": "The SSH login was forgotten — re-enter the key or password to stop the app."})
            return

        yield ev(f"Connecting to {user}@{host} over SSH…", phase="connect")
        try:
            client = await asyncio.to_thread(self._connect, config, creds)
        except Exception as e:
            yield done({"ok": False, "message": f"Could not connect: {e}"})
            return
        try:
            prefix, sudo_stdin = self._sudo_prefix(is_root, password)
            yield ev("Stopping the WebAgent service…", phase="stop")
            rc, out, err = await asyncio.to_thread(
                self._run, client,
                prefix + "systemctl disable --now webagent 2>&1 || true", 40, sudo_stdin)
        except Exception as e:
            yield done({"ok": False, "message": f"Could not stop the app: {e}"})
            return
        finally:
            try:
                client.close()
            except Exception:
                pass
        yield done({
            "ok": True, "deleted": True,
            "message": f"WebAgent was stopped on {host}. The server itself is untouched — "
                       "its files remain and you can re-install any time.",
        })


PROVIDER = SSHVMProvider()

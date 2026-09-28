"""The shared install recipe — one bootstrap script for every fresh-VM target.

Google VM, AWS and a plain Linux box are all just "a fresh Ubuntu machine," so
they install WebAgent the SAME way; they differ only in how the machine is
created and how this script is handed to it (cloud metadata "startup-script" for
GCE/AWS; over SSH for a plain box). Docker uses the repo's Dockerfile instead and
does not call this.

The script targets Ubuntu 24.04 LTS and installs an isolated, uv-managed
Python 3.14 runtime for WebAgent instead of relying on the distro Python:
  1. installs Python, git and Caddy (official apt repo),
  2. IMMEDIATELY fronts the box with Caddy pointed at the app + a friendly
     "installing…" holding page, so the misleading Caddy welcome page never
     shows. The holding page auto-refreshes and turns into the app the moment
     the backend answers; if the install fails it flips to a clear error page.
  3. clones the chosen repo + branch into /opt/webagent,
  4. builds a venv and installs requirements,
  5. writes a minimal .env (LLM is configured in-app afterwards),
  6. installs a systemd unit running uvicorn on 127.0.0.1:<port>,
  7. enables + starts the app service (Caddy is already configured from step 2).

Why Caddy is configured FIRST (before the slow dependency install): the apt
package starts Caddy on its default "Congratulations, configure me" welcome
page the instant it installs. Left until the end — after a multi-minute pip
install — that welcome page is what a visitor sees the whole time, and *forever*
if any later step fails. Configuring Caddy up front replaces it with an honest
status page that needs no working backend.

Its full output is logged on the box at /var/log/webagent-bootstrap.log, and a
failure also rewrites the holding page so the problem is visible in the browser.

We substitute ``__TOKEN__`` placeholders (not ``str.format``) because the script
is full of shell ``$VAR`` and ``{}`` that would collide with format fields.
"""

from __future__ import annotations

_SCRIPT = r"""#!/bin/bash
set -e
exec > /var/log/webagent-bootstrap.log 2>&1
echo "WebAgent bootstrap starting at $(date)"
export DEBIAN_FRONTEND=noninteractive

REPO_URL="__REPO_URL__"
ORIGIN_URL="__ORIGIN_URL__"
BRANCH="__BRANCH__"
DOMAIN="__DOMAIN__"
PORT="__PORT__"
APP_DIR="/opt/webagent"
APP_USER="webagent"

# ── Live status file the holding page polls (see STATUS_DIR below) ──
# Written from the very first line so a visitor sees real progress, not just a
# generic "installing" message, for the whole 3-8 minutes. Plain `printf` (no
# python3/jq dependency) since python3 isn't installed until the first apt step.
STATUS_DIR="/var/www/webagent-status"
STATUS_JSON="$STATUS_DIR/webagent-status.json"
mkdir -p "$STATUS_DIR"
STEP_TOTAL=13
STEP_NUM=0
CUR_LABEL=""
_write_status_json() {
  local detail="${1//\\\\/}"
  detail="${detail//\"/\'}"
  printf '{"step":%s,"total":%s,"label":"%s","detail":"%s"}' \
    "$STEP_NUM" "$STEP_TOTAL" "$CUR_LABEL" "$detail" > "$STATUS_JSON"
}
next_step() {
  STEP_NUM=$((STEP_NUM+1))
  CUR_LABEL="$1"
  _write_status_json "${2:-}"
  echo "[$STEP_NUM/$STEP_TOTAL] $CUR_LABEL ${2:-}"
}
set_detail() {
  _write_status_json "$1"
}

next_step "Preparing server"

next_step "Updating package lists"
apt-get update
next_step "Installing system packages" "python3, git, build-essential"
apt-get install -y python3 python3-venv python3-pip python3-dev git curl \
    build-essential debian-keyring debian-archive-keyring apt-transport-https

# Install uv without modifying shell profiles, then provision a shared managed
# CPython 3.14. Keeping it outside /root means the unprivileged app user can use
# the interpreter backing both virtual environments after bootstrap completes.
UV_BIN="/usr/local/bin/uv"
UV_PYTHON_DIR="/opt/webagent-python"
if [ ! -x "$UV_BIN" ]; then
  curl -LsSf https://astral.sh/uv/install.sh \
    | env UV_UNMANAGED_INSTALL="/usr/local/bin" sh
fi
env UV_PYTHON_INSTALL_DIR="$UV_PYTHON_DIR" "$UV_BIN" python install 3.14
chmod -R a+rX "$UV_PYTHON_DIR"

# ── Caddy (official apt repo) ──
next_step "Installing Caddy web server"
curl -1sLf 'https://dl.cloudsmith.io/public/caddy/stable/gpg.key' \
    | gpg --batch --yes --dearmor -o /usr/share/keyrings/caddy-stable-archive-keyring.gpg
curl -1sLf 'https://dl.cloudsmith.io/public/caddy/stable/debian.deb.txt' \
    | tee /etc/apt/sources.list.d/caddy-stable.list
apt-get update
apt-get install -y caddy

# ── Caddy FIRST: an honest holding page now, the app the moment it is up ──
# The apt package starts Caddy on its default "Congratulations, configure me"
# welcome page immediately. We replace that right away with a reverse proxy to
# the (not-yet-running) app PLUS a handle_errors fallback that serves a friendly
# "installing…" page whenever the backend isn't answering. So a visitor sees an
# honest status page during the whole install — never the misleading welcome
# page — and it turns into the real app automatically once the backend answers.
next_step "Configuring web server"
cat > "$STATUS_DIR/index.html" <<'STATUSEOF'
<!doctype html>
<html lang="en"><head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1, maximum-scale=1, user-scalable=no">
<meta http-equiv="refresh" content="15">
<title>WebAgent — installing…</title>
<style>
  :root { color-scheme: dark; }
  body { margin:0; min-height:100vh; display:flex; align-items:center; justify-content:center;
         font-family: system-ui, -apple-system, "Segoe UI", Roboto, sans-serif;
         background:#0d1117; color:#e6edf3; }
  .card { max-width:34rem; padding:2.5rem 2rem; text-align:center; }
  .dot { width:12px; height:12px; border-radius:50%; background:#3fb950; display:inline-block;
         margin-right:.5rem; vertical-align:middle; animation:pulse 1.4s ease-in-out infinite; }
  @keyframes pulse { 0%,100%{opacity:.35} 50%{opacity:1} }
  h1 { font-size:1.4rem; margin:0 0 1rem; font-weight:650; }
  p  { line-height:1.6; margin:.6rem 0; color:#c9d1d9; }
  .muted { color:#8b949e; font-size:.85rem; margin-top:1.6rem; }
  code { background:#161b22; padding:.15rem .4rem; border-radius:4px; font-size:.85em; }
  .bar { height:8px; border-radius:4px; background:#21262d; overflow:hidden; margin-top:1.4rem; }
  .bar-fill { height:100%; width:0%; background:#3fb950; transition:width .6s ease; }
  .status-line { font-size:.92rem; color:#e6edf3; margin-top:.7rem; min-height:1.3em; }
  .status-detail { font-size:.8rem; color:#8b949e; margin-top:.15rem; min-height:1.1em; word-break:break-word; }
</style></head>
<body><!--WEBAGENT-HOLDING--><div class="card">
  <h1><span class="dot"></span>Installing WebAgent…</h1>
  <p>Your server is up and setting itself up. This usually takes <b>3&ndash;8 minutes</b> on a small VM.</p>
  <p>This page refreshes itself &mdash; when the app is ready it appears here automatically. Nothing to do.</p>
  <div class="bar"><div class="bar-fill" id="waBar"></div></div>
  <p class="status-line" id="waStatus">Starting…</p>
  <p class="status-detail" id="waDetail"></p>
  <p class="muted">Still on this page after ~15 minutes? The install hit a snag. Connect to the VM and read
  <code>/var/log/webagent-bootstrap.log</code> to see what went wrong.</p>
</div>
<script>
(function poll() {
  fetch('/webagent-status.json?_=' + Date.now(), { cache: 'no-store' })
    .then(function (r) { return r.ok ? r.json() : null; })
    .then(function (d) {
      if (d && d.total) {
        var pct = Math.max(4, Math.round((d.step / d.total) * 100));
        document.getElementById('waBar').style.width = pct + '%';
        document.getElementById('waStatus').textContent =
          d.label + ' (' + d.step + '/' + d.total + ')';
        document.getElementById('waDetail').textContent = d.detail || '';
      }
    })
    .catch(function () {})
    .then(function () { setTimeout(poll, 3000); });
})();
</script>
</body></html>
STATUSEOF

if [ -n "$DOMAIN" ]; then
cat > /etc/caddy/Caddyfile <<CADDYEOF
$DOMAIN {
    reverse_proxy localhost:$PORT
    handle_errors {
        root * $STATUS_DIR
        @waNotStatus {
            not path /webagent-status.json
        }
        rewrite @waNotStatus /index.html
        file_server
    }
}
CADDYEOF
else
cat > /etc/caddy/Caddyfile <<CADDYEOF
:80 {
    reverse_proxy localhost:$PORT
    handle_errors {
        root * $STATUS_DIR
        @waNotStatus {
            not path /webagent-status.json
        }
        rewrite @waNotStatus /index.html
        file_server
    }
}
CADDYEOF
fi
systemctl reload caddy 2>/dev/null || systemctl restart caddy

# From here on, any failure flips the holding page to a clear error message (and
# stops), so a broken install shows up in the browser instead of a stuck spinner.
on_error() {
  rc=$?
  echo "BOOTSTRAP FAILED (exit $rc) near line ${1:-?}"
  cat > "$STATUS_DIR/index.html" <<'FAILEOF'
<!doctype html>
<html lang="en"><head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1, maximum-scale=1, user-scalable=no">
<title>WebAgent — install problem</title>
<style>
  :root { color-scheme: dark; }
  body { margin:0; min-height:100vh; display:flex; align-items:center; justify-content:center;
         font-family: system-ui, -apple-system, "Segoe UI", Roboto, sans-serif;
         background:#0d1117; color:#e6edf3; }
  .card { max-width:34rem; padding:2.5rem 2rem; text-align:center; }
  h1 { font-size:1.4rem; margin:0 0 1rem; font-weight:650; color:#f85149; }
  p  { line-height:1.6; margin:.6rem 0; color:#c9d1d9; }
  code { background:#161b22; padding:.15rem .4rem; border-radius:4px; font-size:.85em; }
</style></head>
<body><!--WEBAGENT-INSTALL-FAILED--><div class="card">
  <h1>The WebAgent install didn't finish</h1>
  <p>The server was created, but setting up the app ran into a problem.</p>
  <p>Connect to the VM and read <code>/var/log/webagent-bootstrap.log</code> for the exact error,
  or tear this server down from <b>App Settings &rarr; Deploy</b> and try again.</p>
</div></body></html>
FAILEOF
  systemctl reload caddy 2>/dev/null || true
  exit $rc
}
trap 'on_error $LINENO' ERR

# ── Host firewall: allow HTTP/HTTPS (and keep SSH reachable) ──
# The cloud targets open 80/443 at the network layer (GCE firewall rule on the
# instance tag); this covers the host side so an existing-server (SSH) install
# with ufw enabled also serves the web. Idempotent and self-healing: it re-asserts
# the rules on every boot, and a failure here must NEVER block the install (ufw
# may be absent on minimal images). The SSH port is read from sshd itself so
# enabling ufw can never lock the admin out of a box with SSH on a custom port.
next_step "Allowing HTTP/HTTPS traffic"
if command -v ufw >/dev/null 2>&1; then
  SSH_PORT="$(sshd -T 2>/dev/null | awk '/^port[ \t]/{print $2; exit}')"
  SSH_PORT="${SSH_PORT:-22}"
  ufw allow "${SSH_PORT}/tcp" >/dev/null 2>&1 || true
  ufw allow 80/tcp >/dev/null 2>&1 || true
  ufw allow 443/tcp >/dev/null 2>&1 || true
  ufw --force enable >/dev/null 2>&1 || true
  echo "Host firewall: allowed SSH (${SSH_PORT}/tcp), HTTP (80), HTTPS (443)."
else
  echo "ufw not present — skipping host firewall (cloud firewall still applies)."
fi

# ── Detect subsequent boots: if the app is already installed, skip the fresh
# install and just ensure the systemd unit is current and the service is running.
# Without this guard, every VM restart would rm -rf the app directory, re-clone,
# and overwrite .env — destroying any saved data, uploads, or config changes.
ALREADY_INSTALLED=0
if [ -f "$APP_DIR/.venv/bin/python" ] && [ -f /etc/systemd/system/webagent.service ]; then
  echo "WebAgent is already installed — pulling latest code."
  ALREADY_INSTALLED=1
fi

if [ "$ALREADY_INSTALLED" = "1" ]; then
  # ── Update in-place: pull latest commit, reinstall deps if changed ──
  next_step "Pulling latest code" "${ORIGIN_URL:-$BRANCH} @ $BRANCH"
  git -C "$APP_DIR" fetch origin "$BRANCH"
  git -C "$APP_DIR" reset --hard "origin/$BRANCH"
  # Re-install dependencies in case requirements changed (best-effort; server
  # still starts even if this step fails — what matters is the code is fresh).
  next_step "Updating Python dependencies"
  sudo -u "$APP_USER" "$APP_DIR/.venv/bin/pip" install -r "$APP_DIR/requirements.txt" 2>&1 || true
else

# ── App user + code ──
# Show the CLEAN address (never $REPO_URL, which for a private repo carries the
# access token) so the token never lands in this log or the holding page.
next_step "Cloning WebAgent repository" "${ORIGIN_URL:-$BRANCH} @ $BRANCH"
id -u "$APP_USER" >/dev/null 2>&1 || useradd --system --create-home --shell /usr/sbin/nologin "$APP_USER"
rm -rf "$APP_DIR"
git clone --branch "$BRANCH" --depth 1 "$REPO_URL" "$APP_DIR"
# P2P identities and peer authorizations are machine-local runtime state. A
# release must not contain them, but defensively discard any accidentally
# tracked copy before this machine starts the app and mints its own identity.
rm -f "$APP_DIR/data/config/p2p/keypair.json"
rm -rf "$APP_DIR/data/config/p2p/peers"
rm -rf "$APP_DIR/data/config/p2p/runtime"
mkdir -p "$APP_DIR/data/config/p2p/peers" "$APP_DIR/data/config/p2p/runtime"
# The clone URL may embed the access token (private repo). Reset origin to the
# CLEAN address so the token is never persisted in the box's .git/config; the app
# authenticates future fetch/push with the token from its own vault instead.
if [ -n "$ORIGIN_URL" ]; then git -C "$APP_DIR" remote set-url origin "$ORIGIN_URL"; fi
chown -R "$APP_USER":"$APP_USER" "$APP_DIR"

# ── Python venv + dependencies (forgiving: optional packages never abort) ──
# The server boots fine WITHOUT several optional packages — SQLCipher at-rest
# encryption, the Playwright browser build, the Telegram/Twilio channels, the
# MySQL connector. So a single optional wheel with no binary for this platform
# must NOT kill the whole deploy (the old single `pip install -r` under `set -e`
# did exactly that). We install best-effort, fall back to the lighter
# no-Playwright manifest, then guarantee the handful of packages the app cannot
# even import without. What actually decides success is the post-start
# self-check further down — not whether every last wheel installed.
next_step "Creating Python environment"
PIP="$APP_DIR/.venv/bin/pip"
PY="$APP_DIR/.venv/bin/python"
sudo -u "$APP_USER" env UV_PYTHON_INSTALL_DIR="$UV_PYTHON_DIR" \
  "$UV_BIN" venv --seed --python 3.14 "$APP_DIR/.venv"
sudo -u "$APP_USER" "$PIP" install --upgrade pip wheel setuptools

# Streams pip's own "Collecting <pkg>" lines into the status file's detail field,
# so the holding page shows the actual dependency being installed right now —
# not just a static "installing dependencies" label. `${PIPESTATUS[0]}` (not the
# `while` loop's own exit code) is what tells the caller whether pip succeeded.
install_reqs() {
  sudo -u "$APP_USER" "$PIP" install -r "$1" 2>&1 | while IFS= read -r line; do
    echo "$line"
    case "$line" in
      Collecting\ *) set_detail "${line#Collecting }" ;;
      Installing\ collected\ packages*) set_detail "Finalizing packages" ;;
    esac
  done
  return "${PIPESTATUS[0]}"
}

# `if !` and `|| true` keep every line below exempt from `set -e`, so a failing
# optional wheel is tolerated instead of aborting the install.
next_step "Installing Python dependencies"
if ! install_reqs "$APP_DIR/requirements.txt"; then
  echo "Full requirements install hit a snag — retrying with the lighter set."
  set_detail "Retrying with the lighter dependency set"
  [ -f "$APP_DIR/req_no_playwright.txt" ] && \
    install_reqs "$APP_DIR/req_no_playwright.txt" || true
  # The non-negotiable core the web server needs just to start + serve.
  set_detail "Installing core packages"
  sudo -u "$APP_USER" "$PIP" install \
    fastapi "uvicorn[standard]" wsproto pydantic python-dotenv python-multipart \
    "PyJWT[crypto]" bcrypt httpx openai tiktoken numpy "Pillow>=12.3.0" tzdata \
    "psycopg[binary]" asyncpg || true
fi

# The Playwright pip package alone can't drive a browser — it needs the Chromium
# build plus a set of shared libraries. Best-effort AFTER the core is in: if it
# fails the browser tools simply stay unavailable and the server still runs. The
# OS libs go in as root; the browser itself downloads as the app user so it lands
# in that user's cache where the service (running as that user) will find it.
next_step "Installing browser automation tools"
if sudo -u "$APP_USER" "$PY" -c "import playwright" 2>/dev/null; then
  "$APP_DIR/.venv/bin/playwright" install-deps chromium || true
  sudo -u "$APP_USER" "$APP_DIR/.venv/bin/playwright" install chromium || true
fi

# ── .env (minimal; LLM keys are set in-app) ──
next_step "Writing configuration"
# A QUOTED heredoc delimiter ('ENVEOF') so nothing inside is shell-expanded —
# the admin-password line (when present) is written byte-for-byte, never
# re-interpreted. The whole body is substituted from Python (the env-body slot
# just below), so the port and the optional admin line are already baked in.
# NOTE: do NOT write the literal placeholder token in this comment — the
# renderer replaces every occurrence, and a multi-line body (port + admin
# password) would split this comment and leave a stray shell line.
cat > "$APP_DIR/.env" <<'ENVEOF'
__ENV_BODY__
ENVEOF
chown "$APP_USER":"$APP_USER" "$APP_DIR/.env"

# ── Pre-loaded configuration bundle (optional) ──
# When the admin chose "Include this app's configuration" on the Deploy panel, a
# password-locked WABOOT1 setup code is written here as bootstrap.json. On first
# boot the app (app/admin/bootstrap_bundle.apply_boot_file) decrypts it with the
# BOOTSTRAP_ADMIN_PASSWORD baked into .env above, applies the carried AI / vault /
# database config, activates the database, then renames the file. A QUOTED heredoc
# ('BOOTEOF') so the code is written byte-for-byte. The whole block is substituted
# from Python — it is empty when nothing was embedded.
__BOOTSTRAP_WRITE__

fi  # end of ALREADY_INSTALLED — fresh install or in-place update

# ── systemd service ──
next_step "Installing system service"
cat > /etc/systemd/system/webagent.service <<UNITEOF
[Unit]
Description=WebAgent FastAPI server
After=network.target

[Service]
Type=simple
User=$APP_USER
Group=$APP_USER
WorkingDirectory=$APP_DIR
EnvironmentFile=$APP_DIR/.env
ExecStart=$APP_DIR/.venv/bin/uvicorn app.main:app --host 127.0.0.1 --port $PORT --ws wsproto --proxy-headers --timeout-keep-alive 300 --no-access-log
Restart=always
RestartSec=2
StandardOutput=journal
StandardError=journal
SyslogIdentifier=webagent

[Install]
WantedBy=multi-user.target
UNITEOF

next_step "Starting WebAgent"
systemctl daemon-reload
systemctl enable webagent
systemctl restart webagent
# Caddy was already configured up top (holding page → app). A reload re-asserts
# the config; the status page now auto-yields to the live backend.
systemctl reload caddy 2>/dev/null || systemctl restart caddy

# ── Prove the app actually answers before declaring success ──
# `systemctl restart` returns immediately; the app may still be importing, or may
# crash-loop on a bad/missing dependency. Poll the local port for up to ~2 min.
# If it never answers we deliberately trip the ERR trap (honest error page +
# non-zero exit), so a broken install shows as "didn't finish" — never a false
# "complete". The `if` test exempts the probe from `set -e` while it warms up.
echo "Waiting for the app to answer on 127.0.0.1:$PORT …"
# ANY HTTP response means uvicorn is up — even a 401/403 auth gate. We must NOT
# use `curl -f` here (it treats 4xx as failure), or an app that gates '/' behind
# auth would be mis-read as down. curl returns non-zero only when it can't
# connect at all (refused / timeout) — exactly the "not up yet" signal we want.
APP_UP=0
for _try in $(seq 1 60); do
  set_detail "Waiting for the app to respond (attempt $_try/60)"
  if curl -sS -o /dev/null --max-time 3 "http://127.0.0.1:$PORT/"; then APP_UP=1; break; fi
  sleep 2
done
if [ "$APP_UP" != "1" ]; then
  echo "The app did not answer on port $PORT after ~2 minutes. Recent service log:"
  journalctl -u webagent --no-pager -n 50 || true
  false      # → ERR trap → honest error page + non-zero exit
fi
echo "App answered on port $PORT — WebAgent is live."

# ── Server Manager TUI (the `webagent` command, installed alongside) ──
# Put the standalone Server Manager TUI on the box so an admin who SSHes in can
# run `webagent` to inspect / restart / diagnose the install from a terminal. The
# web server itself keeps running under the systemd unit above — this is just the
# manager front door, so WEBAGENT_TUI_NO_SUPERVISE=1 stops it from auto-starting
# or keep-aliving a SECOND, competing server. Wrapped so a hiccup here never
# blocks the deploy: the server is what matters and it is already up.
next_step "Installing Server Manager tool"
if [ -d "$APP_DIR/TUI" ]; then
  ( set -e
    sudo -u "$APP_USER" env UV_PYTHON_INSTALL_DIR="$UV_PYTHON_DIR" \
      "$UV_BIN" venv --seed --python 3.14 "$APP_DIR/TUI/.venv"
    sudo -u "$APP_USER" "$APP_DIR/TUI/.venv/bin/pip" install --upgrade pip wheel
    sudo -u "$APP_USER" "$APP_DIR/TUI/.venv/bin/pip" install -e "$APP_DIR/TUI"
    cat > /usr/local/bin/webagent <<WALAUNCH
#!/bin/bash
# WebAgent Server Manager (TUI) — installed alongside the server by the in-app
# Deploy bootstrap. The systemd unit owns the running server; this manager just
# inspects / restarts it on request (no auto-start, no keep-alive guardian).
export WEBAGENT_PROJECT="$APP_DIR"
export WEBAGENT_TUI_NO_SUPERVISE=1
exec "$APP_DIR/TUI/.venv/bin/webagent" "\$@"
WALAUNCH
    chmod +x /usr/local/bin/webagent
    echo "Server Manager installed — run 'webagent' on the box to manage it."
  ) || echo "Server Manager (webagent command) install skipped; the web server is unaffected."
fi

echo "WebAgent bootstrap complete at $(date)"
"""


def _env_body(port: int, admin_password: str) -> str:
    """The literal contents of the VM's ``.env`` — the port, plus (optionally) a
    pre-set admin password:

      * ``admin_password`` set → ``BOOTSTRAP_ADMIN_PASSWORD=…`` so the server
        self-creates the admin on first boot (the "set the password now" choice);
      * blank → just the port. The first visitor to the server's address then sets
        the password via the setup page (that page is open until an admin exists —
        there is no localhost restriction to work around anymore).

    Written inside a QUOTED heredoc, so the password is stored verbatim; we only
    strip CR/LF here (a newline would end the .env line / heredoc)."""
    lines = [f"PORT={port or 8080}"]
    pw = (admin_password or "").replace("\r", "").replace("\n", "").strip()
    if pw:
        lines.append(f"BOOTSTRAP_ADMIN_PASSWORD={pw}")
    return "\n".join(lines)


def _bootstrap_write_block(bootstrap_code: str) -> str:
    """The shell that writes the optional pre-loaded ``bootstrap.json`` on the box.

    Empty when nothing was embedded. The code is a ``WABOOT1.<b64url>.<token>``
    string — base64url + dots, no shell metacharacters — so a QUOTED heredoc
    ('BOOTEOF', nothing expanded) writes it verbatim and safely."""
    code = (bootstrap_code or "").strip()
    if not code:
        return ""
    return (
        'echo "Writing pre-loaded configuration bundle"\n'
        'cat > "$APP_DIR/bootstrap.json" <<\'BOOTEOF\'\n'
        f"{code}\n"
        "BOOTEOF\n"
        'chown "$APP_USER":"$APP_USER" "$APP_DIR/bootstrap.json"'
    )


def build_install_script(
    *, repo_url: str, origin_url: str = "", branch: str = "main", domain: str = "",
    port: int = 8080, admin_password: str = "", bootstrap_code: str = "",
) -> str:
    """Render the install script for one deploy with the chosen settings.

    ``repo_url`` is the clone URL — for a PRIVATE repo it carries the access token
    so the box can fetch it. ``origin_url`` is the CLEAN address (no token); when
    given, the box resets its git ``origin`` to it after cloning so the token is
    never persisted in ``.git/config`` (and never printed to the install log).
    Defaults to ``repo_url`` when omitted (public repos, where they're identical).

    ``admin_password`` (optional) pre-sets the first admin on the new VM (see
    ``_env_body``). Leave it blank to let the first visitor set the password.

    ``bootstrap_code`` (optional) is a password-locked ``WABOOT1`` setup code
    (from ``app/deploy/config_embed``) carrying this app's AI / vault / database
    config; when present it is written as ``bootstrap.json`` on the box and applied
    on first boot (unlocked with ``admin_password`` via ``BOOTSTRAP_ADMIN_PASSWORD``)."""
    clean_origin = (origin_url or repo_url or "").strip()
    return (
        _SCRIPT
        .replace("__REPO_URL__", (repo_url or "").strip())
        .replace("__ORIGIN_URL__", clean_origin)
        .replace("__BRANCH__", (branch or "main").strip() or "main")
        .replace("__DOMAIN__", (domain or "").strip())
        .replace("__PORT__", str(port or 8080))
        .replace("__ENV_BODY__", _env_body(port, admin_password))
        .replace("__BOOTSTRAP_WRITE__", _bootstrap_write_block(bootstrap_code))
    )


# A sensible default repo to deploy when the admin leaves the field blank: this
# project's public GitHub home. Overridable per-deploy in the panel.
DEFAULT_REPO_URL = "https://github.com/botboss3000/webagent.git"
DEFAULT_BRANCH = "main"

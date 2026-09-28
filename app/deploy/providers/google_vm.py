"""Deploy target: Google Compute Engine VM (the Phase-1 reference target).

Creates a fresh Ubuntu 24.04 VM, hands it the shared install recipe
(``app/deploy/bootstrap.py``) as the instance ``startup-script``, opens the
firewall for HTTP/HTTPS, and reports the public address. This is exactly how the
app already runs in production, so the steps are known-good.

No extra Python dependency: it drives the Compute Engine REST API directly with
``httpx``, authenticating with the admin's service-account JSON via the same
JWT-bearer token exchange the Google Cloud Scheduler provider uses (RFC 7523 →
an OAuth access token). ``cryptography`` (already a dependency) signs the JWT.
"""

from __future__ import annotations

import base64
import json
import logging
import re
import secrets as _secrets
import time
from typing import Any, AsyncIterator, Dict, Tuple

import httpx

from app.deploy.base import BaseDeployProvider, done, ev
from app.deploy.bootstrap import (
    DEFAULT_BRANCH, DEFAULT_REPO_URL, build_install_script,
)
from app.deploy.manual_common import resolve_clone

logger = logging.getLogger(__name__)

FEATURE = {
    "id": "google_vm",
    "display_name": "Google Compute VM",
    "category": "deploy",
    "status": "beta",
    "summary": "One-click deploy onto a fresh Google Compute Engine VM.",
    "requires": ["a Google service-account JSON with Compute Admin"],
}

_TOKEN_URL = "https://oauth2.googleapis.com/token"
_SCOPE = "https://www.googleapis.com/auth/cloud-platform"
_COMPUTE = "https://compute.googleapis.com/compute/v1"
_IMAGE = "projects/ubuntu-os-cloud/global/images/family/ubuntu-2404-lts-amd64"
_FW_NAME = "webagent-allow-http-https"
_NET_TAG = "webagent-http"
# When "save as an SSH server" is on, we authorise this login on the new VM (via
# instance ssh-keys metadata, which GCE's guest agent provisions with sudo) and
# save its freshly-generated private key to the SSH target's saved-servers list.
_SSH_LINK_USER = "webagent"


def _gen_ssh_keypair() -> Tuple[str, str]:
    """A fresh Ed25519 keypair → (OpenSSH private-key PEM, OpenSSH public line).
    Uses ``cryptography`` (already a dependency); the private key is loadable later
    by paramiko for the SSH deploy target."""
    from cryptography.hazmat.primitives import serialization
    from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
    key = Ed25519PrivateKey.generate()
    priv = key.private_bytes(
        serialization.Encoding.PEM,
        serialization.PrivateFormat.OpenSSH,
        serialization.NoEncryption(),
    ).decode("ascii")
    pub = key.public_key().public_bytes(
        serialization.Encoding.OpenSSH,
        serialization.PublicFormat.OpenSSH,
    ).decode("ascii")
    return priv, pub


def _b64url(data: bytes) -> str:
    return base64.urlsafe_b64encode(data).rstrip(b"=").decode("ascii")


def _google_error(r) -> str:
    """A short, human-readable message from a Google API error response — never
    the raw HTML error page Google serves for some failures."""
    try:
        msg = ((r.json().get("error") or {}).get("message") or "").strip()
        if msg:
            return msg
    except Exception:
        pass
    txt = (r.text or "").strip()
    if not txt or txt[:1] == "<" or "<html" in txt[:200].lower():
        return f"HTTP {r.status_code}"
    return f"HTTP {r.status_code}: {txt[:200]}"


def _compute_api_hint(r):
    """Turn the common 403 into a clear instruction when the Compute Engine API
    simply isn't enabled on the project; otherwise the plain error (or None so
    the caller can use its own fallback wording)."""
    try:
        msg = ((r.json().get("error") or {}).get("message") or "").strip()
    except Exception:
        msg = ""
    low = msg.lower()
    if "has not been used" in low or "is disabled" in low or "accessnotconfigured" in low:
        return ("The Compute Engine API isn't enabled on this project yet. In the Google Cloud "
                "console open APIs & Services → Enable APIs and turn on 'Compute Engine API', "
                "wait a minute, then refresh.")
    return msg or None


def _sign_rs256(private_key_pem: str, message: bytes) -> bytes:
    from cryptography.hazmat.primitives import hashes, serialization
    from cryptography.hazmat.primitives.asymmetric import padding
    key = serialization.load_pem_private_key(
        private_key_pem.encode("utf-8") if isinstance(private_key_pem, str) else private_key_pem,
        password=None,
    )
    return key.sign(message, padding.PKCS1v15(), hashes.SHA256())


def _load_sa(creds: Dict[str, Any]) -> Dict[str, str]:
    raw = creds.get("service_account_json") or ""
    if not raw:
        raise RuntimeError("No service-account JSON provided.")
    data = raw if isinstance(raw, dict) else json.loads(raw)
    if "client_email" not in data or "private_key" not in data:
        raise RuntimeError("Service-account JSON is missing client_email or private_key.")
    return data


async def _access_token(creds: Dict[str, Any]) -> str:
    sa = _load_sa(creds)
    now = int(time.time())
    header = {"alg": "RS256", "typ": "JWT", "kid": sa.get("private_key_id", "")}
    claims = {"iss": sa["client_email"], "scope": _SCOPE, "aud": _TOKEN_URL,
              "iat": now, "exp": now + 3600}
    h = _b64url(json.dumps(header, separators=(",", ":")).encode())
    c = _b64url(json.dumps(claims, separators=(",", ":")).encode())
    sig = _sign_rs256(sa["private_key"], f"{h}.{c}".encode("ascii"))
    assertion = f"{h}.{c}.{_b64url(sig)}"
    async with httpx.AsyncClient(timeout=20.0) as client:
        resp = await client.post(_TOKEN_URL, data={
            "grant_type": "urn:ietf:params:oauth:grant-type:jwt-bearer",
            "assertion": assertion,
        })
    if resp.status_code != 200:
        raise RuntimeError(f"Google token exchange failed: {resp.status_code} {resp.text[:200]}")
    return resp.json()["access_token"]


class GoogleVMProvider(BaseDeployProvider):
    id = "google_vm"
    display_name = "Google Compute VM"
    icon = "cloud"
    summary = "Create a Google Cloud VM and install WebAgent on it automatically."
    requires = ["A Google Cloud project", "A service-account JSON key with the Compute Admin role"]
    # This target can enumerate + manage every VM in the project, so it powers the
    # Cloud VMs admin page (list / start / stop / delete) — see list_instances +
    # instance_action below.
    supports_instances = True
    # The Cloud VMs sign-in panel collects this "id" (the project) plus the
    # service-account "key" below — enough to connect and list every VM.
    connect_config_keys = ["project_id"]
    # Extra non-secret settings mirrored into the shared vault alongside the project
    # id (see manager._mirror_keys), so a second device on a shared vault DB inherits
    # the deploy Zone too — WITHOUT the zone appearing in the sign-in form or gating
    # the "connected" check (those read connect_config_keys only).
    shared_config_keys = ["zone"]
    # Reveal the deploy form in STAGES so it never overwhelms: first only the
    # project ID (config_fields[0]), then the key (the first credential field),
    # then the rest of the settings + the action buttons. Honoured by deploy.js
    # (_renderProvider splits the fields; _applyCloudStages gates the reveal).
    progressive = True

    config_fields = [
        {"key": "project_id", "label": "Google Cloud project ID", "type": "text",
         "required": True, "placeholder": "my-project-123456",
         # A clickable helper link under the field + a rich tip whose bubble carries
         # two screenshots of where the ID appears in the console (deploy.js renders
         # `link` and the {text, images} tip; see _buildField / _showTip).
         "link": {"url": "https://console.cloud.google.com/",
                  "label": "Open the Google Cloud console ↗"},
         "tip": {
             "text": "Your project's ID (not its display name). Find it in the "
                     "Google Cloud console top-bar project picker, or on the welcome "
                     "page. No project yet? Create one there first — creating a "
                     "project is free.",
             "images": [
                 "/ui/main-panel/app/settings/data-settings/img/gcp-project-welcome.png",
                 "/ui/main-panel/app/settings/data-settings/img/gcp-project-picker.png",
             ],
         }},
        # A dropdown of common worldwide zones (pick by where your users are), plus
        # a "Custom…" entry that reveals a text box so ANY of Google's ~40 zones can
        # still be typed — see deploy.js `_buildField` (the `custom` select branch).
        {"key": "zone", "label": "Zone (data-centre location)", "type": "select",
         "default": "us-central1-a", "custom": True,
         "custom_label": "✏️ Custom… (type any zone)",
         "custom_placeholder": "e.g. us-west2-a — any Google Cloud zone",
         "tip": "Where the server physically lives. Pick the location closest to most "
                "of your users for speed. The list covers the common regions; choose "
                "“Custom…” to type any other Google Cloud zone. Full list: Cloud "
                "console → Compute Engine → Zones.",
         "options": [
             {"value": "us-central1-a", "label": "🇺🇸 US Central — Iowa"},
             {"value": "us-east1-b", "label": "🇺🇸 US East — S. Carolina"},
             {"value": "us-west1-a", "label": "🇺🇸 US West — Oregon"},
             {"value": "northamerica-northeast1-a", "label": "🇨🇦 Canada — Montréal"},
             {"value": "europe-west2-a", "label": "🇬🇧 Europe — London"},
             {"value": "europe-west1-b", "label": "🇧🇪 Europe — Belgium"},
             {"value": "europe-west3-a", "label": "🇩🇪 Europe — Frankfurt"},
             {"value": "asia-southeast1-a", "label": "🇸🇬 Asia — Singapore"},
             {"value": "asia-northeast1-a", "label": "🇯🇵 Asia — Tokyo"},
             {"value": "asia-south1-a", "label": "🇮🇳 Asia — Mumbai"},
             {"value": "australia-southeast1-a", "label": "🇦🇺 Australia — Sydney"},
             {"value": "southamerica-east1-a", "label": "🇧🇷 South America — São Paulo"},
         ]},
        {"key": "machine_type", "label": "Machine size", "type": "select",
         "default": "e2-small",
         "tip": "Bigger machines cost more per month but serve more people at once. "
                "Pick a starting size below — you can resize the server later if you "
                "outgrow it. The note under the menu describes who each size suits. "
                "The first option is Google's free tier — read its note for the catch.",
         "options": [
             {"value": "e2-micro", "label": "Free tier — e2-micro · 2 vCPU (shared) · 1 GB",
              "note": "Free forever on Google's Always-Free tier — but with strict "
                      "limits. It only stays free when the zone is one of US Iowa, "
                      "Oregon or S. Carolina (the default US Central above qualifies), "
                      "only ONE such VM per Google account counts as free, and only "
                      "the first 30 GB of disk and 1 GB/month of North-America network "
                      "traffic are free — go over any of these and Google bills the "
                      "excess at normal rates. It has just 1 GB of memory, which is "
                      "too small to run the browser tools or several agents on the box "
                      "itself — those will run it out of memory. Best used as a tiny, "
                      "always-on hub that hands the heavy work off to your other, "
                      "bigger devices (see Remote Control)."},
             {"value": "e2-small", "label": "Small — e2-small · 2 vCPU · 2 GB",
              "note": "Cheapest. Best for trying it out or a personal instance — "
                      "about 1–3 people using it at once. Tight on memory: heavy "
                      "browser automation or many agents running together can run it "
                      "out of RAM."},
             {"value": "e2-medium", "label": "Medium — e2-medium · 2 vCPU · 4 GB",
              "note": "Recommended for a small team — about 5–10 active users. "
                      "Comfortable room for the browser tools and a few agent runs at "
                      "the same time."},
             {"value": "e2-standard-2", "label": "Standard — e2-standard-2 · 2 vCPU · 8 GB",
              "note": "For a busy team or heavier automation — roughly 15–25 active "
                      "users or many parallel agent runs. Costs the most of the "
                      "three; choose this if the smaller sizes feel sluggish."},
         ]},
        {"key": "repo_url", "label": "Code to install (git repository)", "type": "text",
         "default": DEFAULT_REPO_URL, "placeholder": DEFAULT_REPO_URL,
         "tip": "Leave this as-is to install the standard WebAgent. Only change it if "
                "you maintain your own copy (a fork) and want the server to run that "
                "instead."},
        # public / private. Supplied by the shared "Repo details" bar in the New-
        # deployment panel (hidden from this cloud form); a private repo also needs a
        # github_token below so the VM can clone it. Non-secret, so it lives here.
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
         "placeholder": "leave blank for a plain IP address",
         "tip": "Optional. If you own a domain (e.g. app.yourcompany.com), enter it "
                "and point its DNS 'A' record at the server's IP — the server then "
                "gets a free HTTPS certificate automatically. Leave blank to reach "
                "the app at the server's raw IP over plain http."},
        {"key": "forget_keys", "label": "Forget my Google Cloud key after deploy", "type": "checkbox",
         "default": True,
         "tip": "About your GOOGLE CLOUD account key (the service-account JSON below) — "
                "NOT the SSH key. Recommended: deletes it from the app's vault the "
                "moment the deploy succeeds, so the app keeps no standing power to "
                "create, delete or bill servers on your cloud account. This is separate "
                "from the SSH access below — you can forget this and still keep SSH "
                "access to the VM. You'll be asked for this key again only if you tear "
                "the server down later."},
        {"key": "save_ssh_server", "label": "Keep SSH access to this VM (saved server)", "type": "checkbox",
         "default": True,
         "tip": "About a SEPARATE, narrow key for THIS ONE VM — not your cloud account. "
                "Recommended: generates an SSH login for the new VM and adds it to the "
                "'Existing server (SSH)' saved-servers list, so afterwards you can "
                "reconnect to re-install, update or stop just this machine over SSH — no "
                "cloud key needed. The SSH key is generated for you and stored encrypted. "
                "Independent of the cloud-key option above."},
    ]
    credential_fields = [
        {"key": "service_account_json", "label": "Service-account key (JSON)",
         "type": "textarea", "secret": True, "required": True,
         "placeholder": "Paste the whole JSON key file here — or drag the downloaded "
                        ".json file onto this box",
         # Accept the downloaded key file dragged straight onto the box (deploy.js
         # _wireDropzone reads it as text and fills the field as if pasted).
         "dropzone": True,
         # A DYNAMIC helper link: the Service-Accounts page for the entered project.
         # {project_id} is substituted from the project-ID field live, and the link
         # stays hidden until an ID is typed (deploy.js _applyDynLink).
         "link": {"dynamic": True,
                  "url": "https://console.cloud.google.com/iam-admin/serviceaccounts"
                         "?authuser=2&project={project_id}",
                  "label": "Open Service Accounts for “{project_id}” ↗"},
         "tip": {
             "text": "In the Cloud console: IAM & Admin → Service Accounts → create "
                     "one with the 'Compute Admin' role, then open it → Keys → Add "
                     "key → JSON and download the file. Paste the file's entire "
                     "contents here (or drag the downloaded file straight onto the "
                     "box). It's stored encrypted, and (with the option above) "
                     "deleted after deploy.",
             # A dynamic "create one" deep-link (also built from the project ID) plus
             # two screenshots of the create-role + add-key steps.
             "link": {"dynamic": True,
                      "url": "https://console.cloud.google.com/iam-admin/"
                             "serviceaccounts/create?authuser=2&project={project_id}",
                      "label": "Create a service account for “{project_id}” ↗"},
             "images": [
                 "/ui/main-panel/app/settings/data-settings/img/gcp-sa-create-role.png",
                 "/ui/main-panel/app/settings/data-settings/img/gcp-sa-keys-addkey.png",
             ],
         }},
        # OPTIONAL GitHub access token for a PRIVATE repository. Supplied by the shared
        # "Repo details" bar (hidden from this cloud form). A SECRET so it rides the
        # encrypted vault and is auto-discarded after a successful deploy with the
        # cloud key. Embedded into the clone URL sent to the new VM (see deploy()).
        {"key": "github_token", "label": "GitHub access token (private repo)",
         "type": "password", "secret": True, "required": False,
         "placeholder": "only for a private repository",
         "tip": "Only needed if the repository above is private. Used once to let the "
                "new server clone it, then discarded with the cloud key."},
        # OPTIONAL. A SECRET so it rides the encrypted vault (never deploy.json) and
        # is auto-discarded after a successful deploy with the cloud key. The Deploy
        # panel renders this into its OWN slot (above the cloud key), NOT the generic
        # cloud-key list — see deploy.js `_renderProvider` / `#ac-deploy-admin`.
        {"key": "admin_password", "label": "Admin password (optional)",
         "type": "password", "secret": True, "required": False,
         "placeholder": "leave blank — first visitor sets it",
         "tip": "Optional — leave it blank to deploy the server as-is, with no admin "
                "password set: the first person to open its address then picks the "
                "password on the setup page. Or type one here (6+ characters) to "
                "pre-set it, so the admin is ready before the address is ever public "
                "(the account is created automatically on first boot). Stored "
                "encrypted, and discarded once the deploy succeeds."},
    ]
    # Only the cloud key is required to deploy — the admin password is optional.
    credential_required = ["service_account_json"]

    def available(self) -> Tuple[bool, str]:
        return True, ""

    # ── test ──
    async def test(self, config: Dict[str, Any], creds: Dict[str, Any]) -> Dict[str, Any]:
        project = (config.get("project_id") or "").strip()
        if not project:
            return {"ok": False, "detail": "Enter your Google Cloud project ID first."}
        try:
            token = await _access_token(creds)
        except Exception as e:
            return {"ok": False, "detail": str(e)}
        # Cheap authorized call: list zones (pageSize 1) under the project.
        url = f"{_COMPUTE}/projects/{project}/zones?maxResults=1"
        try:
            async with httpx.AsyncClient(timeout=20.0) as client:
                r = await client.get(url, headers={"Authorization": f"Bearer {token}"})
            if r.status_code == 200:
                return {"ok": True, "detail": f"Connected to project {project}."}
            if r.status_code == 403:
                return {"ok": False, "detail": "Authenticated, but this service account lacks Compute permissions (needs Compute Admin)."}
            return {"ok": False, "detail": f"HTTP {r.status_code}: {r.text[:200]}"}
        except Exception as e:
            return {"ok": False, "detail": str(e)}

    # ── helpers ──
    async def _ensure_firewall(self, client: httpx.AsyncClient, project: str, token: str) -> None:
        """Make sure the HTTP/HTTPS ingress rule exists AND matches what we need.

        Creates the rule if missing; if a rule with our name already exists
        (previous deploy, or a 409), GETs it and PUTs a corrected version when
        its ports / target tag / source ranges differ — so a stale or hand-edited
        rule can never silently keep HTTP/HTTPS closed. Failures are logged but
        never fatal (the app is still reachable on hosts where the network layer
        already permits 80/443)."""
        url = f"{_COMPUTE}/projects/{project}/global/firewalls"
        body = {
            "name": _FW_NAME,
            "network": "global/networks/default",
            "direction": "INGRESS",
            "allowed": [{"IPProtocol": "tcp", "ports": ["80", "443"]}],
            "sourceRanges": ["0.0.0.0/0"],
            "targetTags": [_NET_TAG],
        }
        headers = {"Authorization": f"Bearer {token}", "Content-Type": "application/json"}
        r = await client.post(url, headers=headers, json=body)
        if r.status_code in (200, 201):
            return
        if r.status_code != 409:
            # Anything else is worth surfacing but not fatal.
            logger.info("firewall ensure returned %s: %s", r.status_code, r.text[:200])
            return
        # Rule already exists — verify it still opens 80/443 for our tag.
        try:
            g = await client.get(f"{url}/{_FW_NAME}", headers=headers)
            if g.status_code != 200:
                logger.info("firewall verify GET returned %s: %s", g.status_code, g.text[:200])
                return
            existing = g.json()
            got_ports = sorted(
                p for a in existing.get("allowed", []) if a.get("IPProtocol") == "tcp"
                for p in a.get("ports", []))
            got_tags = sorted(existing.get("targetTags", []))
            got_ranges = sorted(existing.get("sourceRanges", []))
            want_ports = sorted(body["allowed"][0]["ports"])
            if got_ports == want_ports and got_tags == sorted([_NET_TAG]) \
                    and got_ranges == sorted(["0.0.0.0/0"]):
                return
            logger.info("firewall rule %s differs (%s ports, %s tags, %s ranges) — updating",
                        _FW_NAME, got_ports, got_tags, got_ranges)
            u = await client.put(f"{url}/{_FW_NAME}", headers=headers, json=body)
            if u.status_code not in (200, 201):
                logger.info("firewall update returned %s: %s", u.status_code, u.text[:200])
        except Exception:
            logger.exception("firewall verify/update failed")

    async def _poll_zone_op(self, client: httpx.AsyncClient, project: str, zone: str,
                            op_name: str, token: str, *, tries: int = 60) -> Dict[str, Any]:
        url = f"{_COMPUTE}/projects/{project}/zones/{zone}/operations/{op_name}"
        for _ in range(tries):
            r = await client.get(url, headers={"Authorization": f"Bearer {token}"})
            body = r.json() if r.content else {}
            if body.get("status") == "DONE":
                return body
            import asyncio
            await asyncio.sleep(2)
        return {"status": "TIMEOUT"}

    async def _poll_app_health(
        self,
        ip: str,
        *,
        timeout_s: int = 600,
        require_scoped_p2p: bool = False,
    ) -> AsyncIterator[Dict[str, Any]]:
        """Watch a freshly-created raw-IP VM until its app actually answers.

        Async generator: it yields live install-progress events parsed from the
        box's own ``/webagent-status.json`` (the step/status file the bootstrap
        writes), so the Deploy panel shows the REAL on-server steps — "[3/13]
        Installing system packages", the pip "Collecting …" lines — instead of
        only our own waiting. It finishes by yielding a terminal
        ``{"phase": "verify-result", "state": …}`` event whose state is one of:

          * ``"running"``    — the app responded (a real, non-holding response);
          * ``"failed"``     — the box's holding page flipped to the bootstrap's
                               error marker (the install aborted);
          * ``"installing"`` — still going when we stopped waiting (honest, not a
                               failure — the on-box holding page carries on).

        Only usable on the plain-IP path: with a custom domain Caddy serves over
        HTTPS on the domain and needs the admin's DNS first, so there is nothing
        to poll yet. The bootstrap tags its holding page ``WEBAGENT-HOLDING`` and
        its failure page ``WEBAGENT-INSTALL-FAILED`` so we can tell
        install-in-progress and install-broken apart from the live app."""
        import asyncio
        # Never probe `/` for readiness. During apt installation Caddy briefly
        # serves its stock welcome page before our holding-page configuration is
        # loaded; that unrelated HTTP 200 is not the WebAgent application.
        probe_path = "/api/v1/p2p/status" if require_scoped_p2p else "/api/v1/boot"
        probe = f"http://{ip}{probe_path}"
        status_url = f"http://{ip}/webagent-status.json"
        deadline = time.time() + timeout_s
        # Install-progress dedupe: emit one line per (step, label) change and per
        # detail change (the pip "Collecting <pkg>" lines); never repeat. The
        # bootstrap's "Waiting for the app to respond (attempt N/60)" self-check
        # lines are suppressed — they'd spam the tail of the log for no signal.
        last_key: Tuple = (None, None)
        last_detail = ""
        last_step_line = ""
        async with httpx.AsyncClient(timeout=8.0, follow_redirects=True) as hc:
            while time.time() < deadline:
                await asyncio.sleep(8)
                # The status file is only served once Caddy is installed and
                # configured (steps 4-5 of the bootstrap) — before that the box
                # isn't answering HTTP at all, which is exactly what this ignores.
                try:
                    r = await hc.get(status_url)
                    if r.status_code == 200:
                        d = r.json()
                        step, total = d.get("step"), d.get("total")
                        label = str(d.get("label") or "").strip()
                        detail = str(d.get("detail") or "").strip()
                        key = (step, label)
                        if key != last_key and label:
                            last_key = key
                            last_detail = detail
                            head = f"[{step}/{total}] {label}" if total else label
                            last_step_line = f"{head}{(' — ' + detail) if detail else ''}"
                            yield ev(last_step_line, phase="log")
                        elif (detail and detail != last_detail
                              and not detail.startswith("Waiting for the app to respond")):
                            last_detail = detail
                            yield ev(detail, phase="log")
                except Exception:
                    pass          # not served / not JSON yet — the probe decides readiness
                try:
                    r = await hc.get(probe)
                except Exception:
                    continue          # Caddy / network not up yet — keep waiting
                body = r.text or ""
                if "WEBAGENT-INSTALL-FAILED" in body:
                    if last_step_line:
                        yield ev(f"Install stopped at: {last_step_line}", phase="log", level="warn")
                    yield {"phase": "verify-result", "state": "failed"}
                    return
                if "WEBAGENT-HOLDING" in body:
                    continue          # still installing — the holding page is up
                if require_scoped_p2p:
                    try:
                        capabilities = (r.json().get("capabilities") or {}) if r.status_code == 200 else {}
                    except Exception:
                        capabilities = {}
                    if capabilities.get("scoped_bootstrap"):
                        yield {"phase": "verify-result", "state": "running"}
                        return
                    continue
                # The boot endpoint may be auth-gated on some editions. Any
                # non-404, non-5xx response still proves the app owns the path.
                if r.status_code < 500 and r.status_code != 404:
                    yield {"phase": "verify-result", "state": "running"}
                    return
        yield {"phase": "verify-result", "state": "installing"}

    # ── deploy ──
    async def deploy(self, config: Dict[str, Any], creds: Dict[str, Any]) -> AsyncIterator[Dict[str, Any]]:
        project = (config.get("project_id") or "").strip()
        zone = (config.get("zone") or "us-central1-a").strip() or "us-central1-a"
        machine = (config.get("machine_type") or "e2-small").strip() or "e2-small"
        repo = (config.get("repo_url") or DEFAULT_REPO_URL).strip() or DEFAULT_REPO_URL
        branch = (config.get("branch") or DEFAULT_BRANCH).strip() or DEFAULT_BRANCH
        domain = (config.get("domain") or "").strip()
        requested_name = str(config.get("instance_name") or "").strip()

        if requested_name and not re.fullmatch(
            r"[a-z](?:[-a-z0-9]{0,61}[a-z0-9])?", requested_name
        ):
            yield done({
                "ok": False,
                "message": (
                    "Instance name must use 1–63 lowercase letters, numbers or "
                    "hyphens, start with a letter and end with a letter or number."
                ),
            })
            return

        # Resolve the clone URL from the shared repo details. For a PRIVATE repo the
        # access token is embedded (https://TOKEN@host/…) so the new VM can fetch it;
        # the token rides this one deploy, then the vault discards it with the cloud
        # key. Reuses the manual-target helper (app/deploy/manual_common.resolve_clone).
        # NB: `git_token`, not `token` — `token` is bound to the Google OAuth token below.
        visibility = str(config.get("visibility") or "public").strip().lower()
        git_token = (creds.get("github_token") or "").strip()
        if visibility == "private" and not git_token:
            yield done({"ok": False, "message": "That repository is private — add a GitHub access token in Repo details, or set the repository to Public."})
            return
        _clone = resolve_clone(repo, visibility, git_token)
        clone_url = _clone["clone_url"]        # may embed the token (private repo)
        origin_url = _clone["clean_url"]       # clean address the box's origin resets to

        # Optional pre-set admin password (from the vault). Blank → the first visitor
        # to the server's address sets it via the setup page (open until an admin
        # exists). Typed → bake BOOTSTRAP_ADMIN_PASSWORD in (see bootstrap._env_body).
        admin_password = (creds.get("admin_password") or "").strip()
        if admin_password and len(admin_password) < 6:
            yield done({"ok": False, "message": "The admin password must be at least 6 characters (or leave it blank to set it on first visit)."})
            return

        if not project:
            yield done({"ok": False, "message": "No Google Cloud project ID set."})
            return

        yield ev("Authenticating with Google Cloud…", phase="auth")
        try:
            token = await _access_token(creds)
        except Exception as e:
            yield done({"ok": False, "message": f"Could not authenticate: {e}"})
            return

        name = requested_name or f"webagent-{int(time.time())}-{_secrets.token_hex(2)}"
        startup = build_install_script(
            repo_url=clone_url, origin_url=origin_url, branch=branch, domain=domain, port=8080,
            admin_password=admin_password,
            # Optional pre-loaded configuration bundle (the Deploy panel's "Include
            # this app's configuration" toggle). The manager gathered + encrypted it
            # into config["_bootstrap_code"] before this deploy; blank when off.
            bootstrap_code=config.get("_bootstrap_code", ""),
        )
        # Optionally generate an SSH login for the VM so it can be added to the SSH
        # target's saved servers afterwards. GCE's guest agent provisions the user
        # (with sudo) from the ``ssh-keys`` metadata; we keep the private half to
        # store against the saved server on success.
        save_ssh = bool(config.get("save_ssh_server", True))
        ssh_priv = ssh_pub = ""
        if save_ssh:
            try:
                ssh_priv, ssh_pub = _gen_ssh_keypair()
            except Exception:
                logger.exception("ssh keypair generation failed; skipping SSH link")
                save_ssh = False

        metadata_items = [{"key": "startup-script", "value": startup}]
        if save_ssh and ssh_pub:
            metadata_items.append({"key": "ssh-keys",
                                   "value": f"{_SSH_LINK_USER}:{ssh_pub} {_SSH_LINK_USER}"})
        instance_body = {
            "name": name,
            "machineType": f"zones/{zone}/machineTypes/{machine}",
            "disks": [{
                "boot": True, "autoDelete": True,
                "initializeParams": {"sourceImage": _IMAGE, "diskSizeGb": "20"},
            }],
            "networkInterfaces": [{
                "network": "global/networks/default",
                "accessConfigs": [{"type": "ONE_TO_ONE_NAT", "name": "External NAT"}],
            }],
            "tags": {"items": [_NET_TAG]},
            "metadata": {"items": metadata_items},
        }

        try:
            async with httpx.AsyncClient(timeout=60.0) as client:
                yield ev("Opening the firewall for web traffic (ports 80/443)…", phase="firewall")
                await self._ensure_firewall(client, project, token)

                yield ev(f"Creating the {machine} VM '{name}' in {zone}…", phase="create")
                create_url = f"{_COMPUTE}/projects/{project}/zones/{zone}/instances"
                r = await client.post(create_url, headers={
                    "Authorization": f"Bearer {token}", "Content-Type": "application/json",
                }, json=instance_body)
                if r.status_code >= 300:
                    yield done({"ok": False, "message": f"Create failed: HTTP {r.status_code} — {r.text[:300]}"})
                    return
                op = r.json()
                yield ev("Waiting for the VM to come up…", phase="provision")
                op_done = await self._poll_zone_op(client, project, zone, op.get("name", ""), token)
                if op_done.get("status") != "DONE":
                    yield done({"ok": False, "message": "Timed out waiting for the VM to be created."})
                    return
                if op_done.get("error"):
                    errs = op_done["error"].get("errors", [{}])
                    yield done({"ok": False, "message": f"VM creation error: {errs[0].get('message', 'unknown')}"})
                    return

                yield ev("Reading the server's public address…", phase="address")
                get_url = f"{_COMPUTE}/projects/{project}/zones/{zone}/instances/{name}"
                gi = await client.get(get_url, headers={"Authorization": f"Bearer {token}"})
                ip = ""
                try:
                    nics = gi.json().get("networkInterfaces", [])
                    ip = nics[0]["accessConfigs"][0].get("natIP", "")
                except Exception:
                    ip = ""
        except Exception as e:
            logger.exception("google_vm deploy failed")
            yield done({"ok": False, "message": f"Deploy failed: {e}"})
            return

        public_url = (f"https://{domain}" if domain else (f"http://{ip}" if ip else ""))
        yield ev("VM created. It is now installing WebAgent — this takes a few minutes.", phase="installing", level="ok")

        # Don't claim success until the app actually answers. The old code returned
        # "installing" the instant the VM existed, having verified nothing — so a
        # bootstrap that later failed still showed a green "Deployed". Now we watch
        # the box until its app responds (or its holding page flips to the error
        # marker, or we stop waiting). Only pollable on the raw-IP path: a custom
        # domain needs the admin's DNS before Caddy will serve it, so we keep the
        # honest "installing + point your DNS" messaging in that case.
        state = "installing"
        if ip and not domain:
            yield ev("Verifying the app comes up on the server (this can take a few minutes)…", phase="verify")
            try:
                # The health poll is an async generator: it yields the box's live
                # install-progress lines (from its /webagent-status.json) as it
                # waits, then a terminal "verify-result" event carrying the final
                # state — see _poll_app_health.
                poll = self._poll_app_health(
                    ip,
                    timeout_s=int(config.get("_health_timeout_s") or 600),
                    require_scoped_p2p=bool(config.get("_require_scoped_p2p")),
                )
                async for event in poll:
                    if event.get("phase") == "verify-result":
                        state = event.get("state", "installing")
                        continue
                    yield event
            except Exception:
                logger.exception("google_vm health poll failed")
                state = "installing"
            if state == "running":
                yield ev("The app is up and answering — WebAgent is live.", phase="live", level="ok")
            elif state == "failed":
                yield ev("The VM was created, but the install reported a problem on the box. Open its "
                         "address to see the error, or delete it and try again.", phase="failed", level="err")
            else:
                yield ev("Still installing — it's taking longer than usual. The server's address shows "
                         "live progress and turns into the app when it's ready.", phase="installing", level="warn")

        if domain:
            yield ev(f"Point your domain {domain} at {ip or 'the server IP'} so HTTPS can be issued.", phase="dns", level="warn")
        if admin_password:
            yield ev("Admin account will be created automatically on first boot from the password you set.",
                     phase="claim", level="ok")
        elif "database" in (config.get("embed_sections") or []):
            yield ev("Admin login is carried from this app's shared database — no password to pre-set.",
                     phase="claim", level="ok")
        else:
            where = public_url or "the server's address"
            yield ev(f"No admin password was pre-set: the first visitor to {where} sets it via the setup "
                     "page. Open it yourself as soon as it's live and set your password before anyone else.",
                     phase="claim", level="warn")

        # Add the new VM to the SSH target's saved servers so it can be managed over
        # SSH later. Best-effort: a hiccup here never fails the deploy (the VM is up).
        if save_ssh and ssh_priv and ip:
            try:
                from app.deploy import manager
                await manager.link_ssh_server(
                    host=ip, ssh_user=_SSH_LINK_USER, ssh_port=22, private_key=ssh_priv,
                    label=name, repo_url=repo, branch=branch, domain=domain, source="google_vm")
                yield ev("Saved this VM to your SSH servers (Existing server → Saved servers) "
                         "so you can reconnect to it later.", phase="linked", level="ok")
            except Exception:
                logger.exception("linking google VM to ssh servers failed")
        elif save_ssh and not ip:
            yield ev("Couldn't read the VM's IP, so it wasn't added to your SSH servers — "
                     "you can add it by hand once it has an address.", phase="linked", level="warn")

        # A verified failure returns ok=False so the manager keeps the cloud key
        # (needed to tear the box down) and doesn't stamp a "live" deployment. The
        # VM still appears on the Cloud VMs page, so it's one click to delete.
        ok = state != "failed"
        if state == "running":
            msg = "WebAgent is live on the server."
        elif state == "failed":
            msg = (f"The VM '{name}' was created but the install didn't finish. Open "
                   f"{public_url or 'its address'} for the exact error (or read "
                   f"/var/log/webagent-bootstrap.log on the box), then delete it from the "
                   f"Cloud VMs page and try again.")
        else:
            msg = "Server created. WebAgent is installing and will be live in a few minutes."
        result = {
            "ok": ok,
            "server": name,
            "ip": ip,
            "zone": zone,
            "project": project,
            "public_url": public_url,
            "state": state,
            "message": msg,
        }
        yield done(result)

    # ── destroy ──
    async def destroy(self, config: Dict[str, Any], creds: Dict[str, Any],
                      record: Dict[str, Any]) -> AsyncIterator[Dict[str, Any]]:
        project = (record.get("project") or config.get("project_id") or "").strip()
        zone = (record.get("zone") or config.get("zone") or "").strip()
        name = (record.get("server") or "").strip()
        if not (project and zone and name):
            yield done({"ok": False, "message": "No recorded server to tear down."})
            return
        yield ev("Authenticating with Google Cloud…", phase="auth")
        try:
            token = await _access_token(creds)
        except Exception as e:
            yield done({"ok": False, "message": f"Could not authenticate: {e}"})
            return
        # Tear-down IS just a delete of the recorded server — share the one path
        # the Cloud VMs page's "Delete" action also uses.
        async for evt in self._stream_delete(project, zone, name, token):
            yield evt

    # ── instance management (the Cloud VMs admin page) ──
    async def list_instances(self, config: Dict[str, Any], creds: Dict[str, Any]) -> Dict[str, Any]:
        """Every VM in the project, via one aggregatedList call (all zones)."""
        project = (config.get("project_id") or config.get("project") or "").strip()
        if not project:
            return {"ok": False, "instances": [], "detail": "No Google Cloud project ID set — add it in App Settings → Deploy."}
        try:
            token = await _access_token(creds)
        except Exception as e:
            return {"ok": False, "instances": [], "detail": f"Could not authenticate: {e}"}

        # Which VM is "this app" — the one the Deploy card last created here.
        this_app = ""
        try:
            from app.deploy import store
            this_app = (store.get_deployment(self.id).get("server") or "").strip()
        except Exception:
            this_app = ""

        instances: list[Dict[str, Any]] = []
        # The "every VM across all zones" call: the method is instances.aggregatedList
        # but the REST URL path segment is `aggregated` (not `aggregatedList`, which
        # 404s with Google's HTML error page).
        url = f"{_COMPUTE}/projects/{project}/aggregated/instances?maxResults=500"
        try:
            async with httpx.AsyncClient(timeout=40.0) as client:
                # aggregatedList pages with a nextPageToken; loop until exhausted
                # (capped so a pathological account can't spin forever).
                page_token = ""
                for _ in range(20):
                    page_url = url + (f"&pageToken={page_token}" if page_token else "")
                    r = await client.get(page_url, headers={"Authorization": f"Bearer {token}"})
                    if r.status_code == 403:
                        return {"ok": False, "instances": [],
                                "detail": _compute_api_hint(r) or "Authenticated, but this service account lacks Compute permissions (needs Compute Admin)."}
                    if r.status_code == 404:
                        return {"ok": False, "instances": [], "needs_connect": True,
                                "detail": f"Project '{project}' wasn't found. Check the project ID (it's the id, not the display name)."}
                    if r.status_code >= 300:
                        return {"ok": False, "instances": [], "detail": _google_error(r)}
                    body = r.json() if r.content else {}
                    for _zkey, block in (body.get("items") or {}).items():
                        # Empty zones carry only a `warning`, no `instances`.
                        for inst in (block.get("instances") or []):
                            instances.append(self._summarize_instance(inst, project, this_app))
                    page_token = body.get("nextPageToken") or ""
                    if not page_token:
                        break
        except Exception as e:
            logger.exception("google_vm list_instances failed")
            return {"ok": False, "instances": [], "detail": str(e)}

        instances.sort(key=lambda i: (not i.get("is_this_app"), not i.get("is_webagent"), i.get("name", "")))
        return {"ok": True, "instances": instances, "detail": f"{len(instances)} server(s) in {project}.", "project": project}

    @staticmethod
    def _last_seg(url_or_value: str) -> str:
        s = str(url_or_value or "")
        return s.rsplit("/", 1)[-1] if "/" in s else s

    def _summarize_instance(self, inst: Dict[str, Any], project: str, this_app: str) -> Dict[str, Any]:
        name = inst.get("name", "")
        zone = self._last_seg(inst.get("zone", ""))
        machine_type = self._last_seg(inst.get("machineType", ""))
        ip = ""
        try:
            ip = inst.get("networkInterfaces", [])[0].get("accessConfigs", [])[0].get("natIP", "") or ""
        except Exception:
            ip = ""
        tags = (inst.get("tags") or {}).get("items") or []
        is_webagent = bool(name.startswith("webagent-") or (_NET_TAG in tags) or (this_app and name == this_app))
        return {
            "name": name,
            "zone": zone,
            "project": project,
            "machine_type": machine_type,
            "status": inst.get("status", ""),
            "ip": ip,
            "created": inst.get("creationTimestamp", ""),
            "is_webagent": is_webagent,
            "is_this_app": bool(this_app and name == this_app),
        }

    async def _stream_delete(self, project: str, zone: str, name: str,
                             token: str) -> AsyncIterator[Dict[str, Any]]:
        """The shared delete path — used by both destroy() and the Delete action.
        404 is treated as 'already gone → ok'."""
        try:
            async with httpx.AsyncClient(timeout=60.0) as client:
                yield ev(f"Deleting VM '{name}'…", phase="delete")
                url = f"{_COMPUTE}/projects/{project}/zones/{zone}/instances/{name}"
                r = await client.delete(url, headers={"Authorization": f"Bearer {token}"})
                if r.status_code == 404:
                    yield done({"ok": True, "message": "Server was already gone.", "deleted": True})
                    return
                if r.status_code >= 300:
                    yield done({"ok": False, "message": f"Delete failed: {_google_error(r)}"})
                    return
                op = r.json()
                yield ev("Waiting for the server to be removed…", phase="provision")
                await self._poll_zone_op(client, project, zone, op.get("name", ""), token)
        except Exception as e:
            yield done({"ok": False, "message": f"Tear-down failed: {e}"})
            return
        yield done({"ok": True, "message": f"Server '{name}' deleted.", "deleted": True})

    async def instance_action(self, config: Dict[str, Any], creds: Dict[str, Any],
                              action: str, instance: Dict[str, Any]) -> AsyncIterator[Dict[str, Any]]:
        action = (action or "").lower().strip()
        project = (instance.get("project") or config.get("project_id") or config.get("project") or "").strip()
        zone = (instance.get("zone") or "").strip()
        name = (instance.get("name") or "").strip()
        if action not in ("start", "stop", "delete"):
            yield done({"ok": False, "message": f"Unknown action '{action}'."})
            return
        if not (project and zone and name):
            yield done({"ok": False, "message": "Missing server name / zone / project."})
            return

        yield ev("Authenticating with Google Cloud…", phase="auth")
        try:
            token = await _access_token(creds)
        except Exception as e:
            yield done({"ok": False, "message": f"Could not authenticate: {e}"})
            return

        if action == "delete":
            async for evt in self._stream_delete(project, zone, name, token):
                yield evt
            return

        verb = "Starting" if action == "start" else "Stopping"
        try:
            async with httpx.AsyncClient(timeout=60.0) as client:
                yield ev(f"{verb} VM '{name}'…", phase=action)
                url = f"{_COMPUTE}/projects/{project}/zones/{zone}/instances/{name}/{action}"
                r = await client.post(url, headers={"Authorization": f"Bearer {token}"})
                if r.status_code >= 300:
                    yield done({"ok": False, "message": f"{verb} failed: {_google_error(r)}"})
                    return
                op = r.json()
                yield ev("Waiting for the change to take effect…", phase="provision")
                op_done = await self._poll_zone_op(client, project, zone, op.get("name", ""), token)
                if op_done.get("error"):
                    errs = op_done["error"].get("errors", [{}])
                    yield done({"ok": False, "message": errs[0].get("message", "unknown error")})
                    return
        except Exception as e:
            logger.exception("google_vm %s failed", action)
            yield done({"ok": False, "message": f"{verb} failed: {e}"})
            return
        new_state = "running" if action == "start" else "stopped"
        yield done({"ok": True, "message": f"Server '{name}' {action}ed.",
                    "name": name, "zone": zone, "new_state": new_state})


PROVIDER = GoogleVMProvider()

"""API-only Administrator acceptance. Prepare first; run only after deployment.

This harness never edits model records or decrypts databases. It stores only
IDs, counts, policy decisions, and booleans in target/administrator-api.json.
Approval is restricted to the named case's workspace tools. Live promotion and
host deployment are never approved. --build admits a development image build;
--cargo exercises fixed isolated Cargo jobs. Neither option deploys anything.
"""
import argparse
import copy
import hashlib
import http.cookies
import json
from pathlib import Path
import re
import secrets
import time
import urllib.error
import urllib.parse
import urllib.request

ROOT = Path(__file__).resolve().parents[1]
ABILITIES = ("codebase_admin", "code_index", "git_control", "terminal_control", "v3_workspace", "v3_build", "diagnostics", "remote_tunnel")
SCHEMAS = json.loads((ROOT / "src/agent_loop/tools/administrator-schemas.json").read_text())
TOOLS = {s["name"] for s in SCHEMAS} | {"workspace_seed", "file_read", "file_write", "file_list", "shell", "python"}
FORBIDDEN_APPROVALS = {"promotion_propose", "propose_promotion", "v3_worker_submit"}

class ApiFailure(RuntimeError):
    def __init__(self, status):
        super().__init__("API status " + str(status)); self.status = status

class NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, request, fp, code, msg, headers, newurl):
        return None

class Client:
    def __init__(self, origin):
        if origin not in {"http://100.112.23.15:8081", "https://dev.webagent.live"}:
            raise ValueError("Only the V3 development origin is allowed")
        self.origin = origin; self.cookie = None; self.csrf = None
    def request(self, method, path, body=None, statuses=(200, 202), binary=False):
        if not path.startswith("/") or path.startswith("//") or "\\" in path:
            raise ValueError("Fixed relative API path required")
        headers = {"Content-Type": "application/json"}
        if self.cookie: headers["Cookie"] = self.cookie
        if self.csrf: headers["X-CSRF-Token"] = self.csrf
        payload = None if body is None else json.dumps(body).encode()
        request = urllib.request.Request(self.origin + path, data=payload, headers=headers, method=method)
        try:
            with urllib.request.build_opener(NoRedirect()).open(request, timeout=35) as response:
                raw = response.read(2 * 1024 * 1024 + 1); status = response.status
                cookies = response.headers.get_all("Set-Cookie", [])
        except urllib.error.HTTPError as error:
            status = error.code; raw = error.read(8192); cookies = []
        if status not in statuses: raise ApiFailure(status)
        if len(raw) > 2 * 1024 * 1024: raise ValueError("API result exceeds bound")
        return status, (raw if binary else json.loads(raw or b"null")), cookies
    def get(self, path): return self.request("GET", path)[1]
    def put(self, path, body): return self.request("PUT", path, body)[1]
    def post(self, path, body): return self.request("POST", path, body)[1]
    def login(self, username, password):
        deadline = time.monotonic() + 75
        while True:
            try:
                _, body, cookies = self.request("POST", "/api/v3/auth/login", {"username": username, "password": password})
                break
            except ApiFailure as error:
                if error.status != 429 or time.monotonic() >= deadline: raise
                time.sleep(3)
        for value in cookies:
            parsed = http.cookies.SimpleCookie(); parsed.load(value)
            if "v3_session" in parsed: self.cookie = "v3_session=" + parsed["v3_session"].value
        self.csrf = body["csrf_token"]
        if not self.cookie: raise ValueError("Authenticated login cookie missing")
        return self.get("/api/v3/auth/me")

class Harness:
    def __init__(self, args):
        self.args = args
        self.fixture = json.loads(args.fixture.read_text())
        self.output = args.output
        if self.output.resolve().parent != (ROOT / "target").resolve(): raise ValueError("Results must stay under V3 target")
        self.data = json.loads(self.output.read_text()) if args.resume and self.output.exists() else {"version": 1, "cases": {}, "checks": []}
        self.admin = Client(args.origin); self.owner = Client(args.origin)
    def save(self):
        self.output.parent.mkdir(exist_ok=True)
        self.output.write_text(json.dumps(self.data, indent=2) + "\n")
    def check(self, label, value):
        self.data["checks"].append({"check": label, "pass": bool(value)}); self.save()
        if not value: raise ValueError("Check failed: " + label)
    def login(self):
        self.admin.login("admin", "Rogers58")
        username = self.fixture["username"]
        password = hashlib.sha256(("fixture-" + username + "Rogers58").encode()).hexdigest()
        self.owner.login(username, password)
        self.check("fixture agent owned by ordinary user", self.owner.get("/api/v3/agents/" + self.fixture["agent_id"] + "/abilities")["authority"]["owner_id"] == self.fixture["user_id"])
    def configure(self):
        # Preserve every prior grant and all model fields. Explicit Ask grants
        # ensure each real tool call supplies an observable approval boundary.
        tiers = self.admin.get("/api/v3/admin/access/tiers")["tiers"]
        values = list(tiers.values()) if isinstance(tiers, dict) else tiers
        for tier in values:
            if tier["id"] not in {self.fixture["tier_id"], "operator"}: continue
            changed = copy.deepcopy(tier)
            for ability in ABILITIES: changed.setdefault("ability_permissions", {})[ability] = "auto"
            if tier["id"] != "operator":
                for tool in TOOLS: changed.setdefault("tools", {})[tool] = "ask"
            self.admin.put("/api/v3/admin/access/tiers/" + tier["id"], {"expected_revision": tier["revision"], "tier": changed})
            # Tier edits rotate affected account generations and revoke cookies.
            if tier["id"] == "operator": self.admin.login("admin", "Rogers58")
        username = self.fixture["username"]
        self.owner.login(username, hashlib.sha256(("fixture-" + username + "Rogers58").encode()).hexdigest())
        for client, agent in [(self.owner, self.fixture["agent_id"]), (self.admin, "shared_default")]:
            catalog = client.get("/api/v3/agents/" + agent + "/abilities")
            if not catalog["authority"]["can_manage"]: raise ValueError("Fixture manager authority missing")
            levels = catalog["capability_levels"]; level = copy.deepcopy(levels["levels"][catalog["capability_level_id"]])
            for ability in ABILITIES: level.setdefault("ability_permissions", {})[ability] = "auto"
            for tool in TOOLS: level.setdefault("tool_permissions", {})[tool] = "ask"
            client.put("/api/v3/agents/" + agent + "/capability-levels/" + level["id"], {"expected_revision": levels["revision"], "level": level})
            catalog = client.get("/api/v3/agents/" + agent + "/abilities")
            permissions = copy.deepcopy(catalog["permissions"])
            for ability in ABILITIES: permissions.setdefault("ability_permissions", {})[ability] = "auto"
            for tool in TOOLS: permissions.setdefault("tool_permissions", {})[tool] = "ask"
            client.put("/api/v3/agents/" + agent + "/ability-permissions", {"expected_revision": permissions["revision"], "ability_permissions": permissions["ability_permissions"], "tool_permissions": permissions["tool_permissions"]})
        self.data["configured"] = True; self.save()
    def session(self, client, agent, abilities, existing=None):
        sid = existing or client.post("/api/v3/sessions", {"client_request_id": "admin-api-" + secrets.token_hex(12), "title": "Administrator API acceptance", "agent_id": agent})["id"]
        catalog = client.get("/api/v3/sessions/" + sid + "/abilities")
        state = catalog["state"]
        known = set(ABILITIES) | {entry["id"] for entry in catalog["abilities"]}
        for ability in sorted(known):
            result = client.put("/api/v3/sessions/" + sid + "/abilities/" + ability, {"expected_revision": state["revision"], "state": "active" if ability in abilities else "suppressed"})
            state = result
        effective = client.get("/api/v3/sessions/" + sid + "/abilities")
        present = {entry["id"] for entry in effective["abilities"] if entry.get("active") and entry.get("tools")}
        self.check("session requested abilities available: " + ",".join(sorted(abilities)), set(abilities) <= present)
        return sid
    def run(self, case, client, sid, instructions, required, permitted=None, timeout=240):
        nonce = secrets.token_hex(8)
        permitted = set(permitted or required)
        if "v3_workspace_action" in permitted: permitted.add("v3_workspace_status")
        if "terminal_open" in permitted: permitted.update({"terminal_wait", "terminal_list"})
        if case == "git_conflict": permitted.update({"repository_access", "git_tool"})
        reply_shape = json.dumps({"nonce": nonce, "ok": True, "tool_names": sorted(required)}, separators=(",", ":"))
        prompt = instructions + "\nUse the exact named V3 tools for real calls; do not simulate outputs. Do not touch any host checkout, live agent data, production, external repository, or model settings. Never apply a promotion or deploy. Finish with one valid JSON object in this shape: " + reply_shape + ". Set ok=true only when requested successful calls and explicitly expected rejection checks all match. Replace tool_names with actual calls. If blocked set ok=false. Use JSON double quotes and true/false; no markdown fences. Preserve nonce exactly. Do not print tokens, cookies, environment, credentials or preview bootstrap URL."
        sent = client.post("/api/v3/sessions/" + sid + "/messages", {"text": prompt, "client_message_id": "admin-api-" + nonce})
        rid = sent.get("run_id", sent.get("id"))
        record = {"session_id": sid, "run_id": rid, "observed_approved_tools": [], "pass": False}
        self.data["cases"][case] = record; self.save()
        approvals = set(); deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            snapshot = client.get("/api/v3/sessions/" + sid)
            run = next((item for item in snapshot["runs"] if item["id"] == rid), None)
            if run is None: time.sleep(.3); continue
            if run["status"] == "waiting_approval":
                pending = run.get("pending") or {}; name = pending.get("name"); ident = pending.get("id")
                if name not in permitted or name in FORBIDDEN_APPROVALS or pending.get("arguments", {}).get("operation") == "deploy":
                    record["blocked_approval"] = name; self.save()
                    client.post("/api/v3/runs/" + rid + "/cancel", {"client_request_id": "admin-api-blocked-" + nonce})
                    raise ValueError("Unexpected or forbidden tool approval")
                if name == "git_tool" and pending.get("arguments", {}).get("operation") in {"pull", "push"}:
                    if pending["arguments"].get("remote", "origin") != "acceptance" or pending["arguments"].get("repo") != "acceptance-repo": raise ValueError("Only isolated acceptance repository/remote may perform Git network effects")
                if name == "commit_and_push":
                    args = pending.get("arguments", {})
                    if args.get("remote", "origin") != "acceptance" or args.get("repo") != "acceptance-repo": raise ValueError("Only isolated acceptance repository/remote may push")
                if ident not in approvals:
                    if name == "dev_browser_action":
                        record.setdefault("browser_argument_keys", []).append({k: (v if k in {"action", "expected_sequence"} else len(v) if isinstance(v, str) else type(v).__name__) for k, v in pending.get("arguments", {}).items()})
                    if len(approvals) >= 40: raise ValueError("Bounded approval count exceeded")
                    client.post("/api/v3/approvals/" + ident + "/decision", {"decision": "approve", "client_request_id": "admin-api-approve-" + nonce + "-" + str(len(approvals))})
                    approvals.add(ident); record["observed_approved_tools"].append(name); self.save()
            if run["status"] in {"completed", "failed", "cancelled", "stopped", "recovery"}: break
            time.sleep(.4)
        else:
            client.post("/api/v3/runs/" + rid + "/cancel", {"client_request_id": "admin-api-timeout-" + nonce})
            record["timeout"] = True; self.save(); raise ValueError("Bounded run timed out")
        snapshot = client.get("/api/v3/sessions/" + sid)
        replies = [m for m in snapshot["messages"] if m.get("role") == "assistant" and m.get("run_id") == rid]
        record["status"] = run["status"]; record["assistant_replies"] = len(replies)
        diag = client.get("/api/v3/sessions/" + sid + "/runs/" + rid + "/diagnostics")
        record["checkpoint_decisions"] = {d["checkpoint"]: d["decision"] for d in diag.get("checkpoints", [])}
        record["context_summary"] = diag.get("context_summary")
        summary = {}
        if len(replies) == 1:
            text = replies[0].get("content", "")
            if isinstance(text, str):
                decoder = json.JSONDecoder()
                for match in re.finditer(r"\{", text):
                    try:
                        candidate, _ = decoder.raw_decode(text[match.start():])
                        if candidate.get("nonce") == nonce: summary = candidate; break
                    except (ValueError, AttributeError): pass
        record["model_reported_ok"] = summary.get("ok") is True
        record["required_tools_observed"] = set(required) <= set(record["observed_approved_tools"])
        context = record["context_summary"] or {}
        record["bounded_context"] = bool(context) and context.get("envelope_bytes", 10**9) <= context.get("max_bytes", 0) <= (119808 if client is self.owner else 262144)
        record["pass"] = run["status"] == "completed" and len(replies) == 1 and record["model_reported_ok"] and record["required_tools_observed"] and record["checkpoint_decisions"].get("tool_result") == "pass" and record["bounded_context"]
        self.save()
        if not record["pass"]: raise ValueError("Case failed: " + case)
        # This response is memory-only. Raw model text/tool outputs never enter
        # the result artifact. It may contain IDs needed for the next API read.
        return summary
    def case(self, name):
        if self.args.resume and self.data["cases"].get(name, {}).get("workflow_completed"): return
        client = self.admin if name.startswith("build") else self.owner
        agent = "shared_default" if client is self.admin else self.fixture["agent_id"]
        groups = {"workspace": {"v3_workspace"}, "cargo": {"v3_workspace"}, "index": {"v3_workspace", "code_index"}, "terminal": {"terminal_control"}, "git": {"terminal_control", "git_control"}, "diagnostics": {"diagnostics"}, "tunnel": {"terminal_control", "remote_tunnel"}, "build_status": {"v3_build"}, "build": {"v3_build"}}
        sid = self.session(client, agent, groups[name])
        if name in {"workspace", "index", "cargo"}:
            self.run(name + "_seed", client, sid, "Call v3_workspace_action action=seed request_id=seed-acceptance, then v3_workspace_receipt request_id=seed-acceptance and v3_workspace_status. Confirm seeded source belongs only to this session.", {"v3_workspace_action", "v3_workspace_receipt", "v3_workspace_status"})
        instructions = {
            "workspace": "Call v3_workspace_action action=review request_id=review-acceptance. Then read src/main.rs using action=read request_id=read-acceptance. Read that receipt with v3_workspace_receipt. Finally call action=patch request_id=stale-acceptance with expected_review_sha256 all zeroes and a harmless main.rs patch; it must reject as stale and leave review unchanged. Report ok=true for this expected stale rejection.",
            "cargo": "Review the seeded source to obtain its current review SHA. Call v3_workspace_action action=check request_id=check-acceptance and action=test request_id=test-acceptance with that exact SHA and timeout_seconds=600, then read both receipts. Do not run host Cargo or build an image.",
            "index": "Use code_index_scan action=discover root=webagent-v3 limit=20. Then action=scan_file file_path=src/main.rs root=webagent-v3 with discover's exact returned revision. Use code_index_lookup action=file root=webagent-v3 file_path=src/main.rs; code_index_coverage action=report; code_index_trace kind=symbol value=main; and code_index_progress action=list. Use root=webagent-v3 on every index call. Confirm actual indexed source.",
            "terminal": "Open terminal_open with exactly these arguments: {\"command\": \"printf \\\"READY\\\\n\\\"; read answer; printf \\\"RESULT:%s\\\\n\\\" \\\"$answer\\\"\"}. Read/wait until READY. Obtain a fresh terminal_read sequence, then terminal_send using the same terminal_id, that expected_sequence, and text containing acceptance followed by one real newline (JSON text=\"acceptance\\n\"). Use terminal_wait then terminal_read until RESULT:acceptance. Call terminal_list then terminal_close. Do not use shell/python adapters.",
            "git": "First call repository_access verify_remote=false to inspect sanitized current-agent repository metadata, even if no external connection is configured. Then use terminal_open/read/close to create a fresh ordinary git repository named acceptance-repo and a bare acceptance-remote inside this session workspace. Fixed commands only: git init --initial-branch=main acceptance-repo; git init --bare acceptance-remote; cd acceptance-repo; printf 'initial\\n' > README.txt; git add README.txt; git -c user.name=Acceptance -c user.email=acceptance@localhost commit -m initial; git remote add acceptance ../acceptance-remote. Then git_tool operation=status repo=acceptance-repo, branch_create name=acceptance-check with observed expected_head, branch_list, diff. Using another terminal modify README.txt to 'changed\\n', close it, get fresh git_tool status review SHA/HEAD; commit_and_push repo=acceptance-repo paths=[README.txt] message='acceptance change' remote=acceptance with both exact fences. Confirm local ordinary push succeeded; no external remote or credentials.",
            "diagnostics": "Call read_diagnostics for this run, then dev_browser_url for this current session. Use login_as_current_user path=/api/v3/auth/me, dev_browser_action action=read, dev_browser_record action=start, dev_browser_action action=screenshot, dev_browser_record action=stop, then dev_browser_action action=close. This is API-driven tool acceptance only; no navigation journey, clicks or typing. Include the screenshot artifact_id as field artifact_id in final JSON, without cookie/token text.",
            "tunnel": "Use terminal_open with exactly these arguments: {\"command\": \"port=${V3_ALLOWED_LISTEN_PORTS%%,*}; mkdir -p preview; exec python3 -u -m http.server \\\"$port\\\" --bind 127.0.0.1 --directory preview\"}. Call terminal_read/wait until listening, and terminal_list to observe preview_ports. Call start_tunnel on the FIRST observed owned preview port with duration_seconds=30. Then get_tunnel_url and confirm a concrete active quicktunnel. Never forward the application or an unowned port. Do not print URL/token; report lease_active=true and lease_id only in final JSON. Leave the terminal running for follow-up API checks.",
            "build_status": "Call v3_build_status once. Confirm actual development-only broker inputs SHA and both API/loop container states, without raw environment or logs. Include inputs_sha256 in final JSON.",
            "build": "Call v3_build_status to get exact current inputs_sha256. Call v3_build_image with request_id=api-build-acceptance and that SHA. Read v3_worker_job for the returned job ID. Do not deploy, stop or replace anything. Include job_id and inputs_sha256 in final JSON. Set ok=true if the build job was accepted, including when its state is still queued or running; the harness polls completion separately."
        }
        required = {"workspace": {"v3_workspace_action", "v3_workspace_receipt"}, "cargo": {"v3_workspace_action", "v3_workspace_receipt"}, "index": {"code_index_scan", "code_index_lookup", "code_index_coverage", "code_index_trace", "code_index_progress"}, "terminal": {"terminal_open", "terminal_read", "terminal_send", "terminal_wait", "terminal_list", "terminal_close"}, "git": {"repository_access", "terminal_open", "terminal_read", "terminal_close", "git_tool", "commit_and_push"}, "diagnostics": {"read_diagnostics", "dev_browser_url", "login_as_current_user", "dev_browser_action", "dev_browser_record"}, "tunnel": {"terminal_open", "terminal_read", "terminal_list", "start_tunnel", "get_tunnel_url"}, "build_status": {"v3_build_status"}, "build": {"v3_build_status", "v3_build_image", "v3_worker_job"}}[name]
        summary = self.run(name, client, sid, instructions[name], required, timeout=1400 if name == "cargo" else 300)
        if name == "workspace":
            first_line = (ROOT / "src/main.rs").read_text().splitlines()[0]
            fixture_patch = "--- a/src/main.rs\n+++ b/src/main.rs\n@@ -1 +1,2 @@\n+// Administrator API acceptance\n " + first_line + "\n"
            proposal_summary = self.run("workspace_patch_propose", client, sid, "Read src/main.rs and current review SHA. Apply one harmless trailing source comment '// Administrator API acceptance' using this exact valid unified diff: " + json.dumps(fixture_patch) + " through v3_workspace_action action=patch request_id=valid-patch-acceptance. Review the changed source, then action=propose request_id=proposal-acceptance with exact new review SHA and a short summary. Read its workspace receipt. Creating the proposal is authorized; never approve, promote, or apply it to the live checkout. Report proposal_created=true only if an actual pending proposal exists.", {"v3_workspace_action", "v3_workspace_receipt"})
            proposals = client.get("/api/v3/sessions/" + sid + "/promotions")["promotions"]
            proposal_run = self.data["cases"]["workspace_patch_propose"]["run_id"]
            matching = [p for p in proposals if p.get("proposed_by_run") == proposal_run and p.get("state") == "proposed"]
            self.check("one native pending proposal belongs to acceptance run", len(matching) == 1)
        if name == "git":
            self.run("git_conflict", client, sid, "Use a terminal in acceptance-repo to create conflicting edits to README.txt on branch acceptance-check and main using fixed local git -c user.name=Acceptance -c user.email=acceptance@localhost commit commands, then merge with fixed git -c user.name=Acceptance -c user.email=acceptance@localhost merge acceptance-check to produce a genuine unresolved conflict. Compute its exact sha256 with python3 hashlib in the terminal; close the terminal. Call resolve_conflict repo=acceptance-repo path=README.txt strategy=both with that SHA. Confirm conflict markers are removed and staged=false; do not push this conflict exercise.", {"terminal_open", "terminal_read", "terminal_close", "resolve_conflict"})
        if name == "diagnostics":
            aid = summary.get("artifact_id", "")
            if not re.fullmatch(r"[0-9a-f-]{36}", aid): raise ValueError("Screenshot artifact ID missing")
            path = "/api/v3/sessions/" + sid + "/administrator/artifacts/" + aid
            status, body, _ = client.request("GET", path, binary=True)
            self.check("own screenshot artifact is bounded PNG", status == 200 and body.startswith(b"\x89PNG\r\n\x1a\n") and len(body) <= 1048576)
            status, _, _ = self.admin.request("GET", path, statuses=(403, 404), binary=True)
            self.check("other account cannot download screenshot artifact", status in (403, 404))
        if name == "tunnel":
            self.run("tunnel_unowned_port", client, sid, "Call start_tunnel port=8081 duration_seconds=30; it must reject without exposing the app. Then get_tunnel_url. Expected rejection is success for this check.", {"start_tunnel", "get_tunnel_url"})
            time.sleep(31)
            self.run("tunnel_expired", client, sid, "Call get_tunnel_url and confirm status absent after the 30-second lease. Call terminal_list and close the preview terminal with terminal_close.", {"get_tunnel_url", "terminal_list", "terminal_close"})
        if name == "build":
            job = summary.get("job_id", "")
            if not re.fullmatch(r"[0-9a-f-]{36}", job): raise ValueError("Build job ID missing")
            # Poll through actual model tool calls; never fabricate direct broker API.
            self.data["build_job_id"] = job; self.save()
            deadline = time.monotonic() + self.args.build_wait
            finished = False
            for index in range(40):
                poll = self.run("build_job_poll_" + str(index), client, sid, "Call v3_worker_job job_id=" + job + ". Report its actual status in job_status and stable_inputs=true only when its successful completed receipt says inputs unchanged. Do not retry the build or deploy.", {"v3_worker_job"})
                state = poll.get("job_status")
                if state == "completed":
                    self.check("model-reported build completion with unchanged inputs", poll.get("stable_inputs") is True)
                    finished = True; break
                if state in {"failed", "stale", "uncertain", "interrupted"}:
                    raise ValueError("Build job did not complete safely")
                if time.monotonic() >= deadline: break
                time.sleep(30)
            self.check("bounded actual build job reaches completed receipt", finished)
        if name == "tunnel":
            self.check("model reported concrete active lease", summary.get("lease_active") is True and re.fullmatch(r"[0-9a-f]{32}",summary.get("lease_id","")) is not None)
        self.data["cases"][name]["workflow_completed"] = True
        self.save()
    def repository_policy(self):
        agent = self.fixture["agent_id"]
        path = "/api/v3/agents/" + agent + "/repository-connections"
        ident = "api_acceptance_repo"
        before = self.owner.get(path)
        original = {c["id"]: c for c in before["connections"] if c["id"] != ident}
        token = "fixture-write-only-" + secrets.token_hex(12)
        rotated = "fixture-rotated-only-" + secrets.token_hex(12)
        def body(revision, secret=None, revoked=False):
            return {"expected_revision": revision, "url": "https://example.invalid/owner/repo", "username": "git", "token": secret, "revoked": revoked}
        def private_projection(value):
            text = json.dumps(value)
            return token not in text and rotated not in text and "credential_ref" not in text and "repository_credential:" not in text
        first_body = body(before["revision"], token)
        try:
            first = self.owner.put(path + "/" + ident, first_body)
            self.check("repository write-only credential omitted from save projection", private_projection(first))
            initial = next(c for c in first["connections"] if c["id"] == ident)
            self.check("repository fixture is owned and authenticated", initial["owner_id"] == self.fixture["user_id"] and initial["authenticated"] and initial["configured"])
            status, _, _ = self.owner.request("PUT", path + "/" + ident, first_body, statuses=(409,))
            self.check("stale repository write rejected", status == 409)
            second = self.owner.put(path + "/" + ident, body(first["revision"], rotated))
            changed = next(c for c in second["connections"] if c["id"] == ident)
            self.check("repository credential rotation increments revision without exposing secrets", changed["revision"] == initial["revision"] + 1 and changed["authenticated"] and private_projection(second))
            status, _, _ = self.admin.request("PUT", path + "/" + ident, body(second["revision"]), statuses=(403,))
            self.check("installation oversight cannot replace another connection owner's credential", status == 403)
            status, _, _ = self.owner.request("PUT", "/api/v3/agents/shared_default/repository-connections/" + ident, body(0, token), statuses=(403,))
            self.check("ordinary nonowner cannot write another agent's repository connection", status == 403)
            revoked = self.owner.put(path + "/" + ident, body(second["revision"], revoked=True))
            entry = next(c for c in revoked["connections"] if c["id"] == ident)
            self.check("revoked repository credential is unavailable", entry["revoked"] and not entry["authenticated"] and not entry["configured"])
            reloaded = self.owner.get(path)
            self.check("repository reload omits credential references and both tokens", private_projection(reloaded))
            self.check("repository fixture preserves other connections", original == {c["id"]: c for c in reloaded["connections"] if c["id"] != ident})
        finally:
            # Leave only a revoked fake fixture, never an enabled external target.
            current = self.owner.get(path)
            entry = next((c for c in current["connections"] if c["id"] == ident), None)
            if entry and not entry["revoked"]:
                self.owner.put(path + "/" + ident, body(current["revision"], revoked=True))

    def policy(self):
        self.repository_policy()
        agent = self.fixture["agent_id"]
        catalog = self.owner.get("/api/v3/agents/" + agent + "/abilities")
        levels = catalog["capability_levels"]
        level = copy.deepcopy(levels["levels"][catalog["capability_level_id"]])
        body = {"expected_revision": levels["revision"], "level": level}
        self.owner.put("/api/v3/agents/" + agent + "/capability-levels/" + level["id"], body)
        status, _, _ = self.owner.request("PUT", "/api/v3/agents/" + agent + "/capability-levels/" + level["id"], body, statuses=(409,))
        self.check("stale capability write rejected", status == 409)
        status, _, _ = self.owner.request("PUT", "/api/v3/agents/shared_default/ability-permissions", {"expected_revision": 0, "ability_permissions": {}, "tool_permissions": {}}, statuses=(403,))
        self.check("ordinary nonowner cannot edit shared agent", status == 403)
        status, _, _ = self.owner.request("GET", "/api/v3/admin/access/tiers", statuses=(403,))
        self.check("ordinary owner cannot edit installation tier ceiling", status == 403)
        latest = self.owner.get("/api/v3/agents/" + agent + "/abilities")
        denied = {"id": "api_acceptance_denied", "name": "API acceptance denied", "revision": latest["capability_levels"]["levels"].get("api_acceptance_denied", {}).get("revision", 1), "ability_permissions": {a: "deny" for a in ABILITIES}, "tool_permissions": {}}
        self.owner.put("/api/v3/agents/" + agent + "/capability-levels/api_acceptance_denied", {"expected_revision": latest["capability_levels"]["revision"], "level": denied})
        preview = self.owner.get("/api/v3/agents/" + agent + "/abilities/effective?level_id=api_acceptance_denied")
        self.check("denied level effective preview has no usable Administrator ability", not any(a["id"] in ABILITIES and a.get("available") for a in preview["abilities"]))
        latest = self.owner.get("/api/v3/agents/" + agent + "/abilities")
        prior = latest.get("visibility", {}).get("code_index", "discoverable")
        self.owner.put("/api/v3/agents/" + agent + "/abilities/code_index/visibility", {"expected_revision": latest["revision"], "visibility": "off"})
        try:
            disabled_sid = self.owner.post("/api/v3/sessions", {"client_request_id": "admin-api-disabled-" + secrets.token_hex(8), "title": "Disabled ability API acceptance", "agent_id": agent})["id"]
            disabled = self.owner.get("/api/v3/sessions/" + disabled_sid + "/abilities")["abilities"]
            self.check("agent disabled ability withdrawn from discovery", not any(a["id"] == "code_index" for a in disabled))
        finally:
            current = self.owner.get("/api/v3/agents/" + agent + "/abilities")
            self.owner.put("/api/v3/agents/" + agent + "/abilities/code_index/visibility", {"expected_revision": current["revision"], "visibility": prior})
        sid = self.session(self.owner, agent, {"terminal_control"})
        opened = self.run("persistent_terminal_open", self.owner, sid, "Call terminal_open with {\"command\": \"printf READY; sleep 120\"} then terminal_read until READY. Leave this terminal running; report terminal_running=true only when actual read says running=true.", {"terminal_open", "terminal_read"})
        self.check("API-created terminal persists beyond its completed run", opened.get("terminal_running") is True)
        state = self.owner.get("/api/v3/sessions/" + sid + "/abilities")["state"]
        self.owner.put("/api/v3/sessions/" + sid + "/abilities/terminal_control", {"expected_revision": state["revision"], "state": "suppressed"})
        status, _, _ = self.owner.request("PUT", "/api/v3/sessions/" + sid + "/abilities/terminal_control", {"expected_revision": state["revision"], "state": "active"}, statuses=(409,))
        self.check("stale session activation rejected", status == 409)
        available = self.owner.get("/api/v3/sessions/" + sid + "/abilities")["abilities"]
        self.check("suppressed ability withdrawn from executable discovery", not any(a["id"] == "terminal_control" for a in available))
        current = self.owner.get("/api/v3/sessions/" + sid + "/abilities")["state"]
        self.owner.put("/api/v3/sessions/" + sid + "/abilities/terminal_control", {"expected_revision": current["revision"], "state": "active"})
        listing = self.run("persistent_terminal_suppression", self.owner, sid, "Call terminal_list exactly once. Confirm its actual terminals array is empty after suppression and reactivation; include terminal_count=0 only if truly empty. Do not open another terminal.", {"terminal_list"})
        self.check("ability suppression closes persistent terminals", listing.get("terminal_count") == 0)
        opened = self.run("persistent_terminal_mode_open", self.owner, sid, "Call terminal_open with command='printf READY; sleep 120' then terminal_read until READY. Leave it running and report terminal_running=true only on actual running=true.", {"terminal_open", "terminal_read"})
        self.check("second terminal exists before read-only transition", opened.get("terminal_running") is True)
        self.owner.put("/api/v3/sessions/" + sid + "/mode", {"mode": "read_only"})
        self.check("read-only mode persisted", self.owner.get("/api/v3/sessions/" + sid + "/mode")["mode"] == "read_only")
        self.owner.put("/api/v3/sessions/" + sid + "/mode", {"mode": "write"})
        listing = self.run("persistent_terminal_mode", self.owner, sid, "Call terminal_list exactly once. Confirm the actual array is empty after read-only mode and restoring write mode; include terminal_count=0 only if truly empty. Do not open another terminal.", {"terminal_list"})
        self.check("read-only transition closes persistent terminals", listing.get("terminal_count") == 0)
        self.data["cases"]["policy"] = {"pass": True, "session_id": sid}; self.save()

def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--origin", default="http://100.112.23.15:8081")
    parser.add_argument("--fixture", type=Path, default=ROOT / "target/core-api-acceptance.json")
    parser.add_argument("--output", type=Path, default=ROOT / "target/administrator-api.json")
    parser.add_argument("--cases", default="workspace,index,terminal,git,diagnostics,tunnel,policy,build_status")
    parser.add_argument("--configure", action="store_true")
    parser.add_argument("--resume", action="store_true")
    parser.add_argument("--cargo", action="store_true")
    parser.add_argument("--build", action="store_true")
    parser.add_argument("--build-wait", type=int, default=1800)
    args = parser.parse_args()
    if not 60 <= args.build_wait <= 3600: parser.error("--build-wait must be 60..3600 seconds")
    harness = Harness(args)
    try:
        harness.login()
        if args.configure: harness.configure()
        cases = args.cases.split(",")
        if args.cargo: cases.append("cargo")
        if args.build: cases.append("build")
        for case in cases:
            if case == "policy": harness.policy()
            elif case in {"workspace", "index", "terminal", "git", "diagnostics", "tunnel", "build_status", "cargo", "build"}: harness.case(case)
            else: raise ValueError("Unknown bounded case")
        print(json.dumps({"cases": {k: v.get("pass", False) for k, v in harness.data["cases"].items()}, "checks": len(harness.data["checks"])}))
    except Exception as error:
        harness.data["failure"] = {"type": type(error).__name__, "status": error.status if isinstance(error, ApiFailure) else None}
        harness.save()
        print(json.dumps({"status": "blocked", "type": type(error).__name__, "http_status": error.status if isinstance(error, ApiFailure) else None}))
        raise SystemExit(1)

if __name__ == "__main__": main()

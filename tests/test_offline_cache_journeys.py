"""End-to-end offline-cache journeys against the real application shell.

The authority endpoints are controlled locally so outages, tenant changes and
permission revocation never touch the developer's running account or server.
"""

from __future__ import annotations

import functools
import http.server
import json
import os
import tempfile
import threading
import time
import unittest
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import urlparse


ROOT = Path(__file__).resolve().parents[1]
RUN_ID = f"run-offline-cache-{datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%SZ')}"


def _page(page_id: str, label: str, order: int) -> dict:
    return {
        "id": page_id,
        "label": label,
        "icon": "bot" if page_id == "agents" else "book-open",
        "order": order,
        "html": f"{page_id}.html",
        "mount": f"#tab-{page_id}",
        "dir": f"main-panel/{page_id}",
        "entry": f"ui/main-panel/{page_id}/js/{page_id}.js",
        "start": f"start{page_id.title()}",
        "stop": f"stop{page_id.title()}",
        "css": [f"{page_id}.css"],
        "visibility": "auth",
    }


CATALOG_FULL = {"main": [_page("agents", "Agents", 1), _page("wiki", "Wiki", 7)], "admin": []}
CATALOG_REVOKED = {"main": [_page("agents", "Agents", 1)], "admin": []}


class _JourneyHandler(http.server.SimpleHTTPRequestHandler):
    catalog = CATALOG_FULL

    def log_message(self, format, *args):
        return

    def _json(self, payload: object, status: int = 200) -> None:
        body = json.dumps(payload).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        try:
            self.wfile.write(body)
        except (BrokenPipeError, ConnectionAbortedError, ConnectionResetError):
            pass

    def do_GET(self):
        path = urlparse(self.path).path
        if path == "/api/v1/pages/catalog":
            return self._json(type(self).catalog)
        if path == "/health":
            return self._json({"status": "healthy", "initialization": "ready", "pending": []})
        if path == "/api/v1/browser/routing":
            return self._json({
                "routing": {"session_data": "browser", "session_cache": "browser"},
                "capabilities": {"browser_authority": True, "browser_session_cache": True},
                "cache_scope": "journey-tenant-a",
                "cache_policy": {
                    "persistence_mode": "persistent_cache",
                    "policy_epoch": 1,
                    "schema_version": 2,
                    "metadata_ttl_seconds": 0,
                    "transcript_ttl_seconds": 0,
                    "run_state_ttl_seconds": 0,
                    "generated_html_ttl_seconds": 0,
                    "max_bytes": 64 * 1024 * 1024,
                },
            })
        if path == "/api/v1/agents":
            return self._json({"agents": [{
                "id": "journey-agent", "name": "Journey Agent",
                "template_id": "default", "icon": "bot",
            }]})
        if path.endswith("/abilities"):
            return self._json({"abilities": []})
        if path == "/app":
            self.path = "/index.html"
            return super().do_GET()
        if path.startswith("/api/"):
            return self._json({"detail": "journey endpoint intentionally unavailable"}, 503)
        return super().do_GET()

    def do_POST(self):
        self._json({"detail": "journey is read-only"}, 503)


class OfflineCacheJourneys(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        try:
            from playwright.sync_api import sync_playwright
        except ImportError as exc:
            raise unittest.SkipTest(f"Playwright unavailable: {exc}")
        handler = functools.partial(_JourneyHandler, directory=str(ROOT))
        cls.server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), handler)
        cls.thread = threading.Thread(target=cls.server.serve_forever, daemon=True)
        cls.thread.start()
        cls.playwright = sync_playwright().start()
        try:
            cls.browser = cls.playwright.chromium.launch(headless=True)
        except Exception as exc:
            cls.playwright.stop()
            cls.server.shutdown()
            raise unittest.SkipTest(f"Chromium unavailable: {exc}")
        cls.base = f"http://127.0.0.1:{cls.server.server_port}"
        artifact_root = os.environ.get("WEBAGENT_JOURNEY_ARTIFACTS")
        if artifact_root:
            cls.artifacts = Path(artifact_root).resolve()
            cls.artifacts.mkdir(parents=True, exist_ok=True)
        else:
            cls.artifacts = Path(tempfile.mkdtemp(prefix=f"{RUN_ID}-"))
        cls.results: list[dict] = []

    @classmethod
    def tearDownClass(cls):
        report = {
            "runId": RUN_ID,
            "trajectoryId": "tenant-scoped-offline-cache",
            "seed": "offline-cache-20260825",
            "environment": {"baseUrl": cls.base, "build": "current-working-tree", "browser": "chromium"},
            "result": "pass" if all(row["result"] == "pass" for row in cls.results) else "fail",
            "cases": cls.results,
            "artifacts": [str(path) for path in sorted(cls.artifacts.glob("*.png"))],
            "limitations": ["Authority APIs were deterministic local fixtures; application assets and UI were the real working tree."],
        }
        (cls.artifacts / "run-result.json").write_text(json.dumps(report, indent=2), encoding="utf-8")
        print(f"\njourney report: {cls.artifacts / 'run-result.json'}")
        cls.browser.close()
        cls.playwright.stop()
        cls.server.shutdown()
        cls.server.server_close()

    @staticmethod
    def _identity_script(identity: str = "tenant-a") -> str:
        return f"""
          if (!localStorage.getItem('auth_user_id')) localStorage.setItem('auth_user_id', {json.dumps(identity)});
          if (!localStorage.getItem('auth_token')) localStorage.setItem('auth_token', 'journey-token');
          if (!localStorage.getItem('lastAgentId')) localStorage.setItem('lastAgentId', 'journey-agent');
        """

    def _wait_for_shell(self, page) -> None:
        page.wait_for_selector('.main-tab[data-value="agents"]', timeout=30_000)
        page.wait_for_function("() => navigator.serviceWorker && navigator.serviceWorker.controller", timeout=30_000)

    def _record(self, case_id: str, started: float, evidence: dict, failures: list[str]) -> None:
        self.results.append({
            "id": case_id,
            "result": "fail" if failures else "pass",
            "steps": evidence,
            "metrics": {"durationMs": round((time.perf_counter() - started) * 1000)},
            "issues": failures,
        })

    def test_journey_1_warm_cache_survives_real_browser_restart(self):
        started = time.perf_counter()
        failures: list[str] = []
        evidence: dict = {}
        profile = Path(tempfile.mkdtemp(prefix="webagent-offline-profile-"))
        context = self.playwright.chromium.launch_persistent_context(
            str(profile), headless=True, service_workers="allow",
        )
        context.add_init_script(self._identity_script())
        page = context.pages[0]
        page.goto(f"{self.base}/index.html", wait_until="domcontentloaded")
        self._wait_for_shell(page)
        page.locator('.main-tab[data-value="wiki"]').click()
        page.wait_for_selector('.wiki-welcome-title', timeout=20_000)
        page.locator('.main-tab[data-value="agents"]').click()
        page.wait_for_selector('#agents-grid', timeout=20_000)
        page.wait_for_function("() => localStorage.getItem('webagent.browserCacheCtx.v1')", timeout=20_000)
        seeded = page.evaluate("""async () => {
          const {default: db} = await import('/ui/chat/js/storage/indexeddb.js');
          db.setOwnerScope('journey-tenant-a');
          await db.createSession({id: 'journey-session', agent_id: 'journey-agent', title: 'Offline transcript'});
          await db.addInteractions('journey-session', [
            {id: 'journey-u', role: 'user', content: 'Cached transcript survives restart'},
            {id: 'journey-a', role: 'assistant', content: 'Confirmed from IndexedDB'},
          ]);
          return (await db.getInteractions('journey-session')).length;
        }""")
        evidence["TC-1-S1"] = {"actual": f"online shell primed; {seeded} transcript rows saved", "result": "pass"}
        page.screenshot(path=str(self.artifacts / "TC-1-online.png"), full_page=True)
        context.close()

        context = self.playwright.chromium.launch_persistent_context(
            str(profile), headless=True, service_workers="allow", offline=True,
        )
        page = context.pages[0]
        page.goto(f"{self.base}/app", wait_until="domcontentloaded", timeout=30_000)
        page.wait_for_selector('.main-tab[data-value="wiki"]', timeout=30_000)
        page.locator('.main-tab[data-value="wiki"]').click()
        page.wait_for_selector('.wiki-welcome-title', timeout=20_000)
        page.locator('.main-tab[data-value="agents"]').click()
        page.wait_for_selector('#agents-grid', timeout=20_000)
        cached = page.evaluate("""async () => {
          const {default: db} = await import('/ui/chat/js/storage/indexeddb.js');
          db.setOwnerScope('journey-tenant-a');
          return {
            session: await db.getSession('journey-session'),
            messages: (await db.getInteractions('journey-session')).map(row => row.content),
            aura: document.getElementById('chat-toggle-btn')?.classList.contains('chat-aura-red'),
            badge: !!document.getElementById('offline-reader-badge'),
          };
        }""")
        if cached["messages"] != ["Cached transcript survives restart", "Confirmed from IndexedDB"]:
            failures.append("cached transcript did not survive restart")
        if not cached["aura"] or cached["badge"]:
            failures.append("offline indicator did not use the chat aura exclusively")
        evidence["TC-1-S2"] = {"actual": cached, "result": "fail" if failures else "pass"}
        page.screenshot(path=str(self.artifacts / "TC-1-offline-restart.png"), full_page=True)
        context.close()
        self._record("TC-1", started, evidence, failures)
        self.assertFalse(failures, failures)

    def test_journey_2_different_tenant_fails_closed(self):
        started = time.perf_counter()
        failures: list[str] = []
        context = self.browser.new_context(service_workers="allow")
        context.add_init_script(self._identity_script("tenant-a"))
        page = context.new_page()
        page.goto(f"{self.base}/index.html", wait_until="domcontentloaded")
        self._wait_for_shell(page)
        page.evaluate("""() => {
          localStorage.setItem('auth_user_id', 'tenant-b');
          localStorage.setItem('auth_token', 'tenant-b-token');
        }""")
        context.set_offline(True)
        page.close()
        page = context.new_page()
        page.goto(f"{self.base}/app", wait_until="domcontentloaded", timeout=30_000)
        page.wait_for_selector('[data-catalog-unavailable="1"]', state="attached", timeout=30_000)
        state = page.evaluate("""() => ({
          tabs: [...document.querySelectorAll('.main-tab[data-generated="1"]')].map(el => el.dataset.value),
          notice: document.querySelector('[data-catalog-unavailable="1"]')?.textContent,
          noticeVisible: !!document.querySelector('[data-catalog-unavailable="1"]')?.offsetParent,
          activePanels: [...document.querySelectorAll('#main-panel > .tab-content.active')].map(el => el.id || 'cached-unavailable'),
          aura: document.getElementById('chat-toggle-btn')?.classList.contains('chat-aura-red'),
        })""")
        if state["tabs"]:
            failures.append(f"tenant A tabs leaked into tenant B: {state['tabs']}")
        if "Cached views unavailable" not in (state["notice"] or ""):
            failures.append("closed unavailable state was not shown")
        if not state["noticeVisible"]:
            failures.append("closed unavailable state was mounted but hidden by navigation")
        page.screenshot(path=str(self.artifacts / "TC-2-tenant-isolation.png"), full_page=True)
        context.close()
        self._record("TC-2", started, {"TC-2-S1": {"actual": state, "result": "fail" if failures else "pass"}}, failures)
        self.assertFalse(failures, failures)

    def test_journey_3_reconnect_reconciles_revoked_views(self):
        started = time.perf_counter()
        failures: list[str] = []
        _JourneyHandler.catalog = CATALOG_FULL
        context = self.browser.new_context(service_workers="allow")
        context.add_init_script(self._identity_script())
        page = context.new_page()
        page.goto(f"{self.base}/index.html", wait_until="domcontentloaded")
        self._wait_for_shell(page)
        context.set_offline(True)
        page.wait_for_function("() => document.body.dataset.offlineReadonly === 'true'", timeout=10_000)
        during_outage = page.evaluate("""() => ({
          tabs: [...document.querySelectorAll('.main-tab[data-generated="1"]')].map(el => el.dataset.value),
          aura: document.getElementById('chat-toggle-btn')?.classList.contains('chat-aura-red'),
        })""")
        if "wiki" not in during_outage["tabs"]:
            failures.append("cached Wiki view disappeared during outage")
        _JourneyHandler.catalog = CATALOG_REVOKED
        context.set_offline(False)
        reconnected = True
        try:
            page.wait_for_function("""() =>
              document.body.dataset.offlineReadonly === 'false' &&
              !document.querySelector('.main-tab[data-value="wiki"]')
            """, timeout=25_000)
        except Exception:
            reconnected = False
        after_reconnect = page.evaluate("""() => ({
          tabs: [...document.querySelectorAll('.main-tab[data-generated="1"]')].map(el => el.dataset.value),
          aura: document.getElementById('chat-toggle-btn')?.classList.contains('chat-aura-red'),
          authoritative: window.__pagesCatalogAuthoritative,
        })""")
        if not reconnected or "wiki" in after_reconnect["tabs"] or not after_reconnect["authoritative"]:
            failures.append("authoritative revoked catalog did not replace cached views")
        if after_reconnect["aura"]:
            failures.append("offline chat aura did not clear after confirmed reconnection")
        page.screenshot(path=str(self.artifacts / "TC-3-reconnected.png"), full_page=True)
        context.close()
        self._record("TC-3", started, {
            "TC-3-S1": {"actual": during_outage, "result": "pass" if "wiki" in during_outage["tabs"] else "fail"},
            "TC-3-S2": {"actual": after_reconnect, "result": "fail" if failures else "pass"},
        }, failures)
        self.assertFalse(failures, failures)


if __name__ == "__main__":
    unittest.main()

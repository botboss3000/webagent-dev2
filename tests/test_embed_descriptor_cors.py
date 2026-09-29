"""Embed descriptor endpoint: open-CORS grant + off-by-default contract tests.

The loader on a customer's site fetches ``GET /api/v1/agents/{id}/embed``
cross-origin with no credentials. The endpoint must answer with
``Access-Control-Allow-Origin: *`` so a client can paste the snippet and get a
working widget with zero per-client server config (no WEBAGENT_ALLOWED_ORIGINS,
no restart). The endpoint is already public and returns only presentation data.

Also locks the creation contract: an agent with no embed config is NOT
embeddable by default (embed.enabled defaults to False).
"""

from __future__ import annotations

import unittest
from pathlib import Path
from unittest.mock import patch

from fastapi import FastAPI
from starlette.requests import Request
from starlette.testclient import TestClient

from app.api import agents as agents_module
from app.api.embed_config import public_embed_config

AGENT_ID = "11111111-2222-3333-4444-555555555555"
CROSS_ORIGIN = "https://customer-site.example"


def _agent_row() -> dict:
    return {
        "id": AGENT_ID,
        "name": "Test Assistant",
        "user_mode": "anonymous",
        "metadata": {
            "embed": {
                "enabled": True,
                "allowed_domains": [],
                "accent": "#8BA88E",
                "title": "Test Assistant",
                "greeting": "Hi!",
            },
            "icon": "music",
        },
    }


class _FakeDB:
    async def get_agent_by_id(self, agent_id: str):
        return _agent_row() if agent_id == AGENT_ID else None


def _get(path: str, headers: dict | None = None):
    """Build a bare app with the REAL agents router and issue a GET with a
    stubbed DB — matches the local convention of isolated TestClient tests."""
    app = FastAPI()
    app.include_router(agents_module.router)
    with patch.object(agents_module, "get_db", return_value=_FakeDB()):
        with TestClient(app) as client:
            return client.get(path, headers=headers)


class EmbedDescriptorCorsTests(unittest.TestCase):
    def test_cross_origin_fetch_gets_open_cors_grant(self):
        """Any origin (the customer's site) may read the public descriptor."""
        resp = _get(
            f"/api/v1/agents/{AGENT_ID}/embed",
            headers={"Origin": CROSS_ORIGIN},
        )
        self.assertEqual(resp.status_code, 200)
        self.assertEqual(
            resp.headers.get("access-control-allow-origin"), "*"
        )
        body = resp.json()
        self.assertEqual(body["agent_id"], AGENT_ID)
        self.assertEqual(body["agent_name"], "Test Assistant")
        self.assertTrue(body["enabled"])
        self.assertTrue(body["embeddable"])
        self.assertNotIn("allowed_domains", body["config"])
        # The launcher icon ships as ready-to-inject SVG for the agent's icon.
        svg = body["config"]["agent_icon_svg"]
        self.assertTrue(svg.startswith("<svg"))
        self.assertIn("M9 18V5l12-2v13", svg)  # lucide "music" path

    def test_no_origin_request_still_served(self):
        """Direct/curl/same-origin callers (no Origin) are unaffected."""
        resp = _get(f"/api/v1/agents/{AGENT_ID}/embed")
        self.assertEqual(resp.status_code, 200)
        self.assertEqual(
            resp.headers.get("access-control-allow-origin"), "*"
        )

    def test_unknown_agent_is_404(self):
        resp = _get(
            "/api/v1/agents/99999999-9999-9999-9999-999999999999/embed",
            headers={"Origin": CROSS_ORIGIN},
        )
        self.assertEqual(resp.status_code, 404)

    def test_payload_never_leaks_domain_allowlist(self):
        """The security gate (framing) stays server-side; the client payload
        must not expose allowed_domains."""
        resp = _get(
            f"/api/v1/agents/{AGENT_ID}/embed",
            headers={"Origin": CROSS_ORIGIN},
        )
        self.assertEqual(resp.status_code, 200)
        self.assertNotIn("allowed_domains", resp.json()["config"])
        self.assertNotIn("allowed_domains", resp.json())


class EmbedDefaultContractTests(unittest.TestCase):
    def test_embed_composer_is_capped_to_the_iframe_viewport(self):
        css = (
            Path(__file__).resolve().parents[1] / "ui/embed/embed-chat.css"
        ).read_text(encoding="utf-8")
        self.assertIn(
            ".ec-root .ec-pill, .ec-root .ec-above-pill, .ec-root .ec-below-pill",
            css,
        )
        self.assertIn(
            "width: min(var(--chat-pill-current-width, var(--chat-pill-width, 500px)), 100%);",
            css,
        )
        self.assertIn("max-width: 100%;", css)
        self.assertIn('.ec-auth-actions button[type="submit"]', css)

    def test_controlled_login_card_uses_public_component(self):
        client = (
            Path(__file__).resolve().parents[1] / "ui/embed/embed-chat.js"
        ).read_text(encoding="utf-8")
        self.assertIn("item.kind === 'component' && item.payload?.ui === 'agent_auth'", client)
        self.assertIn("authCard(item.payload)", client)
        self.assertIn("published.set(item.id, { revision: item.revision })", client)
        self.assertIn("'/api/v2/chat/sessions'", client)
        self.assertNotIn("'/api/v1/chat/stream'", client)
        self.assertIn("setAttribute('aria-busy', 'true')", client)
        self.assertIn("setAttribute('aria-busy', 'false')", client)
        self.assertIn("els.activityText.textContent = 'Working…'", client)
        self.assertIn("els.activityText.textContent = ''", client)

    def test_open_embed_keeps_hidden_launcher_geometry_for_panel_placement(self):
        loader = (
            Path(__file__).resolve().parents[1] / "ui/embed/embed.js"
        ).read_text(encoding="utf-8")
        self.assertIn(
            ".wa-launch.wa-hidden{visibility:hidden;pointer-events:none;}",
            loader,
        )
        self.assertNotIn(".wa-launch.wa-hidden{display:none;}", loader)

    def test_embed_document_uses_standalone_profile_aware_client(self):
        from app.main import _render_embed_chat_document

        agent = _agent_row()
        cfg = public_embed_config(agent)
        html = _render_embed_chat_document(AGENT_ID, agent, cfg)

        self.assertIn(f'"agentId":"{AGENT_ID}"', html)
        self.assertIn('window.__EMBED__=', html)
        self.assertIn('/ui/embed/embed-chat.js', html)
        self.assertIn('id="ec-send"', html)
        self.assertNotIn('window.__CHAT_PORTAL__', html)
        self.assertNotIn('id="main-content"', html)

    def test_nested_agent_shell_rootifies_relative_assets(self):
        """Nested /embed/{id} must resolve page assets from the root."""
        from app.main import _render_app_shell

        request = Request({
            "type": "http",
            "http_version": "1.1",
            "method": "GET",
            "scheme": "http",
            "path": f"/embed/{AGENT_ID}",
            "raw_path": f"/embed/{AGENT_ID}".encode(),
            "query_string": b"",
            "headers": [],
            "server": ("testserver", 80),
            "client": ("127.0.0.1", 1234),
            "root_path": "",
        })
        response = _render_app_shell(
            request,
            agent_id=AGENT_ID,
            agent_name="Test Assistant",
            chat_portal=True,
            chat_portal_config={},
        )
        html = response.body.decode("utf-8")
        self.assertNotIn('"./ui/', html)
        self.assertNotIn("'./ui/", html)
        self.assertIn('href="/ui/vendor/fonts/fonts.css"', html)
        self.assertLess(html.index('<base href="/">'), html.index('href="/ui/'))
        self.assertEqual(html.count('<base href="/">'), 1)

    def test_embed_portal_is_a_root_api_shell_alias(self):
        config_js = (
            Path(__file__).resolve().parents[1] / "ui/shared/js/config.js"
        ).read_text(encoding="utf-8")
        self.assertIn("_EMBED_PATH_RE.test(path)", config_js)

    def test_embed_defaults_to_disabled(self):
        """A fresh agent with no embed config is NOT embeddable — off by default."""
        agent = {"id": AGENT_ID, "name": "Fresh", "user_mode": "anonymous", "metadata": {}}
        cfg = public_embed_config(agent)
        self.assertIs(cfg["enabled"], False)

    def test_explicit_embed_enable_turns_it_on(self):
        agent = {
            "id": AGENT_ID,
            "name": "On",
            "user_mode": "anonymous",
            "metadata": {"embed": {"enabled": True}},
        }
        cfg = public_embed_config(agent)
        self.assertIs(cfg["enabled"], True)

    def test_unknown_icon_falls_back_to_chat_bubble(self):
        """An icon name outside the curated set still yields a usable SVG."""
        agent = {
            "id": AGENT_ID,
            "name": "Fallback",
            "user_mode": "anonymous",
            "metadata": {"embed": {"enabled": True}, "icon": "definitely-not-an-icon"},
        }
        cfg = public_embed_config(agent)
        self.assertIn("M21 11.5", cfg["agent_icon_svg"])  # chat-bubble path

    def test_missing_icon_defaults_to_bot(self):
        agent = {"id": AGENT_ID, "name": "Plain", "user_mode": "anonymous", "metadata": {}}
        cfg = public_embed_config(agent)
        self.assertEqual(cfg["agent_icon"], "bot")
        self.assertTrue(cfg["agent_icon_svg"].startswith("<svg"))


if __name__ == "__main__":
    unittest.main()

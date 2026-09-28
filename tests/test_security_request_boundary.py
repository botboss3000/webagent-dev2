"""Phase 4 exact-origin request-boundary contract tests."""

from __future__ import annotations

import os
import unittest
from unittest.mock import patch

import httpx
from fastapi import FastAPI, WebSocket
from fastapi.middleware.cors import CORSMiddleware
from starlette.testclient import TestClient
from starlette.websockets import WebSocketDisconnect

from app.security import (
    CORS_ALLOW_HEADERS,
    CORS_ALLOW_METHODS,
    RequestSecurityMiddleware,
    WebSecurityPolicy,
    normalize_origin,
)
from app.security.request_boundary import InvalidOrigin


def _app(policy: WebSecurityPolicy):
    app = FastAPI()
    mutations: list[str] = []

    @app.get("/resource")
    async def resource():
        return {"ok": True}

    @app.post("/mutate")
    async def mutate():
        mutations.append("changed")
        return {"ok": True}

    @app.websocket("/socket")
    async def socket(websocket: WebSocket):
        await websocket.accept()
        await websocket.send_text("accepted")
        await websocket.close()

    app.add_middleware(
        CORSMiddleware,
        allow_origins=list(policy.allowed_origins),
        allow_credentials=policy.allow_credentials,
        allow_methods=list(CORS_ALLOW_METHODS),
        allow_headers=list(CORS_ALLOW_HEADERS),
    )
    app.add_middleware(RequestSecurityMiddleware, policy=policy)
    return app, mutations


class OriginConfigurationTests(unittest.TestCase):
    def test_only_canonical_exact_origins_are_accepted(self):
        self.assertEqual(
            normalize_origin("https://api.example.test:8443"),
            "https://api.example.test:8443",
        )
        self.assertEqual(
            normalize_origin("chrome-extension://abcdefghijklmnop"),
            "chrome-extension://abcdefghijklmnop",
        )

        for value in (
            "*",
            "null",
            "HTTPS://api.example.test",
            "https://API.example.test",
            "https://api.example.test/",
            "https://api.example.test:443",
            "https://user@api.example.test",
            "https://*.example.test",
            "https://api.example.test/path",
        ):
            with self.subTest(value=value):
                with self.assertRaises(InvalidOrigin):
                    normalize_origin(value)

    def test_environment_policy_is_exact_deduplicated_and_locked_at_boot(self):
        with patch.dict(
            os.environ,
            {
                "WEBAGENT_ALLOWED_ORIGINS": (
                    "https://one.example,https://two.example:8443,"
                    "https://one.example"
                ),
                "WEBAGENT_CORS_ALLOW_CREDENTIALS": "false",
                "WEBAGENT_CSP_MODE": "enforce",
            },
            clear=False,
        ):
            policy = WebSecurityPolicy.from_env()
        self.assertEqual(
            policy.allowed_origins,
            ("https://one.example", "https://two.example:8443"),
        )
        self.assertFalse(policy.allow_credentials)
        self.assertEqual(policy.csp_mode, "enforce")

        os.environ["WEBAGENT_ALLOWED_ORIGINS"] = "https://later.example"
        self.assertNotIn("https://later.example", policy.allowed_origins)


class HttpRequestBoundaryTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.policy = WebSecurityPolicy(
            allowed_origins=("https://client.example",)
        )
        self.app, self.mutations = _app(self.policy)
        self.client = httpx.AsyncClient(
            transport=httpx.ASGITransport(app=self.app),
            base_url="http://testserver",
        )

    async def asyncTearDown(self):
        await self.client.aclose()

    async def test_same_origin_and_allowlisted_origins_can_mutate(self):
        same = await self.client.post(
            "/mutate", headers={"Origin": "http://testserver"}
        )
        allowed = await self.client.post(
            "/mutate", headers={"Origin": "https://client.example"}
        )
        self.assertEqual(same.status_code, 200)
        self.assertEqual(allowed.status_code, 200)
        self.assertEqual(self.mutations, ["changed", "changed"])
        self.assertEqual(
            allowed.headers["access-control-allow-origin"],
            "https://client.example",
        )
        self.assertIn("origin", allowed.headers["vary"].lower())

    async def test_disallowed_and_confusable_origins_fail_before_mutation(self):
        for origin in (
            "https://attacker.example",
            "null",
            "https://client.example.attacker.test",
            "HTTPS://client.example",
            "https://client.example:443",
            "https://user@client.example",
        ):
            with self.subTest(origin=origin):
                response = await self.client.post(
                    "/mutate", headers={"Origin": origin}
                )
                self.assertEqual(response.status_code, 403)
        self.assertEqual(self.mutations, [])

    async def test_sec_fetch_site_rejects_originless_cross_site_mutation(self):
        for site in ("cross-site", "same-site", "unexpected"):
            with self.subTest(site=site):
                response = await self.client.post(
                    "/mutate", headers={"Sec-Fetch-Site": site}
                )
                self.assertEqual(response.status_code, 403)
        self.assertEqual(self.mutations, [])

    async def test_non_browser_server_request_without_origin_still_works(self):
        response = await self.client.post("/mutate")
        self.assertEqual(response.status_code, 200)
        self.assertEqual(self.mutations, ["changed"])

    async def test_preflight_methods_and_headers_are_minimal(self):
        allowed = await self.client.options(
            "/mutate",
            headers={
                "Origin": "https://client.example",
                "Access-Control-Request-Method": "POST",
                "Access-Control-Request-Headers": "authorization,content-type",
            },
        )
        self.assertEqual(allowed.status_code, 200)
        allow_methods = {
            item.strip()
            for item in allowed.headers["access-control-allow-methods"].split(",")
        }
        self.assertEqual(allow_methods, set(CORS_ALLOW_METHODS))
        self.assertNotIn("*", allowed.headers["access-control-allow-headers"])

        bad_method = await self.client.options(
            "/mutate",
            headers={
                "Origin": "https://client.example",
                "Access-Control-Request-Method": "TRACE",
            },
        )
        bad_header = await self.client.options(
            "/mutate",
            headers={
                "Origin": "https://client.example",
                "Access-Control-Request-Method": "POST",
                "Access-Control-Request-Headers": "x-provider-secret",
            },
        )
        self.assertEqual(bad_method.status_code, 400)
        self.assertEqual(bad_header.status_code, 400)

    async def test_security_headers_default_to_report_only(self):
        response = await self.client.get("/resource")
        self.assertEqual(response.headers["referrer-policy"], "no-referrer")
        self.assertEqual(response.headers["x-content-type-options"], "nosniff")
        self.assertIn(
            "object-src 'none'",
            response.headers["content-security-policy-report-only"],
        )
        self.assertIn(
            "base-uri 'self'",
            response.headers["content-security-policy-report-only"],
        )
        self.assertNotIn("unsafe-eval", response.headers[
            "content-security-policy-report-only"
        ])
        policy = response.headers["content-security-policy-report-only"]
        script_directive = next(
            directive.strip() for directive in policy.split(";")
            if directive.strip().startswith("script-src ")
        )
        self.assertNotIn("unsafe-inline", script_directive)
        self.assertIn("style-src 'self' 'unsafe-inline'", policy)

    async def test_https_response_gets_short_hsts(self):
        response = await self.client.get(
            "/resource",
            headers={"Host": "dev.webagent.live", "X-Forwarded-Proto": "https"},
        )
        self.assertEqual(
            response.headers["strict-transport-security"],
            "max-age=86400",
        )

    async def test_public_http_is_redirected_but_health_can_remain_internal(self):
        response = await self.client.get(
            "/resource",
            headers={"Host": "dev.webagent.live", "X-Forwarded-Proto": "http"},
        )
        self.assertEqual(response.status_code, 308)
        self.assertEqual(
            response.headers["location"],
            "https://dev.webagent.live/resource",
        )

    async def test_enforced_csp_preserves_route_specific_frame_policy(self):
        app = FastAPI()

        @app.get("/")
        async def resource():
            from fastapi.responses import PlainTextResponse

            return PlainTextResponse(
                "ok",
                headers={
                    "Content-Security-Policy": (
                        "frame-ancestors https://embed.example"
                    )
                },
            )

        app.add_middleware(
            RequestSecurityMiddleware,
            policy=WebSecurityPolicy(csp_mode="enforce"),
        )
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app),
            base_url="http://testserver",
        ) as client:
            response = await client.get("/")
        csp = response.headers["content-security-policy"]
        self.assertIn("frame-ancestors https://embed.example", csp)
        self.assertIn("object-src 'none'", csp)
        self.assertNotIn("frame-ancestors 'self'", csp)

    async def test_report_only_csp_does_not_conflict_with_enforced_embed_policy(self):
        app = FastAPI()

        @app.get("/embed/agent-id")
        async def resource():
            from fastapi.responses import PlainTextResponse

            return PlainTextResponse(
                "ok",
                headers={
                    "Content-Security-Policy": (
                        "frame-ancestors https://embed.example"
                    )
                },
            )

        app.add_middleware(
            RequestSecurityMiddleware,
            policy=WebSecurityPolicy(csp_mode="report-only"),
        )
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app),
            base_url="http://testserver",
        ) as client:
            response = await client.get("/embed/agent-id")

        enforced = response.headers["content-security-policy"]
        report_only = response.headers["content-security-policy-report-only"]
        self.assertIn("frame-ancestors https://embed.example", enforced)
        self.assertNotIn("frame-ancestors", report_only)
        self.assertIn("object-src 'none'", report_only)


class WebSocketOriginTests(unittest.TestCase):
    def setUp(self):
        self.app, _mutations = _app(
            WebSecurityPolicy(allowed_origins=("https://client.example",))
        )

    def test_same_origin_and_allowlisted_websockets_succeed(self):
        with TestClient(self.app) as client:
            for origin in ("http://testserver", "https://client.example"):
                with self.subTest(origin=origin):
                    with client.websocket_connect(
                        "/socket", headers={"Origin": origin}
                    ) as websocket:
                        self.assertEqual(websocket.receive_text(), "accepted")

    def test_missing_or_disallowed_websocket_origin_is_rejected(self):
        with TestClient(self.app) as client:
            for headers in (
                {},
                {"Origin": "null"},
                {"Origin": "https://client.example.attacker.test"},
            ):
                with self.subTest(headers=headers):
                    with self.assertRaises(WebSocketDisconnect) as caught:
                        with client.websocket_connect(
                            "/socket", headers=headers
                        ):
                            pass
                    self.assertEqual(caught.exception.code, 4403)

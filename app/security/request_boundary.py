"""Exact-origin request integrity and application security headers.

The API uses bearer tokens, so cross-origin callers do not need credentialed
CORS.  An explicit deployment allowlist is still required for browser callers:
embed/frame origins are intentionally a separate per-agent policy.
"""

from __future__ import annotations

import json
import os
from dataclasses import dataclass
from urllib.parse import urlsplit


CORS_ALLOW_METHODS = ("GET", "HEAD", "POST", "PUT", "PATCH", "DELETE")
CORS_ALLOW_HEADERS = ("Accept", "Authorization", "Content-Type")
UNSAFE_METHODS = frozenset({"POST", "PUT", "PATCH", "DELETE"})

DEFAULT_CSP = (
    "default-src 'self'; "
    "base-uri 'self'; "
    "object-src 'none'; "
    "frame-ancestors 'self'; "
    "form-action 'self'; "
    "script-src 'self'; "
    "script-src-attr 'none'; "
    "style-src 'self' 'unsafe-inline'; "
    "connect-src 'self'; "
    "img-src 'self' data: blob:; "
    "media-src 'self' blob:; "
    "font-src 'self' data:; "
    "frame-src 'self'; "
    "worker-src 'self' blob:; "
    "manifest-src 'self'"
)

_CSP_MODES = frozenset({"report-only", "enforce", "disabled"})
_ORIGIN_SCHEMES = frozenset({"http", "https", "chrome-extension", "moz-extension"})


class InvalidOrigin(ValueError):
    """Raised when an origin is not a canonical, exact origin tuple."""


def normalize_origin(value: str) -> str:
    """Validate and return a canonical origin.

    The caller must already provide canonical spelling.  Refusing to silently
    rewrite mixed-case hosts, explicit default ports, trailing slashes, or
    user-info keeps configuration and request comparison exact and auditable.
    """

    raw = (value or "").strip()
    if not raw or raw in {"*", "null"}:
        raise InvalidOrigin("wildcard, null, and empty origins are not allowed")
    if raw != value or any(ch.isspace() for ch in raw):
        raise InvalidOrigin("origin contains whitespace")

    try:
        parsed = urlsplit(raw)
        port = parsed.port
    except ValueError as exc:
        raise InvalidOrigin("origin has an invalid authority or port") from exc

    if parsed.scheme not in _ORIGIN_SCHEMES:
        raise InvalidOrigin("origin scheme is not allowed")
    if not parsed.hostname:
        raise InvalidOrigin("origin must include a host")
    if parsed.username is not None or parsed.password is not None:
        raise InvalidOrigin("origin must not include user-info")
    if parsed.path or parsed.query or parsed.fragment:
        raise InvalidOrigin("origin must not include a path, query, or fragment")

    try:
        parsed.hostname.encode("ascii")
    except UnicodeEncodeError as exc:
        raise InvalidOrigin("origin host must use ASCII/IDNA spelling") from exc
    if "*" in parsed.hostname:
        raise InvalidOrigin("origin must not contain a wildcard")

    if port is not None and (
        (parsed.scheme == "http" and port == 80)
        or (parsed.scheme == "https" and port == 443)
    ):
        raise InvalidOrigin("origin must omit its scheme's default port")
    if parsed.scheme.endswith("-extension") and port is not None:
        raise InvalidOrigin("extension origins cannot include a port")

    host = parsed.hostname
    authority_host = f"[{host}]" if ":" in host else host
    canonical = f"{parsed.scheme}://{authority_host}"
    if port is not None:
        canonical += f":{port}"
    if raw != canonical:
        raise InvalidOrigin("origin is not in canonical form")
    return canonical


def parse_allowed_origins(value: str | None) -> tuple[str, ...]:
    """Parse a comma-separated exact origin allowlist."""

    if not value or not value.strip():
        return ()
    result: list[str] = []
    for candidate in value.split(","):
        origin = normalize_origin(candidate.strip())
        if origin not in result:
            result.append(origin)
    return tuple(result)


def _env_bool(name: str, default: bool = False) -> bool:
    raw = os.environ.get(name)
    if raw is None:
        return default
    value = raw.strip().lower()
    if value in {"1", "true", "yes", "on"}:
        return True
    if value in {"0", "false", "no", "off"}:
        return False
    raise ValueError(f"{name} must be true or false")


@dataclass(frozen=True)
class WebSecurityPolicy:
    """Boot-time, environment-locked browser request policy."""

    allowed_origins: tuple[str, ...] = ()
    allow_credentials: bool = False
    csp_mode: str = "report-only"
    csp_policy: str = DEFAULT_CSP
    max_json_body_bytes: int = 1024 * 1024

    def __post_init__(self) -> None:
        normalized = tuple(normalize_origin(origin) for origin in self.allowed_origins)
        if normalized != self.allowed_origins:
            raise ValueError("allowed_origins must be canonical")
        if self.csp_mode not in _CSP_MODES:
            raise ValueError(
                "WEBAGENT_CSP_MODE must be report-only, enforce, or disabled"
            )
        if "\r" in self.csp_policy or "\n" in self.csp_policy:
            raise ValueError("CSP policy cannot contain newlines")
        if self.max_json_body_bytes <= 0:
            raise ValueError("WEBAGENT_MAX_JSON_BODY_BYTES must be positive")

    @classmethod
    def from_env(cls) -> "WebSecurityPolicy":
        return cls(
            allowed_origins=parse_allowed_origins(
                os.environ.get("WEBAGENT_ALLOWED_ORIGINS")
            ),
            allow_credentials=_env_bool(
                "WEBAGENT_CORS_ALLOW_CREDENTIALS", default=False
            ),
            csp_mode=os.environ.get(
                "WEBAGENT_CSP_MODE", "report-only"
            ).strip().lower(),
            max_json_body_bytes=int(
                os.environ.get("WEBAGENT_MAX_JSON_BODY_BYTES", str(1024 * 1024))
            ),
        )

    def permits(self, origin: str, request_origin: str | None) -> bool:
        try:
            candidate = normalize_origin(origin)
        except InvalidOrigin:
            return False
        return candidate == request_origin or candidate in self.allowed_origins

    @property
    def csp_header(self) -> str | None:
        if self.csp_mode == "enforce":
            return "content-security-policy"
        if self.csp_mode == "report-only":
            return "content-security-policy-report-only"
        return None


def _headers(scope: dict) -> dict[str, list[str]]:
    values: dict[str, list[str]] = {}
    for key, value in scope.get("headers", ()):
        name = key.decode("latin-1").lower()
        values.setdefault(name, []).append(value.decode("latin-1"))
    return values


def _request_origin(scope: dict, headers: dict[str, list[str]]) -> str | None:
    hosts = headers.get("host", ())
    if len(hosts) != 1:
        return None
    scheme = scope.get("scheme", "http").lower()
    # Respect X-Forwarded-Proto so TLS-terminating proxies (Cloudflare tunnel,
    # nginx, etc.) produce the correct scheme in the origin comparison. Without
    # this, the browser's https:// origin never matches the server's http://
    # self-view, and every browser POST/PUT/DELETE is rejected as cross-origin.
    fwd_proto = headers.get("x-forwarded-proto", ())
    if fwd_proto and fwd_proto[0].lower() in ("https", "http"):
        scheme = fwd_proto[0].lower()
    if scheme == "ws":
        scheme = "http"
    elif scheme == "wss":
        scheme = "https"
    try:
        return normalize_origin(f"{scheme}://{hosts[0]}")
    except InvalidOrigin:
        return None


def _single_origin(headers: dict[str, list[str]]) -> str | None:
    origins = headers.get("origin", ())
    if not origins:
        return None
    if len(origins) != 1:
        return ""
    return origins[0]


def _append_vary(headers: list[tuple[bytes, bytes]], token: str) -> None:
    indexes = [
        index for index, (key, _value) in enumerate(headers)
        if key.lower() == b"vary"
    ]
    existing: list[str] = []
    for index in indexes:
        existing.extend(
            item.strip()
            for item in headers[index][1].decode("latin-1").split(",")
            if item.strip()
        )
    if token.lower() not in {item.lower() for item in existing}:
        existing.append(token)
    if indexes:
        first = indexes[0]
        headers[first] = (b"vary", ", ".join(existing).encode("latin-1"))
        for index in reversed(indexes[1:]):
            del headers[index]
    else:
        headers.append((b"vary", token.encode("latin-1")))


def _setdefault_header(
    headers: list[tuple[bytes, bytes]], name: str, value: str
) -> None:
    encoded_name = name.encode("latin-1")
    if any(key.lower() == encoded_name for key, _value in headers):
        return
    headers.append((encoded_name, value.encode("latin-1")))


def _merge_csp_header(
    headers: list[tuple[bytes, bytes]], name: str, policy: str
) -> None:
    """Add missing directives while preserving a route's stricter override.

    Embed pages already supply their per-agent ``frame-ancestors`` directive.
    In enforce mode it must survive while the rest of the application policy is
    still applied.
    """

    encoded_name = name.encode("latin-1")
    for index, (key, value) in enumerate(headers):
        if key.lower() != encoded_name:
            continue
        existing = value.decode("latin-1").strip().rstrip(";")
        existing_names = {
            directive.strip().split(None, 1)[0].lower()
            for directive in existing.split(";")
            if directive.strip()
        }
        additions = [
            directive.strip()
            for directive in policy.split(";")
            if directive.strip()
            and directive.strip().split(None, 1)[0].lower() not in existing_names
        ]
        merged = "; ".join([existing, *additions])
        headers[index] = (key, merged.encode("latin-1"))
        return
    headers.append((encoded_name, policy.encode("latin-1")))


def _without_csp_directive(policy: str, directive_name: str) -> str:
    """Return ``policy`` without one named directive."""
    wanted = directive_name.strip().lower()
    directives = [
        directive.strip()
        for directive in policy.split(";")
        if directive.strip()
        and directive.strip().split(None, 1)[0].lower() != wanted
    ]
    return "; ".join(directives) + (";" if directives else "")


def _has_csp_directive(
    headers: list[tuple[bytes, bytes]], name: str, directive_name: str
) -> bool:
    encoded_name = name.encode("latin-1")
    wanted = directive_name.strip().lower()
    for key, value in headers:
        if key.lower() != encoded_name:
            continue
        return any(
            directive.strip().split(None, 1)[0].lower() == wanted
            for directive in value.decode("latin-1").split(";")
            if directive.strip()
        )
    return False


class RequestSecurityMiddleware:
    """Enforce origin integrity before auth/session work and harden responses."""

    def __init__(self, app, policy: WebSecurityPolicy):
        self.app = app
        self.policy = policy

    async def __call__(self, scope, receive, send):
        scope_type = scope.get("type")
        if scope_type not in {"http", "websocket"}:
            return await self.app(scope, receive, send)

        headers = _headers(scope)
        origin = _single_origin(headers)
        request_origin = _request_origin(scope, headers)
        origin_allowed = bool(
            origin and self.policy.permits(origin, request_origin)
        )

        if scope_type == "websocket":
            # Browser WebSockets always carry Origin.  Requiring it also prevents
            # non-browser clients from bypassing the deployment's browser policy.
            if not origin_allowed:
                await send({"type": "websocket.close", "code": 4403})
                return
            return await self.app(scope, receive, send)

        method = str(scope.get("method", "GET")).upper()
        hosts = headers.get("host", ())
        forwarded_proto = headers.get("x-forwarded-proto", ())
        path = str(scope.get("path", ""))
        public_host = (
            len(hosts) == 1
            and hosts[0].split(":", 1)[0].lower().endswith(".webagent.live")
        )
        if (
            public_host
            and path not in {"/health", "/health/ready"}
            and len(forwarded_proto) == 1
            and forwarded_proto[0].split(",", 1)[0].strip().lower() == "http"
        ):
            query = scope.get("query_string", b"").decode("latin-1")
            location = f"https://{hosts[0]}{path}" + (f"?{query}" if query else "")
            await self._redirect_https(send, location)
            return
        if method in UNSAFE_METHODS:
            if origin is not None and not origin_allowed:
                await self._reject_http(send, "Origin is not allowed")
                return
            fetch_sites = headers.get("sec-fetch-site", ())
            if origin is None and fetch_sites:
                if (
                    len(fetch_sites) != 1
                    or fetch_sites[0].lower() not in {"same-origin", "none"}
                ):
                    await self._reject_http(
                        send, "Cross-origin request is not allowed"
                    )
                    return

        # JSON endpoints parse the full document before dispatch. Buffering at
        # this outer boundary puts a hard ceiling on both Content-Length and
        # chunked bodies before Pydantic, database, or model work can allocate
        # unbounded memory.
        content_types = headers.get("content-type", ())
        if any(value.split(";", 1)[0].strip().lower() == "application/json" for value in content_types):
            body = bytearray()
            while True:
                message = await receive()
                if message.get("type") == "http.disconnect":
                    return
                if message.get("type") != "http.request":
                    continue
                body.extend(message.get("body", b""))
                if len(body) > self.policy.max_json_body_bytes:
                    await self._reject_http(send, "JSON request body is too large", status=413)
                    return
                if not message.get("more_body", False):
                    break
            delivered = False

            async def replay_receive():
                nonlocal delivered
                if delivered:
                    return {"type": "http.request", "body": b"", "more_body": False}
                delivered = True
                return {"type": "http.request", "body": bytes(body), "more_body": False}

            receive = replay_receive

        async def send_hardened(message):
            if message.get("type") == "http.response.start":
                response_headers = list(message.get("headers", ()))
                _setdefault_header(
                    response_headers, "referrer-policy", "no-referrer"
                )
                _setdefault_header(
                    response_headers, "x-content-type-options", "nosniff"
                )
                _setdefault_header(
                    response_headers,
                    "permissions-policy",
                    "camera=(self), microphone=(self), geolocation=()",
                )
                _setdefault_header(
                    response_headers,
                    "x-permitted-cross-domain-policies",
                    "none",
                )
                if request_origin and request_origin.startswith("https://"):
                    _setdefault_header(
                        response_headers,
                        "strict-transport-security",
                        "max-age=86400",
                    )
                if self.policy.csp_header:
                    csp_policy = self.policy.csp_policy
                    # An embeddable route supplies an enforced, per-agent
                    # frame-ancestors policy. In report-only mode the global
                    # header has a different name, so normal merging cannot
                    # replace its default 'self' directive. Keeping both makes
                    # valid customer embeds emit a false violation report.
                    if (
                        self.policy.csp_header.lower()
                        == "content-security-policy-report-only"
                        and _has_csp_directive(
                            response_headers,
                            "content-security-policy",
                            "frame-ancestors",
                        )
                    ):
                        csp_policy = _without_csp_directive(
                            csp_policy, "frame-ancestors"
                        )
                    _merge_csp_header(
                        response_headers,
                        self.policy.csp_header,
                        csp_policy,
                    )
                if origin_allowed:
                    _append_vary(response_headers, "Origin")
                message = {**message, "headers": response_headers}
            await send(message)

        await self.app(scope, receive, send_hardened)

    async def _redirect_https(self, send, location: str) -> None:
        await send({
            "type": "http.response.start",
            "status": 308,
            "headers": [
                (b"location", location.encode("latin-1")),
                (b"content-length", b"0"),
                (b"referrer-policy", b"no-referrer"),
            ],
        })
        await send({"type": "http.response.body", "body": b""})

    async def _reject_http(self, send, detail: str, *, status: int = 403) -> None:
        body = json.dumps({"detail": detail}, separators=(",", ":")).encode()
        headers = [
            (b"content-type", b"application/json"),
            (b"content-length", str(len(body)).encode("ascii")),
            (b"referrer-policy", b"no-referrer"),
            (b"x-content-type-options", b"nosniff"),
            (
                b"permissions-policy",
                b"camera=(self), microphone=(self), geolocation=()",
            ),
            (b"x-permitted-cross-domain-policies", b"none"),
        ]
        if self.policy.csp_header:
            headers.append(
                (
                    self.policy.csp_header.encode("latin-1"),
                    self.policy.csp_policy.encode("latin-1"),
                )
            )
        await send(
            {
                "type": "http.response.start",
                "status": status,
                "headers": headers,
            }
        )
        await send({"type": "http.response.body", "body": body})

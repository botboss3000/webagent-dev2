import asyncio
import json
import shutil
import subprocess
import textwrap
import time
from pathlib import Path

import pytest
from starlette.requests import Request


ROOT = Path(__file__).resolve().parents[1]


def test_hung_optional_section_is_omitted_without_blocking_boot(monkeypatch):
    from app.api import agents, boot as boot_api, chat, features
    from app.auth import identity
    from app.entitlements import service as entitlements

    async def ready(value):
        return value

    async def hung_prompts():
        await asyncio.Event().wait()

    monkeypatch.setattr(boot_api, "BOOT_SECTION_DEADLINE_SECONDS", 0.02)
    monkeypatch.setattr(identity, "request_user_id", lambda _request: "")
    monkeypatch.setattr("app.auth.access_mode", lambda: ready({"mode": "private"}))
    monkeypatch.setattr(
        "app.auth.ui_config",
        lambda _request: ready({"theme": "dark"}),
    )
    monkeypatch.setattr(features, "get_app_prompts", hung_prompts)
    monkeypatch.setattr(chat, "get_suggestions_config", lambda _request: ready({}))
    monkeypatch.setattr(
        agents,
        "get_abilities_catalog",
        lambda _request: ready({"abilities": {}}),
    )
    monkeypatch.setattr(
        agents,
        "get_pages_catalog",
        lambda _request: ready({"pages": ["wiki"]}),
    )
    monkeypatch.setattr(
        entitlements,
        "resolve_capabilities",
        lambda _uid: ready({"chat": True}),
    )

    request = Request({
        "type": "http",
        "method": "GET",
        "path": "/api/v1/boot",
        "query_string": b"",
        "headers": [],
    })
    started = time.perf_counter()
    result = asyncio.run(boot_api.boot(request))
    elapsed = time.perf_counter() - started

    assert elapsed < 0.5
    assert result["sections"]["pages_catalog"] == {"pages": ["wiki"]}
    assert result["sections"]["ui_config"] == {"theme": "dark"}
    assert "app_prompts" not in result["sections"]


@pytest.mark.skipif(shutil.which("node") is None, reason="Node.js is not installed")
def test_hung_boot_cannot_block_catalog_or_account_shell_fetches():
    source = (ROOT / "index.html").read_text(encoding="utf-8")
    marker = "// ── Boot API request coalescer"
    start = source.index("  (function () {", source.index(marker))
    end = source.index("  })();", start) + len("  })();")
    coalescer = source[start:end]

    script = textwrap.dedent(
        f"""
        const calls = [];
        const nativeFetch = (url) => {{
          calls.push(url);
          if (url.startsWith('/api/v1/boot')) return new Promise(() => {{}});
          return Promise.resolve(new Response('{{"ok":true}}', {{
            status: 200,
            headers: {{ 'Content-Type': 'application/json' }},
          }}));
        }};
        const listeners = new Map();
        globalThis.window = {{
          fetch: nativeFetch,
          addEventListener: (name, callback) => listeners.set(name, callback),
          requestIdleCallback: callback => callback(),
        }};
        Object.defineProperty(globalThis, 'performance', {{
          value: {{ now: () => 1 }}, configurable: true,
        }});
        globalThis.document = {{
          createElement: () => {{
            let parsed;
            return {{
              set href(value) {{ parsed = new URL(value, 'http://local'); }},
              get pathname() {{ return parsed.pathname; }},
              get search() {{ return parsed.search; }},
            }};
          }},
        }};
        globalThis.location = {{ pathname: '/app', origin: 'http://local' }};
        globalThis.localStorage = {{ getItem: () => '' }};

        {coalescer}

        const critical = [
          '/api/v1/auth/access-mode',
          '/api/v1/auth/ui-config',
          '/api/v1/pages/catalog',
          '/api/v1/entitlements/me',
        ];
        const responses = await Promise.race([
          Promise.all(critical.map(url => window.fetch(url))),
          new Promise((_, reject) => setTimeout(
            () => reject(new Error('shell fetches waited for hung /boot')), 250,
          )),
        ]);
        const callsBeforeReady = [...calls];
        listeners.get('webagent-app-ready')();
        console.log(JSON.stringify({{
          statuses: responses.map(r => r.status), calls, callsBeforeReady,
        }}));
        """
    )
    completed = subprocess.run(
        ["node", "--input-type=module", "-"],
        cwd=ROOT,
        input=script,
        encoding="utf-8",
        capture_output=True,
        timeout=5,
        check=True,
    )
    result = json.loads(completed.stdout)

    assert result["statuses"] == [200, 200, 200, 200]
    assert set(result["callsBeforeReady"]) == {
        "/api/v1/auth/access-mode",
        "/api/v1/auth/ui-config",
        "/api/v1/pages/catalog",
        "/api/v1/entitlements/me",
    }
    assert not any(call.startswith("/api/v1/boot") for call in result["callsBeforeReady"])
    assert result["calls"][-1].startswith("/api/v1/boot")


def test_transcript_and_agent_abilities_are_not_boot_expected_keys():
    source = (ROOT / "index.html").read_text(encoding="utf-8")
    marker = "function _bootUrlMap"
    start = source.index(marker)
    end = source.index("window.fetch = function", start)
    url_map = source[start:end]

    assert "session_messages" not in url_map
    assert "agent_abilities" not in url_map

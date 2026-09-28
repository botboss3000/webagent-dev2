from __future__ import annotations

import re

from app.bootstrap_asgi import _secure_html


def _script_directive(policy: str) -> str:
    return next(part.strip() for part in policy.split(";") if part.strip().startswith("script-src "))


def test_bootstrap_html_nonces_all_scripts_and_enforces_strict_script_policy() -> None:
    response = _secure_html("<html><script>window.ok=1</script><script src='/app.js'></script></html>")
    body = response.body.decode("utf-8")
    policy = response.headers["content-security-policy"]
    nonce = re.search(r"'nonce-([^']+)'", _script_directive(policy)).group(1)

    assert body.count(f'<script nonce="{nonce}"') == 2
    assert "unsafe-inline" not in _script_directive(policy)
    assert "script-src-attr 'none'" in policy
    assert response.headers["x-frame-options"] == "SAMEORIGIN"


def test_chat_partial_has_no_inline_event_handler() -> None:
    from pathlib import Path

    source = (Path(__file__).resolve().parents[1] / "ui/chat/chat-side-panel.html").read_text(encoding="utf-8")
    assert "onclick=" not in source


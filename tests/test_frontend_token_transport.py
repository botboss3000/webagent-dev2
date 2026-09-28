from pathlib import Path


def test_same_origin_fetches_strip_query_bearer_tokens() -> None:
    source = (Path(__file__).resolve().parents[1] / "ui/shared/js/auth-refresh.js").read_text(encoding="utf-8")
    patched = source[source.index("async function _patchedFetch"):source.index("export function installAuthRefresh")]

    assert "input = _withoutTokenInUrl(input);" in patched
    assert "u.searchParams.delete('token')" in source
    assert "u.searchParams.set('token', token)" not in source
    assert "init = _setAuthHeader(init || {}, tok)" in patched



def test_terminal_and_browser_websockets_use_single_use_tickets() -> None:
    root = Path(__file__).resolve().parents[1]
    terminal = (root / "ui/shared/js/terminal.js").read_text(encoding="utf-8")
    browser = (root / "ui/main-panel/browser/js/browser.js").read_text(encoding="utf-8")

    assert "connectionTicket('/api/v1/terminal/ws')" in terminal
    assert "&ticket=" in terminal
    assert "localStorage.getItem('auth_token')" not in terminal[terminal.index("async function connect()"):terminal.index("ws.onopen")]
    for path in ("/api/v1/browser/ws", "/api/v1/browser/screencast", "/api/v1/control/desktop"):
        assert f"connectionTicket('{path}')" in browser
    proxy_source = browser[browser.index("function _proxyUrl"):browser.index("function _showFrame")]
    assert "connectionTicket('/api/v1/browser/proxy')" in proxy_source
    assert "'?ticket='" in proxy_source
    assert "'?token='" not in proxy_source
    assert "new WebSocket(browserWsUrl() + qs)" in browser

"""Real-browser regressions for high-risk Phase 4 rendering sinks."""

from __future__ import annotations

import functools
import http.server
import threading
import unittest
from pathlib import Path


class _QuietHandler(http.server.SimpleHTTPRequestHandler):
    def log_message(self, format, *args):
        return


class BrowserXSSRegressionTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        try:
            from playwright.sync_api import sync_playwright
        except ImportError as exc:
            raise unittest.SkipTest(f"Playwright unavailable: {exc}")

        root = Path(__file__).resolve().parents[1]
        handler = functools.partial(_QuietHandler, directory=str(root))
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
        cls.page = cls.browser.new_page()
        cls.base = f"http://127.0.0.1:{cls.server.server_port}"
        cls.page.goto(f"{cls.base}/ui/diagnostics.html")
        cls.page.add_script_tag(url=f"{cls.base}/ui/vendor/marked/marked.min.js")
        cls.page.add_script_tag(url=f"{cls.base}/ui/vendor/dompurify/purify.min.js")

    @classmethod
    def tearDownClass(cls):
        if hasattr(cls, "browser"):
            cls.browser.close()
        if hasattr(cls, "playwright"):
            cls.playwright.stop()
        if hasattr(cls, "server"):
            cls.server.shutdown()
            cls.server.server_close()

    def test_tool_markdown_and_filename_payloads_cannot_read_indexeddb(self):
        result = self.page.evaluate(
            """async base => {
              await new Promise((resolve, reject) => {
                const request = indexedDB.open('phase4_xss_sentinel', 1);
                request.onupgradeneeded = () => request.result.createObjectStore('secrets');
                request.onsuccess = () => {
                  const tx = request.result.transaction('secrets', 'readwrite');
                  tx.objectStore('secrets').put('indexeddb-secret', 'sentinel');
                  tx.oncomplete = () => { request.result.close(); resolve(); };
                  tx.onerror = () => reject(tx.error);
                };
                request.onerror = () => reject(request.error);
              });

              window.__phase4XssExecuted = false;
              window.__phase4Exfiltrated = null;
              window.phase4Attack = () => {
                window.__phase4XssExecuted = true;
                const request = indexedDB.open('phase4_xss_sentinel');
                request.onsuccess = () => {
                  const read = request.result
                    .transaction('secrets', 'readonly')
                    .objectStore('secrets')
                    .get('sentinel');
                  read.onsuccess = () => { window.__phase4Exfiltrated = read.result; };
                };
              };
              const payload = '<img src=x onerror=phase4Attack()>';

              const toolHost = document.createElement('div');
              document.body.appendChild(toolHost);
              const {app} = await import(base + '/ui/shared/js/state.js');
              const {logTool} = await import(base + '/ui/shared/js/toolLog.js');
              app.toolLogContent = toolHost;
              logTool({type: 'stream', content: payload});
              logTool({type: 'tool_result', tool: payload, result: payload});

              const attachments = await import(base + '/ui/shared/js/attachments.js');
              const filename = attachments.renderAttachmentElement({
                mime_type: 'application/pdf',
                original_name: payload,
                url: 'about:blank',
              });
              document.body.appendChild(filename);

              const markdown = new Blob([payload], {type: 'text/markdown'});
              const markdownUrl = URL.createObjectURL(markdown);
              attachments.openAttachmentViewer({
                mime_type: 'text/markdown',
                original_name: payload,
                url: markdownUrl,
              });
              await new Promise(resolve => setTimeout(resolve, 250));
              URL.revokeObjectURL(markdownUrl);

              return {
                executed: window.__phase4XssExecuted,
                exfiltrated: window.__phase4Exfiltrated,
                toolImages: toolHost.querySelectorAll('img').length,
                filenameText: filename.textContent,
                markdownHandlers: document.querySelectorAll(
                  '.attachment-viewer-md [onerror], .attachment-viewer-md script'
                ).length,
              };
            }""",
            self.base,
        )
        self.assertFalse(result["executed"])
        self.assertIsNone(result["exfiltrated"])
        self.assertEqual(result["toolImages"], 0)
        self.assertIn("<img", result["filenameText"])
        self.assertEqual(result["markdownHandlers"], 0)


class DebugConsolePolicyTests(unittest.TestCase):
    def test_debug_console_has_no_arbitrary_eval(self):
        source = (
            Path(__file__).resolve().parents[1]
            / "ui"
            / "shared"
            / "js"
            / "debugConsole.js"
        ).read_text(encoding="utf-8")
        self.assertNotIn("eval(", source)
        self.assertNotIn("new Function(", source)


if __name__ == "__main__":
    unittest.main()

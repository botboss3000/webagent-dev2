"""Journey: a touch drag that begins over the chat footer must scroll the transcript.

Regression for the reported defect. At the bottom of a long transcript a touch
drag over the composer pill was claimed by the pill's vertical rubber band: a
non-scrollable element sits at BOTH scroll boundaries at once, so the band armed
a pull and ``preventDefault()``ed every drag. The transcript never moved and the
pill just sprang back, which the reader experiences as "scrolling snaps me back
down"; only a very large swipe (one that began above the footer) overcame it.

The journey drives a real Chromium against a faithful chat-panel DOM using the
real ``ui/shared/js/rubber-band.js`` and ``ui/chat/js/footer-gesture-forward.js``
modules, wired exactly as ``ui/chat/js/chat-ui.js`` wires them. No backend is
required, so this runs deterministically in CI.

Cases:
  S1 footer drag scrolls the transcript and does NOT spring the pill
  S2 a mid-transcript drag is left to the scroller (not claimed)
  S3 the transcript's own bottom over-scroll still pulls (control)
"""

from __future__ import annotations

import functools
import http.server
import threading
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]


class _QuietHandler(http.server.SimpleHTTPRequestHandler):
    def log_message(self, format, *args):  # noqa: A002 - stdlib signature
        return


class ChatFooterScrollJourney(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        try:
            from playwright.sync_api import sync_playwright
        except ImportError as exc:  # pragma: no cover - environment dependent
            raise unittest.SkipTest(f"Playwright unavailable: {exc}")

        handler = functools.partial(_QuietHandler, directory=str(ROOT))
        cls.server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), handler)
        cls.thread = threading.Thread(target=cls.server.serve_forever, daemon=True)
        cls.thread.start()
        cls.playwright = sync_playwright().start()
        try:
            cls.browser = cls.playwright.chromium.launch(headless=True)
        except Exception as exc:  # pragma: no cover - environment dependent
            cls.playwright.stop()
            cls.server.shutdown()
            raise unittest.SkipTest(f"Chromium unavailable: {exc}")
        cls.page = cls.browser.new_page()
        cls.base = f"http://127.0.0.1:{cls.server.server_port}"
        cls.page.goto(f"{cls.base}/ui/diagnostics.html")

    @classmethod
    def tearDownClass(cls):
        if hasattr(cls, "browser"):
            cls.browser.close()
        if hasattr(cls, "playwright"):
            cls.playwright.stop()
        if hasattr(cls, "server"):
            cls.server.shutdown()
            cls.server.server_close()

    # ── fixture ──────────────────────────────────────────────────────────────
    def _mount(self) -> dict:
        """Mount the chat-panel skeleton and wire the real modules the app uses."""
        return self.page.evaluate(
            """async base => {
              // Faithful skeleton: ONE real scroller (#chat-messages) and a footer
              // that — like the app — has no scrollable ancestor, so a gesture that
              // starts there cannot reach the transcript without forwarding.
              document.body.innerHTML = `
                <div id="chat-panel" style="height:520px;overflow:hidden;display:flex;flex-direction:column">
                  <div id="chat-messages" style="flex:1 1 auto;overflow-y:auto;overflow-x:hidden">
                    <div id="chat-messages-inner"></div>
                  </div>
                  <div id="chat-input-area">
                    <div id="chat-input-row">
                      <textarea id="chat-input" class="chat-pill-input" style="min-height:64px"></textarea>
                    </div>
                    <div id="chat-footer-row" style="overflow:hidden;height:40px"></div>
                  </div>
                </div>`;
              const inner = document.getElementById('chat-messages-inner');
              inner.innerHTML = Array.from({ length: 80 },
                (_, i) => '<div style="height:40px">Message row ' + i + '</div>').join('');

              const scroller = document.getElementById('chat-messages');
              const pill = document.getElementById('chat-input');
              scroller.scrollTop = scroller.scrollHeight;

              const rb = await import(base + '/ui/shared/js/rubber-band.js');
              // Exactly as chat-ui.js applies them.
              rb.applyRubberBand(pill, { axis: 'y', pullThreshold: 20, maxPull: 50, wheel: false });
              rb.applyRubberBand(scroller, {
                axis: 'y', pullThreshold: 50, pullThresholdBottom: 50, maxPull: 50,
                resistance: 0.5, snapDuration: 220, bottomEdgeTol: 4,
              });

              const fwd = await import(base + '/ui/chat/js/footer-gesture-forward.js');
              window.__intent = 0;
              fwd.forwardFooterGestures({
                targets: [document.getElementById('chat-input-area'),
                          document.getElementById('chat-footer-row')],
                scroller,
                markIntent: () => { window.__intent += 1; },
              });

              return {
                maxScroll: scroller.scrollHeight - scroller.clientHeight,
                pillRange: pill.scrollHeight - pill.clientHeight,
              };
            }""",
            self.base,
        )

    def _drag(self, selector: str, finger_dy: float, steps: int = 10) -> dict:
        """Drag a finger over `selector`; +finger_dy = finger moves DOWN."""
        return self.page.evaluate(
            """async ({sel, fingerDy, steps}) => {
              const target = document.querySelector(sel);
              const scroller = document.getElementById('chat-messages');
              const pill = document.getElementById('chat-input');
              scroller.scrollTop = scroller.scrollHeight;
              const before = scroller.scrollTop;
              const startIntent = window.__intent;
              const fire = (type, x, y) => {
                const ev = new Event(type, { bubbles: true, cancelable: true });
                const t = { identifier: 1, clientX: x, clientY: y, pageX: x, pageY: y, target };
                ev.touches = [t]; ev.targetTouches = [t]; ev.changedTouches = [t];
                return target.dispatchEvent(ev);
              };
              const x = 150, y0 = 300;
              fire('touchstart', x, y0);
              let prevented = false;
              const targetTransforms = [];
              for (let i = 1; i <= steps; i++) {
                const y = y0 + (fingerDy / steps) * i;
                if (!fire('touchmove', x, y)) prevented = true;
                targetTransforms.push(target.style.transform || '');
              }
              fire('touchend', x, y0 + fingerDy);
              return {
                prevented,
                delta: scroller.scrollTop - before,
                intent: window.__intent - startIntent,
                targetTranslateMax: targetTransforms.reduce((m, t) => {
                  const v = parseFloat((t.match(/translateY\\((-?[\\d.]+)px\\)/) || [])[1]);
                  return Number.isFinite(v) ? Math.max(m, Math.abs(v)) : m;
                }, 0),
              };
            }""",
            {"sel": selector, "fingerDy": finger_dy, "steps": steps},
        )

    # ── cases ────────────────────────────────────────────────────────────────
    def setUp(self):
        mounted = self._mount()
        self.assertGreater(mounted["maxScroll"], 200, "fixture transcript must be scrollable")
        self.assertEqual(mounted["pillRange"], 0, "the pill must have no scroll range of its own")

    def test_footer_drag_scrolls_the_transcript_and_does_not_spring_the_pill(self):
        result = self._drag("#chat-input", finger_dy=220)
        self.assertLess(result["delta"], -100, f"transcript did not scroll up: {result}")
        self.assertGreater(result["intent"], 0, f"footer drag was not reported as scroll intent: {result}")
        self.assertEqual(
            result["targetTranslateMax"], 0,
            f"pill rubber band claimed the drag instead of ignoring it: {result}",
        )

    def test_mid_transcript_drag_is_left_to_the_scroller(self):
        result = self._drag("#chat-messages", finger_dy=220)
        self.assertFalse(result["prevented"], f"transcript band swallowed a mid-transcript drag: {result}")

    def test_transcript_bottom_over_scroll_still_pulls(self):
        result = self._drag("#chat-messages", finger_dy=-220)
        self.assertTrue(result["prevented"], f"bottom pull-to-refresh no longer engages: {result}")
        self.assertGreater(
            result["targetTranslateMax"], 0,
            f"transcript pull produced no stretch: {result}",
        )


if __name__ == "__main__":
    unittest.main()

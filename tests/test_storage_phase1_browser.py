"""Real-browser IndexedDB integration coverage for the Phase 1 cache contract."""

from __future__ import annotations

import functools
import http.server
import threading
import unittest
import uuid
from pathlib import Path


class _QuietHandler(http.server.SimpleHTTPRequestHandler):
    def log_message(self, format, *args):
        return


class IndexedDBIntegrationTests(unittest.TestCase):
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
        cls.page.goto(f"{cls.base}/ui/chat/index.html")

    @classmethod
    def tearDownClass(cls):
        if hasattr(cls, "browser"):
            cls.browser.close()
        if hasattr(cls, "playwright"):
            cls.playwright.stop()
        if hasattr(cls, "server"):
            cls.server.shutdown()
            cls.server.server_close()

    def _scope(self, prefix: str) -> str:
        return f"{prefix}_{uuid.uuid4().hex}"

    def test_working_intro_is_first_entry_in_scrollable_activity_group(self):
        result = self.page.evaluate(
            """async ({base}) => {
              const {app} = await import(base + '/ui/shared/js/state.js');
              const stream = await import(base + '/ui/chat/js/chat-stream.js');
              const host = document.createElement('div');
              document.body.appendChild(host);
              const previous = {
                chatMessages: app.chatMessages,
                activeToolGroupBubble: app._activeToolGroupBubble,
                turnHasBubble: app._turnHasBubble,
              };
              app.chatMessages = host;
              app._activeToolGroupBubble = null;
              app._turnHasBubble = false;
              const user = document.createElement('div');
              user.className = 'chat-bubble user';
              user.dataset.msgId = 'user-intro';
              user.dataset.sessionSeq = '10';
              host.appendChild(user);

              stream.finalizeAgentStep(
                '’ll inspect the Scout path and verify it with browser tests.',
                'working-intro-1', '2026-08-27T12:00:00Z',
                'user-intro', 11, 'working_intro',
              );
              // Durable reconcile must repair a live preview that missed its
              // first streamed token instead of preserving it by entry-id.
              stream.finalizeAgentStep(
                'I’ll inspect the Scout path and verify it with browser tests.',
                'working-intro-1', '2026-08-27T12:00:00Z',
                'user-intro', 11, 'working_intro',
              );
              stream.attachToolCallsToLastBubble([
                {tool: 'read_source', args: {path: 'app/agent/run_scout.py'}, status: 'done'},
              ], null, {turnId: 'user-intro', interactionSeq: 12});

              const group = host.querySelector('.chat-bubble.agent.activity-group');
              const entries = Array.from(group.querySelectorAll(
                '.bubble-tool-calls-panel > .ca-tool-row, .bubble-tool-calls-panel > .ca-progress-row'
              )).map(row => row.classList.contains('ca-progress-row') ? 'intro' : 'tool');
              const summary = {
                bubbleCount: host.querySelectorAll('.chat-bubble.agent').length,
                entries,
                sequence: Array.from(host.children).map(el =>
                  el.classList.contains('user') ? 'user' : 'activity'
                ),
                sessionSeq: group.dataset.sessionSeq,
                introText: group.querySelector('.ca-progress-row')?.innerText || '',
              };
              host.remove();
              app.chatMessages = previous.chatMessages;
              app._activeToolGroupBubble = previous.activeToolGroupBubble;
              app._turnHasBubble = previous.turnHasBubble;
              return summary;
            }""",
            {"base": self.base},
        )
        self.assertEqual(result["bubbleCount"], 1)
        self.assertEqual(result["entries"], ["intro", "tool"])
        self.assertEqual(result["sequence"], ["user", "activity"])
        self.assertEqual(result["sessionSeq"], "11")
        self.assertIn(
            "I’ll inspect the Scout path and verify it with browser tests.",
            result["introText"],
        )


    def test_streamed_agent_message_reuses_saved_structure_without_duplication(self):
        result = self.page.evaluate(
            """async ({base}) => {
              const {app} = await import(base + '/ui/shared/js/state.js');
              const stream = await import(base + '/ui/chat/js/chat-stream.js');
              const host = document.createElement('div');
              host.style.cssText = 'display:flex;flex-direction:column';
              host.id = 'stream-message-test';
              document.body.appendChild(host);
              const previous = {
                chatMessages: app.chatMessages,
                currentSessionId: app.currentSessionId,
                activeTurnModel: app._activeTurnModel,
                activeTurnEffort: app._activeTurnEffort,
                agentTurnBubble: app._agentTurnBubble,
                activeToolGroupBubble: app._activeToolGroupBubble,
                isProcessing: app.isProcessing,
                agentBuffer: app.agentBuffer,
                turnHasBubble: app._turnHasBubble,
              };
              app.chatMessages = host;
              app.currentSessionId = 'stream-test-session';
              app._activeTurnModel = 'test-model';
              app._activeTurnEffort = 'high';

              stream.appendStreamToActiveBubble(
                'Hello', 'assistant-1', '2026-08-07T12:00:00Z'
              );
              stream.appendStreamToActiveBubble(
                ' world', 'assistant-1', '2026-08-07T12:00:00Z'
              );
              // Stream paints are intentionally coalesced so token-rate events
              // cannot force a full layout for every chunk.
              await new Promise(resolve => setTimeout(resolve, 125));

              const snapshot = () => ({
                bubbles: host.querySelectorAll('.chat-bubble.agent').length,
                sections: host.querySelectorAll(':scope > .chat-bubble.agent > .turn-section.llm-section').length,
                gutters: host.querySelectorAll(':scope > .chat-bubble.agent > .turn-gutter').length,
                text: host.querySelector('.turn-section.llm-section')?.textContent,
                model: host.querySelector('.chat-bubble.agent')?.dataset.modelLabel,
                time: host.querySelectorAll('.turn-gutter-time').length,
                buttons: host.querySelectorAll('.turn-gutter button').length,
                deletes: host.querySelectorAll('.turn-gutter .bubble-delete-btn').length,
                streaming: host.querySelectorAll('.chat-bubble.agent.streaming').length,
                turnId: host.querySelector('.chat-bubble.agent')?.dataset.turnId,
                msgId: host.querySelector('.chat-bubble.agent')?.dataset.msgId,
              });

              const live = snapshot();
              const section = host.querySelector('.turn-section.llm-section');
              const gutter = host.querySelector('.turn-gutter');
              section.click();
              const liveHiddenAfterClick = gutter.hasAttribute('hidden');
              section.click();
              const liveShownAfterSecondClick = !gutter.hasAttribute('hidden');
              stream.finalizeAgentResponse('Hello world', 'assistant-1');
              const saved = snapshot();
              section.click();
              const savedHiddenAfterClick = gutter.hasAttribute('hidden');
              section.click();
              const savedShownAfterSecondClick = !gutter.hasAttribute('hidden');
              host.remove();
              app.chatMessages = previous.chatMessages;
              app.currentSessionId = previous.currentSessionId;
              app._activeTurnModel = previous.activeTurnModel;
              app._activeTurnEffort = previous.activeTurnEffort;
              app._agentTurnBubble = previous.agentTurnBubble;
              app._activeToolGroupBubble = previous.activeToolGroupBubble;
              app.isProcessing = previous.isProcessing;
              app.agentBuffer = previous.agentBuffer;
              app._turnHasBubble = previous.turnHasBubble;
              return {
                live,
                saved,
                liveHiddenAfterClick,
                liveShownAfterSecondClick,
                savedHiddenAfterClick,
                savedShownAfterSecondClick,
              };
            }""",
            {"base": self.base},
        )
        self.assertEqual(
            result["live"],
            {
                "bubbles": 1,
                "sections": 1,
                "gutters": 1,
                "text": "Hello world",
                "model": "test-model HIGH",
                "time": 1,
                "buttons": 6,
                "deletes": 0,
                "streaming": 1,
                "turnId": "assistant-1",
                "msgId": "assistant-1",
            },
        )
        self.assertEqual(result["saved"]["bubbles"], 1)
        self.assertEqual(result["saved"]["sections"], 1)
        self.assertEqual(result["saved"]["gutters"], 1)
        self.assertEqual(result["saved"]["text"], "Hello world")
        self.assertEqual(result["saved"]["model"], "test-model HIGH")
        self.assertEqual(result["saved"]["time"], 1)
        self.assertEqual(result["saved"]["buttons"], 7)
        self.assertEqual(result["saved"]["deletes"], 1)
        self.assertEqual(result["saved"]["streaming"], 0)
        self.assertFalse(result["liveHiddenAfterClick"], result)
        self.assertTrue(result["liveShownAfterSecondClick"])
        self.assertFalse(result["savedHiddenAfterClick"])
        self.assertTrue(result["savedShownAfterSecondClick"])

    def test_virtual_scroll_evicts_bubbles_that_leave_viewport(self):
        result = self.page.evaluate(
            """async ({base}) => {
              const {app} = await import(base + '/ui/shared/js/state.js');
              const cache = await import(base + '/ui/chat/js/chat-message-cache.js');
              const virtual = await import(base + '/ui/chat/js/chat-virtual-scroll.js');
              await import(base + '/ui/chat/js/chat-bubble.js');

              const scroller = document.createElement('div');
              scroller.style.cssText = 'height:240px;overflow:auto;position:relative';
              const host = document.createElement('div');
              scroller.appendChild(host);
              document.body.appendChild(scroller);
              const previous = {
                chatMessages: app.chatMessages,
                chatScroller: app._chatScroller,
                currentSessionId: app.currentSessionId,
              };
              app.chatMessages = host;
              app._chatScroller = scroller;
              app.currentSessionId = 'virtual-test-session';

              const messages = [];
              for (let i = 0; i < 100; i++) {
                const id = `virtual-${i}`;
                messages.push({id, role: 'assistant', content: `Message ${i}`, session_seq: i});
                app._agentTurnBubble = null;
                const bubble = app.addChatBubble('agent', `Message ${i}`, null, null, null, id);
                bubble.style.flex = '0 0 80px';
                bubble.style.height = '80px';
                bubble.style.minHeight = '80px';
              }
              cache._messageCache.set(app.currentSessionId, {
                messages, light: true, loadedAt: Date.now(),
              });

              virtual._installVirtualScroll();
              await new Promise(resolve => requestAnimationFrame(resolve));
              const initial = {
                real: host.querySelectorAll('.chat-bubble').length,
                placeholders: host.querySelectorAll('.chat-bubble-placeholder').length,
              };
              scroller.scrollTop = scroller.scrollHeight;
              scroller.dispatchEvent(new Event('scroll'));
              await new Promise(resolve => requestAnimationFrame(() => requestAnimationFrame(resolve)));
              const after = {
                real: host.querySelectorAll('.chat-bubble').length,
                placeholders: host.querySelectorAll('.chat-bubble-placeholder').length,
                lastVisible: host.lastElementChild?.textContent,
              };

              virtual._teardownVirtualScroll();
              cache._messageCache.delete(app.currentSessionId);
              scroller.remove();
              app.chatMessages = previous.chatMessages;
              app._chatScroller = previous.chatScroller;
              app.currentSessionId = previous.currentSessionId;
              return {initial, after};
            }""",
            {"base": self.base},
        )
        self.assertLess(result["initial"]["real"], 20)
        self.assertGreater(result["initial"]["placeholders"], 80)
        self.assertLess(result["after"]["real"], 20)
        self.assertGreater(result["after"]["placeholders"], 80)
        self.assertIn("Message 99", result["after"]["lastVisible"])

    def test_streamed_tool_call_batches_share_one_summary_line(self):
        result = self.page.evaluate(
            """async ({base}) => {
              const {app} = await import(base + '/ui/shared/js/state.js');
              const stream = await import(base + '/ui/chat/js/chat-stream.js');
              const bubbles = await import(base + '/ui/chat/js/chat-bubble.js');
              const host = document.createElement('div');
              document.body.appendChild(host);
              const previous = {
                chatMessages: app.chatMessages,
                currentSessionId: app.currentSessionId,
                agentTurnBubble: app._agentTurnBubble,
                activeToolGroupBubble: app._activeToolGroupBubble,
                turnHasBubble: app._turnHasBubble,
                isProcessing: app.isProcessing,
              };
              app.chatMessages = host;
              app.currentSessionId = 'stream-tool-summary-test';
              app._agentTurnBubble = null;
              app._activeToolGroupBubble = null;
              app._turnHasBubble = false;

              stream.appendStreamToActiveBubble('First step', 'assistant-tool-1');
              stream.attachToolCallsToLastBubble([
                {tool: 'search', args: {}, status: 'done'},
                {tool: 'read', args: {}, status: 'done'},
              ]);
              stream.finalizeAgentStep('First step', 'assistant-tool-1');

              // A later streamed inference step merges into the same agent bubble.
              bubbles.addChatBubble('agent', 'Second step', 'streaming', undefined, 'assistant-tool-2');
              stream.attachToolCallsToLastBubble([
                {tool: 'write', args: {}, status: 'done'},
              ]);
              stream.finalizeAgentStep('Second step', 'assistant-tool-2');

              const summary = {
                bubbles: host.querySelectorAll('.chat-bubble.agent').length,
                toolSections: host.querySelectorAll('.turn-section.tool-section').length,
                containers: host.querySelectorAll('.bubble-tool-calls').length,
                rows: host.querySelectorAll('.bubble-tool-calls-panel > .ca-tool-row').length,
                heading: host.querySelector('.bubble-tool-calls-head')?.textContent.trim(),
              };
              host.remove();
              app.chatMessages = previous.chatMessages;
              app.currentSessionId = previous.currentSessionId;
              app._agentTurnBubble = previous.agentTurnBubble;
              app._activeToolGroupBubble = previous.activeToolGroupBubble;
              app._turnHasBubble = previous.turnHasBubble;
              app.isProcessing = previous.isProcessing;
              return summary;
            }""",
            {"base": self.base},
        )
        self.assertEqual(result["bubbles"], 1)
        self.assertEqual(result["toolSections"], 1)
        self.assertEqual(result["containers"], 1)
        self.assertEqual(result["rows"], 5)  # 3 tools + 2 bundled progress messages
        self.assertIn("3 tool calls", result["heading"])

    def test_activity_group_interleaves_progress_and_tools_by_turn(self):
        result = self.page.evaluate(
            """async ({base}) => {
              const {app} = await import(base + '/ui/shared/js/state.js');
              const bubbles = await import(base + '/ui/chat/js/chat-bubble.js');
              const stream = await import(base + '/ui/chat/js/chat-stream.js');
              const host = document.createElement('div');
              document.body.appendChild(host);
              const previous = {
                chatMessages: app.chatMessages,
                activeToolGroupBubble: app._activeToolGroupBubble,
              };
              app.chatMessages = host;
              app._activeToolGroupBubble = null;

              const before = bubbles.addChatBubble(
                'user', 'before', undefined, undefined, undefined, 'before',
                '2026-08-07T10:00:00Z',
              );
              bubbles._setBubbleSessionSeq(before, 10);
              const resumed = bubbles.addChatBubble(
                'user', 'resumed', undefined, undefined, undefined, 'resumed',
                '2026-08-07T12:00:00Z',
              );
              bubbles._setBubbleSessionSeq(resumed, 40);

              stream.attachActivityEntries([
                {tool: 'one', toolCallId: 'tc-1', args: {}, status: 'done'},
                {tool: 'two', toolCallId: 'tc-2', args: {}, status: 'done'},
                {kind: 'progress', id: 'progress-1', content: 'First update'},
                {tool: 'three', toolCallId: 'tc-3', args: {}, status: 'done'},
                {kind: 'progress', id: 'progress-2', content: 'Second update'},
              ], null, {
                id: 'progress-1',
                interactionSeq: 20,
                turnId: 'user-turn',
                createdAt: '2026-08-07T11:00:00Z',
              }, 'user-turn');

              const group = host.querySelector('.activity-group');
              const rowKinds = Array.from(
                group.querySelectorAll('.bubble-tool-calls-panel > .ca-tool-row'),
              ).map(row => row.classList.contains('ca-progress-row')
                ? 'progress' : row.querySelector('.ca-tool-name')?.textContent);
              const order = Array.from(host.children).map(el =>
                el.classList.contains('activity-group') ? 'activity'
                  : el.querySelector('.bubble-body')?.textContent,
              );
              const heading = group.querySelector('.bubble-tool-calls-head')?.textContent.trim();

              host.remove();
              app.chatMessages = previous.chatMessages;
              app._activeToolGroupBubble = previous.activeToolGroupBubble;
              return {rowKinds, order, heading};
            }""",
            {"base": self.base},
        )
        self.assertEqual(
            result["rowKinds"],
            ["one", "two", "progress", "three", "progress"],
        )
        self.assertEqual(result["order"], ["before", "activity", "resumed"])
        self.assertIn("3 tool calls", result["heading"])
        self.assertIn("2 updates", result["heading"])

    def test_compaction_is_counted_in_activity_group(self):
        result = self.page.evaluate(
            """async ({base}) => {
              const {app} = await import(base + '/ui/shared/js/state.js');
              const stream = await import(base + '/ui/chat/js/chat-stream.js');
              const host = document.createElement('div');
              document.body.appendChild(host);
              const previous = {
                chatMessages: app.chatMessages,
                activeToolGroupBubble: app._activeToolGroupBubble,
              };
              app.chatMessages = host;
              app._activeToolGroupBubble = null;

              const group = stream.attachActivityEntries([
                {tool: 'search', toolCallId: 'tool-1', args: {}, status: 'done'},
                {kind: 'progress', id: 'update-1', content: 'Working'},
                {kind: 'response', id: 'response-1', content: 'Done'},
                {kind: 'system', notice: 'mode', id: 'mode-1',
                 label: 'Ask mode enabled', content: '', mode: 'plan', from: 'ask'},
                {kind: 'system', notice: 'compaction', id: 'compact-1',
                 label: 'Compaction details',
                 content: 'Context: 76,243 \u2192 62,403 tokens.'},
              ], null, {
                id: 'compact-1', turnId: 'user-turn', interactionSeq: 20,
              }, 'user-turn');
              stream.attachActivityEntries([
                {kind: 'system', notice: 'mode', id: 'mode-2',
                 label: 'Auto mode enabled', content: '', mode: 'auto', from: 'plan'},
              ], group, {id: 'mode-2', turnId: 'user-turn', interactionSeq: 21}, 'user-turn');
              const heading = group.querySelector('.bubble-tool-calls-head')?.textContent.trim();
              const row = group.querySelector('.ca-compaction-row');
              const detail = row?.textContent.trim();
              const modeRows = group.querySelectorAll('.ca-mode-row');
              const modeRowCount = modeRows.length;
              const visibleMode = modeRows[0]?.textContent.trim();
              // Cycling back to the mode the group began in cancels the notice:
              // ask \u2192 plan \u2192 auto \u2192 ask leaves nothing behind.
              stream.attachActivityEntries([
                {kind: 'system', notice: 'mode', id: 'mode-3',
                 label: 'Ask mode enabled', content: '', mode: 'ask', from: 'auto'},
              ], group, {id: 'mode-3', turnId: 'user-turn', interactionSeq: 22}, 'user-turn');
              const cancelledHeading = group.querySelector('.bubble-tool-calls-head')?.textContent.trim();
              const cancelledRows = group.querySelectorAll('.ca-mode-row').length;
              host.remove();
              app.chatMessages = previous.chatMessages;
              app._activeToolGroupBubble = previous.activeToolGroupBubble;
              return {heading, detail, modeRows: modeRowCount, visibleMode,
                      cancelledHeading, cancelledRows};
            }""",
            {"base": self.base},
        )
        self.assertIn("1 tool", result["heading"])
        self.assertIn("2 messages", result["heading"])
        self.assertIn("Mode change", result["heading"])
        self.assertNotIn("mode enabled", result["heading"])
        self.assertEqual(1, result["modeRows"])
        # Group line = generic; the expanded row body = the full sentence.
        self.assertEqual("Auto mode enabled", result["visibleMode"])
        self.assertIn("1 compaction", result["heading"])
        self.assertIn(
            f"76,243 {chr(0x2192)} 62,403 tokens", result["detail"],
        )
        # ask → plan → auto → ask cancels the group's notice outright.
        self.assertEqual(0, result["cancelledRows"])
        self.assertNotIn("Mode change", result["cancelledHeading"])
        self.assertIn("1 tool", result["cancelledHeading"])

    def test_reloaded_group_nets_mode_flips_by_durable_metadata(self):
        """Cold-load projection through the BUILD path (not the live append
        path): durable mode rows carry mode/from in their metadata, so a run
        that cycled back to the mode it began in renders with no notice at all,
        while a run that ends elsewhere renders exactly ONE generic row."""
        result = self.page.evaluate(
            """async ({base}) => {
              const {app} = await import(base + '/ui/shared/js/state.js');
              const stream = await import(base + '/ui/chat/js/chat-stream.js');
              const previous = {chatMessages: app.chatMessages,
                                activeToolGroupBubble: app._activeToolGroupBubble};
              const host = document.createElement('div');
              document.body.appendChild(host);
              app.chatMessages = host;
              app._activeToolGroupBubble = null;
              const modeSentence = {ask: 'Ask mode enabled', plan: 'Plan mode enabled',
                                    auto: 'Auto mode enabled'};
              const modeRow = (id, mode, from) => ({
                kind: 'system', notice: 'mode', id,
                label: modeSentence[mode] || 'Mode change',
                content: '', mode, from,
                createdAt: '2026-01-01T00:00:00Z', interactionSeq: 5,
              });
              // ask → plan → auto → ask: nets back to the group's starting mode.
              const cancelled = stream.attachActivityEntries([
                modeRow('m1', 'plan', 'ask'),
                modeRow('m2', 'auto', 'plan'),
                modeRow('m3', 'ask', 'auto'),
              ], null, {id: 'm3', turnId: 'turn-a', interactionSeq: 5}, 'turn-a');
              const cancelledRows = cancelled.querySelectorAll('.ca-mode-row').length;
              const cancelledHead = cancelled.querySelector('.bubble-tool-calls-head')?.textContent.trim();
              // ask → plan → auto: ends somewhere else, so one notice survives.
              const changed = stream.attachActivityEntries([
                modeRow('m4', 'plan', 'ask'),
                modeRow('m5', 'auto', 'plan'),
              ], null, {id: 'm5', turnId: 'turn-b', interactionSeq: 6}, 'turn-b');
              const changedRows = changed.querySelectorAll('.ca-mode-row').length;
              const changedHead = changed.querySelector('.bubble-tool-calls-head')?.textContent.trim();
              const changedRowText = changed.querySelector('.ca-mode-row')?.textContent.trim();
              // Legacy rows carry no from/mode: show the notice, never guess.
              const legacy = stream.attachActivityEntries([
                {kind: 'system', notice: 'mode', id: 'm6',
                 label: 'Ask mode enabled', content: ''},
              ], null, {id: 'm6', turnId: 'turn-c', interactionSeq: 7}, 'turn-c');
              const legacyRows = legacy.querySelectorAll('.ca-mode-row').length;
              const legacyText = legacy.querySelector('.ca-mode-row')?.textContent.trim();
              host.remove();
              app.chatMessages = previous.chatMessages;
              app._activeToolGroupBubble = previous.activeToolGroupBubble;
              return {cancelledRows, cancelledHead, changedRows, changedHead,
                      changedRowText, legacyRows, legacyText};
            }""",
            {"base": self.base},
        )
        self.assertEqual(0, result["cancelledRows"])
        self.assertNotIn("Mode change", result["cancelledHead"] or "")
        self.assertEqual(1, result["changedRows"])
        self.assertIn("Mode change", result["changedHead"])
        # The surviving row's BODY is the durable full sentence, while the
        # collapsed group line stays generic...
        self.assertEqual("Auto mode enabled", result["changedRowText"])
        self.assertNotIn("mode enabled", result["changedHead"].lower())
        # ...and a legacy row with no mode/from metadata keeps its stored sentence.
        self.assertEqual(1, result["legacyRows"])
        self.assertEqual("Ask mode enabled", result["legacyText"])

    def test_mode_pill_cycling_shows_one_generic_notice_and_cancels_on_return(self):
        """The user-visible contract, driven through the REAL footer pill on the
        real chat shell: the activity group says "Mode change" and never names
        the mode ("Ask/Plan/Auto mode enabled" is gone), cycling through the
        modes keeps ONE notice rather than one per click, and clicking all the
        way back to the mode the group began in cancels the notice entirely."""
        page = self.browser.new_page()
        try:
            errors: list[str] = []
            page.on("pageerror", lambda e: errors.append(str(e)))
            page.goto(
                f"{self.base}/ui/chat/chat-side-panel.html",
                wait_until="domcontentloaded",
            )
            result = page.evaluate(
                """async ({base}) => {
                  const {app} = await import(base + '/ui/shared/js/state.js');
                  const ui = await import(base + '/ui/chat/js/chat-ui.js');
                  // Safe fixture: the footer controls initChat() wires.
                  for (const id of ['chat-messages','chat-input','chat-send',
                                    'chat-stop-btn','chat-continue-btn']) {
                    if (!document.getElementById(id)) {
                      const el = document.createElement('div');
                      el.id = id;
                      document.body.appendChild(el);
                    }
                  }
                  app.chatSend = document.getElementById('chat-send');
                  app.chatInput = document.getElementById('chat-input');
                  app.chatMessages = document.getElementById('chat-messages');
                  ui.initChat();
                  // A real transcript host for the notice to render into.
                  const host = document.createElement('div');
                  document.body.appendChild(host);
                  app.chatMessages = host;
                  app.currentSessionId = 'journey-session';
                  const pill = document.getElementById('chat-mode-btn');
                  const snap = () => {
                    const rows = host.querySelectorAll('.ca-mode-row');
                    return {
                      pill: pill.textContent.trim(),
                      mode: app.executionMode,
                      rows: rows.length,
                      rowText: rows[0] ? rows[0].textContent.trim() : '',
                      head: host.querySelector('.bubble-tool-calls-head')?.textContent.trim() || '',
                    };
                  };
                  const startLabel = pill.textContent.trim();
                  const before = snap();
                  const trail = [];
                  // Click until the pill cycles back to where it started.
                  for (let i = 0; i < 4; i++) {
                    pill.click();
                    const step = snap();
                    trail.push(step);
                    if (step.pill === startLabel) break;
                  }
                  host.remove();
                  return {before, trail};
                }""",
                {"base": self.base},
            )
            self.assertEqual([], errors, f"page errors: {errors}")
            trail = result["trail"]
            self.assertGreaterEqual(len(trail), 2, "pill did not cycle back to its start")
            # Every intermediate step: ONE notice, generic wording, never the mode.
            for step in trail[:-1]:
                self.assertEqual(1, step["rows"], step)
                # Expanded row body: the FULL sentence for the mode now on.
                self.assertEqual(f"{step['pill']} mode enabled", step["rowText"], step)
                # Collapsed group line: generic, never the mode name.
                self.assertIn("Mode change", step["head"], step)
                self.assertNotIn("mode enabled", step["head"].lower(), step)
            # Cycling back to the group's starting mode cancels the notice.
            final = trail[-1]
            self.assertEqual(result["before"]["pill"], final["pill"])
            self.assertEqual(0, final["rows"], final)
            self.assertNotIn("Mode change", final["head"], final)
        finally:
            page.close()

    def test_model_switch_notice_is_one_neutral_row_inside_the_group_and_revert_cancels(self):
        """The user-visible contract for the model twin, rendered through the real
        notice path with the exact anchor the WS/picker callers build: the row
        says "Model change" — never a slot or a model name — and renders as ONE
        NORMAL activity row inside the group's panel (no ⚠ heading, no warning
        tint: same computed surface as its sibling row), and a revert back to the
        model state the group began on cancels it."""
        result = self.page.evaluate(
            """async ({base}) => {
              const {app} = await import(base + '/ui/shared/js/state.js');
              await import(base + '/ui/chat/js/chat-bubble.js');
              const stream = await import(base + '/ui/chat/js/chat-stream.js');
              const host = document.createElement('div');
              document.body.appendChild(host);
              const previous = {
                chatMessages: app.chatMessages,
                activeToolGroupBubble: app._activeToolGroupBubble,
              };
              app.chatMessages = host;
              app.currentSessionId = 'model-journey';
              app._activeToolGroupBubble = null;
              // Group with one ordinary tool row AND an ordinary system row, so
              // the notice has same-family siblings to compare surfaces against.
              stream.attachActivityEntries([
                {tool: 'search', toolCallId: 'tool-1', args: {}, status: 'done'},
                {kind: 'system', label: 'Stopped', content: ''},
              ], null, {id: 'tool-1', turnId: 'user-turn', interactionSeq: 5}, 'user-turn');
              const upgrade = (turnId, tool) => stream.notifyModelSwitch(
                'Switched to the premium model',
                {initiator: 'agent', tool: tool || 'set_model', model: 'claude-opus-x',
                 turnId, from: 'default', state: 'override'});
              const revert = (turnId) => stream.notifyModelSwitch(
                'Reverted to standard model',
                {initiator: 'agent', tool: 'reset',
                 turnId, from: 'override', state: 'default'});
              const snap = () => {
                const row = host.querySelector('.ca-model-row');
                const panel = row && row.closest('.bubble-tool-calls-panel');
                const control = host.querySelector('.ca-system-row:not(.ca-model-row)');
                const cs = row ? getComputedStyle(row) : null;
                const cc = control ? getComputedStyle(control) : null;
                return {
                  rows: host.querySelectorAll('.ca-model-row').length,
                  rowText: row ? row.textContent.trim() : '',
                  hasHeading: !!(row && row.querySelector('.ca-activity-entry-head')),
                  head: host.querySelector('.bubble-tool-calls-head')?.textContent.trim() || '',
                  insideGroupPanel: !!(panel && row && panel.contains(row)),
                  bg: cs ? cs.backgroundColor : '',
                  border: cs ? cs.borderTopColor : '',
                  ctrlBg: cc ? cc.backgroundColor : '',
                  ctrlBorder: cc ? cc.borderTopColor : '',
                };
              };
              const before = snap();
              upgrade('t1');
              const afterUpgrade = snap();
              // Positive control: the "not orange" check must have teeth — apply
              // the OLD warning tint and prove the computed surface really moves.
              const canary = document.createElement('style');
              canary.textContent = '.ca-model-row{background:rgba(255,193,7,.08);'
                + 'border-color:rgba(255,193,7,.25);}';
              document.head.appendChild(canary);
              const canarySnap = snap();
              canary.remove();
              upgrade('t2', 'use_premium_model');
              const afterSecondUpgrade = snap();
              revert('t3');
              const afterRevert = snap();
              upgrade('t4');
              const afterReUpgrade = snap();
              host.remove();
              app.chatMessages = previous.chatMessages;
              app._activeToolGroupBubble = previous.activeToolGroupBubble;
              return {before, afterUpgrade, canarySnap, afterSecondUpgrade,
                      afterRevert, afterReUpgrade};
            }""",
            {"base": self.base},
        )
        self.assertEqual(0, result["before"]["rows"], result["before"])
        first = result["afterUpgrade"]
        self.assertEqual(1, first["rows"], first)
        # Expanded row body: the full sentence the mini transcript shows; the
        # collapsed group line: generic.
        self.assertEqual("Custom model enabled", first["rowText"], first)
        self.assertIn("Model change", first["head"], first)
        self.assertNotIn("model enabled", first["head"].lower(), first)
        self.assertNotIn("Switched to", first["head"], first)
        self.assertNotIn("premium", first["head"].lower(), first)
        # Not a special orange bubble: no ⚠ heading repeating the text, and the
        # same computed surface (background + border) as an ordinary system row.
        self.assertFalse(first["hasHeading"], first)
        self.assertEqual(first["ctrlBg"], first["bg"], first)
        self.assertEqual(first["ctrlBorder"], first["border"], first)
        self.assertNotIn("255, 193, 7", first["bg"], first)
        self.assertNotIn("255, 193, 7", first["border"], first)
        # ...and the colour assertion is not vacuous: re-applying the old warning
        # tint DOES move the computed surface.
        canary = result["canarySnap"]
        self.assertIn("255, 193, 7", canary["bg"], canary)
        self.assertNotEqual(canary["bg"], first["bg"], canary)
        # Structurally INSIDE the activity group's panel, not beside the bubbles.
        self.assertTrue(first["insideGroupPanel"], first)
        # A second switch supersedes rather than stacking rows.
        second = result["afterSecondUpgrade"]
        self.assertEqual(1, second["rows"], second)
        self.assertEqual("Premium model enabled", second["rowText"], second)
        # Reverting to the state the group began on cancels the notice outright.
        reverted = result["afterRevert"]
        self.assertEqual(0, reverted["rows"], reverted)
        self.assertNotIn("Model change", reverted["head"], reverted)
        # ...and a later switch announces itself again.
        self.assertEqual(1, result["afterReUpgrade"]["rows"], result["afterReUpgrade"])

    def test_cold_loaded_model_rows_net_inside_their_activity_group(self):
        """Cold-load projection for the model twin: durable rows that netted back
        out render as NO notice, and a run that ended on a different model renders
        ONE generic row inside the group rather than a standalone tinted bubble."""
        result = self.page.evaluate(
            """async ({base}) => {
              const {app} = await import(base + '/ui/shared/js/state.js');
              await import(base + '/ui/chat/js/chat-bubble.js');
              const stream = await import(base + '/ui/chat/js/chat-stream.js');
              const host = document.createElement('div');
              document.body.appendChild(host);
              const previous = {
                chatMessages: app.chatMessages,
                activeToolGroupBubble: app._activeToolGroupBubble,
              };
              app.chatMessages = host;
              app._activeToolGroupBubble = null;
              const durable = (id, tool, from, to) => ({
                kind: 'system', notice: 'model', id,
                label: tool === 'reset' ? 'Standard model enabled' : 'Custom model enabled',
                content: '', model: to, from,
              });
              // Run that upgraded and then reverted before it ended.
              stream.attachActivityEntries([
                {tool: 'search', toolCallId: 'tool-a', args: {}, status: 'done'},
                durable('m1', 'set_model', 'default', 'override'),
                durable('m2', 'reset', 'override', 'default'),
              ], null, {id: 'tool-a', turnId: 'turn-a', interactionSeq: 11}, 'turn-a');
              const cancelledRows = host.querySelectorAll('.ca-model-row').length;
              const cancelledHead = host.querySelector('.bubble-tool-calls-head')?.textContent.trim() || '';
              // Run that ended on an upgraded model.
              stream.attachActivityEntries([
                {tool: 'search', toolCallId: 'tool-b', args: {}, status: 'done'},
                durable('m3', 'set_model', 'default', 'override'),
              ], null, {id: 'tool-b', turnId: 'turn-b', interactionSeq: 12}, 'turn-b');
              const keptRows = host.querySelectorAll('.ca-model-row');
              const kept = keptRows[0];
              const keptPanel = kept && kept.closest('.bubble-tool-calls-panel');
              const result = {
                cancelledRows,
                cancelledHead,
                keptRows: keptRows.length,
                keptText: kept ? kept.textContent.trim() : '',
                keptInsideGroup: !!(keptPanel && kept && keptPanel.contains(kept)),
                strayBubbles: host.querySelectorAll('.chat-bubble.info.system-model').length,
              };
              host.remove();
              return result;
            }""",
            {"base": self.base},
        )
        self.assertEqual(0, result["cancelledRows"], result)
        self.assertNotIn("Model change", result["cancelledHead"], result)
        self.assertEqual(1, result["keptRows"], result)
        self.assertEqual("Custom model enabled", result["keptText"], result)
        self.assertTrue(result["keptInsideGroup"], result)
        # Folded into the group, not painted as a standalone system bubble.
        self.assertEqual(0, result["strayBubbles"], result)

    def test_model_notice_identity_netting_and_pre_send_reorder(self):
        """Two regressions, one journey. (1) A model notice MUST appear for a switch
        between two DIFFERENT non-default models — keying the netting on a coarse
        default/override state made that read as "no change" and cancelled it.
        (2) A notice raised BEFORE the send is treated as applied AFTER it: the
        activity group ends up behind the prompt with the notice as its first row,
        even while the prompt still carries its provisional id."""
        result = self.page.evaluate(
            """async ({base}) => {
              const {app} = await import(base + '/ui/shared/js/state.js');
              const cb = await import(base + '/ui/chat/js/chat-bubble.js');
              const stream = await import(base + '/ui/chat/js/chat-stream.js');
              const host = document.createElement('div');
              document.body.appendChild(host);
              const previous = {chatMessages: app.chatMessages, active: app._activeToolGroupBubble};
              app.chatMessages = host;
              app.currentSessionId = 'journey';
              app._activeToolGroupBubble = null;
              const rows = () => Array.from(host.querySelectorAll('.ca-model-row'))
                .map(r => r.textContent.trim());
              const settle = () => new Promise(resolve => setTimeout(resolve, 40));
              // (1) identity netting — settle between steps so deferred row/head
              // work cannot leak across the reads.
              const n = (state, from) => stream.notifyModelSwitch(
                'prose', {initiator: 'user', state, from, slot: state});
              n('role:premium', 'default');
              await settle();
              const afterSwitch = rows();
              n('custom:2', 'role:premium');
              await settle();
              const afterSecond = rows();
              n('default', 'custom:2');
              await settle();
              const afterRoundTrip = rows();
              // (2) pre-send placement, prompt id deliberately misaligned
              host.innerHTML = '';
              app._activeToolGroupBubble = null;
              stream.notifyModelSwitch('prose', {initiator: 'user', state: 'role:premium',
                from: 'default', slot: 'role:premium'});
              const userBubble = cb.addChatBubble('user', 'my message', undefined, undefined,
                undefined, 'msg-1', new Date().toISOString());
              app.adoptPreSendNoticeGroup(userBubble);
              stream.attachActivityEntries(
                [{tool: 'search', toolCallId: 'tool-1', args: {}, status: 'done'}], null,
                {id: 'tool-1', turnId: 'turn-1', interactionSeq: 3}, 'turn-1');
              await settle();
              const group = host.querySelector('.chat-bubble.activity-group');
              const msg = host.querySelector('.chat-bubble.user');
              const groupRows = Array.from(group.querySelectorAll('.ca-tool-row'));
              const notice = host.querySelector('.ca-model-row');
              const panel = notice && notice.closest('.bubble-tool-calls-panel');
              const inside = !!(panel && group && panel.contains(notice) && group.contains(notice)
                && !notice.parentElement.classList.contains('chat-bubble'));
              const after = !!(msg && group
                && (msg.compareDocumentPosition(group) & Node.DOCUMENT_POSITION_FOLLOWING)
                && msg.nextElementSibling === group);
              const out = {
                afterSwitch, afterSecond, afterRoundTrip,
                order: Array.from(host.children).map(c => (c.classList.contains('user') ? 'user' : 'group')),
                groupCount: host.querySelectorAll(':scope > .chat-bubble.activity-group').length,
                groupAfterMessage: msg.nextElementSibling === group,
                noticeIsFirst: !!(groupRows[0]
                  && groupRows[0].classList.contains('ca-model-row')),
                body: notice ? notice.textContent.trim() : '',
                head: group.querySelector('.bubble-tool-calls-head').textContent.trim(),
                layout: {webagent_layout_assertion: true, relation: 'inside',
                         anchor: 'activity group', passed: !!(inside && after)},
              };
              host.remove();
              app.chatMessages = previous.chatMessages;
              app._activeToolGroupBubble = previous.active;
              return out;
            }""",
            {"base": self.base},
        )
        # (1) the switch between two non-default models still announces itself
        self.assertEqual(["Premium model enabled"], result["afterSwitch"], result)
        self.assertEqual(["Custom 2 model enabled"], result["afterSecond"], result)
        self.assertEqual([], result["afterRoundTrip"], result)
        # (2) reordered behind the message, notice first, inside the group
        self.assertEqual(["user", "group"], result["order"], result)
        self.assertEqual(1, result["groupCount"], result)
        self.assertTrue(result["groupAfterMessage"], result)
        self.assertTrue(result["noticeIsFirst"], result)
        self.assertEqual("Premium model enabled", result["body"], result)
        self.assertIn("Model change", result["head"], result)
        self.assertNotIn("model enabled", result["head"].lower(), result)
        self.assertEqual(
            {"webagent_layout_assertion": True, "relation": "inside",
             "anchor": "activity group", "passed": True},
            result["layout"], result,
        )

    def test_reconcile_falls_back_to_transient_when_worker_event_is_missed(self):
        result = self.page.evaluate(
            """async ({base}) => {
              const {app} = await import(base + '/ui/shared/js/state.js');
              const bubbles = await import(base + '/ui/chat/js/chat-bubble.js');
              await import(base + '/ui/chat/js/chat-stream.js');
              await import(base + '/ui/chat/js/session-load.js');
              const host = document.createElement('div');
              document.body.appendChild(host);
              const previous = {
                chatMessages: app.chatMessages,
                currentSessionId: app.currentSessionId,
                addChatBubble: app.addChatBubble,
                setContextTokens: app.setContextTokens,
              };
              const originalTimeout = window.setTimeout;
              let context = null;
              app.chatMessages = host;
              app.currentSessionId = 'session';
              app.addChatBubble = bubbles.addChatBubble;
              app.setContextTokens = value => { context = value; };
              window.setTimeout = () => 1;
              const row = {
                id: 'compact-reconcile', role: 'system',
                source: 'system:compaction',
                content: 'Context: 76,243 to 62,403 tokens.',
                metadata: JSON.stringify({compaction: {
                  version: 1, status: 'success', trigger: 'manual',
                  tokens_before: 76243, tokens_after: 62403,
                  tokens_saved: 13840,
                }}),
              };
              app._appendMessagesToTranscript([row], true, true);
              app._appendMessagesToTranscript([row], true, true);
              const snapshot = {
                transient: host.querySelectorAll('.system-compaction-transient').length,
                transientText: host.querySelector(
                  '.system-compaction-transient',
                )?.textContent.trim(),
                durable: host.querySelectorAll(
                  '.ca-compaction-row[data-activity-entry-id="compact-reconcile"]',
                ).length,
                context,
              };
              window.setTimeout = originalTimeout;
              host.remove();
              app.chatMessages = previous.chatMessages;
              app.currentSessionId = previous.currentSessionId;
              app.addChatBubble = previous.addChatBubble;
              app.setContextTokens = previous.setContextTokens;
              return snapshot;
            }""",
            {"base": self.base},
        )
        self.assertEqual(result["transient"], 1)
        self.assertEqual(
            result["transientText"], "Compacted history 76.2k to 62.4k tokens",
        )
        self.assertEqual(result["durable"], 1)
        self.assertIsNone(result["context"])

    def test_live_compaction_and_reconcile_share_one_durable_row(self):
        result = self.page.evaluate(
            """async ({base}) => {
              const {app} = await import(base + '/ui/shared/js/state.js');
              const bubbles = await import(base + '/ui/chat/js/chat-bubble.js');
              const stream = await import(base + '/ui/chat/js/chat-stream.js');
              const session = await import(base + '/ui/chat/js/session-load.js');
              const host = document.createElement('div');
              document.body.appendChild(host);
              const previous = {
                chatMessages: app.chatMessages,
                currentSessionId: app.currentSessionId,
                addChatBubble: app.addChatBubble,
              };
              const originalTimeout = window.setTimeout;
              app.chatMessages = host;
              app.currentSessionId = 'session';
              app.addChatBubble = bubbles.addChatBubble;
              window.setTimeout = () => 1;
              const detail = '**Compaction details** Context agreed.';
              stream.notifyCompactionCompleted({
                type: 'compaction_completed', status: 'success',
                session_id: 'session', interaction_id: 'compact-race',
                tokens_before: 76243, tokens_after: 62403, detail,
              });
              session._emitRealBubble({
                id: 'compact-race', role: 'system',
                source: 'system:compaction', content: detail,
                metadata: JSON.stringify({compaction: {
                  status: 'success', tokens_after: 62403,
                }}),
              });
              const count = host.querySelectorAll(
                '.ca-compaction-row[data-activity-entry-id="compact-race"]',
              ).length;
              window.setTimeout = originalTimeout;
              host.remove();
              app.chatMessages = previous.chatMessages;
              app.currentSessionId = previous.currentSessionId;
              app.addChatBubble = previous.addChatBubble;
              return count;
            }""",
            {"base": self.base},
        )
        self.assertEqual(result, 1)

    def test_cold_load_folds_compaction_detail_without_transient(self):
        result = self.page.evaluate(
            """async ({base}) => {
              const session = await import(base + '/ui/chat/js/session-load.js');
              const messages = [
                {id: 'user-1', role: 'user', content: 'work', session_seq: 1},
                {id: 'compact-1', role: 'system',
                 source: 'system:compaction',
                 content: '**Compaction details** Context: 76,243 to 62,403 tokens.',
                 metadata: JSON.stringify({compaction: {
                   version: 1, status: 'success', trigger: 'manual',
                   owner_turn_id: 'user-1', tokens_before: 76243,
                   tokens_after: 62403, tokens_saved: 13840,
                 }}),
                 turn_id: 'user-1', session_seq: 2},
              ];
              const rows = session._buildPhasedRenderables(
                messages, new Map(), false, null,
              );
              const activity = rows.find(row => row && row._activityGroup);
              return {
                standalone: rows.some(row => row && row.id === 'compact-1'),
                entries: activity?.entries || [],
              };
            }""",
            {"base": self.base},
        )
        self.assertFalse(result["standalone"])
        self.assertEqual(len(result["entries"]), 1)
        self.assertEqual(result["entries"][0]["notice"], "compaction")
        self.assertIn("Compaction details", result["entries"][0]["content"])


    def test_persisted_substantive_progress_rows_keep_tool_calls(self):
        result = self.page.evaluate(
            """async ({base}) => {
              const {app} = await import(base + '/ui/shared/js/state.js');
              const bubbles = await import(base + '/ui/chat/js/chat-bubble.js');
              const stream = await import(base + '/ui/chat/js/chat-stream.js');
              const session = await import(base + '/ui/chat/js/session-load.js');
              const host = document.createElement('div');
              document.body.appendChild(host);
              const previous = {chatMessages: app.chatMessages, activeToolGroupBubble: app._activeToolGroupBubble};
              app.chatMessages = host;
              app._activeToolGroupBubble = null;
              bubbles.addChatBubble('user', 'Inspect this', undefined, undefined, undefined, 'user-1', '2026-09-12T12:00:00Z');
              const call = (id, name) => ({id, function: {name, arguments: '{}'}});
              const messages = [
                {id: 'user-1', role: 'user', content: 'Inspect this', session_seq: 1, interaction_seq: 1},
                // Answer-like progress must remain in activity until an explicit
                // message_phase main/final row arrives; it is never optimistically
                // promoted into a standalone agent bubble.
                {id: 'progress-1', role: 'assistant', turn_id: 'user-1', content: 'I inspected the first source and will now verify the result in detail.', output: JSON.stringify({tool_calls: [call('call-one', 'read_source')]}), message_phase: 'progress', progress_kind: 'main', session_seq: 2, interaction_seq: 2, created_at: '2026-09-12T12:00:01Z'},
                {id: 'progress-2', role: 'assistant', turn_id: 'user-1', content: 'The first result is clear, so I am checking the matching test coverage next.', output: JSON.stringify({tool_calls: [call('call-two', 'search_source')]}), message_phase: 'progress', session_seq: 3, interaction_seq: 3, created_at: '2026-09-12T12:00:02Z'},
              ];
              const rows = session._buildPhasedRenderables(messages, new Map(), false, null);
              const activity = rows.find(row => row && row._activityGroup);
              const group = stream.attachActivityEntries(activity.entries, null, {id: activity.firstInteractionId, activityGroupId: activity.id, createdAt: activity.created_at, turnId: activity.ownerTurnId, interactionSeq: activity.session_seq}, activity.ownerTurnId);
              const renderedRows = Array.from(group.querySelectorAll('.bubble-tool-calls-panel > .ca-tool-row'));
              const order = renderedRows.map(row => row.querySelector('.ca-tool-name')?.textContent || 'message');
              const summary = {toolNames: renderedRows.map(row => row.querySelector('.ca-tool-name')?.textContent).filter(Boolean), order, heading: group.querySelector('.bubble-tool-calls-head')?.textContent.trim(), text: group.textContent};
              host.remove();
              app.chatMessages = previous.chatMessages;
              app._activeToolGroupBubble = previous.activeToolGroupBubble;
              return summary;
            }""",
            {"base": self.base},
        )
        self.assertEqual(result["toolNames"], ["read_source", "search_source"])
        self.assertEqual(result["order"], ["message", "read_source", "message", "search_source"])
        self.assertIn("2 tools", result["heading"])
        self.assertIn("2 messages", result["heading"])
        self.assertIn("inspected the first source", result["text"])
        self.assertIn("checking the matching test coverage", result["text"])

    def test_visible_bubbles_bound_activity_despite_internal_turn_changes(self):
        result = self.page.evaluate(
            """async ({base}) => {
              const session = await import(base + '/ui/chat/js/session-load.js');
              const call = (id, name) => ({id, function: {name, arguments: '{}'}});
              const messages = [
                {id: 'user-1', role: 'user', content: 'Build it', session_seq: 1, interaction_seq: 1},
                {id: 'progress-a', role: 'assistant', turn_id: 'run-a', content: 'Starting work.', message_phase: 'progress', output: JSON.stringify({tool_calls: [call('a', 'read_source')]}), session_seq: 2, interaction_seq: 2},
                {id: 'model-a', role: 'system', source: 'system:model', content: 'Model changed', metadata: JSON.stringify({model: 'model-a'}), turn_id: 'run-b', session_seq: 3, interaction_seq: 3},
                {id: 'progress-b', role: 'assistant', turn_id: 'run-c', content: 'Verifying work.', message_phase: 'progress', output: JSON.stringify({tool_calls: [call('b', 'search_source')]}), session_seq: 4, interaction_seq: 4},
                {id: 'main-1', role: 'assistant', turn_id: 'run-c', content: 'Initial overview.', message_phase: 'main', session_seq: 5, interaction_seq: 5},
                {id: 'progress-c', role: 'assistant', turn_id: 'run-d', content: 'Final verification.', message_phase: 'progress', output: JSON.stringify({tool_calls: [call('c', 'pytest')]}), session_seq: 6, interaction_seq: 6},
                {id: 'final-1', role: 'assistant', turn_id: 'run-e', content: 'Final closing.', message_phase: 'final', session_seq: 7, interaction_seq: 7},
              ];
              const rows = session._buildPhasedRenderables(messages, new Map(), false, null);
              return rows.map(row => row && row._activityGroup
                ? {kind: 'activity', owner: row.ownerTurnId, entryCount: row.entries.length}
                : {kind: row.role, id: row.id, closer: row._agentCloser === true});
            }""",
            {"base": self.base},
        )
        self.assertEqual(
            result,
            [
                {"kind": "user", "id": "user-1", "closer": False},
                {"kind": "activity", "owner": "user-1", "entryCount": 5},
                {"kind": "assistant", "id": "main-1", "closer": True},
                {"kind": "activity", "owner": "user-1", "entryCount": 2},
                {"kind": "assistant", "id": "final-1", "closer": True},
            ],
        )

    def test_live_stream_activity_stays_collapsed_until_user_expands_it(self):
        result = self.page.evaluate(
            """async ({base}) => {
              const {app} = await import(base + '/ui/shared/js/state.js');
              const bubbles = await import(base + '/ui/chat/js/chat-bubble.js');
              const stream = await import(base + '/ui/chat/js/chat-stream.js');
              const host = document.createElement('div');
              document.body.appendChild(host);
              const originalSetTimeout = window.setTimeout;
              const previous = {
                chatMessages: app.chatMessages,
                activeToolGroupBubble: app._activeToolGroupBubble,
                currentSessionId: app.currentSessionId,
                isProcessing: app.isProcessing,
              };
              try {
                window.setTimeout = () => 1;
                app.chatMessages = host;
                app.currentSessionId = 'live-preview-session';
                app._activeToolGroupBubble = null;
                const startedAt = new Date(Date.now() - 1200).toISOString();
                bubbles.addChatBubble(
                  'user', 'Build it', undefined, undefined,
                  undefined, 'user-live', startedAt,
                );
                stream.appendStreamToActiveBubble(
                  'Inspecting the current implementation…', 'step-live',
                  startedAt, 'user-live', 2,
                );
                const group = host.querySelector('.chat-bubble.activity-group');
                const panel = group?.querySelector('.bubble-tool-calls-panel');
                const head = group?.querySelector('.bubble-tool-calls-head');
                const before = {
                  previews: host.querySelectorAll('.bubble-tool-calls-preview').length,
                  standaloneStreaming: host.querySelectorAll('.chat-bubble.agent.streaming').length,
                  current: group?.querySelectorAll('.bubble-tool-calls-current').length || 0,
                  currentTime: group?.querySelectorAll('.bubble-tool-calls-current-time').length || 0,
                  collapsed: head?.getAttribute('aria-expanded') === 'false' && panel?.hidden === true,
                  rowsUserVisible: !!(panel && !panel.hidden && panel.querySelector('.ca-tool-row')),
                  liveSummary: head?.textContent || '',
                };
                // The group was created by a recent progress event, but the tool
                // actually began 62 minutes ago. Its timestamp must become the
                // run-clock anchor rather than leaving the header near zero.
                const toolStartedAt = new Date(Date.now() - (62 * 60 * 1000)).toISOString();
                stream.attachActivityEntries([{
                  tool: 'read_source', toolCallId: 'tool-live', args: {}, status: 'running',
                  createdAt: toolStartedAt,
                }], null, {
                  id: 'tool-live', createdAt: startedAt, turnId: 'user-live', interactionSeq: 3,
                }, 'user-live');
                stream.notifyModelSwitch('Premium model enabled', {
                  turnId: 'user-live', createdAt: startedAt,
                  model: 'premium', from: 'default', state: 'override',
                });
                const minuteSummary = head?.textContent || '';
                head?.click();
                const expanded = {
                  open: head?.getAttribute('aria-expanded') === 'true' && panel?.hidden === false,
                  rowInsideGroup: !!panel?.querySelector('.ca-live-preview-row')?.closest('.chat-bubble.activity-group'),
                  visibleRows: panel?.querySelectorAll('.ca-tool-row').length || 0,
                };
                head?.click();
                // The closing gate's verdict is the ONLY promotion authority:
                // a terminal response is promoted (renders a separate closer)
                // only when the backend stamped an explicit main/final phase.
                stream.finalizeAgentResponse(
                  'The implementation is complete.', 'step-live', false,
                  new Date().toISOString(), 'user-live', 4, 'main',
                );
                const closer = host.querySelector('.chat-bubble.agent.terminal-closer');
                const after = {
                  previews: host.querySelectorAll('.bubble-tool-calls-preview').length,
                  closers: host.querySelectorAll('.chat-bubble.agent.terminal-closer').length,
                  normalAssistantBubbles: host.querySelectorAll(':scope > .chat-bubble.agent.terminal-closer').length,
                  closerAfterGroup: closer?.previousElementSibling === group,
                  collapsed: head?.getAttribute('aria-expanded') === 'false' && panel?.hidden === true,
                  closerText: closer?.textContent || '',
                  summary: head?.textContent || '',
                };

                // Isolate an interrupted live run: it preserves its counter and
                // hidden rows without publishing a terminal assistant bubble.
                host.replaceChildren();
                app._activeToolGroupBubble = null;
                bubbles.addChatBubble(
                  'user', 'Stop this', undefined, undefined,
                  undefined, 'user-interrupted', startedAt,
                );
                stream.appendStreamToActiveBubble(
                  'Still checking the live state…', 'step-interrupted',
                  startedAt, 'user-interrupted', 5,
                );
                const interruptedGroup = host.querySelector('.chat-bubble.activity-group');
                const interruptedHead = interruptedGroup?.querySelector('.bubble-tool-calls-head');
                const interruptedPanel = interruptedGroup?.querySelector('.bubble-tool-calls-panel');
                stream.markAgentInterrupted(
                  'step-interrupted', new Date().toISOString(), 5, 'user-interrupted', 'Stopped',
                );
                const interrupted = {
                  collapsed: interruptedHead?.getAttribute('aria-expanded') === 'false'
                    && interruptedPanel?.hidden === true,
                  summary: interruptedHead?.textContent || '',
                  current: interruptedGroup?.querySelectorAll('.bubble-tool-calls-current').length || 0,
                  currentTime: interruptedGroup?.querySelectorAll('.bubble-tool-calls-current-time').length || 0,
                  previews: host.querySelectorAll('.bubble-tool-calls-preview').length,
                  terminalBubbles: host.querySelectorAll('.chat-bubble.agent.terminal-closer').length,
                };
                return {before, minuteSummary, expanded, after, interrupted};
              } finally {
                window.setTimeout = originalSetTimeout;
                host.remove();
                app.chatMessages = previous.chatMessages;
                app._activeToolGroupBubble = previous.activeToolGroupBubble;
                app.currentSessionId = previous.currentSessionId;
                app.isProcessing = previous.isProcessing;
              }
            }""",
            {"base": self.base},
        )
        self.assertEqual(result["before"]["previews"], 0)
        self.assertEqual(result["before"]["standaloneStreaming"], 0)
        self.assertEqual(result["before"]["current"], 0)
        self.assertEqual(result["before"]["currentTime"], 0)
        self.assertTrue(result["before"]["collapsed"])
        self.assertFalse(result["before"]["rowsUserVisible"])
        self.assertNotIn("1 tool", result["before"]["liveSummary"])
        self.assertIn("1 message", result["before"]["liveSummary"])
        self.assertNotIn("(", result["before"]["liveSummary"])
        self.assertNotIn("Inspecting the current implementation", result["before"]["liveSummary"])
        self.assertNotRegex(result["before"]["liveSummary"], r"\b(?:now|\d+[sm])\b")
        self.assertEqual(result["minuteSummary"], "1 tool / 1 message / Model change (1h 2m)›")
        self.assertTrue(result["expanded"]["open"])
        self.assertTrue(result["expanded"]["rowInsideGroup"])
        self.assertGreaterEqual(result["expanded"]["visibleRows"], 2)
        self.assertEqual(result["after"]["previews"], 0)
        self.assertEqual(result["after"]["closers"], 1)
        self.assertEqual(result["after"]["normalAssistantBubbles"], 1)
        self.assertTrue(result["after"]["closerAfterGroup"])
        self.assertTrue(result["after"]["collapsed"])
        self.assertTrue(result["after"]["summary"].endswith("Model change (1h 2m)›"))
        self.assertIn("implementation is complete", result["after"]["closerText"])
        self.assertTrue(result["interrupted"]["collapsed"])
        self.assertRegex(result["interrupted"]["summary"], r"\b[1-9]\d* messages?\b")
        self.assertEqual(result["interrupted"]["current"], 0)
        self.assertEqual(result["interrupted"]["currentTime"], 0)
        self.assertEqual(result["interrupted"]["previews"], 0)
        self.assertEqual(result["interrupted"]["terminalBubbles"], 0)

    def test_compaction_completion_shows_two_second_transient_without_footer_update(self):
        result = self.page.evaluate(
            """async ({base}) => {
              const {app} = await import(base + '/ui/shared/js/state.js');
              const bubbles = await import(base + '/ui/chat/js/chat-bubble.js');
              const stream = await import(base + '/ui/chat/js/chat-stream.js');
              const host = document.createElement('div');
              document.body.appendChild(host);
              const previous = {
                chatMessages: app.chatMessages,
                sessionId: app.currentSessionId,
                setContextTokens: app.setContextTokens,
                formatContextTokens: app.formatContextTokens,
                activeToolGroupBubble: app._activeToolGroupBubble,
              };
              const originalTimeout = window.setTimeout;
              let scheduled = null;
              let context = null;
              app.chatMessages = host;
              app.currentSessionId = 'session';
              app.setContextTokens = (tokens) => { context = tokens; };
              app.formatContextTokens = (tokens) => {
                const value = (tokens / 1000).toFixed(1).replace(/\\.0$/, '');
                return value + 'k';
              };
              app._activeToolGroupBubble = null;
              bubbles.addChatBubble(
                'user', '/compact', undefined, undefined,
                undefined, 'compact-command',
              );
              window.setTimeout = (fn, ms) => { scheduled = {fn, ms}; return 1; };

              const event = {
                type: 'compaction_completed', status: 'success',
                session_id: 'session', interaction_id: 'compact-timer',
                history_tokens_before: 44982, history_tokens_after: 31142,
                detail: 'History: 44,982 → 31,142 tokens (13,840 saved, 31%). '
                  + 'Folded 12 older message(s) into 1 new summary part(s).',
              };
              stream.showCompactionProgress();
              const progressNote = host.querySelector('.compaction-progress-note');
              const progress = progressNote?.textContent.trim();
              const progressIsActivity = progressNote?.classList.contains(
                'live-activity-note',
              );
              const progressIsTail = progressNote?.closest('.chat-bubble') === host.lastElementChild;
              const standaloneProgress = host.querySelectorAll(
                '.system-compaction-progress',
              ).length;
              stream.notifyCompactionCompleted(event);
              stream.notifyCompactionCompleted(event);
              const immediate = Array.from(host.querySelectorAll('.system-compaction-transient'))
                .map(el => el.textContent.trim());
              const heading = host.querySelector(
                '.bubble-tool-calls-label')?.textContent.trim();
              const durable = host.querySelector(
                '.ca-compaction-row .ca-activity-entry-body',
              )?.textContent.trim();
              const delay = scheduled?.ms;
              const progressAfterCompletion = host.querySelectorAll(
                '.compaction-progress-note',
              ).length;
              scheduled?.fn();
              const after = host.querySelectorAll('.system-compaction-transient').length;
              stream.notifyCompactionCompleted({...event, interaction_id: 'replayed', replayed: true});
              const afterReplay = host.querySelectorAll('.system-compaction-transient').length;

              window.setTimeout = originalTimeout;
              host.remove();
              app.chatMessages = previous.chatMessages;
              app.currentSessionId = previous.sessionId;
              app.setContextTokens = previous.setContextTokens;
              app.formatContextTokens = previous.formatContextTokens;
              app._activeToolGroupBubble = previous.activeToolGroupBubble;
              return {progress, progressIsActivity, progressIsTail,
                      standaloneProgress, progressAfterCompletion, immediate, delay,
                      after, afterReplay, context, durable, heading};
            }""",
            {"base": self.base},
        )
        # "Compacting…" is a mock activity note: with activity notes configured
        # OFF it must never paint a transcript row. It ticks in the footer
        # activity element instead (chatActivityStart from the lock helper), and
        # the transcript-mirror preference is the only thing that could put the
        # row back — it is off in the shipped config.
        self.assertFalse(result.get("progress"), result)
        self.assertFalse(result.get("progressIsActivity"), result)
        self.assertFalse(result.get("progressIsTail"), result)
        self.assertEqual(result["standaloneProgress"], 0)
        self.assertEqual(result["progressAfterCompletion"], 0)
        self.assertEqual(
            result["immediate"], ["Compacted history 45k to 31.1k tokens"],
        )
        self.assertEqual(result["delay"], 2000)
        self.assertEqual(result["after"], 0)
        self.assertEqual(result["afterReplay"], 0)
        self.assertIsNone(result["context"])
        self.assertEqual(result["heading"], "1 compaction")
        self.assertIn("History: 44,982", result["durable"])
        self.assertNotIn("Folded", result["immediate"][0])

    def test_live_reconcile_routes_compaction_to_group_without_info_twin(self):
        result = self.page.evaluate(
            """async ({base}) => {
              const {app} = await import(base + '/ui/shared/js/state.js');
              const bubbles = await import(base + '/ui/chat/js/chat-bubble.js');
              const stream = await import(base + '/ui/chat/js/chat-stream.js');
              await import(base + '/ui/chat/js/session-load.js');
              const reconcile = await import(base + '/ui/chat/js/chat-reconcile.js');
              const host = document.createElement('div');
              document.body.appendChild(host);
              const previous = {
                chatMessages: app.chatMessages,
                currentSessionId: app.currentSessionId,
                addChatBubble: app.addChatBubble,
                setContextTokens: app.setContextTokens,
              };
              const originalTimeout = window.setTimeout;
              app.chatMessages = host;
              app.currentSessionId = 'session';
              app.addChatBubble = bubbles.addChatBubble;
              app.setContextTokens = () => {};
              window.setTimeout = () => 1;
              const detail = 'Context: 83,968 → 76,453 tokens (7,515 saved, 9%). '
                + 'Folded 78 older message(s) into 1 new summary part(s).';
              const compactRow = {
                id: 'compact-live-reconcile', role: 'system',
                source: 'system:compaction', content: detail, session_seq: 99,
                metadata: JSON.stringify({compaction: {
                  version: 1, status: 'success', trigger: 'manual',
                  tokens_before: 83968, tokens_after: 76453,
                  tokens_saved: 7515, owner_turn_id: 'prior-user',
                }}),
              };
              app._appendMessagesToTranscript([compactRow], true, false);
              stream.showCompactionProgress();
              reconcile._renderSystemRow(compactRow);
              const row = host.querySelector('.ca-compaction-row');
              const snapshot = {
                standalone: host.querySelectorAll(
                  '.chat-bubble.info[data-msg-id="compact-live-reconcile"]').length,
                transient: host.querySelector(
                  '.system-compaction-transient')?.textContent.trim(),
                heading: host.querySelector(
                  '.bubble-tool-calls-label')?.textContent.trim(),
                detailVisible: !!row?.getClientRects().length,
                panelDisplay: getComputedStyle(host.querySelector(
                  '.bubble-tool-calls-panel')).display,
                progress: host.querySelectorAll('.compaction-progress-note').length,
                durable: host.querySelectorAll('.ca-compaction-row').length,
              };
              window.setTimeout = originalTimeout;
              host.remove();
              app.chatMessages = previous.chatMessages;
              app.currentSessionId = previous.currentSessionId;
              app.addChatBubble = previous.addChatBubble;
              app.setContextTokens = previous.setContextTokens;
              return snapshot;
            }""",
            {"base": self.base},
        )
        self.assertEqual(result["standalone"], 0)
        self.assertEqual(result["transient"], "Compacted history 84k to 76.5k tokens")
        self.assertEqual(result["heading"], "1 compaction")
        self.assertFalse(result["detailVisible"], result)
        self.assertEqual(result["panelDisplay"], "none")
        self.assertEqual(result["progress"], 0)
        self.assertEqual(result["durable"], 1)

    def test_persisted_tools_updates_and_responses_share_one_run_bubble(self):
        result = self.page.evaluate(
            """async ({base}) => {
              const {app} = await import(base + '/ui/shared/js/state.js');
              const bubbles = await import(base + '/ui/chat/js/chat-bubble.js');
              const stream = await import(base + '/ui/chat/js/chat-stream.js');
              const host = document.createElement('div');
              document.body.appendChild(host);
              const previous = {
                chatMessages: app.chatMessages,
                activeToolGroupBubble: app._activeToolGroupBubble,
              };
              app.chatMessages = host;
              app._activeToolGroupBubble = null;
              bubbles.addChatBubble(
                'user', 'Run it', undefined, undefined,
                undefined, 'user-run', '2026-08-21T12:00:00Z',
              );

              stream.attachActivityEntries([
                {tool: 'search', toolCallId: 'tool-1', args: {}, status: 'done'},
              ], null, {id: 'step-1', activityGroupId: 'segment-1',
                        turnId: 'user-run', interactionSeq: 10}, 'user-run');
              bubbles.addChatBubble(
                'info', 'Model changed to gpt-test', 'system-model',
                undefined, undefined, 'model-change',
              );
              stream.attachActivityEntries([
                {kind: 'progress', id: 'update-1', content: 'Checking results'},
              ], null, {id: 'step-2', activityGroupId: 'segment-2',
                        turnId: 'user-run', interactionSeq: 20}, 'user-run');
              stream.attachActivityEntries([
                {kind: 'response', id: 'response-1', content: 'The result is ready'},
              ], null, {id: 'step-3', activityGroupId: 'segment-3',
                        turnId: 'user-run', interactionSeq: 30}, 'user-run');

              // Simulate the matching live final event arriving after DB-tail
              // cleared its active pointer. It must adopt the durable group.
              app._activeToolGroupBubble = null;
              stream.attachActivityEntries([
                {kind: 'response', id: 'response-1', content: 'The result is ready'},
              ], null, {id: 'response-1', turnId: 'user-run'}, 'user-run');

              const groups = host.querySelectorAll(':scope > .activity-group');
              const group = groups[0];
              const rows = Array.from(group.querySelectorAll(
                '.bubble-tool-calls-panel > .ca-tool-row',
              )).map(row => row.classList.contains('ca-progress-row') ? 'update'
                : row.classList.contains('ca-response-row') ? 'response' : 'tool');
              const result = {
                groups: groups.length,
                containers: group.querySelectorAll('.bubble-tool-calls').length,
                rows,
                heading: group.querySelector('.bubble-tool-calls-head')?.textContent.trim(),
              };
              host.remove();
              app.chatMessages = previous.chatMessages;
              app._activeToolGroupBubble = previous.activeToolGroupBubble;
              return result;
            }""",
            {"base": self.base},
        )
        self.assertEqual(result["groups"], 1)
        self.assertEqual(result["containers"], 1)
        self.assertEqual(result["rows"], ["tool", "update", "response"])
        self.assertIn("1 tool / 2 messages", result["heading"])

    def test_live_activity_reuses_turn_group_after_active_pointer_resets(self):
        result = self.page.evaluate(
            """async ({base}) => {
              const {app} = await import(base + '/ui/shared/js/state.js');
              const stream = await import(base + '/ui/chat/js/chat-stream.js');
              const host = document.createElement('div');
              document.body.appendChild(host);
              const previous = {
                chatMessages: app.chatMessages,
                currentSessionId: app.currentSessionId,
                activeToolGroupBubble: app._activeToolGroupBubble,
              };
              app.chatMessages = host;
              app._activeToolGroupBubble = null;

              stream.attachActivityEntries([
                {tool: 'search', toolCallId: 'call-1', args: {}, status: 'done'},
              ], null, {
                id: 'assistant-step-1', turnId: 'user-turn', interactionSeq: 10,
              }, 'user-turn');

              // Simulate a stream/reconcile boundary clearing transient state.
              app._activeToolGroupBubble = null;
              stream.attachActivityEntries([
                {kind: 'progress', id: 'assistant-step-2', content: 'Update two'},
                {tool: 'read', toolCallId: 'call-2', args: {}, status: 'done'},
              ], null, {
                id: 'assistant-step-2', turnId: 'user-turn', interactionSeq: 20,
              }, 'user-turn');

              const groups = host.querySelectorAll(':scope > .activity-group');
              const heading = groups[0]?.querySelector('.bubble-tool-calls-head')?.textContent;
              const rows = groups[0]?.querySelectorAll(
                '.bubble-tool-calls-panel > .ca-tool-row',
              ).length || 0;
              host.remove();
              app.chatMessages = previous.chatMessages;
              app._activeToolGroupBubble = previous.activeToolGroupBubble;
              return {groups: groups.length, heading, rows};
            }""",
            {"base": self.base},
        )
        self.assertEqual(result["groups"], 1)
        self.assertEqual(result["rows"], 3)
        self.assertIn("2 tool calls", result["heading"])
        self.assertIn("1 update", result["heading"])

    def test_live_activity_coalesces_different_backend_turn_ids(self):
        result = self.page.evaluate(
            """async ({base}) => {
              const {app} = await import(base + '/ui/shared/js/state.js');
              const bubbles = await import(base + '/ui/chat/js/chat-bubble.js');
              const stream = await import(base + '/ui/chat/js/chat-stream.js');
              const host = document.createElement('div');
              document.body.appendChild(host);
              const previous = {
                chatMessages: app.chatMessages,
                activeToolGroupBubble: app._activeToolGroupBubble,
              };
              app.chatMessages = host;
              app._activeToolGroupBubble = null;
              const user = bubbles.addChatBubble(
                'user', 'Do the work', undefined, undefined,
                undefined, 'visible-user-turn', '2026-08-12T10:00:00Z',
              );
              bubbles._setBubbleSessionSeq(user, 1);

              // Reproduce recovery/inference segments already rendered as
              // separate disclosures with different backend turn identities.
              stream.attachActivityEntries([
                {tool: 'one', toolCallId: 'one', args: {}, status: 'done'},
              ], null, {id: 'a1', activityGroupId: 'segment-1',
                        turnId: 'backend-turn-1', interactionSeq: 10}, 'backend-turn-1');
              stream.attachActivityEntries([
                {kind: 'progress', id: 'p2', content: 'Second update'},
                {tool: 'two', toolCallId: 'two', args: {}, status: 'done'},
              ], null, {id: 'a2', activityGroupId: 'segment-2',
                        turnId: 'backend-turn-2', interactionSeq: 20}, 'backend-turn-2');
              app._activeToolGroupBubble = null;

              // The next live batch must coalesce every disclosure after the
              // latest visible user message, regardless of backend turn id.
              stream.attachActivityEntries([
                {kind: 'progress', id: 'p3', content: 'Third update'},
                {tool: 'three', toolCallId: 'three', args: {}, status: 'done'},
              ], null, {id: 'a3', turnId: 'backend-turn-3', interactionSeq: 30},
                 'backend-turn-3');

              const groups = host.querySelectorAll(':scope > .activity-group');
              const heading = groups[0]?.querySelector('.bubble-tool-calls-head')?.textContent;
              const rows = groups[0]?.querySelectorAll(
                '.bubble-tool-calls-panel > .ca-tool-row',
              ).length || 0;
              host.remove();
              app.chatMessages = previous.chatMessages;
              app._activeToolGroupBubble = previous.activeToolGroupBubble;
              return {groups: groups.length, heading, rows};
            }""",
            {"base": self.base},
        )
        self.assertEqual(result["groups"], 1)
        self.assertEqual(result["rows"], 5)
        self.assertIn("3 tool calls", result["heading"])
        self.assertIn("2 updates", result["heading"])

    def test_prepending_older_page_cannot_merge_old_final_into_latest_final(self):
        result = self.page.evaluate(
            """async ({base}) => {
              const {app} = await import(base + '/ui/shared/js/state.js');
              const bubbles = await import(base + '/ui/chat/js/chat-bubble.js');
              await import(base + '/ui/chat/js/chat-stream.js');
              const session = await import(base + '/ui/chat/js/session-load.js');
              const host = document.createElement('div');
              document.body.appendChild(host);
              const previous = {
                chatMessages: app.chatMessages,
                agentTurnBubble: app._agentTurnBubble,
                activeToolGroupBubble: app._activeToolGroupBubble,
              };
              app.chatMessages = host;
              app.addChatBubble = bubbles.addChatBubble;
              app._agentTurnBubble = null;
              app._activeToolGroupBubble = null;

              session._renderSessionWindowed([
                {id: 'late-progress', role: 'assistant', content: 'finishing',
                 message_phase: 'progress', turn_id: 'user-2', session_seq: 220,
                 created_at: '2026-08-12T11:16:00Z', status: 'complete'},
                {id: 'final-2', role: 'assistant', content: 'changes complete',
                 message_phase: 'final', turn_id: 'user-2', session_seq: 300,
                 created_at: '2026-08-12T11:17:00Z', status: 'complete'},
              ], 'session', null, false, false, true);

              session._prependMessagesToTranscript([
                {id: 'old-progress', role: 'assistant', content: 'planning',
                 message_phase: 'progress', turn_id: 'user-1', session_seq: 20,
                 created_at: '2026-08-12T10:08:00Z', status: 'complete'},
                {id: 'final-1', role: 'assistant', content: 'shall I implement it?',
                 message_phase: 'final', turn_id: 'user-1', session_seq: 100,
                 created_at: '2026-08-12T10:10:00Z', status: 'complete'},
                {id: 'user-2', role: 'user', content: 'continue', turn_id: 'user-2',
                 session_seq: 101, created_at: '2026-08-12T11:09:00Z', status: 'complete'},
              ], true);

              const finals = Array.from(host.querySelectorAll('.chat-bubble.agent'))
                .filter(el => !el.classList.contains('activity-group'))
                .map(el => ({
                  seq: Number(el.dataset.sessionSeq),
                  text: el.textContent,
                  sections: el.querySelectorAll(':scope > .llm-section').length,
                }));
              const order = Array.from(host.children).map(el =>
                Number(el.dataset.sessionSeq),
              ).filter(Number.isFinite);
              const debug = Array.from(host.children).map(el => ({
                className: el.className, seq: el.dataset.sessionSeq,
                text: el.textContent,
              }));

              host.remove();
              app.chatMessages = previous.chatMessages;
              app._agentTurnBubble = previous.agentTurnBubble;
              app._activeToolGroupBubble = previous.activeToolGroupBubble;
              return {finals, order, debug};
            }""",
            {"base": self.base},
        )
        self.assertEqual(
            [item["seq"] for item in result["finals"]], [100, 300], result
        )
        self.assertIn("shall I implement it?", result["finals"][0]["text"])
        self.assertIn("changes complete", result["finals"][1]["text"])
        self.assertEqual([item["sections"] for item in result["finals"]], [1, 1])
        self.assertEqual(result["order"], sorted(result["order"]))

    def test_reprojection_keeps_recovery_notice_at_its_sequence_boundary(self):
        result = self.page.evaluate(
            """async ({base}) => {
              const {app} = await import(base + '/ui/shared/js/state.js');
              const bubbles = await import(base + '/ui/chat/js/chat-bubble.js');
              await import(base + '/ui/chat/js/chat-stream.js');
              const session = await import(base + '/ui/chat/js/session-load.js');
              const host = document.createElement('div');
              document.body.appendChild(host);
              const previous = {
                chatMessages: app.chatMessages,
                agentTurnBubble: app._agentTurnBubble,
                activeToolGroupBubble: app._activeToolGroupBubble,
              };
              app.chatMessages = host;
              app.addChatBubble = bubbles.addChatBubble;
              const messages = [
                {id: 'user', role: 'user', content: 'go', turn_id: 'user',
                 session_seq: 1, created_at: '2026-08-12T11:00:00Z'},
                {id: 'before-recovery', role: 'assistant', content: 'first work',
                 message_phase: 'progress', turn_id: 'user', session_seq: 10,
                 created_at: '2026-08-12T11:01:00Z', status: 'complete'},
                {id: 'recovery', role: 'system', content: 'Recovered and resumed',
                 session_seq: 15, created_at: '2026-08-12T11:02:00Z'},
                {id: 'after-recovery', role: 'assistant', content: 'resumed work',
                 message_phase: 'progress', turn_id: 'user', session_seq: 20,
                 created_at: '2026-08-12T11:03:00Z', status: 'complete'},
                {id: 'final', role: 'assistant', content: 'done',
                 message_phase: 'final', turn_id: 'user', session_seq: 30,
                 created_at: '2026-08-12T11:04:00Z', status: 'complete'},
              ];
              session._renderSessionWindowed(messages, 'session', null, false, false, true);
              const order = Array.from(host.children).map(el => ({
                seq: Number(el.dataset.sessionSeq),
                activity: el.classList.contains('activity-group'),
                text: el.textContent,
              }));
              host.remove();
              app.chatMessages = previous.chatMessages;
              app._agentTurnBubble = previous.agentTurnBubble;
              app._activeToolGroupBubble = previous.activeToolGroupBubble;
              return order;
            }""",
            {"base": self.base},
        )
        self.assertEqual([item["seq"] for item in result], [1, 10, 15, 20, 30])
        activities = [item for item in result if item["activity"]]
        self.assertEqual(len(activities), 2)
        self.assertIn("first work", activities[0]["text"])
        self.assertNotIn("resumed work", activities[0]["text"])
        self.assertIn("resumed work", activities[1]["text"])
        self.assertNotIn("first work", activities[1]["text"])
        self.assertIn("Recovered and resumed", result[2]["text"])

    def test_live_transcript_uses_time_only_until_durable_sequence_arrives(self):
        result = self.page.evaluate(
            """async ({base}) => {
              const {app} = await import(base + '/ui/shared/js/state.js');
              const bubbles = await import(base + '/ui/chat/js/chat-bubble.js');
              const host = document.createElement('div');
              document.body.appendChild(host);
              const previous = {
                chatMessages: app.chatMessages,
                agentTurnBubble: app._agentTurnBubble,
                activeToolGroupBubble: app._activeToolGroupBubble,
              };
              app.chatMessages = host;
              app._agentTurnBubble = null;
              app._activeToolGroupBubble = null;

              const saved = bubbles.addChatBubble(
                'user', 'saved-old', undefined, undefined, undefined, 'saved-old',
                '2026-08-07T10:00:00Z',
              );
              bubbles._setBubbleSessionSeq(saved, 10);
              const recent = bubbles.addChatBubble(
                'user', 'recent-live', undefined, undefined, undefined, 'recent-live',
                '2026-08-07T12:00:00Z',
              );
              // Simulate an older replay arriving after the recent live node.
              const delayed = bubbles.addChatBubble(
                'user', 'delayed-live', undefined, undefined, undefined, 'delayed-live',
                '2026-08-07T11:00:00Z',
              );
              const provisional = Array.from(host.children).map(
                el => el.querySelector('.bubble-body')?.textContent,
              );

              // Durable sequences replace timestamp fallback. Deliberately use
              // conflicting timestamps to prove time cannot override saved order.
              bubbles._setBubbleSessionSeq(delayed, 30);
              bubbles._setBubbleSessionSeq(recent, 20);
              const durable = Array.from(host.children).map(
                el => el.querySelector('.bubble-body')?.textContent,
              );

              host.remove();
              app.chatMessages = previous.chatMessages;
              app._agentTurnBubble = previous.agentTurnBubble;
              app._activeToolGroupBubble = previous.activeToolGroupBubble;
              return {provisional, durable};
            }""",
            {"base": self.base},
        )
        self.assertEqual(
            result["provisional"],
            ["saved-old", "delayed-live", "recent-live"],
        )
        self.assertEqual(
            result["durable"],
            ["saved-old", "recent-live", "delayed-live"],
        )

    def test_stopped_marker_stays_at_interrupted_turn_after_resume(self):
        result = self.page.evaluate(
            """async ({base}) => {
              const {app} = await import(base + '/ui/shared/js/state.js');
              const bubbles = await import(base + '/ui/chat/js/chat-bubble.js');
              const stream = await import(base + '/ui/chat/js/chat-stream.js');
              const host = document.createElement('div');
              document.body.appendChild(host);
              const previous = {
                chatMessages: app.chatMessages,
                agentTurnBubble: app._agentTurnBubble,
                activeToolGroupBubble: app._activeToolGroupBubble,
                isProcessing: app.isProcessing,
              };
              app.chatMessages = host;
              app._agentTurnBubble = null;
              app._activeToolGroupBubble = null;

              const before = bubbles.addChatBubble(
                'user', 'before', undefined, undefined, undefined, 'before',
                '2026-08-07T10:00:00Z',
              );
              bubbles._setBubbleSessionSeq(before, 10);
              stream.markAgentInterrupted('stopped-turn', '2026-08-07T11:00:00Z');
              const resumed = bubbles.addChatBubble(
                'user', 'resumed', undefined, undefined, undefined, 'resumed',
                '2026-08-07T12:00:00Z',
              );
              bubbles._setBubbleSessionSeq(resumed, 40);

              const provisional = Array.from(host.children).map(el =>
                el.classList.contains('activity-group') ? 'Stopped'
                  : (el.querySelector('.bubble-body')?.textContent
                    || el.querySelector('.llm-section')?.textContent),
              );
              // DB-tail later supplies the interrupted interaction's durable row.
              stream.markAgentInterrupted(
                'stopped-turn', '2026-08-07T10:30:00Z', 20,
              );
              const stopped = host.querySelector('.activity-group');
              const durable = Array.from(host.children).map(el =>
                el.classList.contains('activity-group') ? 'Stopped'
                  : (el.querySelector('.bubble-body')?.textContent
                    || el.querySelector('.llm-section')?.textContent),
              );

              host.remove();
              app.chatMessages = previous.chatMessages;
              app._agentTurnBubble = previous.agentTurnBubble;
              app._activeToolGroupBubble = previous.activeToolGroupBubble;
              app.isProcessing = previous.isProcessing;
              return {
                provisional, durable,
                stoppedSeq: stopped?.getAttribute('data-session-seq'),
                stoppedCount: host.querySelectorAll(':scope > .activity-group').length,
              };
            }""",
            {"base": self.base},
        )
        self.assertEqual(result["provisional"], ["before", "Stopped", "resumed"])
        self.assertEqual(result["durable"], ["before", "Stopped", "resumed"])
        self.assertEqual(result["stoppedSeq"], "20")
        self.assertEqual(result["stoppedCount"], 1)

    def test_live_tool_group_uses_owning_assistant_sequence(self):
        result = self.page.evaluate(
            """async ({base}) => {
              const {app} = await import(base + '/ui/shared/js/state.js');
              const bubbles = await import(base + '/ui/chat/js/chat-bubble.js');
              const stream = await import(base + '/ui/chat/js/chat-stream.js');
              const host = document.createElement('div');
              document.body.appendChild(host);
              const previous = {
                chatMessages: app.chatMessages,
                agentTurnBubble: app._agentTurnBubble,
                activeToolGroupBubble: app._activeToolGroupBubble,
                turnHasBubble: app._turnHasBubble,
              };
              app.chatMessages = host;
              app._agentTurnBubble = null;
              app._activeToolGroupBubble = null;
              app._turnHasBubble = false;

              const before = bubbles.addChatBubble(
                'user', 'before', undefined, undefined, undefined, 'before',
                '2026-08-07T10:00:00Z',
              );
              bubbles._setBubbleSessionSeq(before, 10);
              const resumed = bubbles.addChatBubble(
                'user', 'resumed', undefined, undefined, undefined, 'resumed',
                '2026-08-07T12:00:00Z',
              );
              bubbles._setBubbleSessionSeq(resumed, 40);

              stream.attachToolCallsToLastBubble([
                {tool: 'search', args: {}, status: 'done'},
                {tool: 'read', args: {}, status: 'done'},
              ], undefined, {
                id: 'assistant-tools', sessionSeq: 20,
                createdAt: '2026-08-07T11:00:00Z',
              });
              const toolBubble = host.querySelector('[data-msg-id="assistant-tools"]');
              const order = Array.from(host.children).map(el =>
                el.classList.contains('tool-only')
                  ? el.querySelector('.bubble-tool-calls-head')?.textContent.trim()
                  : el.querySelector('.bubble-body')?.textContent,
              );

              host.remove();
              app.chatMessages = previous.chatMessages;
              app._agentTurnBubble = previous.agentTurnBubble;
              app._activeToolGroupBubble = previous.activeToolGroupBubble;
              app._turnHasBubble = previous.turnHasBubble;
              return {
                order,
                seq: toolBubble?.getAttribute('data-session-seq'),
                id: toolBubble?.getAttribute('data-msg-id'),
              };
            }""",
            {"base": self.base},
        )
        self.assertEqual(result["order"][0], "before")
        self.assertIn("2 tool calls", result["order"][1])
        self.assertEqual(result["order"][2], "resumed")
        self.assertEqual(result["seq"], "20")
        self.assertEqual(result["id"], "assistant-tools")

    def test_zz_folded_stream_keeps_last_complete_message_until_finalize(self):
        result = self.page.evaluate(
            """async ({base}) => {
              const {app} = await import(base + '/ui/shared/js/state.js');
              const prompts = await import(base + '/ui/shared/js/app-prompts.js');
              const stream = await import(base + '/ui/chat/js/chat-stream.js');
              const bubbles = await import(base + '/ui/chat/js/chat-bubble.js');
              const host = document.createElement('div');
              document.body.appendChild(host);
              const previous = {
                chatMessages: app.chatMessages,
                currentSessionId: app.currentSessionId,
                activeToolGroupBubble: app._activeToolGroupBubble,
                turnHasBubble: app._turnHasBubble,
                fetch: window.fetch,
              };
              window.fetch = async () => new Response(JSON.stringify({chat_ui: {
                chat_common: {
                  fold_main_messages: true,
                  classify_main_messages: true,
                  show_tool_calls: true,
                  mirror_activity_in_transcript: true,
                  message_visibility: {defaults: {main: true, progress: true, tool: true}},
                },
              }}), {headers: {'Content-Type': 'application/json'}});
              await prompts.loadUiMessages();
              window.fetch = previous.fetch;
              app.chatMessages = host;
              app.currentSessionId = 'folded-complete-preview-session';
              app._activeToolGroupBubble = null;
              app._turnHasBubble = false;
              bubbles.addChatBubble('user', 'Do the work', undefined, undefined,
                undefined, 'user-complete-preview', '2026-09-03T12:00:00Z');
              stream.mirrorActivityNote(
                'Working...', 'folded-complete-preview-session', 'user-complete-preview',
              );
              stream.finalizeAgentStep(
                'The previous complete update remains visible.',
                'complete-step-1', '2026-09-03T12:00:01Z',
                'user-complete-preview', 2,
              );

              const snapshot = () => {
                const group = host.querySelector('.chat-bubble.agent.activity-group');
                return {
                  groups: host.querySelectorAll('.chat-bubble.agent.activity-group').length,
                  rows: group?.querySelectorAll(
                    '.bubble-tool-calls-panel > .ca-tool-row',
                  ).length || 0,
                  liveRows: group?.querySelectorAll(
                    '[data-activity-entry-id^="live-preview:"]',
                  ).length || 0,
                  preview: group?.querySelector(
                    '.bubble-tool-calls-preview .ca-activity-entry-body',
                  )?.textContent || '',
                  heading: group?.querySelector(
                    '.bubble-tool-calls-head',
                  )?.textContent.trim() || '',
                };
              };

              const before = snapshot();
              stream.appendStreamToActiveBubble(
                'The', 'partial-step-2', '2026-09-03T12:00:02Z',
                'user-complete-preview', 3,
              );
              await new Promise(resolve => setTimeout(resolve, 125));
              const firstToken = snapshot();
              stream.appendStreamToActiveBubble(
                ' next answer is still streaming and is not complete.',
                'partial-step-2', '2026-09-03T12:00:02Z',
                'user-complete-preview', 3,
              );
              await new Promise(resolve => setTimeout(resolve, 125));
              const accumulated = snapshot();
              stream.finalizeAgentStep(
                'The next answer is still streaming and is now complete.',
                'partial-step-2', '2026-09-03T12:00:03Z',
                'user-complete-preview', 3,
              );
              const finalized = snapshot();
              stream.finalizeAgentResponse(
                'Done.', 'complete-step-3', false, '2026-09-03T12:00:04Z',
                'user-complete-preview', 4,
              );
              const shortFinal = snapshot();

              host.remove();
              app.chatMessages = previous.chatMessages;
              app.currentSessionId = previous.currentSessionId;
              app._activeToolGroupBubble = previous.activeToolGroupBubble;
              app._turnHasBubble = previous.turnHasBubble;
              return {before, firstToken, accumulated, finalized, shortFinal};
            }""",
            {"base": self.base},
        )
        self.assertEqual(result["before"]["preview"], "The previous complete update remains visible.")
        self.assertEqual(result["firstToken"], result["before"])
        self.assertEqual(result["accumulated"], result["before"])
        self.assertEqual(result["before"]["groups"], 1)
        self.assertEqual(result["before"]["liveRows"], 0)
        self.assertEqual(result["finalized"]["groups"], 1)
        self.assertEqual(result["shortFinal"]["liveRows"], 0)
        self.assertEqual(result["finalized"]["rows"], 2)
        self.assertEqual(
            result["finalized"]["preview"],
            "The next answer is still streaming and is now complete.",
        )
        self.assertIn("2 messages", result["finalized"]["heading"])
        self.assertEqual(result["shortFinal"]["preview"], "Done.")
        self.assertIn("3 messages", result["shortFinal"]["heading"])

    def test_zz_folded_stream_uses_one_run_bubble_and_complete_final_preview(self):
        result = self.page.evaluate(
            """async ({base}) => {
              const {app} = await import(base + '/ui/shared/js/state.js');
              const prompts = await import(base + '/ui/shared/js/app-prompts.js');
              const stream = await import(base + '/ui/chat/js/chat-stream.js');
              const bubbles = await import(base + '/ui/chat/js/chat-bubble.js');
              const host = document.createElement('div');
              document.body.appendChild(host);
              const previous = {
                chatMessages: app.chatMessages,
                currentSessionId: app.currentSessionId,
                activeToolGroupBubble: app._activeToolGroupBubble,
                turnHasBubble: app._turnHasBubble,
                fetch: window.fetch,
              };
              window.fetch = async () => new Response(JSON.stringify({chat_ui: {
                chat_common: {
                  fold_main_messages: true,
                  classify_main_messages: true,
                  show_tool_calls: true,
                  mirror_activity_in_transcript: true,
                  message_visibility: {defaults: {main: true, progress: true, tool: true}},
                },
              }}), {headers: {'Content-Type': 'application/json'}});
              await prompts.loadUiMessages();
              window.fetch = previous.fetch;
              app.chatMessages = host;
              app.currentSessionId = 'folded-session';
              app._activeToolGroupBubble = null;
              app._turnHasBubble = false;
              bubbles.addChatBubble('user', 'Do the work', undefined, undefined,
                undefined, 'user-turn', '2026-08-21T12:00:00Z');
              stream.mirrorActivityNote('Thinking…', 'folded-session', 'user-turn');

              stream.appendStreamToActiveBubble(
                'Alpha', 'step-1', '2026-08-21T12:00:01Z', 'user-turn', 2,
              );
              stream.appendStreamToActiveBubble(
                ' beta', 'step-1', '2026-08-21T12:00:01Z', 'user-turn', 2,
              );
              await new Promise(resolve => setTimeout(resolve, 125));
              stream.attachToolCallsToLastBubble([
                {tool: 'search', toolCallId: 'search-1', args: {}, status: 'done'},
              ], undefined, {id: 'step-1', turnId: 'user-turn', interactionSeq: 2});
              stream.appendStreamToActiveBubble(
                ' gamma', 'step-1', '2026-08-21T12:00:01Z', 'user-turn', 2,
              );
              await new Promise(resolve => setTimeout(resolve, 125));

              // Reproduce a late activity-pointer change. The next ticker
              // update must migrate/replace the existing mirror, not create a
              // second progress bubble.
              const foreign = bubbles.addChatBubble(
                'agent', '', 'tool-only activity-group', undefined,
                'foreign-group', 'foreign-group', '2026-08-21T12:00:01Z',
              );
              foreign.dataset.activityTurnId = 'foreign-turn';
              app._activeToolGroupBubble = foreign;
              stream.mirrorActivityNote(
                'Turn 2: Reading results', 'folded-session', 'user-turn',
              );
              foreign.remove();

              const live = {
                bubbles: host.querySelectorAll(':scope > .chat-bubble.agent').length,
                progressNotes: host.querySelectorAll('.live-activity-note').length,
                progressText: host.querySelector('.live-activity-label')?.textContent,
                progressIsBubbleTail: host.querySelector('.live-activity-note')
                  === host.querySelector('.live-activity-note')?.closest('.chat-bubble')?.lastElementChild,
                progressBubbleIsTranscriptTail: host.querySelector('.live-activity-note')
                  ?.closest('.chat-bubble') === host.lastElementChild,
                previews: host.querySelectorAll('.bubble-tool-calls-preview').length,
                previewText: host.querySelector('.bubble-tool-calls-preview .ca-activity-entry-body')?.textContent,
                heading: host.querySelector('.bubble-tool-calls-head')?.textContent.trim(),
              };
              stream.finalizeAgentStep('Alpha beta gamma', 'step-1',
                '2026-08-21T12:00:02Z', 'user-turn', 2);
              stream.finalizeAgentResponse('Final answer', 'step-2', false,
                '2026-08-21T12:00:03Z', 'user-turn', 3);
              const final = {
                bubbles: host.querySelectorAll(':scope > .chat-bubble.agent').length,
                previews: host.querySelectorAll('.bubble-tool-calls-preview').length,
                previewText: host.querySelector('.bubble-tool-calls-preview .ca-activity-entry-body')?.textContent,
                heading: host.querySelector('.bubble-tool-calls-head')?.textContent.trim(),
                rows: host.querySelectorAll('.bubble-tool-calls-panel > .ca-tool-row').length,
              };
              host.remove();
              app.chatMessages = previous.chatMessages;
              app.currentSessionId = previous.currentSessionId;
              app._activeToolGroupBubble = previous.activeToolGroupBubble;
              app._turnHasBubble = previous.turnHasBubble;
              return {live, final};
            }""",
            {"base": self.base},
        )
        self.assertEqual(result["live"]["bubbles"], 1)
        self.assertEqual(result["live"]["progressNotes"], 1)
        self.assertEqual(result["live"]["progressText"], "Turn 2: Reading results")
        self.assertTrue(result["live"]["progressIsBubbleTail"])
        self.assertTrue(result["live"]["progressBubbleIsTranscriptTail"])
        self.assertEqual(result["live"]["previews"], 0)
        self.assertIn("1 tool", result["live"]["heading"])
        self.assertEqual(result["final"]["bubbles"], 1)
        self.assertEqual(result["final"]["previews"], 1)
        self.assertEqual(result["final"]["previewText"], "Final answer")
        self.assertIn("1 update", result["final"]["heading"])
        self.assertIn("1 response", result["final"]["heading"])
        self.assertEqual(result["final"]["rows"], 3)

    def test_live_progress_note_stays_at_transcript_bottom(self):
        result = self.page.evaluate(
            """async ({base}) => {
              const {app} = await import(base + '/ui/shared/js/state.js');
              const bubbles = await import(base + '/ui/chat/js/chat-bubble.js');
              const stream = await import(base + '/ui/chat/js/chat-stream.js');
              const prompts = await import(base + '/ui/shared/js/app-prompts.js');
              const host = document.createElement('div');
              document.body.appendChild(host);
              const previous = {
                chatMessages: app.chatMessages,
                currentSessionId: app.currentSessionId,
                activeToolGroupBubble: app._activeToolGroupBubble,
                fetch: window.fetch,
              };
              window.fetch = async () => new Response(JSON.stringify({chat_ui: {
                chat_common: {
                  mirror_activity_in_transcript: true,
                  message_visibility: {
                    defaults: {main: true, progress: true, tool: true},
                  },
                },
              }}), {headers: {'Content-Type': 'application/json'}});
              await prompts.loadUiMessages();
              window.fetch = previous.fetch;
              app.chatMessages = host;
              app.currentSessionId = 'progress-tail-session';
              app._activeToolGroupBubble = null;

              bubbles.addChatBubble(
                'user', 'Continue the work', undefined, undefined,
                undefined, 'user-turn', '2026-08-21T12:00:00Z',
              );
              stream.mirrorActivityNote(
                'Resuming...', 'progress-tail-session', 'user-turn',
              );
              const note = host.querySelector('.live-activity-note');
              const progressBubble = note.closest('.chat-bubble');

              // A later row must not pull the owning run's live disclosure away
              // from its user prompt.
              const later = bubbles.addChatBubble(
                'agent', 'A later response', undefined, undefined,
                undefined, 'later-response', '2026-08-21T12:00:02Z',
              );
              bubbles._setBubbleSessionSeq(later, 20);
              stream.mirrorActivityNote(
                'Still resuming...', 'progress-tail-session', 'user-turn',
              );
              app._reorderTranscriptCanonical();

              const snapshot = {
                noteCount: host.querySelectorAll('.live-activity-note').length,
                noteText: host.querySelector('.live-activity-label')?.textContent,
                progressIsTranscriptTail: progressBubble === host.lastElementChild,
                progressFollowsOwner: progressBubble.previousElementSibling
                  ?.dataset.msgId === 'user-turn',
                noteIsBubbleTail: note === progressBubble.lastElementChild,
              };
              host.remove();
              app.chatMessages = previous.chatMessages;
              app.currentSessionId = previous.currentSessionId;
              app._activeToolGroupBubble = previous.activeToolGroupBubble;
              return snapshot;
            }""",
            {"base": self.base},
        )
        self.assertEqual(result["noteCount"], 1)
        self.assertEqual(result["noteText"], "Still resuming...")
        self.assertFalse(result["progressIsTranscriptTail"])
        self.assertTrue(result["progressFollowsOwner"])
        self.assertTrue(result["noteIsBubbleTail"])

    def test_folded_activity_preview_has_stable_height_scroll_and_frozen_full_view(self):
        result = self.page.evaluate(
            """async ({base}) => {
              const stylePaths = [
                '/ui/shared/css/app1.css',
                '/ui/shared/css/design-system.css',
                '/ui/shared/css/index.css',
              ];
              const addedStyles = [];
              for (const path of stylePaths) {
                if (document.querySelector(`link[data-layout-test="${path}"]`)) continue;
                await new Promise((resolve, reject) => {
                  const link = document.createElement('link');
                  link.rel = 'stylesheet';
                  link.href = base + path;
                  link.dataset.layoutTest = path;
                  link.onload = resolve;
                  link.onerror = reject;
                  document.head.appendChild(link);
                  addedStyles.push(link);
                });
              }

              const {app} = await import(base + '/ui/shared/js/state.js');
              const stream = await import(base + '/ui/chat/js/chat-stream.js');
              const bubbles = await import(base + '/ui/chat/js/chat-bubble.js');
              const panelHost = document.createElement('div');
              panelHost.id = 'chat-panel';
              const host = document.createElement('div');
              host.style.cssText = 'display:flex;flex-direction:column;width:1000px';
              panelHost.appendChild(host);
              document.body.appendChild(panelHost);
              const previous = {
                chatMessages: app.chatMessages,
                activeToolGroupBubble: app._activeToolGroupBubble,
              };
              app.chatMessages = host;
              app._activeToolGroupBubble = null;

              const completePreview = 'Here is the complete preview.\\n\\nThis final line must remain visible.';
              const group = stream.attachActivityEntries([
                {kind: 'response', id: 'older-response', content: 'X'.repeat(600)},
                {kind: 'response', id: 'latest-response', content: completePreview},
              ], null, {id: 'latest-response', turnId: 'user-layout'}, 'user-layout');
              const note = document.createElement('div');
              note.className = 'live-activity-note';
              note.innerHTML = '<span class="live-activity-label">Working…</span>';
              group.appendChild(note);
              const closer = bubbles.addChatBubble(
                'agent', 'Audited final answer', 'summary-bubble', undefined,
                undefined, 'closer-layout',
              );

              const panel = group.querySelector('.bubble-tool-calls-panel');
              const slot = group.querySelector('.bubble-tool-calls-preview-slot');
              const preview = group.querySelector('.bubble-tool-calls-preview');
              const body = preview.querySelector('.ca-activity-entry-body');
              const headingLabel = group.querySelector('.bubble-tool-calls-label');
              const noteLabel = note.querySelector('.live-activity-label');
              const style = getComputedStyle(group);
              const previewStyle = getComputedStyle(preview);
              const closerStyle = getComputedStyle(closer);
              const snapshot = {
                previewText: body.textContent,
                slotHeight: slot.getBoundingClientRect().height,
                previewHeight: preview.getBoundingClientRect().height,
                previewOverflowY: previewStyle.overflowY,
                followingGap: note.getBoundingClientRect().top - preview.getBoundingClientRect().bottom,
                shortContentHeight: body.getBoundingClientRect().height,
                fullViewButton: !!preview.querySelector('.bubble-tool-calls-preview-full'),
                groupWidth: group.getBoundingClientRect().width,
                hostWidth: host.getBoundingClientRect().width,
                panelDisplay: getComputedStyle(panel).display,
                background: style.backgroundColor,
                borderWidth: style.borderTopWidth,
                borderRadius: style.borderTopLeftRadius,
                backdropFilter: style.backdropFilter,
                overflow: style.overflow,
                previewBorderWidth: previewStyle.borderTopWidth,
                closerBorderWidth: closerStyle.borderTopWidth,
                closerBorderRadius: closerStyle.borderTopLeftRadius,
                headingLeft: headingLabel.getBoundingClientRect().left,
                progressLeft: noteLabel.getBoundingClientRect().left,
              };

              preview.querySelector('.bubble-tool-calls-preview-full').click();
              const frozenText = document.querySelector(
                '.activity-preview-popover-message .ca-activity-entry-body',
              )?.textContent;
              closer.remove();
              snapshot.initialGroupHeight = group.getBoundingClientRect().height;
              snapshot.initialTailReserve = parseFloat(getComputedStyle(group).paddingBottom);
              const longPreview = Array.from(
                {length: 80}, (_, i) => `Long preview line ${i + 1}`,
              ).join('\\n');
              stream.attachActivityEntries([
                {kind: 'response', id: 'newer-response', content: longPreview},
              ], group, {id: 'newer-response', turnId: 'user-layout'}, 'user-layout');
              const replacementSlot = group.querySelector('.bubble-tool-calls-preview-slot');
              const replacement = group.querySelector('.bubble-tool-calls-preview');
              snapshot.longSlotHeight = replacementSlot.getBoundingClientRect().height;
              snapshot.longPreviewHeight = replacement.getBoundingClientRect().height;
              snapshot.longPreviewScrollable = replacement.scrollHeight > replacement.clientHeight;
              snapshot.longGroupHeight = group.getBoundingClientRect().height;
              snapshot.inlinePreviewChanged = replacement.querySelector(
                '.ca-activity-entry-body',
              )?.textContent.startsWith('Long preview line 1');
              snapshot.frozenTextAfterUpdate = document.querySelector(
                '.activity-preview-popover-message .ca-activity-entry-body',
              )?.textContent;
              snapshot.frozenText = frozenText;

              const shortPreview = 'Short replacement';
              stream.attachActivityEntries([
                {kind: 'response', id: 'short-response', content: shortPreview},
              ], group, {id: 'short-response', turnId: 'user-layout'}, 'user-layout');
              const shortReplacement = group.querySelector('.bubble-tool-calls-preview');
              snapshot.shortGroupHeight = group.getBoundingClientRect().height;
              snapshot.shortTailReserve = parseFloat(getComputedStyle(group).paddingBottom);
              snapshot.shortFollowingGap = note.getBoundingClientRect().top
                - shortReplacement.getBoundingClientRect().bottom;

              const mediumPreview = 'Medium preview line one\\n\\nMedium preview line two\\n\\nMedium preview line three';
              stream.attachActivityEntries([
                {kind: 'response', id: 'medium-response', content: mediumPreview},
              ], group, {id: 'medium-response', turnId: 'user-layout'}, 'user-layout');
              const mediumReplacement = group.querySelector('.bubble-tool-calls-preview');
              snapshot.mediumGroupHeight = group.getBoundingClientRect().height;
              snapshot.mediumTailReserve = parseFloat(getComputedStyle(group).paddingBottom);
              snapshot.mediumFollowingGap = note.getBoundingClientRect().top
                - mediumReplacement.getBoundingClientRect().bottom;

              // Expanded disclosures hide the preview and must not retain its
              // former tail reserve. A matching closer removes the preview too.
              group.querySelector('.bubble-tool-calls-head').click();
              snapshot.expandedTailReserve = parseFloat(getComputedStyle(group).paddingBottom);
              stream.suppressMatchingResponsePreview(mediumPreview, 'medium-response');
              snapshot.previewRemovedByCloser = !group.querySelector('.bubble-tool-calls-preview');
              snapshot.removedTailReserve = parseFloat(getComputedStyle(group).paddingBottom);
              document.querySelector('.activity-preview-popover-close')?.click();
              snapshot.popoverClosed = !document.querySelector('.activity-preview-popover');

              panelHost.remove();
              addedStyles.forEach(link => link.remove());
              app.chatMessages = previous.chatMessages;
              app._activeToolGroupBubble = previous.activeToolGroupBubble;
              return snapshot;
            }""",
            {"base": self.base},
        )
        self.assertEqual(
            result["previewText"],
            "Here is the complete preview.\n\nThis final line must remain visible.",
        )
        self.assertAlmostEqual(result["slotHeight"], result["previewHeight"], delta=0.5)
        self.assertLess(result["previewHeight"], 250)
        self.assertLess(result["followingGap"], 20)
        self.assertEqual(result["previewOverflowY"], "auto")
        self.assertLess(result["shortContentHeight"], 100)
        self.assertAlmostEqual(result["longSlotHeight"], result["longPreviewHeight"], delta=0.5)
        self.assertEqual(result["longPreviewHeight"], 250)
        self.assertTrue(result["fullViewButton"])
        self.assertTrue(result["longPreviewScrollable"])
        self.assertTrue(result["inlinePreviewChanged"])
        self.assertEqual(result["frozenText"], result["previewText"])
        self.assertEqual(result["frozenTextAfterUpdate"], result["previewText"])
        self.assertTrue(result["popoverClosed"])
        self.assertAlmostEqual(result["initialTailReserve"], 0, delta=0.5)
        self.assertGreater(result["longGroupHeight"], result["initialGroupHeight"])
        self.assertGreaterEqual(result["shortGroupHeight"] + 0.5, result["longGroupHeight"])
        self.assertGreater(result["shortTailReserve"], 0)
        self.assertLess(result["shortFollowingGap"], 20)
        self.assertGreaterEqual(result["mediumGroupHeight"] + 0.5, result["shortGroupHeight"])
        self.assertLess(result["mediumTailReserve"], result["shortTailReserve"])
        self.assertGreaterEqual(result["mediumTailReserve"], 0)
        self.assertLess(result["mediumFollowingGap"], 20)
        self.assertAlmostEqual(result["expandedTailReserve"], 0, delta=0.5)
        self.assertTrue(result["previewRemovedByCloser"])
        self.assertAlmostEqual(result["removedTailReserve"], 0, delta=0.5)
        self.assertLess(result["groupWidth"], result["hostWidth"] * 0.6)
        self.assertEqual(result["panelDisplay"], "none")
        self.assertIn(result["background"], ("rgba(0, 0, 0, 0)", "transparent"))
        self.assertEqual(result["borderWidth"], "0px")
        self.assertEqual(result["borderRadius"], "0px")
        self.assertEqual(result["backdropFilter"], "none")
        self.assertEqual(result["overflow"], "visible")
        self.assertNotEqual(result["previewBorderWidth"], "0px")
        self.assertNotEqual(result["closerBorderWidth"], "0px")
        self.assertNotEqual(result["closerBorderRadius"], "0px")
        self.assertAlmostEqual(result["progressLeft"], result["headingLeft"], delta=0.5)

    def test_closer_orders_after_user_hides_matching_preview_and_has_no_header(self):
        result = self.page.evaluate(
            """async ({base}) => {
              const {app} = await import(base + '/ui/shared/js/state.js');
              const stream = await import(base + '/ui/chat/js/chat-stream.js');
              const bubbles = await import(base + '/ui/chat/js/chat-bubble.js');
              const host = document.createElement('div');
              host.style.cssText = 'display:flex;flex-direction:column';
              document.body.appendChild(host);
              const previous = {
                chatMessages: app.chatMessages,
                activeToolGroupBubble: app._activeToolGroupBubble,
                agentTurnBubble: app._agentTurnBubble,
              };
              app.chatMessages = host;
              app._activeToolGroupBubble = null;
              app._agentTurnBubble = null;

              // Reproduce the race: folded response materializes before the
              // owning user bubble and has no useful timestamp/sequence yet.
              stream.attachActivityEntries(
                [{kind: 'response', id: 'assistant-1', content: 'Final answer'}],
                null,
                {id: 'assistant-1', turnId: 'user-1'},
                'user-1',
              );
              const previewBefore = host.querySelectorAll('.bubble-tool-calls-preview').length;
              const entryHeadersBefore = host.querySelectorAll(
                '.ca-progress-row > .ca-activity-entry-head, '
                  + '.ca-response-row > .ca-activity-entry-head',
              ).length;
              bubbles.addChatBubble(
                'user', 'Question', undefined, undefined, 'user-1', 'user-1',
              );

              const beforeMismatch = stream.suppressMatchingResponsePreview(
                'Different answer', 'assistant-1',
              );
              const previewAfterMismatch = host.querySelectorAll(
                '.bubble-tool-calls-preview',
              ).length;
              const closer = bubbles.addChatBubble(
                'agent', 'Final answer', 'summary-bubble', undefined,
                undefined, 'closer-1',
              );
              const matched = stream.suppressMatchingResponsePreview(
                'Final answer', 'assistant-1',
              );

              const order = Array.from(host.children).map(el => {
                if (el.classList.contains('user')) return 'user';
                if (el.classList.contains('activity-group')) return 'activity';
                if (el.classList.contains('summary-bubble')) return 'closer';
                return 'other';
              });
              const snapshot = {
                previewBefore,
                entryHeadersBefore,
                beforeMismatch,
                previewAfterMismatch,
                matched,
                previewAfter: host.querySelectorAll('.bubble-tool-calls-preview').length,
                responseRows: host.querySelectorAll(
                  '.bubble-tool-calls-panel > .ca-response-row',
                ).length,
                closerLabels: closer.querySelectorAll(':scope > .label').length,
                closerText: closer.textContent.trim(),
                order,
              };

              host.remove();
              app.chatMessages = previous.chatMessages;
              app._activeToolGroupBubble = previous.activeToolGroupBubble;
              app._agentTurnBubble = previous.agentTurnBubble;
              return snapshot;
            }""",
            {"base": self.base},
        )
        self.assertEqual(result["previewBefore"], 1)
        self.assertEqual(result["entryHeadersBefore"], 0)
        self.assertFalse(result["beforeMismatch"])
        self.assertEqual(result["previewAfterMismatch"], 1)
        self.assertTrue(result["matched"])
        self.assertEqual(result["previewAfter"], 0)
        self.assertEqual(result["responseRows"], 1)
        self.assertEqual(result["closerLabels"], 0)
        self.assertEqual(result["closerText"], "Final answer")
        self.assertEqual(result["order"], ["user", "activity", "closer"])

    def test_persisted_same_owner_segments_do_not_cross_closer_boundaries(self):
        result = self.page.evaluate(
            """async ({base}) => {
              const {app} = await import(base + '/ui/shared/js/state.js');
              const stream = await import(base + '/ui/chat/js/chat-stream.js');
              const bubbles = await import(base + '/ui/chat/js/chat-bubble.js');
              const host = document.createElement('div');
              host.style.cssText = 'display:flex;flex-direction:column';
              document.body.appendChild(host);
              const previous = {
                chatMessages: app.chatMessages,
                activeToolGroupBubble: app._activeToolGroupBubble,
              };
              app.chatMessages = host;
              app._activeToolGroupBubble = null;

              const user = bubbles.addChatBubble(
                'user', 'Question', undefined, undefined, 'user-1', 'user-1',
              );
              bubbles._setBubbleSessionSeq(user, 1);
              const first = stream.attachActivityEntries(
                [{kind: 'response', id: 'response-a', content: 'First phase'}],
                null,
                {id: 'response-a', activityGroupId: 'segment-a', turnId: 'user-1', interactionSeq: 2},
                'user-1',
              );
              const closerA = bubbles.addChatBubble(
                'agent', 'Closer A', 'summary-bubble', undefined, undefined, 'closer-a',
              );
              bubbles._setBubbleSessionSeq(closerA, 3);
              const second = stream.attachActivityEntries(
                [{kind: 'response', id: 'response-b', content: 'Second phase'}],
                null,
                {id: 'response-b', activityGroupId: 'segment-b', turnId: 'user-1', interactionSeq: 4},
                'user-1',
              );
              const closerB = bubbles.addChatBubble(
                'agent', 'Closer B', 'summary-bubble', undefined, undefined, 'closer-b',
              );
              bubbles._setBubbleSessionSeq(closerB, 5);

              const order = Array.from(host.children).map(el =>
                el.dataset.activitySegmentId || el.dataset.msgId || el.dataset.turnId,
              );
              const responseIds = Array.from(host.querySelectorAll(
                '.bubble-tool-calls-panel > .ca-response-row',
              ))
                .map(row => row.__entry && row.__entry.id);
              const snapshot = {
                order,
                segmentCount: host.querySelectorAll('.activity-group').length,
                responseIds,
                firstIsDistinct: first !== second,
                duplicateMsgIds: Array.from(host.children)
                  .map(el => el.dataset.msgId)
                  .filter(Boolean)
                  .filter((id, i, ids) => ids.indexOf(id) !== i),
              };

              host.remove();
              app.chatMessages = previous.chatMessages;
              app._activeToolGroupBubble = previous.activeToolGroupBubble;
              return snapshot;
            }""",
            {"base": self.base},
        )
        self.assertTrue(result["firstIsDistinct"])
        self.assertEqual(result["segmentCount"], 2)
        self.assertEqual(result["responseIds"], ["response-a", "response-b"])
        self.assertEqual(
            result["order"],
            ["user-1", "segment-a", "closer-a", "segment-b", "closer-b"],
        )
        self.assertEqual(result["duplicateMsgIds"], [])

    def test_refresh_message_bypasses_cache_and_repairs_panel_and_preview(self):
        result = self.page.evaluate(
            """async ({base}) => {
              const {app} = await import(base + '/ui/shared/js/state.js');
              const stream = await import(base + '/ui/chat/js/chat-stream.js');
              await import(base + '/ui/chat/js/chat-bubble-actions.js');
              await import(base + '/ui/chat/js/session-load.js');
              const host = document.createElement('div');
              host.style.cssText = 'display:flex;flex-direction:column';
              document.body.appendChild(host);
              const previous = {
                chatMessages: app.chatMessages,
                currentSessionId: app.currentSessionId,
                activeToolGroupBubble: app._activeToolGroupBubble,
                fetch: window.fetch,
              };
              app.chatMessages = host;
              app.currentSessionId = 'refresh-repair-session';
              app._activeToolGroupBubble = null;
              stream.attachActivityEntries(
                [{kind: 'response', id: 'assistant-refresh', content: 'Short'}],
                null,
                {id: 'assistant-refresh', turnId: 'user-refresh'},
                'user-refresh',
              );

              let request = null;
              window.fetch = async (url, init) => {
                request = {url: String(url), cache: init && init.cache};
                return new Response(JSON.stringify({messages: [{
                  id: 'assistant-refresh', role: 'assistant',
                  content: 'Complete authoritative response', status: 'complete',
                  created_at: '2026-08-22T12:00:00Z', session_seq: 2,
                }]}), {headers: {'Content-Type': 'application/json'}});
              };

              const preview = host.querySelector('.bubble-tool-calls-preview');
              preview.querySelector('.turn-gutter-more').click();
              const refresh = document.querySelector(
                '.bubble-more-menu [data-refresh-row="1"]',
              );
              refresh.click();
              for (let i = 0; i < 30; i += 1) {
                if (preview.querySelector('.ca-activity-entry-body')?.textContent
                    === 'Complete authoritative response') break;
                await new Promise(resolve => setTimeout(resolve, 20));
              }

              const snapshot = {
                request,
                previewText: preview.querySelector('.ca-activity-entry-body')?.textContent,
                panelText: host.querySelector(
                  '.bubble-tool-calls-panel > .ca-response-row .ca-activity-entry-body',
                )?.textContent,
                sourceText: host.querySelector(
                  '.bubble-tool-calls-panel > .ca-response-row',
                )?.__entry?.content,
              };
              document.querySelector('.bubble-more-menu')?.remove();
              host.remove();
              window.fetch = previous.fetch;
              app.chatMessages = previous.chatMessages;
              app.currentSessionId = previous.currentSessionId;
              app._activeToolGroupBubble = previous.activeToolGroupBubble;
              return snapshot;
            }""",
            {"base": self.base},
        )
        self.assertIn("_refresh=", result["request"]["url"])
        self.assertEqual(result["request"]["cache"], "no-store")
        self.assertEqual(result["previewText"], "Complete authoritative response")
        self.assertEqual(result["panelText"], "Complete authoritative response")
        self.assertEqual(result["sourceText"], "Complete authoritative response")

    def test_background_cache_refresh_replaces_truncated_existing_row(self):
        result = self.page.evaluate(
            """async ({base}) => {
              const {app} = await import(base + '/ui/shared/js/state.js');
              const cacheModule = await import(base + '/ui/chat/js/chat-message-cache.js');
              const session = await import(base + '/ui/chat/js/session-load.js');
              const sid = 'truncated-cache-repair';
              const previous = {
                chatMessages: app.chatMessages,
                currentSessionId: app.currentSessionId,
              };
              app.chatMessages = null;
              app.currentSessionId = sid;
              cacheModule._messageCache.set(sid, {
                messages: [{
                  id: 'assistant-cache', role: 'assistant', content: 'Hi',
                  status: 'streaming', session_seq: 1,
                }],
                maxSeq: 1, light: true,
              });
              session._mergeCachedRefresh(sid, [{
                id: 'assistant-cache', role: 'assistant',
                content: 'Hi! Ready when you are — what are we working on today?',
                status: 'complete', session_seq: 1,
              }], false);
              const repaired = cacheModule._messageCache.get(sid).messages[0];
              cacheModule._messageCache.delete(sid);
              app.chatMessages = previous.chatMessages;
              app.currentSessionId = previous.currentSessionId;
              return repaired;
            }""",
            {"base": self.base},
        )
        self.assertEqual(result["status"], "complete")
        self.assertEqual(
            result["content"],
            "Hi! Ready when you are — what are we working on today?",
        )

    def test_active_stream_polls_durable_tail_even_while_websocket_is_recent(self):
        result = self.page.evaluate(
            """async ({base}) => {
              const {app} = await import(base + '/ui/shared/js/state.js');
              const reconcile = await import(base + '/ui/chat/js/chat-reconcile.js');
              if (typeof app.stopReconcileLoop === 'function') app.stopReconcileLoop();
              const previous = {
                fetch: window.fetch,
                currentSessionId: app.currentSessionId,
                currentUserId: app.currentUserId,
                isProcessing: app.isProcessing,
                lastWsEventAt: app._lastWsEventAt,
                lastInteractionSeq: app.lastInteractionSeq,
              };
              let calls = 0;
              window.fetch = async url => {
                if (String(url).includes('/api/v1/db/session-tail')) {
                  calls += 1;
                  return new Response(JSON.stringify({
                    messages: [],
                    run: {active: true, latest_session_seq: 0},
                  }), {status: 200, headers: {'Content-Type': 'application/json'}});
                }
                return previous.fetch(url);
              };
              app.currentSessionId = 'active-order-poll';
              app.currentUserId = '';
              app.isProcessing = true;
              app._lastWsEventAt = {'active-order-poll': Date.now()};
              app.lastInteractionSeq = {'active-order-poll': 0};
              reconcile.startReconcileLoop();
              await new Promise(resolve => setTimeout(resolve, 950));
              app.stopReconcileLoop();

              window.fetch = previous.fetch;
              app.currentSessionId = previous.currentSessionId;
              app.currentUserId = previous.currentUserId;
              app.isProcessing = previous.isProcessing;
              app._lastWsEventAt = previous.lastWsEventAt;
              app.lastInteractionSeq = previous.lastInteractionSeq;
              reconcile.startReconcileLoop();
              return calls;
            }""",
            {"base": self.base},
        )
        self.assertGreaterEqual(result, 1)

    def test_multi_user_isolation_and_logout_purge(self):
        result = self.page.evaluate(
            """async ({base, first, second}) => {
              const {default: db} = await import(base + '/ui/chat/js/storage/indexeddb.js');
              db.setOwnerScope(first);
              await db.createSession({id: 'shared', agent_id: 'a', title: 'secret'});
              const firstCount = await db.countSessions();
              db.setOwnerScope(second);
              const secondCount = await db.countSessions();
              db.setOwnerScope(first);
              await db.clearAll();
              const purgedCount = await db.countSessions();
              return {firstCount, secondCount, purgedCount};
            }""",
            {
                "base": self.base,
                "first": self._scope("user_a"),
                "second": self._scope("user_b"),
            },
        )
        self.assertEqual(result, {"firstCount": 1, "secondCount": 0, "purgedCount": 0})

    def test_device_purge_resolves_scope_after_reload_and_only_then_acks(self):
        page = self.browser.new_page()
        try:
            page.goto(f"{self.base}/ui/chat/index.html")
            result = page.evaluate(
                """async ({base, userId, revision}) => {
                  const payload = btoa(JSON.stringify({
                    user_id: userId, sub: userId, rev: revision,
                  })).replace(/=/g, '').replace(/\\+/g, '-').replace(/\\//g, '_');
                  const token = `header.${payload}.signature`;
                  localStorage.setItem('webagent_accounts', JSON.stringify([{
                    user_id: userId, username: userId, access_token: token,
                  }]));
                  localStorage.setItem('webagent_active_user_id', userId);
                  localStorage.setItem('auth_user_id', userId);
                  localStorage.setItem('auth_token', token);

                  const input = new TextEncoder().encode(
                    `webagent-browser-cache:${userId}:${revision}`,
                  );
                  const bytes = new Uint8Array(await crypto.subtle.digest('SHA-256', input));
                  const scope = Array.from(
                    bytes, byte => byte.toString(16).padStart(2, '0'),
                  ).join('').slice(0, 24);
                  const {default: db} = await import(
                    base + '/ui/chat/js/storage/indexeddb.js'
                  );
                  db.setOwnerScope(scope);
                  await db.createSession({id: 'secret', agent_id: 'a'});
                  db.close();
                  return {token, scope};
                }""",
                {"base": self.base, "userId": self._scope("purge"), "revision": 3},
            )
            page.reload()
            outcome = page.evaluate(
                """async ({base, token}) => {
                  let acknowledgements = 0;
                  window.fetch = async url => {
                    if (String(url).includes('/device/purge-ack')) acknowledgements += 1;
                    return new Response('{}', {status: 200});
                  };
                  const {purgeAndAcknowledge} = await import(
                    base + '/ui/shared/js/device-purge.js'
                  );
                  const purged = await purgeAndAcknowledge(
                    token, {forgetAccount: false},
                  );
                  const {default: db} = await import(
                    base + '/ui/chat/js/storage/indexeddb.js'
                  );
                  return {
                    purged,
                    acknowledgements,
                    remaining: await db.countSessions(),
                  };
                }""",
                {"base": self.base, "token": result["token"]},
            )
            self.assertEqual(
                outcome,
                {"purged": True, "acknowledgements": 1, "remaining": 0},
            )

            absent = page.evaluate(
                """async ({base}) => {
                  const userId = 'absent-tenant';
                  const payload = btoa(JSON.stringify({
                    user_id: userId, sub: userId, rev: 99,
                  })).replace(/=/g, '').replace(/\\+/g, '-').replace(/\\//g, '_');
                  const token = `header.${payload}.signature`;
                  localStorage.setItem('webagent_accounts', JSON.stringify([{
                    user_id: userId, username: userId, access_token: token,
                  }]));
                  localStorage.setItem('webagent_active_user_id', userId);
                  localStorage.setItem('auth_user_id', userId);
                  let acknowledgements = 0;
                  window.fetch = async () => {
                    acknowledgements += 1;
                    return new Response('{}', {status: 200});
                  };
                  const {purgeAndAcknowledge} = await import(
                    base + '/ui/shared/js/device-purge.js'
                  );
                  const purged = await purgeAndAcknowledge(
                    token, {forgetAccount: false},
                  );
                  return {purged, acknowledgements};
                }""",
                {"base": self.base},
            )
            self.assertEqual(absent, {"purged": True, "acknowledgements": 1})
        finally:
            page.close()

    def test_multi_tab_sequence_allocation_is_atomic(self):
        result = self.page.evaluate(
            """async ({base, scope}) => {
              const mod = await import(base + '/ui/chat/js/storage/indexeddb.js');
              const one = new mod.SessionDB();
              const two = new mod.SessionDB();
              one.setOwnerScope(scope);
              two.setOwnerScope(scope);
              await one.createSession({id: 's', agent_id: 'a'});
              await Promise.all([
                one.addInteraction('s', {id: 'one', role: 'user', content: 'one'}),
                two.addInteraction('s', {id: 'two', role: 'assistant', content: 'two'}),
              ]);
              const rows = await one.getInteractions('s');
              return rows.map(row => row.session_seq);
            }""",
            {"base": self.base, "scope": self._scope("tabs")},
        )
        self.assertEqual(result, [0, 1])

    def test_canonical_order_inserts_unsequenced_rows_by_real_timestamp(self):
        result = self.page.evaluate(
            """async ({base}) => {
              const {sortTranscriptCanonical} = await import(
                base + '/ui/chat/js/transcript-order.js'
              );
              const rows = [
                {id: 'durable-late', session_seq: 20,
                 created_at: '2026-08-22T12:02:00Z'},
                {id: 'legacy-middle', session_seq: null,
                 created_at: '2026-08-22 12:01:00'},
                {id: 'durable-early', session_seq: 10,
                 created_at: '2026-08-22T12:00:00Z'},
              ];
              return sortTranscriptCanonical(rows).map(row => row.id);
            }""",
            {"base": self.base},
        )
        self.assertEqual(
            result, ["durable-early", "legacy-middle", "durable-late"]
        )

    def test_server_cache_replace_preserves_missing_sequence(self):
        result = self.page.evaluate(
            """async ({base, scope}) => {
              const {SessionDB} = await import(base + '/ui/chat/js/storage/indexeddb.js');
              const db = new SessionDB();
              db.setOwnerScope(scope);
              await db.createSession({id: 's', agent_id: 'a'});
              await db.replaceInteractions('s', [
                {id: 'saved', role: 'assistant', content: 'saved', session_seq: 8,
                 created_at: '2026-08-22T12:00:00Z'},
                {id: 'unstamped', role: 'tool', content: '', session_seq: null,
                 created_at: '2026-08-22T12:01:00Z'},
              ]);
              const rows = await db.getInteractions('s', Infinity);
              db.close();
              return rows.map(row => ({id: row.id, session_seq: row.session_seq}));
            }""",
            {"base": self.base, "scope": self._scope("null_seq")},
        )
        self.assertEqual(
            result,
            [
                {"id": "saved", "session_seq": 8},
                {"id": "unstamped", "session_seq": None},
            ],
        )

    def test_hybrid_merge_replaces_stale_streaming_row(self):
        result = self.page.evaluate(
            """async ({base, scope}) => {
              window.fetch = async url => {
                if (String(url).includes('/api/v1/browser/routing')) {
                  return new Response(JSON.stringify({
                    routing: {session_data: 'server', session_cache: 'browser'},
                    capabilities: {browser_authority: false, browser_session_cache: true},
                    cache_scope: scope,
                    cache_policy: {schema_version: 2, transcript_ttl_seconds: 900},
                  }), {status: 200, headers: {'Content-Type': 'application/json'}});
                }
                throw new Error('unexpected fetch ' + url);
              };
              const {storageAdapter} = await import(
                base + '/ui/chat/js/storage/storage-adapter.js'
              );
              const {default: db} = await import(
                base + '/ui/chat/js/storage/indexeddb.js'
              );
              await storageAdapter.autoSelectMode('agent');
              await db.createSession({
                id: 's', agent_id: 'a', _authority: 'server', _dirty: false,
              });
              await db.replaceInteractions('s', [{
                id: 'a', role: 'assistant', content: 'Hi', status: 'streaming',
                session_seq: 2, created_at: '2026-08-22T12:00:00Z',
              }]);
              await storageAdapter.mergeInteractionsIntoCache('s', [{
                id: 'a', role: 'assistant', content: 'Hi — complete response',
                status: 'complete', session_seq: 2,
                created_at: '2026-08-22T12:00:00Z',
              }], null);
              const row = (await db.getInteractions('s', Infinity))[0];
              return {content: row.content, status: row.status, count: await db.countInteractions('s')};
            }""",
            {"base": self.base, "scope": self._scope("stale_stream")},
        )
        self.assertEqual(
            result,
            {"content": "Hi — complete response", "status": "complete", "count": 1},
        )

    def test_tombstone_survives_reopen_after_interrupted_sync(self):
        result = self.page.evaluate(
            """async ({base, scope}) => {
              const mod = await import(base + '/ui/chat/js/storage/indexeddb.js');
              const db = new mod.SessionDB();
              db.setOwnerScope(scope);
              await db.createSession({
                id: 's', agent_id: 'a', server_revision: 4, local_revision: 7,
              });
              await db.deleteSession('s');
              db.close();
              const reopened = new mod.SessionDB();
              reopened.setOwnerScope(scope);
              return (await reopened.listSyncOutbox())[0];
            }""",
            {"base": self.base, "scope": self._scope("interrupt")},
        )
        self.assertEqual(result["operation"], "delete")
        self.assertEqual(result["base_server_revision"], 4)

    def test_transcript_age_does_not_evict_but_quota_preserves_authority(self):
        result = self.page.evaluate(
            """async ({base, scope}) => {
              const {default: db} = await import(base + '/ui/chat/js/storage/indexeddb.js');
              db.setOwnerScope(scope);
              await db.createSession({
                id: 'cached', agent_id: 'a', _authority: 'server',
                content_hash: 'hash', cache_schema_version: 1,
                cache_expires_at: '2000-01-01T00:00:00.000Z', _dirty: false,
              });
              await db.createSession({
                id: 'owned', agent_id: 'a', _authority: 'browser', _dirty: true,
              });
              const agePolicy = await db.enforceCachePolicy({maxBytes: 50000000});
              const retainedAfterAgeSweep = !!(await db.getSession('cached'));
              const policy = await db.enforceCachePolicy({maxBytes: 0});
              return {
                agePolicy, retainedAfterAgeSweep, policy,
                cached: await db.getSession('cached'),
                owned: await db.getSession('owned'),
              };
            }""",
            {"base": self.base, "scope": self._scope("quota")},
        )
        self.assertTrue(result["retainedAfterAgeSweep"])
        self.assertEqual(result["agePolicy"]["evicted"], 0)
        self.assertIsNone(result["cached"])
        self.assertIsNotNone(result["owned"])
        self.assertTrue(result["policy"]["quotaExceeded"])

    def test_stream_projection_upsert_replaces_partial_row(self):
        result = self.page.evaluate(
            """async ({base, scope}) => {
              const {SessionDB} = await import(base + '/ui/chat/js/storage/indexeddb.js');
              const db = new SessionDB();
              db.setOwnerScope(scope);
              await db.createSession({id: 's', agent_id: 'a', _authority: 'server'});
              await db.upsertCachedInteraction('s', {
                id: 'a1', role: 'assistant', content: 'partial',
                session_seq: 8, status: 'streaming',
              });
              await db.upsertCachedInteraction('s', {
                id: 'a1', role: 'assistant', content: 'complete response',
                session_seq: 8, status: 'complete',
              });
              const rows = await db.getInteractions('s', Infinity);
              return {rows, session: await db.getSession('s')};
            }""",
            {"base": self.base, "scope": self._scope("stream_projection")},
        )
        self.assertEqual(len(result["rows"]), 1)
        self.assertEqual(result["rows"][0]["content"], "complete response")
        self.assertEqual(result["rows"][0]["session_seq"], 8)
        self.assertTrue(result["session"]["cache_projection_dirty"])

    def test_session_cache_orders_strictly_by_real_activity(self):
        result = self.page.evaluate(
            """async ({base, scope}) => {
              const {SessionDB} = await import(base + '/ui/chat/js/storage/indexeddb.js');
              const db = new SessionDB();
              db.setOwnerScope(scope);
              await db.createSession({
                id: 'pin-second', agent_id: 'a', pinned: true, sort_order: 1,
                activity_at: '2026-08-21T12:00:00Z',
              });
              await db.createSession({
                id: 'pin-first', agent_id: 'a', pinned: true, sort_order: 0,
                activity_at: '2026-08-20T12:00:00Z',
              });
              await db.createSession({
                id: 'old', agent_id: 'a', pinned: false,
                activity_at: '2026-08-18T12:00:00Z',
                updated_at: '2099-01-01T00:00:00Z',
              });
              await db.createSession({
                id: 'recent', agent_id: 'a', pinned: false,
                activity_at: '2026-08-19T12:00:00Z',
                updated_at: '2020-01-01T00:00:00Z',
              });
              return (await db.listSessions()).map(row => ({
                id: row.id, sort_order: row.sort_order,
              }));
            }""",
            {"base": self.base, "scope": self._scope("session_order")},
        )
        self.assertEqual(
            result,
            [
                {"id": "pin-first", "sort_order": 0},
                {"id": "pin-second", "sort_order": 1},
                {"id": "recent", "sort_order": None},
                {"id": "old", "sort_order": None},
            ],
        )

    def test_session_cache_recent_order_uses_exact_activity_time(self):
        result = self.page.evaluate(
            """async ({base, scope}) => {
              const {SessionDB} = await import(base + '/ui/chat/js/storage/indexeddb.js');
              const db = new SessionDB();
              db.setOwnerScope(scope);
              // Exact activity timestamps determine the order, regardless of
              // how many turns each session contains.
              await db.createSession({
                id: 'one-off-newer', agent_id: 'a', pinned: false,
                activity_at: '2026-08-21T10:59:00Z', activity_count: 1,
              });
              await db.createSession({
                id: 'steady', agent_id: 'a', pinned: false,
                activity_at: '2026-08-21T06:01:00Z', activity_count: 31,
              });
              // The most recently active session stays first.
              await db.createSession({
                id: 'new-bucket', agent_id: 'a', pinned: false,
                activity_at: '2026-08-21T12:01:00Z', activity_count: 1,
              });
              return (await db.listSessions()).map(row => row.id);
            }""",
            {"base": self.base, "scope": self._scope("exact_recent_order")},
        )
        self.assertEqual(result, ["new-bucket", "one-off-newer", "steady"])

    def test_hybrid_warm_cache_reads_share_one_background_list_refresh(self):
        result = self.page.evaluate(
            """async ({base, scope}) => {
              let refreshCount = 0;
              let releaseRefresh;
              let markRefreshStarted;
              const refreshStarted = new Promise(resolve => { markRefreshStarted = resolve; });
              window.fetch = async url => {
                const value = String(url);
                if (value.includes('/api/v1/browser/routing')) {
                  return new Response(JSON.stringify({
                    routing: {session_data: 'server', session_cache: 'browser'},
                    capabilities: {browser_authority: false, browser_session_cache: true},
                    cache_scope: scope,
                    cache_policy: {schema_version: 2, max_bytes: 50000000},
                  }), {status: 200, headers: {'Content-Type': 'application/json'}});
                }
                if (value.includes('/api/v1/db/sessions')) {
                  refreshCount += 1;
                  markRefreshStarted();
                  await new Promise(resolve => { releaseRefresh = resolve; });
                  return new Response(JSON.stringify({sessions: [{
                    id: 'warm', agent_id: 'agent', title: 'Warm session',
                    created_at: '2026-08-21T12:00:00Z',
                    updated_at: '2026-08-21T12:00:00Z',
                    activity_at: '2026-08-21T12:00:00Z',
                    revision: 2, content_hash: 'server-hash',
                  }]}), {status: 200, headers: {'Content-Type': 'application/json'}});
                }
                throw new Error('unexpected fetch ' + value);
              };

              const {storageAdapter} = await import(
                base + '/ui/chat/js/storage/storage-adapter.js'
              );
              const {default: db} = await import(
                base + '/ui/chat/js/storage/indexeddb.js'
              );
              await storageAdapter.autoSelectMode('agent');
              await db.createSession({
                id: 'warm', agent_id: 'agent', title: 'Cached session',
                created_at: '2026-08-21T12:00:00Z',
                updated_at: '2026-08-21T12:00:00Z',
                activity_at: '2026-08-21T12:00:00Z',
                authority_revision: 1, content_hash: 'cached-hash',
                cache_schema_version: 2, _authority: 'server',
              });

              let deltaCount = 0;
              let resolveDelta;
              const delta = new Promise(resolve => { resolveDelta = resolve; });
              const onDelta = () => {
                deltaCount += 1;
                resolveDelta();
              };
              window.addEventListener('sessions-delta', onDelta);
              try {
                const reads = Promise.all([
                  storageAdapter.listSessions('single-flight-user'),
                  storageAdapter.listSessions('single-flight-user'),
                ]);
                const cached = await reads;
                await refreshStarted;
                releaseRefresh();
                await delta;
                await new Promise(resolve => setTimeout(resolve, 0));
                return {
                  mode: storageAdapter.mode,
                  refreshCount,
                  deltaCount,
                  titles: cached.map(rows => rows[0] && rows[0].title),
                };
              } finally {
                window.removeEventListener('sessions-delta', onDelta);
              }
            }""",
            {"base": self.base, "scope": self._scope("hybrid_single_flight")},
        )
        self.assertEqual(result["mode"], "hybrid")
        self.assertEqual(result["refreshCount"], 1)
        self.assertEqual(result["deltaCount"], 1)
        self.assertEqual(result["titles"], ["Cached session", "Cached session"])

    def test_hybrid_reorder_updates_indexeddb_before_server_ack(self):
        result = self.page.evaluate(
            """async ({base, scope}) => {
              let releaseServer;
              let notifyStarted;
              const started = new Promise(resolve => { notifyStarted = resolve; });
              let submitted = null;
              window.fetch = async (url, options = {}) => {
                const value = String(url);
                if (value.includes('/api/v1/browser/routing')) {
                  return new Response(JSON.stringify({
                    routing: {session_data: 'server', session_cache: 'browser'},
                    capabilities: {browser_authority: false, browser_session_cache: true},
                    cache_scope: scope,
                    cache_policy: {schema_version: 2, max_bytes: 50000000},
                  }), {status: 200, headers: {'Content-Type': 'application/json'}});
                }
                if (value.includes('/api/v1/db/sessions/reorder')) {
                  submitted = JSON.parse(options.body);
                  notifyStarted();
                  await new Promise(resolve => { releaseServer = resolve; });
                  return new Response(JSON.stringify({success: true, updated: 2}), {
                    status: 200, headers: {'Content-Type': 'application/json'},
                  });
                }
                throw new Error('unexpected fetch ' + value);
              };
              const {storageAdapter} = await import(
                base + '/ui/chat/js/storage/storage-adapter.js'
              );
              const {default: db} = await import(
                base + '/ui/chat/js/storage/indexeddb.js'
              );
              await storageAdapter.autoSelectMode('agent');
              await db.createSession({
                id: 'one', agent_id: 'a', pinned: true, sort_order: 0,
                updated_at: '2026-01-01T00:00:00Z',
                _authority: 'server', cache_schema_version: 2, content_hash: 'one-hash',
              });
              await db.createSession({
                id: 'two', agent_id: 'a', pinned: true, sort_order: 1,
                updated_at: '2026-01-02T00:00:00Z',
                _authority: 'server', cache_schema_version: 2, content_hash: 'two-hash',
              });
              const pending = storageAdapter.reorderSessions('authenticated', ['two', 'one']);
              await started;
              const beforeAck = await db.listSessions();
              releaseServer();
              const response = await pending;
              const shaped = await storageAdapter.listSessions('authenticated');
              return {
                mode: storageAdapter.mode,
                beforeAck: beforeAck.map(row => ({
                  id: row.id, sort_order: row.sort_order, updated_at: row.updated_at,
                })),
                submitted,
                response,
                shaped: shaped.map(row => ({id: row.id, sort_order: row.sort_order})),
              };
            }""",
            {"base": self.base, "scope": self._scope("hybrid_reorder")},
        )
        self.assertEqual(result["mode"], "hybrid")
        self.assertEqual([row["id"] for row in result["beforeAck"]], ["two", "one"])
        self.assertEqual([row["sort_order"] for row in result["beforeAck"]], [0, 1])
        self.assertEqual(
            [row["updated_at"] for row in result["beforeAck"]],
            ["2026-01-02T00:00:00Z", "2026-01-01T00:00:00Z"],
        )
        self.assertEqual(result["submitted"]["order"], ["two", "one"])
        self.assertEqual(result["response"], {"success": True, "updated": 2})
        self.assertEqual(result["shaped"][0], {"id": "two", "sort_order": 0})

    def test_hybrid_reorder_rolls_back_when_server_and_reconcile_fail(self):
        result = self.page.evaluate(
            """async ({base, scope}) => {
              window.fetch = async url => {
                const value = String(url);
                if (value.includes('/api/v1/browser/routing')) {
                  return new Response(JSON.stringify({
                    routing: {session_data: 'server', session_cache: 'browser'},
                    capabilities: {browser_authority: false, browser_session_cache: true},
                    cache_scope: scope,
                    cache_policy: {schema_version: 2, max_bytes: 50000000},
                  }), {status: 200, headers: {'Content-Type': 'application/json'}});
                }
                if (value.includes('/api/v1/db/sessions/reorder')) {
                  return new Response('{}', {status: 503});
                }
                if (value.includes('/api/v1/db/sessions')) {
                  throw new Error('offline during reconcile');
                }
                throw new Error('unexpected fetch ' + value);
              };
              const {storageAdapter} = await import(
                base + '/ui/chat/js/storage/storage-adapter.js'
              );
              const {default: db} = await import(
                base + '/ui/chat/js/storage/indexeddb.js'
              );
              await storageAdapter.autoSelectMode('agent');
              await db.createSession({id: 'one', agent_id: 'a', pinned: true, sort_order: 0});
              await db.createSession({id: 'two', agent_id: 'a', pinned: true, sort_order: 1});
              let error = '';
              try {
                await storageAdapter.reorderSessions('authenticated', ['two', 'one']);
              } catch (e) {
                error = e.message;
              }
              return {
                error,
                rows: (await db.listSessions()).map(row => ({
                  id: row.id, sort_order: row.sort_order,
                })),
              };
            }""",
            {"base": self.base, "scope": self._scope("hybrid_reorder_rollback")},
        )
        self.assertIn("HTTP 503", result["error"])
        self.assertEqual(
            result["rows"],
            [{"id": "one", "sort_order": 0}, {"id": "two", "sort_order": 1}],
        )

    def test_sessions_page_prime_paints_dropdown_without_network_wait(self):
        result = self.page.evaluate(
            """async ({base, userId}) => {
              const {app} = await import(base + '/ui/shared/js/state.js');
              const list = await import(
                base + '/ui/chat/elements/session-dropdown/list.js'
              );
              const {storageAdapter} = await import(
                base + '/ui/chat/js/storage/storage-adapter.js'
              );
              storageAdapter.switchToNormal();
              const previous = {
                userId: app.currentUserId,
                sessionId: app.currentSessionId,
                fetch: window.fetch,
              };
              app.currentUserId = userId;
              app.currentSessionId = 'warm-session';
              let menu = document.getElementById('session-dropdown-menu');
              let createdMenu = false;
              if (!menu) {
                menu = document.createElement('div');
                menu.id = 'session-dropdown-menu';
                document.body.appendChild(menu);
                createdMenu = true;
              }
              list.primeSessionMetadataCache(userId, [{
                id: 'warm-session',
                title: 'Already on the Sessions page',
                pinned: true,
                sort_order: 0,
                activity_at: '2026-08-21T12:00:00Z',
              }]);
              let fetchCalls = 0;
              window.fetch = async () => {
                fetchCalls += 1;
                await new Promise(() => {});
              };
              const started = performance.now();
              const servedFromCache = await Promise.race([
                list.populateSessionSelect(userId, {preferCache: true}),
                new Promise(resolve => setTimeout(() => resolve('timeout'), 250)),
              ]);
              const elapsed = performance.now() - started;
              const text = menu.textContent || '';
              if (createdMenu) menu.remove();
              window.fetch = previous.fetch;
              app.currentUserId = previous.userId;
              app.currentSessionId = previous.sessionId;
              return {servedFromCache, elapsed, fetchCalls, text};
            }""",
            {"base": self.base, "userId": self._scope("page_prime")},
        )
        self.assertIs(result["servedFromCache"], True)
        self.assertLess(result["elapsed"], 250)
        self.assertEqual(result["fetchCalls"], 0)
        self.assertIn("Already on the Sessions page", result["text"])

    def test_corrupt_or_stale_cache_manifest_fails_closed_to_server(self):
        result = self.page.evaluate(
            """async ({base, scope}) => {
              let serverReads = 0;
              window.fetch = async url => {
                const value = String(url);
                if (value.includes('/api/v1/browser/routing')) {
                  return new Response(JSON.stringify({
                    routing: {session_data: 'server', session_cache: 'browser'},
                    capabilities: {
                      browser_authority: false, browser_session_cache: true,
                    },
                    cache_scope: scope,
                    cache_policy: {schema_version: 1, max_bytes: 50000000},
                  }), {status: 200, headers: {'Content-Type': 'application/json'}});
                }
                if (value.includes('/api/v1/db/sessions')) {
                  serverReads += 1;
                  return new Response(JSON.stringify({sessions: []}), {
                    status: 200, headers: {'Content-Type': 'application/json'},
                  });
                }
                throw new Error('unexpected fetch ' + value);
              };
              const {storageAdapter} = await import(
                base + '/ui/chat/js/storage/storage-adapter.js'
              );
              const {default: db} = await import(
                base + '/ui/chat/js/storage/indexeddb.js'
              );
              await storageAdapter.autoSelectMode('agent');
              await db.createSession({
                id: 'corrupt', agent_id: 'a', _authority: 'server',
                cache_schema_version: 999, content_hash: '',
                cache_expires_at: '2999-01-01T00:00:00.000Z',
              });
              const rows = await storageAdapter.listSessions('authenticated');
              return {mode: storageAdapter.mode, serverReads, rows};
            }""",
            {"base": self.base, "scope": self._scope("corrupt")},
        )
        self.assertEqual(result["mode"], "hybrid")
        self.assertEqual(result["serverReads"], 1)
        self.assertEqual(result["rows"], [])

    def test_partial_sync_marks_only_acknowledged_revision_clean(self):
        result = self.page.evaluate(
            """async ({base, scope}) => {
              const {default: db} = await import(base + '/ui/chat/js/storage/indexeddb.js');
              const {syncEngine} = await import(base + '/ui/chat/js/storage/sync.js');
              db.setOwnerScope(scope);
              await db.createSession({id: 'ok', agent_id: 'a', _dirty: true});
              await db.createSession({id: 'conflict', agent_id: 'a', _dirty: true});
              await syncEngine.markDirty('ok');
              await syncEngine.markDirty('conflict');
              window.fetch = async (_url, options) => {
                const body = JSON.parse(options.body);
                return new Response(JSON.stringify({
                  results: body.mutations.map(row => row.session_id === 'ok'
                    ? {
                        session_id: row.session_id,
                        mutation_id: row.mutation_id,
                        status: 'applied',
                        server_revision: 1,
                        content_hash: 'server-hash',
                        client_revision: row.client_revision,
                      }
                    : {
                        session_id: row.session_id,
                        mutation_id: row.mutation_id,
                        status: 'conflict',
                        server_revision: 2,
                        content_hash: 'newer',
                        client_revision: row.client_revision,
                        error: 'stale base revision',
                      }),
                }), {status: 200, headers: {'Content-Type': 'application/json'}});
              };
              const syncResult = await syncEngine.flush('authenticated');
              return {
                syncResult,
                ok: await db.getSession('ok'),
                conflict: await db.getSession('conflict'),
              };
            }""",
            {"base": self.base, "scope": self._scope("partial")},
        )
        self.assertFalse(result["syncResult"]["ok"])
        self.assertFalse(result["ok"]["_dirty"])
        self.assertTrue(result["conflict"]["_dirty"])
        self.assertEqual(result["conflict"]["_sync_error"], "stale base revision")

    def test_hybrid_transcript_serves_only_revision_validated_cache_hit(self):
        result = self.page.evaluate(
            """async ({base, scope}) => {
              let validations = 0;
              let fullReads = 0;
              window.fetch = async url => {
                const value = String(url);
                if (value.includes('/api/v1/browser/routing')) {
                  return new Response(JSON.stringify({
                    routing: {session_data: 'server', session_cache: 'browser'},
                    capabilities: {
                      browser_authority: false, browser_session_cache: true,
                    },
                    cache_scope: scope,
                    cache_policy: {
                      schema_version: 2, metadata_ttl_seconds: 300,
                      transcript_ttl_seconds: 900, max_bytes: 50000000,
                    },
                  }), {status: 200, headers: {'Content-Type': 'application/json'}});
                }
                if (value.includes('/api/v1/db/session-messages')) {
                  if (value.includes('manifest_only=true')) {
                    validations += 1;
                    return new Response(JSON.stringify({
                      messages: [], not_modified: true, cache_status: 'validated',
                      manifest: {
                        authority_revision: 2, content_hash: 'server-hash',
                        cache_schema_version: 2, interaction_count: 2,
                      },
                    }), {status: 200, headers: {'Content-Type': 'application/json'}});
                  }
                  fullReads += 1;
                  return new Response(JSON.stringify({
                    messages: [
                      {id: 'u', session_id: 'validated', role: 'user', content: 'hello', session_seq: 1},
                      {id: 'a', session_id: 'validated', role: 'assistant', content: 'hi', session_seq: 2},
                    ],
                    manifest: {
                      authority_revision: 2, content_hash: 'server-hash',
                      cache_schema_version: 2, interaction_count: 2,
                    },
                    turn_boundary_complete: true,
                    has_more: false,
                    has_newer: false,
                  }), {status: 200, headers: {'Content-Type': 'application/json'}});
                }
                throw new Error('unexpected fetch ' + value);
              };
              const {storageAdapter} = await import(
                base + '/ui/chat/js/storage/storage-adapter.js'
              );
              const {default: db} = await import(
                base + '/ui/chat/js/storage/indexeddb.js'
              );
              await storageAdapter.autoSelectMode('agent');
              await db.createSession({
                id: 'validated', agent_id: 'a', _authority: 'server',
                authority_revision: 2, content_hash: 'server-hash',
                cache_schema_version: 2, interaction_count: 2,
                cache_expires_at: '2999-01-01T00:00:00.000Z', _dirty: false,
              });
              await storageAdapter.getInteractions('validated', 20);
              const data = await storageAdapter.getInteractions('validated', 20);
              const validation = await storageAdapter.revalidateTranscript('validated');
              await db.updateInteraction('u', {content: 'tampered'});
              const recovered = await storageAdapter.getInteractions('validated', 20);
              return {
                validations, fullReads, cacheStatus: data.cache_status,
                validationNotModified: validation.not_modified,
                validatedProjection: validation.messages.map(row => row.id),
                validatedProjectionComplete: validation.cache_projection.complete,
                messages: data.messages.map(row => row.content),
                recovered: recovered.messages.map(row => row.content),
              };
            }""",
            {"base": self.base, "scope": self._scope("validated")},
        )
        self.assertEqual(result["validations"], 1)
        self.assertEqual(result["fullReads"], 2)
        self.assertEqual(result["cacheStatus"], "cached-hit")
        self.assertTrue(result["validationNotModified"])
        self.assertEqual(result["validatedProjection"], ["u", "a"])
        self.assertTrue(result["validatedProjectionComplete"])
        self.assertEqual(result["messages"], ["hello", "hi"])
        self.assertEqual(result["recovered"], ["hello", "hi"])

    def test_lifecycle_does_not_expire_cached_views_by_age(self):
        result = self.page.evaluate(
            """async ({base, scope}) => {
              const {default: db} = await import(base + '/ui/chat/js/storage/indexeddb.js');
              db.setOwnerScope(scope);
              db.configureLifecyclePolicy({
                metadata_ttl_seconds: 1,
                run_state_ttl_seconds: 1,
                generated_html_ttl_seconds: 1,
              });
              const raw = await db.ready();
              await new Promise((resolve, reject) => {
                const tx = raw.transaction(
                  ['agent_config', 'session_runs', 'genui_html', 'app_cache'],
                  'readwrite',
                );
                tx.objectStore('agent_config').put({
                  id: 'old-agent', config_hash: 'old', cached_at: '2000-01-01T00:00:00Z',
                  expires_at: '2000-01-02T00:00:00Z', label: 'saved agent',
                });
                tx.objectStore('session_runs').put({
                  id: 'old-run', session_id: 'old-session', status: 'complete',
                  started_at: '2000-01-01T00:00:00Z', finished_at: '2000-01-01T00:01:00Z',
                  expires_at: '2000-01-02T00:00:00Z',
                });
                tx.objectStore('genui_html').put({
                  slug: 'old-page', html: '<main>saved page</main>',
                  saved_at: '2000-01-01T00:00:00Z', expires_at: '2000-01-02T00:00:00Z',
                });
                tx.objectStore('app_cache').put({
                  key: 'old-view', value: {label: 'saved view'}, expires_at: 2,
                  updated_at: 1, size: 10,
                });
                tx.oncomplete = resolve;
                tx.onerror = () => reject(tx.error);
                tx.onabort = () => reject(tx.error);
              });
              const cleanup = await db.enforceLifecyclePolicy({now: Date.now()});
              const appCache = await new Promise((resolve, reject) => {
                const tx = raw.transaction('app_cache', 'readonly');
                const req = tx.objectStore('app_cache').get('old-view');
                req.onsuccess = () => resolve(req.result || null);
                req.onerror = () => reject(req.error);
              });
              return {
                cleanup,
                agent: await db.getCachedAgentConfig('old-agent'),
                runs: await db.listRuns('old-session'),
                html: await db.getGenuiHtml('old-page'),
                appCache,
              };
            }""",
            {"base": self.base, "scope": self._scope("no-expiry")},
        )
        self.assertEqual(result["cleanup"]["rows_removed"], 0)
        self.assertEqual(result["agent"]["label"], "saved agent")
        self.assertEqual(result["runs"][0]["id"], "old-run")
        self.assertEqual(result["html"], "<main>saved page</main>")
        self.assertEqual(result["appCache"]["value"]["label"], "saved view")


if __name__ == "__main__":
    unittest.main()


> **V2 integration note:** This widget is an existing integration and still
> uses legacy `/api/v1/chat` transport and session-tail behavior. Its output
> must be treated as an integration surface pending migration through
> `app/chat/api.py` and the V2 publication service. Do not add a second public
> transcript authority here. Once migrated, the widget must consume the same
> typed public items and cursor/replay contract as the main panel.
# Chat widget (`ui/chat-widget/`)

A **floating mini chat** any page can spawn to run a *task-scoped* agent session
without disturbing the main chat side-panel. Consumers:
- the git-control star (⭐) in the file editor sidebar (`ui/shared/js/files-git.js`),
  which asks the agent to commit & push in a widget instead of hijacking the main panel;
- the **ability-table search/chat pill** (`buildAbilitySearchPill` in
  `ui/shared/js/dom-utils.js`), which on send opens a widget talking to WebAgent
  the manager via the exported helper **`spawnWebagentAbilityChat({ text, attachmentIds })`**.

## Files

| File | Purpose |
|------|---------|
| `js/chat-widget.js` | The factory — `createChatWidget(options)` returns a widget instance. All DOM is built in JS; state is per-instance, so several widgets can run at once. |
| `chat-widget.css` | Styling for the fixed bottom-right layer, the card, mini bubbles, tool-call group, and the minimized "Done" chip. Design-system variables only (dark + light). Linked from root `index.html`. |

## How it works

- The widget generates a **fresh UUID session id** (server creates the session
  implicitly on the first `POST /api/v1/chat/send`) and claims that session's
  live events via `registerSessionSubscriber(sessionId, handler)` in
  `ui/shared/js/agentWs.js` — the one global WebSocket then routes the
  session's events to the widget instead of dropping them as "foreign".
  **One handler per session** (last registration wins); each widget owns its
  own session so this never collides in practice.
- Streams text into mini bubbles (markdown on finalize via the shared
  `_fillAgentBubble` from `ui/chat/js/chat-bubble.js`); tool calls
  render as a collapsible group using the shared `buildToolRow` from
  `ui/shared/js/chat-activity.js`.
- **Reliable updates — WS is smooth, the DB is durable.** Exactly like the main
  panel (`ui/chat/js/chat-reconcile.js`), the WebSocket only delivers
  when the browser socket and the running agent share one server process. When
  they don't (dev port-stacking, prod multi-worker) or the socket goes briefly
  silent, each widget polls `GET /api/v1/db/session-tail` — **gated on WS
  silence** (`WS_SILENCE_MS`), so zero overhead while the socket is live — and
  renders the same bubbles from the DB tail. This is what makes the view update
  reliably right after a send and after Stop/Continue, and lets it finish a turn
  even if the live `response` event is lost. Both paths converge on the same
  per-key bubbles (idempotent SET), so there are no duplicates.
- Transcript messages use the panel's bubble renderer and flex-column sizing;
  progress/responses and tool calls use its `.bubble-tool-calls` disclosure and
  shared row builders. The widget owns only session routing and frame chrome.
- The composer is rendered by the shared chat-pill layout from `chat_ui.json`.
  Stop (`POST /api/v1/chat/interrupt`) and Continue (sends `"continue"`) are
  the same in-pill controls as the panel and move with its configured row/zone.
- **Draggable + resizable.** Drag the header to move the card; drag any
  edge/corner to resize it (min 260×200). The first such gesture switches the
  card to floating (fixed) positioning, detaching it from the docked stack.
- On the final response: green **Done** state → collapses to a compact chip
  after ~1.5s (cancelled if the pointer is over the card; click the chip to
  re-expand). **Close (✕) ≠ Stop**: close only detaches the widget (the run
  continues server-side; the session stays in the main panel's session list).
- The docked layer shifts left when the main chat panel is open so they never
  overlap.
- **Survives page navigation (by design + watchdog).** The floating layer is
  attached to `<body>`, OUTSIDE every page (pages are just shown/hidden under
  `#stage`), so switching tabs can never remove or hide a running widget. A
  small `MutationObserver` watchdog hardens this further: if anything rebuilds
  `<body>` and removes the layer while a widget is still inside, the layer is
  re-attached as the top-most element. The guard stays dormant during normal
  use and never fights a real close — `close()`/`_maybeRemoveLayer` empty the
  layer first, so an empty removal is left alone.
- No persistence across page reloads (deliberate) — a running task continues
  server-side and is reachable from the session dropdown. Widgets capture the
  user id at creation; close them on account switch.

## Factory options

| Option | Meaning |
|--------|---------|
| `title` | Header label (replaced by the auto-generated session title when it arrives). |
| `iconName` | Lucide icon name for the header. |
| `agentId` / `ensureAgent` | A pre-resolved agent id, **or** an async function returning one (do ability setup in there). |
| `initialMessage` | Sent automatically once the agent is resolved. |
| `initialAttachmentIds` | `attachment_ids` to ride the **first** message only (the file-only first message — no text — is still a valid send). |
| `executionMode` | `'ask'` (default) / `'plan'` / `'auto'` — passed per send (legacy `'read'`/`'write'` still accepted by the backend). |
| `sessionId` / `initialSessionSeq` | Attach to an existing session and start DB reconciliation after a known snapshot sequence, so copied history can remain context-only. |
| `onDone(finalText)` | Fires on each final response (e.g. refresh a panel). |
| `onClose()` | Fires when the user closes the widget. |

Instance methods: `open()`, `close()`, `send(text)`, `interrupt()`,
`minimize()`, `restore()`; getters `sessionId`, `status`, `el`.

## Usage sketch (logic)

Build your task message → `createChatWidget({ title, ensureAgent, initialMessage, onDone })`
→ `.open()`. That's it — no session juggling, no touching `app.currentSessionId`.

---

# Chat launcher (`js/chat-launcher.js`)

The **shared launcher component**: a floating (or inline) icon button that opens
a chat widget, with up to four configurable corner buttons on hover. It's the
engine behind the global WebAgent launcher (`js/webagent-launcher.js` is now a
thin wrapper that delegates here) and the way any page gets its OWN chat
launcher with a distinct icon, linked agent, corner buttons, and widget options.

## Factory API

```js
import { createChatLauncher } from './chat-launcher.js';

const launcher = createChatLauncher({
  mountEl: document.body,       // where the button is appended
  position: 'fixed',            // 'fixed' floating FAB | 'inline' in-page
  icon: 'bot',                  // 'bot' (animated) | Lucide name | raw SVG/HTML
  iconSize: 27,
  ariaLabel: 'Open WebAgent chat',
  label: '', showLabel: false,  // optional text label chip beside the icon
  agentId: null,                // direct agent id, OR
  ensureAgent: null,            // async () => agentId (caller does ability setup), OR
  resolveAgent: null,           // async () => agent record {id, name, icon}
  cornerButtons: {...},         // same shape as chat_ui.json launcher.corner_buttons;
                                //   null → default set, false → none
  cornerButtonsProvider: null,  // async () => corner config (fetched lazily, like the
                                //   old ui-config profile read)
  cornerActions: {},            // custom corner types: { myType: (btn, ctx) => void }
  widget: {...},                // any createChatWidget options (session contract,
                                //   transformMessage, onDone…)
  draggable: true,              // default true for 'fixed'
  storageKey: null,             // localStorage key for the saved position
  hoverDelayMs: 200,
  iconMagnet: true,             // pointer-attracted icon (fixed launchers)
  elementPickup: false,         // wire the element-pickup corner action + app hooks
  onOpen: null, onClose: null,
});
launcher.open();  launcher.close();  launcher.destroy();  launcher.setConfig({...});
```

Built-in corner action types: `sessions`, `new_session`, `close_all`,
`open_all`, `element_pickup` (the last only does anything when
`elementPickup: true`). Custom types override built-ins per-launcher.

## `<chat-launcher>` custom element

The factory also registers a `chat-launcher` element (in the MAIN document —
the widget layer lives on `<body>`, so don't put it inside a Gen UI shadow).
Attributes: `icon`, `agent` (agent id), `label`, `position`, `draggable`,
`aria-label`, `storage-key`; or `src` = inline JSON **or** a URL to a JSON
config file; or an inline `<script type="application/json">` child. The element
exposes `.launcher` and fires `chat-launcher-ready` once mounted.

## Per-page configuration

Two drop-in ways to give a page its own launcher with NO per-page code:

**1. Gen UI pages — `widget.json` in the page folder.** Drop
`data/user_data/<uid>/genui/<slug>/widget.json` (or PATCH
`/api/v1/genui/{slug}/widget` — the design agent can do this via the API). The
file holds the same `createChatLauncher` options. It's served to the Gen UI tab
baked into the page as `window.__GENUI_WIDGET`; `genui.js` mounts the launcher
in the main document when the page opens and destroys it on page switch. Agent
resolution: `agent_id` in the file → the page's owning `agent_id` → the default
WebAgent. The page's existing `session_config` is applied to the launcher's
widget automatically (same session targeting as the page's chat buttons).

```json
// data/user_data/<uid>/genui/<slug>/widget.json
{
  "icon": "sparkles",
  "position": "fixed",
  "ariaLabel": "Ask about this page",
  "agent_id": "optional-agent-id",
  "cornerButtons": {
    "enabled": true, "hover_delay_ms": 200,
    "top_left":     { "enabled": true, "type": "sessions",    "icon": "list",       "tooltip": "Show sessions" },
    "top_right":    { "enabled": true, "type": "new_session", "icon": "plus",       "tooltip": "New session" },
    "bottom_left":  { "enabled": false, "type": "element_pickup", "icon": "crosshair", "tooltip": "Element pickup" },
    "bottom_right": { "enabled": true, "type": "open_all",    "icon": "maximize-2", "tooltip": "Open all minimized" }
  },
  "widget": { "title": "Ask the agent", "iconName": "bot" }
}
```

PATCH `/api/v1/genui/{slug}/widget` with `{"widget": {…}}` to set it, or `{}` to
remove the page's launcher. GET `/api/v1/genui/{slug}/widget` to read it.

**2. Catalog pages — a `widget` block in the page's `page.json`.** Any
main-panel / admin-tools page descriptor may carry the same options:

```json
{ "id": "agents", "label": "Agents", "icon": "bot",
  "widget": { "icon": "bot", "position": "fixed", "cornerButtons": { "enabled": false } } }
```

The shell (`ui/shared/js/tabs.js`) mounts the launcher on tab activate and
destroys it on deactivate, so each page gets its own — no per-page code.

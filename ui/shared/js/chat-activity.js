'use strict';

import { app } from './state.js';
import { chatUiFlag } from './app-prompts.js';
import { apiPath } from './config.js';
import { authHeaders } from './left-login.js';
import { copyText } from './clipboard.js';
import { renderVaultCredentialCard } from '../../vault-credential/vault-credential-card.js';
import { renderSshConnectionCard } from '../../ssh-control/ssh-connection-card.js';
import { fetchUrl } from './media-cache.js';

const _SECURE_CARD_HANDOFF_TOOLS = new Set(['ssh_request_connection']);

// Secure credential/connection tool results carry a `ui` discriminator. Pop the
// corresponding browser-to-vault card without routing any entered secret back
// through the agent. Live events only, so history reloads never reopen stale cards.
function _maybeShowSecureCard(event) {
  if (!event || event.error) return false;
  try {
    const payload = typeof event.result === 'string' ? JSON.parse(event.result) : event.result;
    if (event.tool === 'request_credential' && payload && payload.ui === 'vault_credential_form' && payload.key_id) {
      renderVaultCredentialCard(payload);
      return true;
    } else if (event.tool === 'ssh_request_connection' && payload && payload.ui === 'ssh_connection_form') {
      renderSshConnectionCard(payload);
      return true;
    }
  } catch (_) { /* not a card payload — ignore */ }
  return false;
}

function _maybeShowChatComponent(event) {
  if (!event || event.error) return;
  try {
    const payload = typeof event.result === 'string' ? JSON.parse(event.result) : event.result;
    if (payload && payload.ui === 'chat_component' && payload.component && app.presentChatComponent) {
      app.presentChatComponent(payload.component).catch(() => {});
    }
  } catch (_) { /* not a component payload */ }
}

// ── Chat activity ("thinking") indicator ───────────────────────────────────
// While the agent is working on the CURRENT session's turn, the web-chat pill
// glows ("thinking") and a small bar above it ticks through each interaction as
// a short note — e.g. "Toolcall get_time", "Searching memory", "Writing reply…".
//
// The bar is CLICKABLE: it expands a panel listing every tool call made this
// turn, each row independently expandable to show its full arguments + result.
// Tool calls accumulate across all steps of the turn. When the turn ends, the
// glow + pulse stop; if the turn made any tool calls the bar stays in a resting
// state ("N tool calls") so you can still open it and inspect — until the next
// turn resets it or you switch sessions.
//
// Driven entirely by the per-user WebSocket event stream (see agentWs.js, which
// forwards every current-session event to app._chatActivityHandler). Decoupled
// from the chat bubble pipeline: chat.js just calls app.chatActivityStart()/
// Stop() for instant feedback on send + HTTP-error paths that never produce a
// WebSocket system event.

let rootEl = null;   // #chat-activity (container)
let pillEl = null;   // #chat-input-row (the .chat-pill — glows)
let barEl = null;    // #chat-activity-bar (clickable header chip)
let textEl = null;   // .chat-activity-text (the ticking note)
let panelEl = null;  // #chat-activity-panel (tool-call list)

// ── Token bar elements ──
let tokenBarEl = null;   // #chat-token-bar
let tokensInEl = null;   // #chat-tokens-in
let tokensOutEl = null;  // #chat-tokens-out
let tokenSpinnerEl = null; // #chat-token-spinner
let footerLeftEl = null;  // #chat-pill-stats — click target for the context panel
let modelBtnEl = null;   // #chat-model-btn — dedicated "switch model" button below the pill
let modelCtxEl = null;   // #chat-model-ctx (live context tokens / model's max limit)
let costEl = null;       // #chat-cost (session cost so far)
let _modelListPanelEl = null; // model list panel element (hoisted for cross-function access)
let _modelListPanelList = null;
let _modelListPanelEffort = null;
let _contextTokens = 0;  // most recent provider-reported prompt tokens sent this session
let _contextRevision = 0; // guards a slow ledger fetch from overwriting a newer live event
// (exact per-llm-call input_tokens — the ACTUAL context sent to the provider,
// so it reflects compaction: after a fold the next call reports the reduced
// prompt and this adopts it. Not a monotonic high-water mark.)
let _modelContextLimit = null; // model's context window limit (from current-model-info)
let _costInput = null;   // active model's USD price per 1M input tokens (from current-model-info)
let _costOutput = null;  // active model's USD price per 1M output tokens (from current-model-info)
// Alternate-engine agents (e.g. Local Claude Code) don't bill on the app's model,
// so the session cost is meaningless for them — keep the token/ctx counters but
// hide the price chip. Set from current-model-info's `engine` field.
let _altEngine = false;  // true when the active agent runs on a non-default engine
let _altEngineId = '';   // which engine ('claude_code' | 'codex' | …) — '' when native
let cumulativeIn = 0;
let cumulativeOut = 0;
// LocalStorage cache key for token counters — survives page refreshes.
// Format: JSON {sessionId, in, out} — only shown when sessionId matches.
const _TOKEN_CACHE_KEY = 'chat_token_counters';

function _saveTokenCache() {
  const sid = app.currentSessionId || '';
  if (!sid) return;
  try {
    localStorage.setItem(_TOKEN_CACHE_KEY, JSON.stringify({
      sessionId: sid,
      in: cumulativeIn,
      out: cumulativeOut,
      ts: Date.now(),
    }));
  } catch (_) { /* quota exceeded — non-critical */ }
}

function _loadTokenCache() {
  const sid = app.currentSessionId || '';
  if (!sid) return;
  try {
    const raw = localStorage.getItem(_TOKEN_CACHE_KEY);
    if (!raw) return;
    const cached = JSON.parse(raw);
    if (cached && cached.sessionId === sid && (cached.in > 0 || cached.out > 0)) {
      cumulativeIn = cached.in;
      cumulativeOut = cached.out;
      if (tokensInEl) tokensInEl.textContent = cumulativeIn.toLocaleString();
      if (tokensOutEl) tokensOutEl.textContent = cumulativeOut.toLocaleString();
      _updateTokenBar();
    }
  } catch (_) { /* corrupt cache — ignore */ }
}
// Running house-credit total — the sum of each call's immutable credit_units.
// Never recompute earlier calls when a model or tier schedule changes.
// Reconciled against /session-cost on session load.
let _sessionCreditUnits = 0;
let _sessionHasUnknownPriced = false;  // any call ran on a model with no published price → show n/a, not $0
let _streamCharCount = 0;     // chars streamed in current ongoing LLM call
let _pendingOutEstimate = 0;  // estimated output tokens for current streaming call
let _pendingCtxEstimate = 0;  // estimated ctx tokens during thinking ramp (replaced by real data)
let _ctxComposition = null;   // newest context breakdown (live event or GET) for the panel's bar
let _thinkingRamp = null;     // interval handle for the pre-stream thinking ramp

let active = false;     // a turn is in progress
let resting = false;    // turn ended but tool calls remain to inspect
let expanded = false;   // panel open
let currentNote = '';
let _noteSessionId = '';  // session the current note belongs to — the transcript
                          // mirror must never paint another session's progress
let _noteTurnId = '';     // durable user-turn id for the mirrored run bubble
let clearTimer = null;  // delayed text-clear after a no-tools fade-out
let endTimer = null;    // delayed stop() after a system Error/Stopped note
let watchdog = null;    // safety auto-stop if a turn never reports completion

// POST-TURN HOUSEKEEPING SETTLE ──────────────────────────────────────────────
// A turn's system event (`response`/`interrupted`/`error`) is NOT the last
// thing the backend emits: fire-and-forget background steps run AFTER it — the
// memory upsert (memory_save_start/_end), turn hooks, the optimizer, etc. Each
// emits a `*_start` note that re-lights the bar, but the bar's only general way
// to clear an active note is a system event or the 3-min watchdog — so a
// post-turn `*_start` with no recognised system event would hang the chip (e.g.
// "Saving memory") for three minutes. `_turnEnded` marks that we're past the
// turn's system event; while set, a re-light uses the short backstop below
// and any matching `*_end`/completion step clears the bar promptly. Reset at the
// next real turn boundary (user_message / new turn / session switch).
let _turnEnded = false;
// Backstop for a post-turn re-light whose completion event never arrives (the
// save errored before emitting `*_end`, or the event was dropped). The matching
// completion step normally clears the bar within ~1s; this only bounds the worst
// case so a post-turn note can't linger anywhere near the 3-min watchdog.
const POST_TURN_SETTLE_MS = 15000;

// One entry per tool call this turn: { tool, args, status, result, durationMs,
// errorType, turn, open }. status: 'running' | 'done' | 'error'.
let toolCalls = [];
// Durable assistant row that owns the current inference turn's tool calls.
// A live tool-only line uses this to acquire the same transcript identity and
// ordering key that the saved projection receives after refresh.
let currentToolAnchor = null;

// SYNTHETIC STANDALONE tool calls fire OUTSIDE a reply bubble's normal tool-call
// accumulation, so they're rendered out-of-band straight onto the transcript as
// their own foldable tool bubble (see _renderSynthToolBubble), mirroring the
// reload renderer (session-load.js _buildSynthCall). Two kinds:
//   • vision ingestion — process_image / route_attachment, emitted during
//     user-turn ingestion (before turn 1, before any reply bubble); the turn
//     reset that brackets ingestion would otherwise wipe the activity list.
//   • loop-node memory — memory_search (before the turn) / memory_save (after);
//     neither is a model-issued tool call, and memory_save fires post-`response`.
//     They render as small debug notes in the transcript instead of bubbles.
// Args arrive differently per tool, so we stash them by tool name until the
// completion event lands: vision via its tool_call event; memory_search via its
// memory_search_start pipeline step; memory_save's args ride its memory_save_end.
const _SYNTH_TOOLS = {
  process_image: true, route_attachment: true,
  memory_search: true, memory_save: true,
  app_control: true,   // Launcher element-pickup fingerprint
  resume_session: true, // Automatic recovery retry
};

// Recovery retries are synth-flagged (they fire outside a reply bubble's normal
// tool-call accumulation) but they are NOT out-of-band actions: a retry belongs to
// the run it resumed, so it folds into that run's activity group like any other
// tool call. Only when no run group is open to join does it get its own bubble.
const _RECOVERY_TOOLS = { resume_session: true };
let _pendingSynthArgs = {};   // toolName → args, awaiting that tool's completion event

// Tracks whether the chat layer has created at least one bubble for the current
// turn's output (text or tool calls). chat-stream.js sets this to true when it
// creates/finds a bubble. Cleared on new-turn boundaries so the first tool_call
// of a new turn knows no bubble exists yet and creates one.
// Exposed on app so chat-stream.js can read/write it cross-module.
app._turnHasBubble = false;

// Append a small debug note to the chat transcript (memory_search / memory_save).
// Shown as a subtle italic line — not a full bubble. Deduplicates consecutive
// identical notes so rapid tool runs don't spam the transcript.
function _appendMemoryNote(message) {
  if (!app.chatMessages) return;
  const last = app.chatMessages.lastElementChild;
  if (last && last.classList.contains('memory-debug-note') && last.textContent === '— ' + message) return;
  const el = document.createElement('div');
  el.className = 'memory-debug-note';
  el.textContent = '— ' + message;
  app.chatMessages.appendChild(el);
}
app._appendMemoryNote = _appendMemoryNote;
export { _appendMemoryNote };

// Current inference-turn number within this exchange. A single user message can
// drive several LLM "turns" (call → tools → call → …); the backend stamps each
// with a `turn_start` pipeline event carrying `turn`. 0 = not yet in a turn.
// Reset only at a true new exchange (user_message) / session switch — NOT in
// _resetForNewTurn, so a mid-turn reattach keeps the number it already learned.
let currentTurn = 0;

// 3 min of total silence on the current session ⇒ assume the run died and drop
// the glow. Real turns emit pipeline/stream events far more often.
const WATCHDOG_MS = 180000;

let _outsideHandler = null;
let _keyHandler = null;

// Clear a pending setTimeout (if any) and return null, so callers can write
// `x = _killTimer(x)` to both cancel and null out a timer handle in one step.
function _killTimer(id) { if (id) clearTimeout(id); return null; }
function _clearTextTimer() { clearTimer = _killTimer(clearTimer); }
function _clearEndTimer()  { endTimer = _killTimer(endTimer); }
function _armWatchdog() {
  watchdog = _killTimer(watchdog);
  watchdog = setTimeout(() => { watchdog = null; stop(); }, WATCHDOG_MS);
}
function _disarmWatchdog() { watchdog = _killTimer(watchdog); }

// Re-light triggered by a POST-turn background step (see `_turnEnded`). Show the
// note, but auto-settle on a short backstop instead of the long in-turn watchdog,
// so the chip can't hang if the step's completion event never arrives. Shares the
// endTimer slot with _endSoon(), so the real `*_end` event (which calls _endSoon)
// simply re-arms it shorter and wins.
function _armPostTurnSettle() {
  endTimer = _killTimer(endTimer);
  endTimer = setTimeout(() => { endTimer = null; stop(); }, POST_TURN_SETTLE_MS);
}

// True for a pipeline step that signals a background op FINISHED (memory_save_end,
// and any future *_end/*_complete/*_done/*_saved step). Used only while _turnEnded
// is set, so in-turn `*_end` steps (e.g. llm_call_end, fired every LLM call) never
// trip it and prematurely clear a live turn.
function _isCompletionStep(step) {
  return typeof step === 'string'
    && /_(end|complete|completed|done|finish|finished|saved)$/.test(step);
}

// Pop the new note in with a quick slide/fade so each interaction "appears".
function _animateText() {
  if (!textEl || typeof textEl.animate !== 'function') return;
  try {
    textEl.animate(
      [
        { opacity: 0, transform: 'translateY(3px)' },
        { opacity: 1, transform: 'translateY(0)' },
      ],
      { duration: 180, easing: 'ease-out' },
    );
  } catch (_) { /* Web Animations API unavailable — non-fatal */ }
}

/** Start a gentle ramp that makes the output counter tick up while the agent
 *  is thinking (no stream content yet). Increments by ~2-4 tokens every ~800ms
 *  so the user sees movement right away.
 */
function _startThinkingRamp() {
  _stopThinkingRamp();
  // If we already have real stream data or real tokens, don't fake it
  if (_streamCharCount > 0 || cumulativeOut > 0) return;
  _setSpinnerDir('out');
  _thinkingRamp = setInterval(() => {
    // Ramp grows slightly faster over time
    const bump = Math.min(8, 2 + Math.floor(_pendingOutEstimate / 50));
    _pendingOutEstimate += bump;
    _updateOutDisplay();
  }, 800);
}

function _stopThinkingRamp() {
  if (_thinkingRamp) {
    clearInterval(_thinkingRamp);
    _thinkingRamp = null;
  }
  _pendingCtxEstimate = 0;
  _renderCtxIndicator();
}

function _displayedOut() {
  // What the user sees for output tokens = cumulative real + streaming estimate
  return cumulativeOut + _pendingOutEstimate;
}

function _animateCountUp(el, target, durationMs = 300) {
  if (!el) return;
  const from = parseInt(el.textContent.replace(/,/g, ''), 10) || 0;
  if (from === target) return;
  const diff = target - from;
  const start = performance.now();

  function step(now) {
    const elapsed = now - start;
    const progress = Math.min(elapsed / durationMs, 1);
    // ease-out quadratic
    const eased = 1 - (1 - progress) * (1 - progress);
    const current = Math.round(from + diff * eased);
    el.textContent = current;
    if (progress < 1) {
      requestAnimationFrame(step);
    } else {
      el.textContent = target; // ensure exact final value
    }
  }

  requestAnimationFrame(step);
}

function _setSpinnerDir(dir) {
  if (!tokenSpinnerEl) return;
  tokenSpinnerEl.classList.remove('spinning-out', 'spinning-in');
  if (dir === 'out') tokenSpinnerEl.classList.add('spinning-out');
  else if (dir === 'in') tokenSpinnerEl.classList.add('spinning-in');
}

function _updateTokenBar() {
  if (!tokenBarEl) return;
  // Token bar is an optional stat: when it's not in the configured stats list,
  // keep it hidden even if CSS display rules would resurrect it; when it IS
  // enabled, clear any inline 'none' left by a previous disabled pass.
  const cfg = _statsConfig();
  if (cfg && !cfg.some(e => e.type === 'token-bar')) {
    tokenBarEl.style.display = 'none';
    return;
  }
  if (cfg) tokenBarEl.style.display = '';
  tokenBarEl.classList.toggle('active', active || cumulativeIn > 0 || cumulativeOut > 0);
}

function _updateOutDisplay() {
  _animateCountUp(tokensOutEl, _displayedOut());
}

/** Called on each stream chunk to update the live token estimate */
function _onStreamContent(content) {
  if (!content) return;
  // First stream content — stop the thinking ramp, switch to real estimates
  if (_streamCharCount === 0) {
    _stopThinkingRamp();
    _setSpinnerDir('out');
  }
  _streamCharCount += content.length;
  const newEstimate = Math.round(_streamCharCount / 4);
  if (newEstimate !== _pendingOutEstimate) {
    _pendingOutEstimate = newEstimate;
    _updateOutDisplay();
  }
}

function addTokens(inputTokens, outputTokens, callCreditUnits) {
  // Real tokens arrived — stop the thinking ramp
  _stopThinkingRamp();
  // Add this single call's locked house credits. Never recompute from cumulative
  // tokens because earlier calls retain the schedule active when they ran.
  if (typeof callCreditUnits === 'number' && callCreditUnits > 0) {
    _sessionCreditUnits += callCreditUnits;
  }
  if (typeof inputTokens === 'number' && inputTokens > 0) {
    cumulativeIn += inputTokens;
    _setSpinnerDir('in');
    _animateCountUp(tokensInEl, cumulativeIn);
  }
  // Real output tokens arrived — always clear the streaming estimate
  if (typeof outputTokens === 'number') {
    _pendingOutEstimate = 0;
    _streamCharCount = 0;
    if (outputTokens > 0) {
      cumulativeOut += outputTokens;
      _setSpinnerDir('out');
      _animateCountUp(tokensOutEl, cumulativeOut);
    }
  }
  _updateTokenBar();
  _renderCost();
  _saveTokenCache();
}

// ── Active-model context indicator (next to in/out counters) ────────────────

/** The stats config resolved by applyStatsConfig (chat-pill-config.js) from
 *  chat_ui.json → controls.stats.visible: [{ type, decimals, el }]. Null until
 *  the config system applies — renderers then fall back to the compact ctx +
 *  cost defaults so the pill still works on the legacy path. */
function _statsConfig() {
  const el = document.getElementById('chat-pill-stats');
  return (el && Array.isArray(el._statsConfig)) ? el._statsConfig : null;
}

/** Format a context/token count for the pill: x.xk below 1M (1 decimal),
 *  x.xxM at/above 1M (2 decimals). An explicit `decimals` overrides the
 *  default for both ranges (config: stats entry "decimals"). */
function _fmtCtxNum(n, decimals) {
  if (!n || typeof n !== 'number') return '';
  if (n >= 1_000_000) return `${(n / 1_000_000).toFixed(decimals == null ? 2 : decimals)}M`;
  return `${(n / 1000).toFixed(decimals == null ? 1 : decimals)}k`;
}

/** Best-effort context window for a Claude Code model id, by family. The local
 *  `claude` CLI isn't in the app's model catalog, so we map instead of looking up:
 *  current Opus/Sonnet run a 1M window, Haiku 200K. Mirrors the backend's
 *  _claude_model_window so a live-reported model resolves the same as the load-time
 *  resolve. Returns null for an unknown id (footer just omits the max). */
function _claudeCtxWindow(model) {
  const m = (model || '').toLowerCase();
  if (!m) return null;
  if (m.includes('haiku')) return 200_000;
  if (m.includes('opus') || m.includes('sonnet') || m.includes('fable') || m.includes('mythos')) return 1_000_000;
  return null;
}

/** Fetch the active model's context window / max output and show it in the
 *  footer. Resolves the user's effective model server-side (one call). Silent
 *  on failure — the indicator just stays hidden. */
async function refreshModelContext() {
  if (!modelCtxEl) return;
  try {
    const headers = { ...authHeaders() };
    const aid = app.currentAgentId || '';
    const sid = app.currentSessionId || '';
    const qs = new URLSearchParams();
    if (aid) qs.set('agent_id', aid);
    if (sid) qs.set('session_id', sid);
    const url = apiPath('/admin/settings/current-model-info')
      + (qs.toString() ? `?${qs.toString()}` : '');
    const res = await fetch(url, { headers });
    if (!res.ok) throw new Error(`HTTP ${res.status}`);
    const d = await res.json();
    _modelContextLimit = d.context || null;
    _currentModelName = d.model || '';
    _costInput = (typeof d.cost_input === 'number') ? d.cost_input : null;
    _costOutput = (typeof d.cost_output === 'number') ? d.cost_output : null;
    // Non-default engines (Local Claude Code, …) don't run on this model — drop
    // the price chip; the token/ctx counters stay (they reflect the real run).
    _altEngine = !!(d.engine && d.engine !== 'default');
    _altEngineId = _altEngine ? String(d.engine) : '';
    if (_altEngine) {
      // Adopt the configured harness model + effort so the changer/panel labels
      // match before the panel ever opens (and even when no panel was opened).
      _engineCurModel = d.model || '';
      const eff = (d.reasoning_effort && d.reasoning_effort !== 'default') ? String(d.reasoning_effort) : '';
      _engineCurEffort = eff;
    }
    _renderCtxIndicator();
    _renderCost();
    _renderModelBtn();
  } catch (e) {
    _altEngine = false;   // unknown engine ⇒ assume normal billing, don't strand the price hidden
    _altEngineId = '';
    // Re-render from the data we already hold rather than force-hiding: a
    // transient model-info failure must not wipe a valid ctx readout (the ctx
    // number comes from llm_call_end / setContextTokens, NOT from this fetch)
    // or strand the cost chip in its pre-fetch alt-engine state.
    _renderCtxIndicator();
    _renderCost();
    _renderModelBtn();
  }
}

/** Label the dedicated model button with the active slot's three-letter abbreviation
 *  (STD, PRM, VIS, IMG). The full model name is only visible inside the picker panel. */
function _renderModelBtn() {
  if (!modelBtnEl) return;
  // Alternate engine: show the harness's own model choice instead of slot labels.
  if (_altEngine) {
    const catalog = _engineCatalogNow();
    let label = 'Model';
    let title = 'Click to switch model for this chat';
    if (catalog) {
      const m = _engineCurModel || '';
      if (m) {
        const hit = catalog.models.find(x => x.v === m)
          || (catalog.lane === 'claude_code' ? catalog.models.find(x => x.v === _claudeAliasOf(m)) : null);
        label = hit ? hit.label : (m.length > 16 ? '…' + m.slice(-15) : m);
        title = `Model: ${m} — click to switch model for this chat`;
      } else {
        label = 'Default';
      }
    }
    modelBtnEl.textContent = label;
    modelBtnEl.title = title;
    return;
  }
  // Agent-driven override (Model Switcher): show the real model + warning color
  // on the dedicated model button too, until the run ends (backend cleanup).
  if (_agentOverride) {
    modelBtnEl.classList.add('agent-override');
    modelBtnEl.textContent = _agentOverride.model.length > 16 ? '…' + _agentOverride.model.slice(-15) : _agentOverride.model;
    modelBtnEl.title = `Model: ${_agentOverride.model} (agent-set) — click to switch model for this chat`;
    return;
  }
  modelBtnEl.classList.remove('agent-override');
  const slot = _allSlots.find(s => {
    const ref = _slotRef(s);
    return ref === _currentSlotRef;
  });
  const name = (slot && slot.model) || '';
  let label = '';
  if (!slot || !name) {
    label = 'Model';
  } else {
    label = name.length > 16 ? '…' + name.slice(-15) : name;
  }
  modelBtnEl.textContent = label;
  modelBtnEl.title = name
    ? `Model: ${name} — click to switch model for this chat`
    : 'Click to switch model for this chat';
}

/** Format a USD amount for the footer. Rounds UP to the configured decimals
 *  (default 2 = cents) so the very first charge — however small — reads as
 *  "$0.01": an indicator that billing has started, never a trailing "$0.00".
 *  `decimals` comes from the config entry (stats "decimals" override). */
function _fmtCredits(credits, decimals) {
  const dp = (decimals == null) ? 2 : decimals;
  const scale = Math.pow(10, dp);
  return `◇ ${(Math.ceil(credits * scale) / scale).toFixed(dp)}`;
}

/** Render the session-credit chip from the running per-call total (_sessionCreditUnits).
 *  Each call was priced at its own model's rate the moment it finished, so the
 *  total stays accurate across mid-session model switches — unlike the old
 *  "cumulative tokens × current model rate", which re-priced earlier calls.
 *  Config-driven: hidden when the "cost" stat is disabled, when the session is
 *  on an alternate engine (Local Claude Code — not billed on the app's model),
 *  or while the total is still zero (billing hasn't started — hidden away, not
 *  "$0"). Shows "n/a" when calls ran on models with no published price (a $0
 *  would falsely read as free). Resets on session switch; reconciled from
 *  /session-cost on load. */
function _renderCost() {
  if (!costEl) return;
  const cfg = _statsConfig();
  const entry = cfg ? cfg.find(e => e.type === 'cost') : null;
  if (cfg && !entry) {
    costEl.hidden = true;
    costEl.style.display = 'none';   // hidden attr alone loses to .chat-cost { display:flex }
    costEl.innerHTML = '';
    return;
  }
  const decimals = entry ? entry.decimals : null;
  // Alternate-engine agents (e.g. Local Claude Code) aren't billed on the app's
  // model, so a price here would be misleading — hide it, keep the counters.
  if (_altEngine) {
    costEl.style.display = 'none';
    costEl.innerHTML = '';
    return;
  }
  const total = _sessionCreditUnits;
  let label;
  if (total > 0) {
    label = _fmtCredits(total, decimals);
    costEl.style.display = '';
  } else if (_sessionHasUnknownPriced) {
    label = 'n/a';
    costEl.style.display = '';
  } else {
    label = '';
    costEl.style.display = 'none';
  }
  costEl.innerHTML = label;
  costEl.title = 'House credits used this session — each call charged at its published model schedule, '
    + 'summed across any model switches'
    + (_currentModelName ? ` (now: ${_currentModelName})` : '')
    + (_sessionHasUnknownPriced && total <= 0 ? ' — some calls have no published price' : '');
}

/** Called on session load and repeatedly as messages stream in / windows load
 *  (cache append, tail-poll, reposition, …). Deliberately does NOT touch the ctx
 *  readout: a chars/4 estimate of whatever window the browser currently holds
 *  under-reports long sessions and was the source of the counter's "drops" (a
 *  partial-window re-estimate stomping the exact count). The ctx pill is exact
 *  counts only — setContextTokens (server LATEST) and llm_call_end (live, exact).
 *  Here we only clear a stale ramp and restore the IN/OUT token cache, which is
 *  unrelated to the ctx number. */
function setContextFromMessages(messages) {
  _pendingCtxEstimate = 0; // clear any stale ramp from a previous session
  // Restore cached counters instantly; session-load then applies the one
  // authoritative ledger payload without issuing another aggregate request.
  _loadTokenCache();
}

function setSessionUsage(usage) {
  if (!usage || typeof usage !== 'object') return;
  cumulativeIn = Number(usage.input_tokens) || 0;
  cumulativeOut = Number(usage.output_tokens) || 0;
  // Catch-up only, never down: a zeroed/stale load payload must not wipe a
  // live total mid-session (resetTokens zeroes both on a genuine session
  // switch first, so cross-session contamination isn't possible).
  _sessionCreditUnits = Math.max(_sessionCreditUnits, Number(usage.total_credit_units) || 0);
  if (usage.has_unknown === true) _sessionHasUnknownPriced = true;
  _animateCountUp(tokensInEl, cumulativeIn);
  _animateCountUp(tokensOutEl, cumulativeOut);
  _updateTokenBar();
  _renderCost();
  _saveTokenCache();
}

/** Set the ctx indicator from the server's whole-session value — the most recent
 *  provider-reported prompt token count for this session (the LAST chat usage
 *  row's input_tokens — the actual context sent, reflecting compaction).
 *  Applied by the loader after setContextFromMessages, since the open fetch is a
 *  small window whose messages alone would under-report. */
function setContextTokens(tokens, model = '') {
  if (typeof tokens !== 'number' || tokens < 0) return;
  _contextTokens = tokens;
  if (model) _currentModelName = String(model);
  _contextRevision += 1;
  _pendingCtxEstimate = 0; // clear the thinking ramp when real data arrives
  _renderCtxIndicator();
}

/** What the user sees for ctx tokens = real context + thinking ramp estimate */
function _displayedCtx() {
  return (_contextTokens || 0) + _pendingCtxEstimate;
}

/** Render the ctx indicator from the config-driven stats set. Two modes:
 *  "ctx" (the default) shows just the current context — "77.2k" — while
 *  "ctx-max" shows the full "ctx 77.2k / 200k" current/max readout. Hidden
 *  while the current context is zero (or would render as "0.0k") or nothing
 *  is known yet; the hover tooltip always keeps the exact counts and the
 *  model's window. */
function _renderCtxIndicator() {
  if (!modelCtxEl) return;
  const cfg = _statsConfig();
  let entry = null;
  let full = false;
  if (cfg) {
    entry = cfg.find(e => e.type === 'ctx') || cfg.find(e => e.type === 'ctx-max');
    full = !!cfg.find(e => e.type === 'ctx-max');
    if (!entry) {
      modelCtxEl.hidden = true;
      modelCtxEl.style.display = 'none';   // hidden attr alone loses to .chat-model-ctx { display:flex }
      modelCtxEl.innerHTML = '';
      modelCtxEl.title = "Active model's context window and max output";
      return;
    }
  }
  const decimals = entry ? entry.decimals : null;
  const ctx = _displayedCtx();
  const max = _modelContextLimit;
  const num = _fmtCtxNum(ctx, decimals);
  // Nothing known yet, or the value would render as a zero — hide it entirely
  // (an empty chip would otherwise sit in the strip with a stray tooltip).
  if (!num || num.startsWith('0.0')) {
    modelCtxEl.hidden = true;
    modelCtxEl.style.display = 'none';
    modelCtxEl.innerHTML = '';
    modelCtxEl.title = "Active model's context window and max output";
    return;
  }
  modelCtxEl.innerHTML = full
    ? `<span class="chat-token-label">ctx</span> ${num} <span class="chat-ctx-sep">/</span> ${_fmtCtxNum(max, decimals) || '?'}`
    : num;
  const ctxSlot = _allSlots.find(s => {
    const ref = _slotRef(s);
    return ref === _currentSlotRef;
  });
  modelCtxEl.title = `${(ctxSlot && ctxSlot.model) || _currentModelName || '??'} — ctx ${(_contextTokens || 0).toLocaleString()} / max ${max ? max.toLocaleString() : '?'} — click to switch model`;
  modelCtxEl.hidden = false;
  modelCtxEl.style.display = '';
  // Keep the open Claude panel's context section in step with live updates.
  if (_claudePanelEl && _claudePanelEl.style.display !== 'none') _renderClaudeContext();
}

// ── Context panel (click ctx indicator) ─────────────────────────────────────
// The stats strip's click target (#chat-pill-stats) opens this floating panel.
// Model selection and model info now live in the model-changer UI
// (#chat-model-changer → .chat-model-list-panel), so this panel only carries
// per-chat context-management (compaction) controls.

let _modelPickerEl = null;       // the floating dropdown container
let _compactEl = null;           // context-compaction section (sliders + "Compact now")
let _allSlots = [];              // fetched slot list: [{type, role?, position?, model, provider, context, description, ...}]
let _modelListLoaded = false;    // the roster fetch ANSWERED — an empty list is then a real answer, not "still loading"
let _allEffortMap = {};          // per-slot effort: {"role:standard": "high", "custom:2": "low"}
let _currentSlotRef = '';        // the currently-selected slot ref (e.g. "role:text", "custom:2")
let _currentModelName = '';      // the resolved model name (fallback display — set by refreshModelContext and WS)
let _agentOverride = null;       // {model: '<id>'} when the AGENT switched this session's model
                                 //   (Model Switcher: set_model / use_premium_model); null otherwise.
                                 //   Refreshed from /admin/settings/agent-models (agent_override)
                                 //   and the live 'model_override' WS event; cleared at run end by
                                 //   the backend cleanup (runner._finish_run) → pill returns to normal.

function _slotRef(slot) {
  if (!slot) return '';
  // One visible row represents one roster entry, even where that entry has
  // several routing roles. Entry IDs therefore take precedence over role refs.
  if (slot.entry_id) return 'entry:' + slot.entry_id;
  if (slot.type === 'role') return 'role:' + (slot.role || '');
  return 'custom:' + (slot.position || 0);
}

// Agent identity cards are intentionally separate from generic chat components:
// password fields never enter component state, transcript persistence, or model
// context. The full credential form is used by the standalone embed; inside the
// signed-in app this card proves and links the already-authenticated app account.
function _maybeShowAgentProfileCard(event) {
  if (!event || event.error) return;
  let payload;
  try { payload = typeof event.result === 'string' ? JSON.parse(event.result) : event.result; }
  catch (_) { return; }
  if (!payload || !['agent_auth', 'agent_profile_admin'].includes(payload.ui)
      || !app.addChatBubble) return;
  const bubble = app.addChatBubble('agent', '', 'agent-profile-card');
  const text = bubble?.querySelector('.chat-bubble-text') || bubble;
  if (!text) return;
  text.textContent = '';
  const card = document.createElement('section');
  card.style.cssText = 'display:grid;gap:8px;padding:4px;min-width:260px';
  const title = document.createElement('strong');
  title.textContent = payload.ui === 'agent_auth' ? 'Agent sign-in' : 'Agent profile administration';
  card.appendChild(title);
  if (payload.ui === 'agent_auth') {
    const note = document.createElement('small');
    note.textContent = 'Use your signed-in app account for this agent. The app credential is verified but never copied into the agent database.';
    const button = document.createElement('button');
    button.type = 'button'; button.textContent = 'Use app account';
    button.addEventListener('click', async () => {
      button.disabled = true;
      try {
        const response = await fetch(apiPath(`/api/v1/agents/${encodeURIComponent(payload.agent_id)}/auth/app-link`), {
          method: 'POST', headers: { 'Content-Type': 'application/json', ...authHeaders() }, body: '{}',
        });
        const data = await response.json().catch(() => ({}));
        if (!response.ok) throw new Error(data.detail || 'Agent sign-in failed.');
        note.textContent = `Signed in with the ${data.principal?.profile?.name || 'Member'} profile.`;
        button.remove();
      } catch (error) { note.textContent = error.message; button.disabled = false; }
    });
    card.append(note, button);
  } else {
    const profiles = document.createElement('div');
    profiles.textContent = `Profiles: ${(payload.profiles || []).map((p) => p.name).join(', ') || 'none'}`;
    const members = document.createElement('div');
    members.textContent = `Members: ${(payload.members || []).length}`;
    const note = document.createElement('small');
    note.textContent = 'Profile and member changes made through this protected tool are checked again by the server against your agent-administrator role.';
    card.append(profiles, members, note);
  }
  text.appendChild(card);
}

function _buildModelPicker() {
  if (_modelPickerEl) return;
  const picker = document.createElement('div');
  picker.className = 'chat-model-picker';
  // ── Context-compaction section ──
  // Two sliders tune THIS chat's absolute token budgets — activation target and
  // verbatim hot tail — saved as a per-session override. Manual compaction
  // enters the isolated controlled-run command queue.
  const compact = document.createElement('div');
  compact.className = 'cmp-compact';
  compact.innerHTML =
      `<div class="cmp-cx" hidden>`
    +   `<div class="cmp-cx-top"><span class="cmp-cx-title">What's in this context</span>`
    +     `<span class="cmp-cx-total">&ndash;</span></div>`
    +   `<div class="cmp-cx-bar">`
    +     `<span class="cmp-cx-seg cmp-cx-prompt"></span>`
    +     `<span class="cmp-cx-seg cmp-cx-runtime"></span>`
    +     `<span class="cmp-cx-seg cmp-cx-reasoning"></span>`
    +     `<span class="cmp-cx-seg cmp-cx-compactable"></span>`
    +     `<span class="cmp-cx-seg cmp-cx-verbatim"></span>`
    +   `</div>`
    +   `<div class="cmp-cx-legend">`
    +     `<span class="cmp-cx-key"><i class="cmp-cx-dot cmp-cx-prompt"></i>Base prompt &amp; tools <b data-cx-val="prompt">&ndash;</b></span>`
    +     `<span class="cmp-cx-key cmp-cx-detail"><i class="cmp-cx-dot cmp-cx-runtime"></i>Runtime state <b data-cx-val="runtime">&ndash;</b></span>`
    +     `<span class="cmp-cx-key cmp-cx-detail"><i class="cmp-cx-dot cmp-cx-reasoning"></i>Replayed reasoning <b data-cx-val="reasoning">&ndash;</b></span>`
    +     `<span class="cmp-cx-key"><i class="cmp-cx-dot cmp-cx-compactable"></i>Compactable <b data-cx-val="compactable">&ndash;</b></span>`
    +     `<span class="cmp-cx-key"><i class="cmp-cx-dot cmp-cx-verbatim"></i>Verbatim <b data-cx-val="verbatim">&ndash;</b></span>`
    +   `</div>`
    +   `<div class="cmp-cx-hint"></div>`
    + `</div>`
    + `<div class="cmp-cc-head">Context compaction</div>`
    + `<div class="cmp-cc-row">`
    +   `<div class="cmp-cc-top"><span class="cmp-cc-label">Target history</span>`
    +     `<span class="cmp-cc-val" data-val="threshold">100K tokens</span></div>`
    +   `<input type="range" class="cmp-cc-slider" data-key="threshold" min="10000" max="1050000" step="10000" value="100000">`
    +   `<div class="cmp-cc-hint">Compactable conversation-history budget. System prompt, tools, and provider output are separate.</div>`
    + `</div>`
    + `<div class="cmp-cc-row">`
    +   `<div class="cmp-cc-top"><span class="cmp-cc-label">Keep verbatim</span>`
    +     `<span class="cmp-cc-val" data-val="tail">30K tokens</span></div>`
    +   `<input type="range" class="cmp-cc-slider" data-key="tail" min="5000" max="1050000" step="5000" value="30000">`
    +   `<div class="cmp-cc-hint">Newest history budget kept word-for-word; completed older tool exchanges may be summarized.</div>`
    + `</div>`
    + `<button type="button" class="cmp-cc-btn">`
    +   `<span class="cmp-cc-btn-label">Compact now</span>`
    +   `<span class="cmp-cc-btn-status"></span></button>`
    + `<div class="cmp-cc-note">Applies to this chat only — takes effect next turn.</div>`;
  picker.appendChild(compact);
  document.body.appendChild(picker);
  _modelPickerEl = picker;
  _compactEl = compact;

  // Slider "input" updates the live token label; "change" (on release) saves it.
  compact.querySelectorAll('.cmp-cc-slider').forEach(sl => {
    sl.addEventListener('input', () => _onCompactSlide(sl));
    sl.addEventListener('change', () => _onCompactCommit(sl));
    // Don't let a click inside the picker bubble to the document close handler.
    sl.addEventListener('click', (e) => e.stopPropagation());
  });
  const compactBtn = compact.querySelector('.cmp-cc-btn');
  if (compactBtn) compactBtn.addEventListener('click', (e) => { e.stopPropagation(); _runCompactNow(compactBtn); });

  // Close on click outside
  document.addEventListener('click', (e) => {
    if (_modelPickerEl && !_modelPickerEl.contains(e.target)
        && e.target !== modelBtnEl && !(modelBtnEl && modelBtnEl.contains(e.target))
        && !(footerLeftEl && footerLeftEl.contains(e.target))) {
      _modelPickerEl.style.display = 'none';
    }
  });

  // Close on Escape
  document.addEventListener('keydown', (e) => {
    if (e.key === 'Escape' && _modelPickerEl && _modelPickerEl.style.display !== 'none') {
      _modelPickerEl.style.display = 'none';
    }
  });
}

function _escHtml(s) {
  return String(s).replace(/[&<>"']/g, c => ({ '&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;' }[c]));
}

/** Build the curated model list for the footer's model list panel (model
 *  changer): only the identified agent's own persisted roster, resolved
 *  server-side by /admin/settings/agent-models. Global/no-agent chat retains
 *  the legacy app-default fallback. Never widen an agent footer to the user's
 *  Available models catalog. */
async function _fetchModelsForPicker() {
  const headers = { ...authHeaders() };

  const aid = app.currentAgentId || '';
  const sid = app.currentSessionId || '';
  const modelMap = new Map();
  let activeSlotData = null;

  // Per-agent candidate list — now returns slot structure.
  if (aid || sid) {
    try {
      const qs = new URLSearchParams();
      if (aid) qs.set('agent_id', aid);
      if (sid) qs.set('session_id', sid);
      const res = await fetch(apiPath('/admin/settings/agent-models?' + qs.toString()), { headers });
      if (res.ok) {
        const data = await res.json();
        const slots = data.slots || [];
        // A model can occupy several semantic roles. The footer is a model
        // picker, so show that roster entry once while retaining its first
        // role-aware slot for the selection API.
        // Keep every semantic slot, even when Standard and Premium deliberately
        // point to the same entry. The backend role map is the authority.
        _allSlots = slots.map(s => ({ ...s, context: null }));
        _modelListLoaded = true;
        _allEffortMap = data.model_effort || {};
        activeSlotData = data.active_slot || null;
        // Agent-driven concrete-model override (Model Switcher ability) — the
        // footer shows it warning-colored until the run ends and the backend
        // clears it (see _syncModelChanger). User slot picks never set this.
        const ao = data.agent_override;
        _agentOverride = (ao && ao.model) ? { model: ao.model } : null;
      }
    } catch (_) { /* best-effort */ }
  }

  // A footer attached to an agent/session must never widen to the user's
  // Available models or app defaults. Empty means this specific agent has no
  // adopted roster yet. Only global/no-agent chat retains the legacy fallback.
  if (!_allSlots.length && !(aid || sid)) {
    try {
      const res = await fetch(apiPath('/admin/settings/multi-providers'), { headers });
      if (res.ok) {
        const data = await res.json();
        (data.providers || []).forEach(p => {
          const visibleName = p && (p.display_name || p.model);
          if (visibleName && p.enabled !== false && !modelMap.has(p.entry_id || visibleName)) {
            modelMap.set(p.entry_id || visibleName, { id: visibleName, context: null });
          }
        });
        // Build a flat slot-less model list (legacy path).
        _allSlots = Array.from(modelMap.values()).map((m, i) => ({
          type: 'custom', position: i + 1, model: m.id, provider: '', context: null,
        }));
      }
    } catch (_) { /* best-effort */ }
  }

  // Set current slot ref from the active slot data, or default to first slot.
  if (activeSlotData) {
    // The flat picker identifies every visible row by entry ID. A legacy role
    // selection is still returned with its role for routing, but the backend
    // includes its matched entry ID so that one row remains highlighted.
    _currentSlotRef = activeSlotData.entry_id
      ? 'entry:' + activeSlotData.entry_id
      : (activeSlotData.type === 'role'
          ? 'role:' + (activeSlotData.role || 'standard')
          : 'custom:' + (activeSlotData.custom_position || 1));
  } else if (_allSlots.length && !(aid || sid)) {
    // Legacy agent-less chat: the synthesized list IS the roster, so its first
    // row is what the turn runs on.
    const first = _allSlots[0];
    _currentSlotRef = first.type === 'role'
      ? 'role:' + first.role
      : (first.entry_id ? 'entry:' + first.entry_id : 'custom:' + first.position);
  } else {
    // An agent/session chat with no active slot means NOTHING is appointed for
    // this turn (no Standard model, or the stored pick no longer resolves).
    // Leave the ref empty so the footer reports that state — presenting the
    // first roster row as the running model is how a "Default" silently
    // masqueraded as a real selection.
    _currentSlotRef = '';
  }
}

/** Reconcile the running session-cost chip AND the IN/OUT token counters against
 *  the authoritative backend (sum of every call's locked-in usage for this session).
 *  Called on session load so a reload / session switch shows the true accumulated
 *  cost and tokens, not zero, plus periodically to self-heal dropped SSE events.
 *  Catch-up only: the ledger can briefly lag a just-finished call (the billing
 *  write lands after the llm_call_end event), so the live total is never stepped
 *  DOWN mid-session — a reload applies the ledger exactly. Best-effort: leaves
 *  live accumulators untouched on failure. */
async function _fetchSessionCost() {
  const sid = app.currentSessionId || '';
  // No session id yet (boot / mid-navigation): skip rather than wipe — the
  // load path applies the authoritative ledger, and zeroing here would clear
  // the chips on a session we're merely switching between.
  if (!sid) return;
  const requestedRevision = _contextRevision;
  const headers = { ...authHeaders() };
  try {
    const res = await fetch(apiPath(`/admin/settings/session-cost?session_id=${encodeURIComponent(sid)}`), { headers });
    if (res.ok) {
      const d = await res.json();
      if (app.currentSessionId !== sid) return;  // switched sessions mid-fetch — don't cross-contaminate
      if (d && typeof d.total_credit_units === 'number') {
        _sessionCreditUnits = Math.max(_sessionCreditUnits, d.total_credit_units);
        _renderCost();
      }
      if (d && typeof d.has_unknown === 'boolean') {
        // Monotonic within a session: a live llm_call_end may have flagged an
        // unknown-priced call that the ledger snapshot (taken a moment earlier)
        // doesn't yet include. Never step that flag back down on a stale poll —
        // resetTokens clears it on session switch.
        _sessionHasUnknownPriced = _sessionHasUnknownPriced || d.has_unknown;
        _renderCost();
      }
      // The ledger is authoritative for a restored session, including legitimate
      // downward movement after task/tool reduction. Replace cached/open-payload
      // values, but never overwrite an llm_call_end that arrived after this
      // request began.
      if (d && typeof d.context_tokens === 'number' && d.context_tokens > 0
          && _contextRevision === requestedRevision) {
        _contextTokens = d.context_tokens;
        if (d.context_model) _currentModelName = String(d.context_model);
        _contextRevision += 1;
        _pendingCtxEstimate = 0;
        _renderCtxIndicator();
        // Rescale the composition bar to the restored exact total.
        _renderContextComposition();
      }
      // Also seed the IN/OUT token counters from the per-model breakdown
      if (d && d.by_model) {
        let totalIn = 0;
        let totalOut = 0;
        for (const m of Object.values(d.by_model)) {
          totalIn += m.input || 0;
          totalOut += m.output || 0;
        }
        if (totalIn > 0 || totalOut > 0) {
          cumulativeIn = totalIn;
          cumulativeOut = totalOut;
          _pendingCtxEstimate = 0;
          if (tokensInEl) tokensInEl.textContent = totalIn.toLocaleString();
          if (tokensOutEl) tokensOutEl.textContent = totalOut.toLocaleString();
          _updateTokenBar();
          _saveTokenCache();
        }
      }
    }
  } catch (_) { /* best-effort */ }
}

/** Persist the chosen model. Returns true on a confirmed save, false otherwise.
 *  Scope, in order of preference:
 *    • In a chat session  → saved on the SESSION (sessions.metadata.llm_config),
 *      so this conversation remembers its own model without touching the agent
 *      default or other sessions. Resolved app-default → agent → session.
 *    • Agent but no session → overrides the agent's llm_config.model.
 *    • Neither → updates the global provider config's model. */
async function _selectModel(slotRef) {
  try {
    const headers = { 'Content-Type': 'application/json', ...authHeaders() };
    const sid = app.currentSessionId || '';
    const aid = app.currentAgentId || '';

    // Resolve the visible flat roster row to its routing selection.
    const slot = _allSlots.find(s => _slotRef(s) === slotRef);
    if (!slot) return false;
    const selectionType = slot.type;
    const role = slot.type === 'role' ? slot.role : '';
    const customPos = slot.type === 'custom' ? slot.position : 0;

    const payload = {
      selection_type: selectionType,
      role: role,
      custom_position: customPos,
      entry_id: slot.entry_id || '',
    };

    if (sid) {
      const postRes = await fetch(apiPath('/api/v2/chat/sessions/' + encodeURIComponent(sid) + '/model'), {
        method: 'POST', headers,
        body: JSON.stringify(payload),
      });
      if (postRes.ok) {
        // Session endpoint succeeded — model saved on the server session.
        // Announce the switch in the transcript (user-initiated: the footer
        // picker has no tool call, so the notice + persisted row must carry
        // initiator='user' themselves).
        try {
          const label = `Switched to ${slot.model || 'configured model'}`;
          // Model IDENTITY pair for the group netting — the selection itself, not
          // a coarse default/override state. Two different custom models must
          // read as a change (premium → custom-2 is not "override → override"),
          // while a pick that lands back on the group's starting selection still
          // cancels. Keying on the coarse state cancelled every second switch and
          // made the notice vanish from both the group and the transcript.
          const currentRef = String(_currentSlotRef || '').trim();
          if (typeof app.notifyModelSwitch === 'function') {
            app.notifyModelSwitch(label, {
              initiator: 'user', slot: slotRef, model: slot.model || '',
              state: slotRef || 'default',
              from: currentRef || 'default',
            });
          }
        } catch (_) { /* best-effort */ }
      } else {
        // Session endpoint failed (e.g. session doesn't exist on server yet —
        // common before the first message is sent). Store the pending selection
        // in sessionStorage so it can be applied once the session row exists.
        // Never fall back to writing agent metadata — model selections are
        // per-session and must not bleed into other chats.
        try {
          sessionStorage.setItem('ws:pendingModelSel', JSON.stringify(payload));
        } catch (_) { /* best-effort */ }
      }
    } else if (aid) {
      // No session — save as agent's llm_config override.
      const uq = '?user_id=' + encodeURIComponent(app.currentUserId || '');
      const getRes = await fetch(apiPath('/api/v1/agents/' + encodeURIComponent(aid) + uq), { headers });
      if (!getRes.ok) return false;
      const agent = await getRes.json();
      const existing = (agent.agent && agent.agent.llm_config) || {};
      const llmCfg = { ...existing, ...payload, use_default: false };
      const putRes = await fetch(apiPath('/api/v1/agents/' + encodeURIComponent(aid)), {
        method: 'PUT', headers,
        body: JSON.stringify({ user_id: app.currentUserId || '', llm_config: llmCfg }),
      });
      if (!putRes.ok) return false;
    } else {
      return false;
    }
    _currentSlotRef = slotRef;
    refreshModelContext();
    return true;
  } catch (e) {
    console.warn('Failed to switch model:', e);
    return false;
  }
}

// ── Context-compaction panel (bottom of the model picker) ────────────────────

// Map a slider's data-key to the label + save field it drives.
const _CC_FIELDS = {
  threshold: { valSel: 'threshold', field: 'compact_target_tokens' },
  tail: { valSel: 'tail', field: 'verbatim_tail_tokens' },
};

function _ccValEl(key) {
  return _compactEl ? _compactEl.querySelector(`.cmp-cc-val[data-val="${key}"]`) : null;
}

function _fmtCompactTokens(value) {
  const n = Math.max(0, Number(value) || 0);
  if (n >= 1000000) return `${(n / 1000000).toFixed(2).replace(/0+$/, '').replace(/\.$/, '')}M tokens`;
  if (n >= 1000) return `${Math.round(n / 1000)}K tokens`;
  return `${Math.round(n).toLocaleString()} tokens`;
}

// Live label update while dragging (no save yet).
function _onCompactSlide(slider) {
  const key = slider.dataset.key;
  const el = _ccValEl(key);
  if (el) el.textContent = _fmtCompactTokens(slider.value);
}

// Persist on release. Flash the label to confirm the save landed.
async function _onCompactCommit(slider) {
  const meta = _CC_FIELDS[slider.dataset.key];
  if (!meta) return;
  const el = _ccValEl(slider.dataset.key);
  const ok = await _saveCompaction(meta.field, parseInt(slider.value, 10));
  if (el) {
    el.classList.remove('cmp-cc-saved', 'cmp-cc-failed');
    // Force a reflow so re-adding the class re-triggers the flash animation.
    void el.offsetWidth;
    el.classList.add(ok ? 'cmp-cc-saved' : 'cmp-cc-failed');
  }
}

/** POST one absolute compaction token field for this session. Returns true on a
 *  confirmed save; no-op (false) outside a chat session. */
async function _saveCompaction(field, tokens) {
  const sid = app.currentSessionId || '';
  if (!sid) return false;
  try {
    const headers = { 'Content-Type': 'application/json', ...authHeaders() };
    const res = await fetch(apiPath('/api/v2/chat/sessions/' + encodeURIComponent(sid) + '/compaction'), {
      method: 'POST', headers,
      body: JSON.stringify({
        [field]: tokens,
      }),
    });
    return res.ok;
  } catch (e) {
    console.warn('Failed to save compaction setting:', e);
    return false;
  }
}

/** chat_ui.json → chat_common.context_composition. Accepts a plain boolean or a
 *  { enabled } object so either style of override works. Defaults ON. */
function _contextCompositionEnabled() {
  const v = chatUiFlag('context_composition', true);
  if (v && typeof v === 'object') return v.enabled !== false;
  return v !== false;
}

function _contextCompositionDetailed() {
  const v = chatUiFlag('context_composition', true);
  return Boolean(v && typeof v === 'object' && v.detailed_segments === true);
}

/** Token label for the composition bar. Delegates to the pill's own formatter
 *  (_fmtCtxNum) at 1000+ so the bar's total reads exactly like the ctx chip
 *  beside it — two different roundings of the same count look like a bug.
 *  Small segments (a short verbatim tail) stay whole numbers instead of "0.0k". */
function _fmtCxTokens(value) {
  const n = Math.max(0, Number(value) || 0);
  if (n < 1000) return String(Math.round(n));
  return _fmtCtxNum(n);
}

/** Adopt a fresh breakdown (live context_budget event or the GET payload) and
 *  repaint. Cached even while the panel is closed so the next open is instant. */
function _setContextComposition(comp) {
  _ctxComposition = (comp && typeof comp === 'object') ? comp : null;
  _renderContextComposition();
}

/** Repaint the panel's context-composition bar from the newest breakdown.
 *  The segments are chars/4 ESTIMATES, so only their RATIO is used: they are
 *  scaled to the provider-exact prompt count the ctx chip already shows, which
 *  keeps the bar and the readout beside it from ever disagreeing. Zero-width
 *  segments collapse; the whole block hides until a breakdown exists. */
function _renderContextComposition() {
  if (!_compactEl) return;
  const box = _compactEl.querySelector('.cmp-cx');
  if (!box) return;
  const comp = _ctxComposition;
  const detailed = _contextCompositionDetailed();
  const parts = comp ? [
    ['prompt', Math.max(0, Number(comp.base_prompt_tokens ?? comp.prompt_tokens) || 0)],
    ...(detailed ? [
      ['runtime', Math.max(0, Number(comp.runtime_state_tokens) || 0)],
      ['reasoning', Math.max(0, Number(comp.reasoning_tokens) || 0)],
    ] : []),
    ['compactable', Math.max(0, Number(comp.compactable_tokens) || 0)],
    ['verbatim', Math.max(0, Number(comp.verbatim_tokens) || 0)],
  ] : [];
  box.classList.toggle('cmp-cx-detailed', detailed);
  const est = parts.reduce((sum, p) => sum + p[1], 0);
  if (!_contextCompositionEnabled() || est <= 0) {
    box.hidden = true;
    return;
  }
  const exact = _contextTokens || 0;
  const total = exact > 0 ? exact : est;
  box.hidden = false;
  let running = 0;
  parts.forEach(([key, tokens], i) => {
    // The last segment absorbs the rounding remainder so the legend's three
    // numbers always sum to exactly the total shown above the bar.
    const last = i === parts.length - 1;
    const scaled = last ? Math.max(0, total - running)
                        : Math.round(tokens * (total / est));
    running += scaled;
    const seg = box.querySelector(`.cmp-cx-seg.cmp-cx-${key}`);
    if (seg) seg.style.flexBasis = (100 * tokens / est) + '%';
    const val = box.querySelector(`[data-cx-val="${key}"]`);
    if (val) val.textContent = _fmtCxTokens(scaled);
  });
  const totalEl = box.querySelector('.cmp-cx-total');
  if (totalEl) totalEl.textContent = _fmtCxTokens(total);
  const hintEl = box.querySelector('.cmp-cx-hint');
  if (hintEl) {
    let hint = exact > 0
      ? 'Total is the exact prompt sent; the split between segments is estimated.'
      : 'Split is estimated (chars/4) until the next call reports the exact total.';
    if (comp && comp.truncated) hint += ' Snapshot was size-capped, so the split is partial.';
    hintEl.textContent = hint;
  }
  const names = { prompt: 'Base prompt & tools', runtime: 'Runtime state', reasoning: 'Replayed reasoning', compactable: 'Compactable history', verbatim: 'Verbatim tail' };
  box.title = parts
    .map(([key, tokens]) => `${names[key]}: ${tokens.toLocaleString()} tokens (estimated)`)
    .join('\n');
}

/** Load the effective compaction settings for the current chat and reflect them on
 *  the two sliders + labels. Best-effort — leaves the defaults on any failure. */
async function _loadCompactionSettings() {
  if (!_compactEl) return;
  const sid = app.currentSessionId || '';
  if (!sid) return;
  try {
    const res = await fetch(apiPath('/api/v2/chat/sessions/' + encodeURIComponent(sid) + '/compaction'),
      { headers: { ...authHeaders() } });
    if (!res.ok) return;
    const d = await res.json();
    const limit = Number(d.token_limit) || 1050000;
    const set = (key, tokens) => {
      const sl = _compactEl.querySelector(`.cmp-cc-slider[data-key="${key}"]`);
      const el = _ccValEl(key);
      if (sl) sl.max = String(limit);
      if (sl && typeof tokens === 'number') sl.value = String(tokens);
      if (el && typeof tokens === 'number') el.textContent = _fmtCompactTokens(tokens);
    };
    set('threshold', d.compact_target_tokens);
    set('tail', d.verbatim_tail_tokens);
    // The stored breakdown of the LAST assembled context, so the bar is already
    // populated when the panel opens (the live event only fires on the next turn).
    if (d.composition) _setContextComposition(d.composition);
  } catch (_) { /* best-effort — sliders keep their defaults */ }
}

let _compactPending = null;

async function _runCompactNow(btn) {
  if (!btn || btn.disabled) return;
  const sid = app.currentSessionId;
  const statusEl = btn.querySelector('.cmp-cc-btn-status');
  if (!sid || !app.currentAgentId) {
    if (statusEl) statusEl.textContent = 'Open an agent chat first.';
    return;
  }
  const clientId = crypto.randomUUID();
  _compactPending = {sessionId: sid, clientId};
  btn.disabled = true;
  if (statusEl) statusEl.textContent = 'Waiting for acceptance…';
  try {
    const {queueSubmission} = await import('../../chat/core/panel.js');
    await queueSubmission({client_message_id: clientId, text: '/compact',
      attachment_ids: [], execution_mode: app.executionMode || 'ask'});
  } catch (error) {
    if (_compactPending?.clientId === clientId) _compactPending = null;
    btn.disabled = false;
    if (statusEl) statusEl.textContent = error.message || 'Could not queue compaction.';
  }
}

window.addEventListener('webagent-chat-send-status', (event) => {
  const pending = _compactPending;
  if (!pending || event.detail?.sessionId !== pending.sessionId ||
      event.detail?.id !== pending.clientId) return;
  const btn = pending.sessionId === app.currentSessionId
    ? _compactEl?.querySelector('.cmp-cc-btn') : null;
  const statusEl = btn?.querySelector('.cmp-cc-btn-status');
  if (event.detail.status === 'rejected') {
    _compactPending = null;
    if (btn) btn.disabled = false;
    if (statusEl) statusEl.textContent = 'Compaction was not accepted.';
  } else if (event.detail.status === 'offline') {
    if (statusEl) statusEl.textContent = 'Waiting for connection…';
  } else if (event.detail.status === 'accepted') {
    _compactPending = null;
    if (statusEl) statusEl.textContent = 'Compaction queued…';
  }
});

window.addEventListener('webagent-public-chat-state', (event) => {
  if (event.detail?.sessionId !== app.currentSessionId) return;
  const btn = _compactEl?.querySelector('.cmp-cc-btn');
  if (!btn) return;
  const statusEl = btn.querySelector('.cmp-cc-btn-status');
  const runs = (event.detail.runs || []).filter(run => run.kind === 'compaction');
  const active = runs.find(run => ['queued','running','finalizing'].includes(run.state));
  if (active) {
    btn.disabled = true;
    if (statusEl) statusEl.textContent = active.state === 'queued'
      ? 'Compaction queued…' : 'Compacting…';
  } else if (!_compactPending || _compactPending.sessionId !== app.currentSessionId) {
    btn.disabled = false;
    if (statusEl && runs.length) statusEl.textContent = 'Latest result is in chat.';
  }
});

// Toggle the context panel open/close when clicking the footer left area
function _toggleModelPicker() {
  if (_modelPickerEl && _modelPickerEl.style.display !== 'none') {
    _modelPickerEl.style.display = 'none';
    return;
  }
  // Close the model list panel if open
  if (_modelListPanelEl) _modelListPanelEl.style.display = 'none';
  _buildModelPicker();

  // Vertical anchor: pin the panel's bottom edge just above the footer row, so it
  // grows upward from there (prefer the dedicated model button, then the ctx chip).
  const vAnchor = modelBtnEl
    || (modelCtxEl && modelCtxEl.style.display !== 'none' ? modelCtxEl : null)
    || document.getElementById('chat-pill-stats')
    || document.getElementById('chat-input-row');
  // Horizontal anchor: align the panel's left edge to the chat pill's left edge.
  const pill = document.getElementById('chat-input-row');
  const hAnchor = pill || vAnchor;
  if (!vAnchor || !hAnchor) return;
  const vRect = vAnchor.getBoundingClientRect();
  const hRect = hAnchor.getBoundingClientRect();
  _modelPickerEl.style.top = 'auto';
  _modelPickerEl.style.bottom = (window.innerHeight - vRect.top + 6) + 'px';
  _modelPickerEl.style.left = Math.max(4, Math.min(hRect.left, window.innerWidth - 290)) + 'px';
  // Show it after a microtask so the document click handler (set in _buildModelPicker)
  // doesn't fire on this same click event and immediately close it.
  setTimeout(() => {
    _modelPickerEl.style.display = 'block';
  }, 0);

  // Pull this chat's effective compaction settings into the sliders.
  _loadCompactionSettings();
  // Repaint the composition bar immediately from whatever we already hold (a live
  // event may have landed while the panel was closed); the GET above refines it.
  _renderContextComposition();
}

// ── Claude Code panel (replaces the model picker for claude_code agents) ──────
// The app's OpenRouter model list is meaningless for a Local Claude Code agent —
// it runs the machine's `claude`, not the app LLM. So for those agents the footer
// click opens THIS panel instead: real context used / window, the subscription's
// 5-hour + weekly rolling-limit usage (best-effort — hidden when the undocumented
// endpoint can't be reached), and one-tap model selection (Default/Opus/Sonnet/
// Haiku) saved onto the agent's claude_code config (the engine reads it next turn).

const _CLAUDE_USAGE_URL = '/api/v1/claude-code/usage';
let _claudePanelEl = null;
let _claudeModelSaving = false;
let _engineCurModel = '';   // engine model currently configured on the agent (for changer sync)
let _engineCurEffort = '';  // engine effort currently configured on the agent
let _engineCatalogCache = null;  // live catalog fetched from the backend (CLI), or null
let _engineCatalogCacheTs = 0;   // when the live cache was fetched
const _ENGINE_CATALOG_TTL_MS = 5 * 60 * 1000; // re-fetch after 5 min so a long-lived page doesn't pin stale models

// STATIC fallback catalog — used only when the engine's own live catalog can't
// be fetched (see _getEngineCatalog). For Codex the live source is `codex debug
// models` (per-model efforts + defaults); for Claude the CLI exposes no catalog
// command, so this curated list IS the source (blank value = the CLI's default).
const _ENGINE_CATALOG = {
  claude_code: {
    lane: 'claude_code',
    label: 'Claude',
    models: [
      { v: '', label: 'Default', sub: "Claude's own default" },
      { v: 'fable', label: 'Fable', sub: 'Latest flagship' },
      { v: 'opus', label: 'Opus', sub: 'Most capable' },
      { v: 'sonnet', label: 'Sonnet', sub: 'Balanced' },
      { v: 'haiku', label: 'Haiku', sub: 'Fastest' },
    ],
    // claude --effort <level> (from `claude --help`)
    efforts: ['', 'low', 'medium', 'high', 'xhigh', 'max'],
  },
  codex: {
    lane: 'codex_code',
    label: 'Codex',
    models: [
      { v: '', label: 'Default', sub: "Codex's own default" },
      { v: 'gpt-5.6-sol', label: 'GPT-5.6-Sol', sub: 'Latest frontier agentic coding model.' },
      { v: 'gpt-5.6-terra', label: 'GPT-5.6-Terra', sub: 'Balanced agentic coding model for everyday work.' },
      { v: 'gpt-5.6-luna', label: 'GPT-5.6-Luna', sub: 'Fast and affordable agentic coding model.' },
      { v: 'gpt-5.5', label: 'GPT-5.5', sub: 'Frontier model for complex coding, research, and real-world work.' },
      { v: 'gpt-5.4', label: 'GPT-5.4', sub: 'Strong model for everyday coding.' },
      { v: 'gpt-5.4-mini', label: 'GPT-5.4-Mini', sub: 'Small, fast, and cost-efficient model for simpler coding tasks.' },
    ],
    // codex model_reasoning_effort (config override) — conservative union that
    // every listed model supports; the LIVE catalog narrows this per model.
    efforts: ['', 'low', 'medium', 'high', 'xhigh'],
  },
};

const _ENGINE_EFFORT_LABELS = {
  minimal: 'Minimal', low: 'Low', medium: 'Medium', high: 'High',
  xhigh: 'X-High', max: 'Max', ultra: 'Ultra',
};

/** The static (curated) catalog for the agent's engine, or null when not an
 *  alternate engine. */
function _engineCatalog() {
  return _ENGINE_CATALOG[_altEngineId] || null;
}

/** The catalog used for immediate, synchronous label rendering: the live cache
 *  when it has loaded, otherwise the static curated list. */
function _engineCatalogNow() {
  return _engineCatalogCache || _engineCatalog();
}

/** Fetch the LIVE engine catalog from the backend (which reads it from the local
 *  CLI, e.g. `codex debug models`). Falls back to the static curated catalog on
 *  any failure. Cached ~5 min in the page (the backend caches the CLI query too,
 *  ~15 min). ``force`` bypasses the page cache and asks the backend to re-query
 *  the CLI (what the Config tab's "Query CLI for latest model options" button
 *  does — the footer picks up the fresh list on its next open). */
async function _getEngineCatalog(force) {
  const staticCat = _engineCatalog();
  if (!_altEngineId || !staticCat) return staticCat;
  const fresh = _engineCatalogCache && (Date.now() - _engineCatalogCacheTs) < _ENGINE_CATALOG_TTL_MS;
  if (fresh && !force) return _engineCatalogCache;
  try {
    const url = apiPath('/api/v1/engines/model-catalog?engine=' + encodeURIComponent(_altEngineId))
      + (force ? '&force=1' : '');
    const res = await fetch(url, { headers: { ...authHeaders() } });
    if (res.ok) {
      const d = await res.json();
      const cat = (d && d.source === 'cli' && d.catalog
        && Array.isArray(d.catalog.models) && d.catalog.models.length) ? d.catalog : null;
      if (cat) {
        _engineCatalogCache = {
          lane: cat.lane || staticCat.lane,
          label: cat.label || staticCat.label,
          models: cat.models.map(m => ({
            v: m.v,
            label: m.label || m.v,
            sub: m.sub || '',
            efforts: (Array.isArray(m.efforts) && m.efforts.length) ? m.efforts : null,
            default_effort: m.default_effort || '',
          })),
          efforts: null, // per-model efforts always win when the CLI provides them
        };
        _engineCatalogCacheTs = Date.now();
        return _engineCatalogCache;
      }
    }
  } catch (_) { /* best-effort — fall back to curated */ }
  return _engineCatalogCache || staticCat;
}

/** Effort options for a given model in a catalog: the model's own supported
 *  levels when present, else the engine-level list. Always includes the
 *  "Default" (CLI default) option first. */
function _effortOptions(cat, model) {
  const out = [{ v: '', label: 'Default' }];
  if (!cat) return out;
  let levels = null;
  if (model) {
    const mm = cat.models.find(x => x.v === model);
    if (mm && Array.isArray(mm.efforts) && mm.efforts.length) levels = mm.efforts;
  }
  if (!levels && Array.isArray(cat.efforts)) {
    levels = cat.efforts.filter(v => v !== '').map(v => ({ v, label: _ENGINE_EFFORT_LABELS[v] || v }));
  }
  if (levels) {
    for (const e of levels) {
      if (e && e.v) out.push({ v: e.v, label: e.label || _ENGINE_EFFORT_LABELS[e.v] || e.v });
    }
  }
  return out;
}

/** Which preset row a configured model id maps to ('' = Default, null = a custom
 *  id that matches no preset so nothing is ticked). */
function _claudeAliasOf(model) {
  const m = (model || '').toLowerCase();
  if (!m) return '';
  if (m.includes('fable')) return 'fable';
  if (m.includes('opus')) return 'opus';
  if (m.includes('sonnet')) return 'sonnet';
  if (m.includes('haiku')) return 'haiku';
  return null;
}

/** Format a reset timestamp (ISO string or epoch s/ms) to a short local label;
 *  echoes the raw value back if it can't be parsed. */
function _fmtReset(v) {
  let d = null;
  if (typeof v === 'number') d = new Date(v > 1e12 ? v : v * 1000);
  else if (typeof v === 'string') { const t = Date.parse(v); if (!isNaN(t)) d = new Date(t); }
  if (!d || isNaN(d.getTime())) return typeof v === 'string' ? v : '';
  try { return d.toLocaleString([], { month: 'short', day: 'numeric', hour: 'numeric', minute: '2-digit' }); }
  catch (_) { return d.toLocaleString(); }
}

function _buildClaudePanel() {
  if (_claudePanelEl) return;
  const p = document.createElement('div');
  // Reuse the model-picker container chrome (position/border/shadow) + own body.
  p.className = 'chat-model-picker chat-claude-panel';
  p.innerHTML = `
    <div class="ccp-sec ccp-context">
      <div class="ccp-head">Context</div>
      <div class="ccp-ctx-row"><span class="ccp-ctx-num">—</span><span class="ccp-ctx-max"></span></div>
      <div class="ccp-bar"><div class="ccp-bar-fill"></div></div>
    </div>
    <div class="ccp-sec ccp-usage" style="display:none">
      <div class="ccp-head">Plan usage</div>
      <div class="ccp-usage-body"></div>
    </div>
    <div class="ccp-sec ccp-models">
      <div class="ccp-head">Model</div>
      <div class="ccp-model-list"></div>
    </div>
    <div class="ccp-sec ccp-effort">
      <div class="ccp-head">Effort</div>
      <div class="ccp-effort-btns"></div>
    </div>`;
  document.body.appendChild(p);
  _claudePanelEl = p;

  document.addEventListener('click', (e) => {
    if (_claudePanelEl && _claudePanelEl.style.display !== 'none'
        && !_claudePanelEl.contains(e.target)
        && e.target !== modelBtnEl && !(modelBtnEl && modelBtnEl.contains(e.target))
        && !(footerLeftEl && footerLeftEl.contains(e.target))
        && e.target !== modelCtxEl && !(modelCtxEl && modelCtxEl.contains(e.target))) {
      _claudePanelEl.style.display = 'none';
    }
  });
}

/** Fill the context section from the live footer state (real tokens / window). */
function _renderClaudeContext() {
  if (!_claudePanelEl) return;
  const ctx = _displayedCtx();
  const max = _modelContextLimit;
  const numEl = _claudePanelEl.querySelector('.ccp-ctx-num');
  const maxEl = _claudePanelEl.querySelector('.ccp-ctx-max');
  const fill = _claudePanelEl.querySelector('.ccp-context .ccp-bar-fill');
  if (numEl) numEl.textContent = ctx ? _fmtCtxNum(ctx) : '—';
  if (maxEl) maxEl.textContent = max ? ' / ' + _fmtCtxNum(max) : '';
  if (fill) fill.style.width = ((ctx && max) ? Math.max(0, Math.min(100, ctx / max * 100)) : 0) + '%';
}

function _usageRowHtml(label, w) {
  if (!w) return '';
  const pct = (typeof w.percent === 'number') ? w.percent : null;
  const pctStr = (pct != null) ? `${Math.round(pct)}%` : '—';
  const fillW = (pct != null) ? Math.max(0, Math.min(100, pct)) : 0;
  const warn = (pct != null && pct >= 80) ? ' warn' : '';
  const reset = w.resets_at ? _fmtReset(w.resets_at) : '';
  return `<div class="ccp-u-row">
      <div class="ccp-u-top"><span class="ccp-u-label">${_escHtml(label)}</span><span class="ccp-u-pct">${pctStr}</span></div>
      <div class="ccp-bar"><div class="ccp-bar-fill${warn}" style="width:${fillW}%"></div></div>
      ${reset ? `<div class="ccp-u-reset">resets ${_escHtml(reset)}</div>` : ''}
    </div>`;
}

/** Best-effort plan usage. Hides the whole section if the endpoint is unavailable
 *  (undocumented — see plugins/engines/claude_code/claude_code_login.py get_plan_usage). */
async function _loadClaudeUsage() {
  const sec = _claudePanelEl && _claudePanelEl.querySelector('.ccp-usage');
  const body = _claudePanelEl && _claudePanelEl.querySelector('.ccp-usage-body');
  if (!sec || !body) return;
  try {
    const res = await fetch(_CLAUDE_USAGE_URL, { headers: { ...authHeaders() } });
    const d = res.ok ? await res.json() : null;
    const rows = d && d.available
      ? _usageRowHtml('5-hour', d.five_hour) + _usageRowHtml('Weekly', d.weekly) : '';
    if (rows) { body.innerHTML = rows; sec.style.display = ''; }
    else sec.style.display = 'none';
  } catch (_) {
    sec.style.display = 'none';
  }
}

/** Load the engine's model rows + effort buttons, ticking whatever the agent is
 *  configured to use (from its claude_code/codex_code metadata lane). Uses the
 *  LIVE catalog from the CLI when available (per-model effort levels), else the
 *  curated static list. */
async function _loadEngineModels() {
  const list = _claudePanelEl && _claudePanelEl.querySelector('.ccp-model-list');
  const effBox = _claudePanelEl && _claudePanelEl.querySelector('.ccp-effort-btns');
  if (!list) return;
  const catalog = (await _getEngineCatalog()) || _engineCatalog();
  if (!catalog) return;
  const aid = app.currentAgentId || '';
  let current = '';
  let effort = '';
  try {
    const uq = `?user_id=${encodeURIComponent(app.currentUserId || '')}`;
    const res = await fetch(`/api/v1/agents/${encodeURIComponent(aid)}${uq}`, { headers: { ...authHeaders() } });
    if (res.ok) {
      const a = await res.json();
      const lane = (a.agent || a)[catalog.lane] || {};
      current = String(lane.model || '').trim();
      effort = String(lane.effort || '').trim();
    }
  } catch (_) { /* best-effort */ }
  _engineCurModel = current;
  _engineCurEffort = effort;
  // Model rows: Claude ticks via alias (a stored full id like claude-opus-4-5
  // still matches the Opus preset); Codex ticks on exact id.
  const isClaude = catalog.lane === 'claude_code';
  const activeAlias = isClaude ? _claudeAliasOf(current) : current;
  list.innerHTML = '';
  catalog.models.forEach(m => {
    const row = document.createElement('div');
    row.className = 'cmp-item ccp-model-row' + (m.v === activeAlias ? ' cmp-selected' : '');
    row.innerHTML = `<span class="ccp-m-main"><span class="ccp-m-label">${_escHtml(m.label)}</span>`
      + `<span class="ccp-m-sub">${_escHtml(m.sub)}</span></span><span class="cmp-item-status"></span>`;
    row.addEventListener('click', () => _onPickEngineModel(row, m.v));
    list.appendChild(row);
  });
  // Effort buttons — per-model levels when the live catalog provides them.
  _renderEngineEffortButtons(catalog, current, effort);
}

/** Render the effort row for the currently-selected model. */
function _renderEngineEffortButtons(catalog, model, effort) {
  const effBox = _claudePanelEl && _claudePanelEl.querySelector('.ccp-effort-btns');
  if (!effBox || !catalog) return;
  const isClaude = catalog.lane === 'claude_code';
  const options = _effortOptions(catalog, model);
  effBox.innerHTML = '';
  options.forEach(opt => {
    const b = document.createElement('button');
    b.type = 'button';
    b.className = 'cmp-eff-btn' + (opt.v === effort ? ' cmp-eff-on' : '');
    b.textContent = opt.label;
    b.title = opt.v
      ? `Reasoning effort ${opt.v}${isClaude ? ' (claude --effort)' : ' (codex model_reasoning_effort)'}`
      : "Use the CLI's own default effort";
    b.addEventListener('click', () => _onPickEngineEffort(b, opt.v));
    effBox.appendChild(b);
  });
}

/** Save one field (model / effort) onto the engine's metadata lane. */
async function _saveEngineField(field, value) {
  const aid = app.currentAgentId || '';
  const catalog = _engineCatalog();
  if (!aid || !catalog) return false;
  try {
    const res = await fetch(`/api/v1/agents/${encodeURIComponent(aid)}`, {
      method: 'PUT',
      headers: { 'Content-Type': 'application/json', ...authHeaders() },
      body: JSON.stringify({ user_id: app.currentUserId || '', [catalog.lane]: { [field]: value } }),
    });
    return res.ok;
  } catch (_) { return false; }
}

async function _onPickEngineModel(row, value) {
  if (_claudeModelSaving) return;
  _claudeModelSaving = true;
  const status = row.querySelector('.cmp-item-status');
  if (status) status.innerHTML = '<span class="cmp-spinner"></span>';
  const [ok] = await Promise.all([_saveEngineField('model', value), new Promise(r => setTimeout(r, 280))]);
  if (status) status.innerHTML = ok
    ? '<span class="cmp-check" title="Saved">✓</span>'
    : '<span class="cmp-cross" title="Save failed">✕</span>';
  if (ok) {
    _engineCurModel = value;
    _claudePanelEl.querySelectorAll('.ccp-model-row.cmp-selected').forEach(el => el.classList.remove('cmp-selected'));
    row.classList.add('cmp-selected');
    refreshModelContext();   // footer name + window follow the new model
    // Per-model effort levels may differ — re-render the effort row for it.
    const cat = _engineCatalogNow();
    _renderEngineEffortButtons(cat, value, _engineCurEffort);
    setTimeout(() => { if (status) status.innerHTML = ''; }, 900);
  }
  _claudeModelSaving = false;
}

async function _onPickEngineEffort(btn, level) {
  if (_claudeModelSaving) return;
  _claudeModelSaving = true;
  btn.classList.add('cmp-eff-saving');
  const [ok] = await Promise.all([_saveEngineField('effort', level), new Promise(r => setTimeout(r, 280))]);
  btn.classList.remove('cmp-eff-saving');
  if (ok) {
    _engineCurEffort = level;
    _claudePanelEl.querySelectorAll('.cmp-eff-btn.cmp-eff-on').forEach(el => el.classList.remove('cmp-eff-on'));
    btn.classList.add('cmp-eff-on');
  }
  _claudeModelSaving = false;
}

function _toggleEnginePanel() {
  if (_claudePanelEl && _claudePanelEl.style.display !== 'none') {
    _claudePanelEl.style.display = 'none';
    return;
  }
  _buildClaudePanel();
  // Anchor identically to the model picker: bottom-pinned above the footer,
  // left-aligned to the chat pill.
  const vAnchor = modelBtnEl
    || (modelCtxEl && modelCtxEl.style.display !== 'none' ? modelCtxEl : null)
    || document.getElementById('chat-pill-stats')
    || document.getElementById('chat-input-row');
  const pill = document.getElementById('chat-input-row');
  const hAnchor = pill || vAnchor;
  if (!vAnchor || !hAnchor) return;
  const vRect = vAnchor.getBoundingClientRect();
  const hRect = hAnchor.getBoundingClientRect();
  _claudePanelEl.style.top = 'auto';
  _claudePanelEl.style.bottom = (window.innerHeight - vRect.top + 6) + 'px';
  _claudePanelEl.style.left = Math.max(4, Math.min(hRect.left, window.innerWidth - 290)) + 'px';
  setTimeout(() => { _claudePanelEl.style.display = 'block'; }, 0);
  _renderClaudeContext();
  _loadEngineModels();
  // Plan-usage section is Claude-specific; skip for other engines.
  const catalog = _engineCatalog();
  if (catalog && catalog.lane === 'claude_code') _loadClaudeUsage();
}

function resetTokens() {
  cumulativeIn = 0;
  cumulativeOut = 0;
  _sessionCreditUnits = 0;
  _sessionHasUnknownPriced = false;
  _pendingOutEstimate = 0;
  _streamCharCount = 0;
  _pendingCtxEstimate = 0;
  _contextTokens = 0; // new session — never carry the previous session's high-water mark
  _ctxComposition = null; // neither the previous session's composition breakdown
  _contextRevision += 1;
  _setSpinnerDir(null);
  if (tokensInEl) tokensInEl.textContent = '0';
  if (tokensOutEl) tokensOutEl.textContent = '0';
  _updateTokenBar();
  _renderCost();
  // Re-render the ctx readout too, so a reset can't leave the previous
  // session's stale ctx (e.g. "88K") showing beside a freshly-zeroed cost.
  _renderCtxIndicator();
  try { localStorage.removeItem(_TOKEN_CACHE_KEY); } catch (_) {}
}

// ── Live per-stage elapsed timer ────────────────────────────────────────────
// Each note (Building tools / Loading history / Searching memory / Preparing /
// Thinking… / Writing reply…) shows the seconds spent ON THAT STAGE, ticking
// live, so the user can SEE which step is slow — not just that "memory" ran.
// `currentNote` stays the bare label (so setNote's dedupe still works); the
// element shows label + suffix, refreshed on a light interval.
let _stageTimer = null;   // setInterval handle ticking the elapsed suffix
let _stageStart = 0;      // performance.now() when the current stage began
// The stage clock is useful feedback, but its transcript mirror triggers
// layout/scroll work. A one-second cadence is human-readable and avoids
// continuously rewriting the transcript while an agent is thinking.
const _STAGE_TICK_MS = 1000;

// System/one-shot notes that shouldn't carry a running clock.
const _NO_CLOCK_NOTES = new Set(['Error', 'Stopped', 'Blocked for safety']);

function _clearStageTimer() {
  if (_stageTimer) { clearInterval(_stageTimer); _stageTimer = null; }
  _stageStart = 0;
}

function _renderNote() {
  let txt = currentNote || '';
  if (_stageStart && txt) {
    const elapsedSecs = Math.floor((performance.now() - _stageStart) / 1000);
    if (elapsedSecs >= 1) txt += '  \u00b7  ' + elapsedSecs + 's';
  }
  // A transcript mirror is independent of whether this footer profile places
  // an activity chip. This keeps progress durable in the transcript without
  // reviving the static fallback outside a configured row.
  if (textEl) textEl.textContent = txt;
  // Duplicate the same text into the transcript as an agent bubble
  // (mirror_activity_in_transcript) - the bar above the pill stays untouched.
  // The note is session-scoped: mirrorActivityNote re-checks the session id and
  // refuses to paint when it doesn't match the session on screen.
  if (app.mirrorActivityNote) {
    try { app.mirrorActivityNote(txt, _noteSessionId, _noteTurnId); } catch (_) { /* non-fatal */ }
  }
}

function setNote(text) {
  // Dedupe identical notes — this also tames the per-chunk spam from `stream`
  // events, which would otherwise re-fire on every token.
  if (!text || text === currentNote) return;
  // While a stop was requested, keep "Stopping…" pinned. The backend is still
  // unwinding (stream tail, tool results, restore notes from the reconcile
  // poll) and none of that may replace the stopping note — only system
  // events, which clear _stopPending before calling setNote, may override it.
  if (app._stopPending && text !== 'Stopping…') return;
  currentNote = text;
  // Begin a fresh stage clock (unless this is a system note), then refresh on
  // a light interval so the elapsed seconds tick up live next to the label.
  _clearStageTimer();
  if (active && !_NO_CLOCK_NOTES.has(text)) {
    _stageStart = performance.now();
    _stageTimer = setInterval(_renderNote, _STAGE_TICK_MS);
  }
  _renderNote();
  _animateText();
}

// ── Mock (ambient) activity notes ───────────────────────────────────────────
// Progress notes the CLIENT fabricates for its OWN background work — a cached
// transcript re-sync, a run parked in the auto-resume ladder. They are not run
// events, so they follow the SAME rendering rule as the real run notes:
//   • they tick in the footer activity element (the one configured `activity`
//     control above the pill), and
//   • they reach the transcript ONLY when that mirror is configured on
//     (chat_ui.json → mirror_activity_in_transcript) — never forced.
// This catalogue is the single home for the fabricated labels, so the activity
// element is the one surface that renders "all the various mock activities".
// The optimistic client notes for real actions ("Compacting...", "Sending…",
// "Uploading attachment…", "Stopping…", "Creating BTW session…", "Codex is
// working…") already route through chatActivityStart/setNote, i.e. this same
// bar, and — like every other activity note — they reach the transcript only
// when the mirror is configured on. The two below need their own lane because
// they are raised while NO run is active (a background re-sync, a parked run),
// so there is no run note for them to ride.
const MOCK_ACTIVITY_NOTES = Object.freeze({
  assessing: 'Assessing…',       // cached transcript re-sync (session-load.js)
  reassessing: 'Re-assessing…',  // run parked in the auto-resume ladder (reconcile)
});

let _ambientNote = '';        // bare label currently shown in the bar ('' = none)
let _ambientSessionId = '';   // session the ambient note belongs to
let _ambientStart = 0;        // performance.now() when the ambient note began
let _ambientTimer = null;     // ticks the ambient note's elapsed suffix
let _ambientMirrored = false; // did it actually paint a transcript row?

// Resolve a mock label ('' when unknown) — the catalogued spelling call sites
// share so a fabricated note is never re-typed at the use site.
function mockActivityLabel(key) {
  return MOCK_ACTIVITY_NOTES[key] || '';
}

// Forget the ambient note WITHOUT the public teardown — used when a real run
// takes over the bar or the viewed session changes.
function _dropAmbient() {
  if (_ambientTimer) { clearInterval(_ambientTimer); _ambientTimer = null; }
  _ambientNote = '';
  _ambientSessionId = '';
  _ambientStart = 0;
  _ambientMirrored = false;
  if (barEl) barEl.classList.remove('ambient');
}

// Show a mock note in the footer activity element. `key` is a MOCK_ACTIVITY_NOTES
// key (or a literal label). Returns the label shown, or null when a real run owns
// the bar — a run note always wins. Pass { ticking:false } for a static note.
function mockActivityNote(key, opts = {}) {
  _acquireElements();   // self-heal refs if init ran before the panel partial landed
  const label = MOCK_ACTIVITY_NOTES[key] || (typeof key === 'string' ? key : '');
  if (!label) { mockActivityNoteClear(); return null; }
  if (active) return null;   // a real run owns the bar
  _ambientNote = label;
  _ambientSessionId = opts.sessionId || app.currentSessionId || '';
  _ambientStart = performance.now();
  if (rootEl) rootEl.classList.add('visible');
  if (barEl) barEl.classList.add('ambient');
  _renderAmbientNote();
  if (_ambientTimer) { clearInterval(_ambientTimer); _ambientTimer = null; }
  if (opts.ticking !== false) {
    _ambientTimer = setInterval(_renderAmbientNote, _STAGE_TICK_MS);
  }
  return label;
}

function _renderAmbientNote() {
  if (!_ambientNote) return '';
  let txt = _ambientNote;
  if (_ambientStart) {
    const secs = Math.floor((performance.now() - _ambientStart) / 1000);
    if (secs >= 1) txt += '  \u00b7  ' + secs + 's';
  }
  if (textEl) textEl.textContent = txt;
  // Same rule as a run note: footer always, transcript only when the activity
  // mirror is configured. mirrorActivityNote re-checks the session id, so a note
  // painted for another session can never bleed into the one on screen.
  if (app.mirrorActivityNote) {
    try { _ambientMirrored = !!app.mirrorActivityNote(txt, _ambientSessionId, undefined); }
    catch (_) { /* non-fatal */ }
  }
  return txt;
}

// Retire the mock note and hand the bar back. Only a real run keeps the bar
// standing; otherwise the note's presence was the only thing lighting it.
function mockActivityNoteClear() {
  const had = !!_ambientNote;
  const wasMirrored = _ambientMirrored;
  const owner = _ambientSessionId;
  _dropAmbient();
  if (!active && !resting) {
    if (textEl) textEl.textContent = '';
    if (rootEl) rootEl.classList.remove('visible');
  }
  // Retire the transcript row too — but only the one this note painted, and only
  // when no real run owns the live line.
  if (had && wasMirrored && !active && app.mirrorActivityEnd) {
    try { app.mirrorActivityEnd(owner || app.currentSessionId); } catch (_) { /* non-fatal */ }
  }
}

// "Turn N: " prefix for the in-loop notes (tool calls), blank before turn 1.
function _turnPrefix() {
  return currentTurn > 0 ? 'Turn ' + currentTurn + ': ' : '';
}

// Show/hide the chevron + clickability based on whether there's anything to open.
function _updateBarAffordance() {
  if (!barEl) return;
  const has = toolCalls.length > 0;
  barEl.classList.toggle('has-tools', has);
  if (!has && expanded) closePanel();
}

// Wipe this turn's tool list + panel (called at every new-turn boundary).
function _resetForNewTurn() {
  toolCalls = [];
  currentToolAnchor = null;
  app._turnHasBubble = false;
  closePanel();
  renderPanel();
  _updateBarAffordance();
}

// Switch the indicator into its live "working" look.
function _activate() {
  // A real run now owns the bar — drop any mock note still ticking there.
  _dropAmbient();
  active = true;
  // Expose turn-active state globally so background pollers (session list /
  // related-sessions refresh in session-init.js) can PAUSE while a turn is
  // streaming — their heavy, un-offloaded DB endpoints otherwise jam the
  // server's event loop and starve the live reply. Cleared in stop().
  try { window.__agentTurnActive = true; } catch (_) {}
  if (typeof app.beginRunControls === 'function') {
    try { app.beginRunControls(_noteTurnId); } catch (_) {}
  }
  resting = false;
  // Cancel any pending text-blank scheduled by a just-finished turn's stop(). A
  // re-activation within that 260ms window would otherwise have its fresh note
  // wiped, leaving the bar visible with no text — i.e. a lone idle dot.
  _clearTextTimer();
  if (rootEl) { rootEl.classList.remove('resting'); rootEl.classList.add('visible'); }
  if (pillEl) pillEl.classList.add('thinking');
  _startThinkingRamp();
  _updateTokenBar();
}

function start(initialNote, ownerTurnId) {
  // RECORDER-HOOK: recorder.js observes decisions only. Remove this try block and comment together.
  try {
    window.__recorderTrace?.('run.started', {
      session_id: app.currentSessionId,
      run_id: ownerTurnId,
    });
  } catch (_) {}

  _acquireElements();   // self-heal refs if init ran before the panel partial landed
  if (ownerTurnId) _noteTurnId = String(ownerTurnId);
  _clearTextTimer();
  _clearEndTimer();
  _turnEnded = false;   // user just sent — a fresh turn is beginning
  if (!active) { _resetForNewTurn(); _activate(); }
  _armWatchdog();
  _noteSessionId = app.currentSessionId;   // the note belongs to the session being sent from
  setNote(initialNote || 'Thinking…');
}

function stop() {
  _disarmWatchdog();
  _clearEndTimer();
  _stopThinkingRamp();
  _clearStageTimer();
  if (!active && !resting) return;
  active = false;
  try { window.__agentTurnActive = false; } catch (_) {}
  if (pillEl) pillEl.classList.remove('thinking');
  _setSpinnerDir(null);

  // A system response proves the agent has progressed past every tool call in
  // this inference turn.  A dropped WebSocket `tool_result` must therefore not
  // leave a permanent "running…" card in the transcript.  Keep the call for
  // auditability, but settle its presentation; a reload can still hydrate the
  // full persisted result from the session record.
  for (const call of toolCalls) {
    if (call.status !== 'running') continue;
    call.status = 'done';
    call.result = 'Completed — this browser did not receive the live tool result.';
    call.durationMs = null;
  }

  // Attach any remaining tool calls from the last turn (if not already
  // attached by a turn_start boundary). Each turn's calls are attached
  // incrementally as turns progress, so this only catches the final turn.
  if (toolCalls.length > 0 && app.attachToolCallsToLastBubble) {
    const calls = toolCalls.slice();
    app.attachToolCallsToLastBubble(calls, undefined, currentToolAnchor);
  }

  // Always fade the activity bar out — tool calls now live on the bubble.
  resting = false;
  currentNote = '';
  if (rootEl) rootEl.classList.remove('visible', 'resting');
  closePanel();
  _clearTextTimer();
  clearTimer = setTimeout(() => { if (textEl) textEl.textContent = ''; }, 260);
  _updateBarAffordance();
  _updateTokenBar();
  // Settle the transcript mirror: remove the live note row; keep the bubble
  // only if it carries this turn's tool calls / updates.
  if (app.mirrorActivityEnd) {
    try { app.mirrorActivityEnd(); } catch (_) { /* non-fatal */ }
  }
}

// Show a final note (Error / Stopped) briefly, then settle.
function _endSoon() {
  _disarmWatchdog();
  _clearEndTimer();
  endTimer = setTimeout(() => { endTimer = null; stop(); }, 1000);
}

// Called when the viewed session changes — a glow/list left over from the
// previous session must not linger on the shared pill.
export function chatActivitySessionChanged() {
  _disarmWatchdog();
  _stopThinkingRamp();
  _clearTextTimer();
  _clearEndTimer();
  _clearStageTimer();   // a stale per-stage tick must not keep re-painting
  active = false;
  // A stop requested on the previous session must not pin "Stopping…" (or
  // swallow notes) on the session the user just switched to.
  app._stopPending = false;
  try { window.__agentTurnActive = false; } catch (_) {}
  if (typeof app.endRunControls === 'function') {
    try { app.endRunControls(); } catch (_) {}
  }
  resting = false;
  _turnEnded = false;
  toolCalls = [];
  currentToolAnchor = null;
  currentTurn = 0;
  currentNote = '';
  _noteSessionId = '';
  _dropAmbient();   // a mock note belongs to the session it was raised for
  app._activeToolGroupBubble = null;
  app._turnHasBubble = false;
  // The viewed session is not processing until proven otherwise. Resetting this
  // lets the reconcile poll treat the newly-opened session as fresh, so a run
  // already in progress for it (automation / another device) is restored
  // instead of being skipped by the wasProcessing gate.
  app.isProcessing = false;
  closePanel();
  renderPanel();
  _updateBarAffordance();
  // A mirror bubble left over from the previous session must not linger on the
  // session the user just switched to (same cleanup as stop()).
  if (app.mirrorActivityEnd) {
    try { app.mirrorActivityEnd(); } catch (_) { /* non-fatal */ }
  }
  if (pillEl) pillEl.classList.remove('thinking');
  if (rootEl) rootEl.classList.remove('visible', 'resting');
  if (textEl) textEl.textContent = '';
  resetTokens();
  // Re-fetch model slot data for the current session so the model selector
  // (#chat-model-btn) reflects the new session's active model, not the
  // previous session's. _fetchModelsForPicker populates _allSlots and
  // _currentSlotRef, and the wrapped version auto-calls _syncModelChanger.
  // We also call _renderModelBtn() to update the dedicated button text.
  _fetchModelsForPicker().then(() => _renderModelBtn());
}

// Re-light the live indicator after a page refresh, from the durable run-state
// snapshot the server persists for the in-flight operation (see run_state_set_op
// / session_runs.current_op). `op` is the JSON string (or parsed object) from
// run.current_op; null/absent means "active but between tools" → generic note.
// Live WS events then take over and update it as the turn continues.
//
// `fallbackNote` replaces that generic note when the run is parked in the
// auto-resume ladder (`run.recovering`): no live events are coming, so the
// caller passes the mock "re-assessing…" note and the reconcile poll re-enters
// here every tick purely to keep it armed until the revival fails.
function chatActivityRestore(op, fallbackNote) {
  _acquireElements();   // self-heal refs if init ran before the panel partial landed
  let data = op;
  if (typeof op === 'string') {
    try { data = JSON.parse(op); } catch (_) { data = null; }
  }
  const note = (data && data.note) || fallbackNote || 'Working…';
  // Hot path: a recovering run carries no live op snapshot, so a repeat call
  // says nothing new — just re-arm the in-turn watchdog (whose expiry would
  // otherwise clear the bar) and skip the work below. Re-running _activate() on
  // every poll tick would needlessly restart the thinking ramp and re-issue the
  // run-controls wiring.
  if (!data && active && currentNote === note) { _armWatchdog(); return; }
  _turnEnded = false;   // restoring an in-flight turn — not post-turn
  _activate();
  _noteSessionId = app.currentSessionId;   // restoring the CURRENT session's run
  if (data && typeof data.turn === 'number' && data.turn > 0) currentTurn = data.turn;
  if (data && data.tool) addToolCall(data.tool, data.args || {});
  setNote(note);
  _armWatchdog();
}

// ── Tool-call accumulation ──────────────────────────────────────────────────

function addToolCall(tool, args, toolCallId) {
  toolCalls.push({
    tool: tool || 'tool',
    toolCallId: toolCallId || null,
    args: args || {},
    status: 'running',
    result: null,
    durationMs: null,
    errorType: null,
    turn: currentTurn,
    open: false,
  });
  _updateBarAffordance();
  if (expanded) renderPanel();
}

function resolveToolResult(tool, result, durationMs, isError, errorType, toolCallId) {
  // Pair with the oldest still-running call of the same name (no call-id in the
  // event stream, so order is the best we have).
  let entry = null;
  if (toolCallId) {
    entry = toolCalls.find(call =>
      call.toolCallId === toolCallId && call.status === 'running',
    ) || null;
  }
  for (let i = 0; !entry && i < toolCalls.length; i++) {
    if (toolCalls[i].tool === tool && toolCalls[i].status === 'running') { entry = toolCalls[i]; break; }
  }
  if (!entry) {
    // A result with no matching open call (e.g. mid-turn reattach) — record it.
    entry = { tool: tool || 'tool', args: {}, status: 'running', result: null, durationMs: null, errorType: null, turn: currentTurn, open: false };
    toolCalls.push(entry);
  }
  entry.status = isError ? 'error' : 'done';
  entry.result = result == null ? '' : String(result);
  entry.durationMs = (typeof durationMs === 'number') ? durationMs : null;
  entry.errorType = errorType || null;
  _updateBarAffordance();
  if (expanded) renderPanel();
}

// Render a completed synthetic tool call (vision ingestion / loop-node memory)
// straight onto the transcript as its own foldable tool-only bubble, in the spot
// it happened — memory_search/vision after the user message before the reply,
// memory_save after the reply. Bypasses the activity-bar turn accounting entirely
// so it can't be wiped by an ingestion-phase or post-turn reset. No-ops safely if
// the chat bubble layer isn't present (e.g. a non-chat host of this module).
// `info`: { args, result, error, durationMs, errorType, id }. Missing args fall
// back to whatever was stashed in _pendingSynthArgs for this tool.
function _renderSynthToolBubble(toolName, info, prebuiltCall) {
  if (!app.chatMessages || typeof app.addChatBubble !== 'function'
      || typeof app.attachToolCallsToLastBubble !== 'function') return;
  info = info || {};
  const call = prebuiltCall || _synthToolCall(toolName, info);
  try {
    // Dedicated tool-only bubble with an explicit target, so it doesn't merge
    // into (or get attributed to) any prior turn's tool group.
    const bubble = app.addChatBubble('agent', '', 'tool-only', undefined,
      'synth-live-' + toolName + '-' + (info.id || ''));
    app.attachToolCallsToLastBubble([call], bubble);
    // This out-of-band bubble must not absorb the agent's later tool-only turns.
    app._activeToolGroupBubble = null;
  } catch (_) { /* best-effort live render; reload shows it from the persisted row */ }
}

// Build the transcript row for one synthetic tool completion, consuming whatever
// its tool_call event stashed in _pendingSynthArgs.
function _synthToolCall(toolName, info) {
  info = info || {};
  const call = {
    tool: toolName,
    args: info.args || _pendingSynthArgs[toolName] || {},
    status: info.error ? 'error' : 'done',
    result: info.result == null ? '' : String(info.result),
    durationMs: (typeof info.durationMs === 'number') ? info.durationMs : null,
    errorType: info.errorType || null,
    turn: 0,
    open: false,
  };
  delete _pendingSynthArgs[toolName];
  return call;
}

// Fold a recovery retry into the run it resumed instead of giving it a bubble of
// its own, so the retry reads as part of that run's activity — the same place any
// other tool call of the run lands. Two ways to find that run's group: the OPEN
// activity shell (a closer reply or new user turn clears that pointer, which is
// exactly the "no closer or user message in between" condition), or — because a
// crash/interrupt settles the shell and clears the pointer — the same shell
// re-resolved by the run's durable turn id, the key its own tool lines group by.
// Returns false when there is no group to join (e.g. a fresh page), leaving the
// caller to render the retry as its own bubble.
function _attachRecoveryToOpenGroup(call) {
  if (!app.chatMessages || typeof app.attachToolCallsToLastBubble !== 'function') return false;
  const open = app._activeToolGroupBubble;
  if (open && open.isConnected) {
    try { app.attachToolCallsToLastBubble([call], open); return true; }
    catch (_) { return false; }
  }
  const owner = String(_noteTurnId || '');
  if (!owner) return false;
  try { app.attachToolCallsToLastBubble([call], undefined, { turnId: owner }); return true; }
  catch (_) { return false; }
}

// ── Panel rendering ─────────────────────────────────────────────────────────

function _fmtArgs(args) {
  if (args == null) return '(none)';
  try {
    if (typeof args === 'object' && Object.keys(args).length === 0) return '(none)';
    return JSON.stringify(args, null, 2);
  } catch (_) {
    return String(args);
  }
}

function _makeCopyBtn(text, label) {
  const btn = document.createElement('button');
  btn.type = 'button';
  btn.className = 'ca-tool-copy-btn';
  btn.title = 'Copy ' + label;
  btn.textContent = 'copy';
  btn.addEventListener('click', (e) => {
    e.stopPropagation();
    copyText(text).then(() => {
      btn.textContent = 'copied';
      btn.classList.add('copied');
      setTimeout(() => { btn.textContent = 'copy'; btn.classList.remove('copied'); }, 1500);
    }).catch(() => {});
  });
  return btn;
}

// Delete button for a tool-call row. Works in two contexts:
//   • Bubble accordion — resolves the DB interaction_id from the parent
//     `.chat-bubble`'s `data-msg-id` (available live OR after reload).
//   • Activity-bar panel — requires `_detailMsgId` (only present after reload).
// Clicking removes the tool call from the DB plus its DOM row.
function _makeToolDeleteBtn(entry, row) {
  const btn = document.createElement('button');
  btn.type = 'button';
  btn.className = 'ca-tool-delete-btn';
  btn.title = 'Delete this tool call from context';
  btn.innerHTML = '<i data-lucide="trash-2" style="width:11px;height:11px;"></i>';

  // Auto-reset timer so the armed state expires after 4s.
  let _armedTimer = null;

  function _resetToolDeleteBtn() {
    if (_armedTimer) { clearTimeout(_armedTimer); _armedTimer = null; }
    btn.dataset.state = '';
    btn.title = 'Delete this tool call from context';
    btn.innerHTML = '<i data-lucide="trash-2" style="width:11px;height:11px;"></i>';
    btn.classList.remove('warning');
  }

  function _resetAllToolDeleteBtns() {
    document.querySelectorAll('.ca-tool-delete-btn[data-state="warning"]').forEach(b => {
      b.dataset.state = '';
      b.title = 'Delete this tool call from context';
      b.innerHTML = '<i data-lucide="trash-2" style="width:11px;height:11px;"></i>';
      b.classList.remove('warning');
    });
  }

  btn.addEventListener('click', async (e) => {
    e.stopPropagation();

    // Two-click confirm with auto-timeout
    if (btn.dataset.state !== 'warning') {
      _resetAllToolDeleteBtns();
      btn.dataset.state = 'warning';
      btn.title = 'Click again to remove this tool call';
      btn.innerHTML = '<i data-lucide="alert-triangle" style="width:12px;height:12px;"></i>';
      btn.classList.add('warning');
      if (_armedTimer) clearTimeout(_armedTimer);
      _armedTimer = setTimeout(_resetToolDeleteBtn, 4000);
      return;
    }

    // ── Second click: confirm the delete ──
    if (_armedTimer) { clearTimeout(_armedTimer); _armedTimer = null; }

    // Mark the row as deleted (strikethrough)
    row.classList.add('deleted');
    btn.style.display = 'none';

    const sid = app.currentSessionId;
    if (!sid) return;

    const bubble = row.closest('.chat-bubble');
    const interactionId = entry._detailMsgId
      || (bubble && (bubble.dataset.msgId || bubble.dataset.turnId))
      || '';
    const idx = (entry._detailIdx != null) ? entry._detailIdx : parseInt(row.dataset.i, 10);
    if (!interactionId || idx == null || isNaN(idx)) return;

    const uid = app.currentUserId || '';
    try {
      const url = apiPath('/api/v1/db/tool-call?db=user.db'
        + '&session_id=' + encodeURIComponent(sid)
        + '&interaction_id=' + encodeURIComponent(interactionId)
        + '&tool_call_idx=' + encodeURIComponent(idx)
        + (uid ? '&user_id=' + encodeURIComponent(uid) : ''));
      const resp = await fetch(url, { method: 'DELETE', headers: { ...authHeaders() } });
      if (!resp.ok) {
        const errData = await resp.json().catch(() => ({}));
        console.warn('Delete tool call failed:', resp.status, errData);
        alert('Could not delete this tool call (server responded ' + resp.status + ').');
        row.classList.remove('deleted');
        btn.style.display = '';
        _resetToolDeleteBtn();
        return;
      }

      // Inject restore + permanent-delete buttons into the row head
      _injectToolDeletedActions(row, interactionId, idx);

    } catch (e) {
      console.warn('Delete tool call error:', e);
      alert('Could not delete this tool call — no response from the server.');
      row.classList.remove('deleted');
      btn.style.display = '';
      _resetToolDeleteBtn();
    }
  });
  return btn;
}

/**
 * Inject restore + permanent-delete buttons into a deleted tool call row's head.
 * Both use the two-click hazard confirm pattern.
 */
function _injectToolDeletedActions(row, interactionId, idx) {
  if (!row) return;
  const head = row.querySelector('.ca-tool-head');
  if (!head) return;

  const btnRow = document.createElement('span');
  btnRow.style.cssText = 'display:inline-flex;gap:3px;margin-left:auto;flex:0 0 auto;';

  const restoreBtn = document.createElement('button');
  restoreBtn.type = 'button';
  restoreBtn.className = 'ca-tool-delete-btn';
  restoreBtn.title = 'Restore this tool call';
  restoreBtn.innerHTML = '<i data-lucide="undo-2" style="width:11px;height:11px;"></i>';
  restoreBtn.addEventListener('click', async (e) => {
    e.stopPropagation();
    if (restoreBtn.dataset.state !== 'warning') {
      restoreBtn.dataset.state = 'warning';
      restoreBtn.title = 'Click again to restore';
      restoreBtn.innerHTML = '<i data-lucide="alert-triangle" style="width:12px;height:12px;"></i>';
      restoreBtn.classList.add('warning');
      const timer = setTimeout(() => {
        restoreBtn.dataset.state = '';
        restoreBtn.title = 'Restore this tool call';
        restoreBtn.innerHTML = '<i data-lucide="undo-2" style="width:11px;height:11px;"></i>';
        restoreBtn.classList.remove('warning');
      }, 4000);
      restoreBtn._timer = timer;
      return;
    }
    if (restoreBtn._timer) { clearTimeout(restoreBtn._timer); restoreBtn._timer = null; }
    const sid = app.currentSessionId;
    if (!sid || !interactionId || idx == null) return;
    const uid = app.currentUserId || '';
    try {
      const url = apiPath('/api/v1/db/tool-call/restore?db=user.db'
        + '&session_id=' + encodeURIComponent(sid)
        + '&interaction_id=' + encodeURIComponent(interactionId)
        + '&tool_call_idx=' + encodeURIComponent(idx)
        + (uid ? '&user_id=' + encodeURIComponent(uid) : ''));
      const resp = await fetch(url, { method: 'POST', headers: { ...authHeaders() } });
      if (resp.ok) {
        row.classList.remove('deleted');
        btnRow.remove();
      } else {
        alert('Could not restore this tool call (server responded ' + resp.status + ').');
        restoreBtn.dataset.state = '';
        restoreBtn.title = 'Restore this tool call';
        restoreBtn.innerHTML = '<i data-lucide="undo-2" style="width:11px;height:11px;"></i>';
        restoreBtn.classList.remove('warning');
      }
    } catch (e) {
      alert('Could not restore this tool call — no response from the server.');
    }
  });
  btnRow.appendChild(restoreBtn);

  const permDeleteBtn = document.createElement('button');
  permDeleteBtn.type = 'button';
  permDeleteBtn.className = 'ca-tool-delete-btn';
  permDeleteBtn.title = 'Permanently delete this tool call';
  permDeleteBtn.innerHTML = '<i data-lucide="trash-2" style="width:11px;height:11px;"></i>';
  permDeleteBtn.addEventListener('click', async (e) => {
    e.stopPropagation();
    if (permDeleteBtn.dataset.state !== 'warning') {
      permDeleteBtn.dataset.state = 'warning';
      permDeleteBtn.title = 'Click again to permanently erase';
      permDeleteBtn.innerHTML = '<i data-lucide="alert-triangle" style="width:12px;height:12px;"></i>';
      permDeleteBtn.classList.add('warning');
      const timer = setTimeout(() => {
        permDeleteBtn.dataset.state = '';
        permDeleteBtn.title = 'Permanently delete this tool call';
        permDeleteBtn.innerHTML = '<i data-lucide="trash-2" style="width:11px;height:11px;"></i>';
        permDeleteBtn.classList.remove('warning');
      }, 4000);
      permDeleteBtn._timer = timer;
      return;
    }
    if (permDeleteBtn._timer) { clearTimeout(permDeleteBtn._timer); permDeleteBtn._timer = null; }
    const sid = app.currentSessionId;
    if (!sid || !interactionId || idx == null) return;
    const uid = app.currentUserId || '';
    try {
      const url = apiPath('/api/v1/db/tool-call?db=user.db'
        + '&session_id=' + encodeURIComponent(sid)
        + '&interaction_id=' + encodeURIComponent(interactionId)
        + '&tool_call_idx=' + encodeURIComponent(idx)
        + (uid ? '&user_id=' + encodeURIComponent(uid) : '')
        + '&permanent=true');
      const resp = await fetch(url, { method: 'DELETE', headers: { ...authHeaders() } });
      if (resp.ok) {
        row.remove();
      } else {
        alert('Could not permanently delete this tool call (server responded ' + resp.status + ').');
        permDeleteBtn.dataset.state = '';
        permDeleteBtn.title = 'Permanently delete this tool call';
        permDeleteBtn.innerHTML = '<i data-lucide="trash-2" style="width:11px;height:11px;"></i>';
        permDeleteBtn.classList.remove('warning');
      }
    } catch (e) {
      alert('Could not permanently delete this tool call — no response from the server.');
    }
  });
  btnRow.appendChild(permDeleteBtn);

  // Insert after the meta span, before the caret
  const caret = head.querySelector('.ca-tool-caret');
  if (caret) head.insertBefore(btnRow, caret);
  else head.appendChild(btnRow);
}

function _buildRow(entry, idx) {
  const row = document.createElement('div');
  row.className = 'ca-tool-row' + (entry.open ? ' open' : '') + (entry._deleted ? ' deleted' : '');
  row.dataset.i = String(idx);
  // Tag with the parent interaction id so per-message delete can selectively mark rows.
  if (entry._detailMsgId) row.dataset.detailMsgId = entry._detailMsgId;

  const head = document.createElement('button');
  head.type = 'button';
  head.className = 'ca-tool-head';
  head.setAttribute('aria-expanded', entry.open ? 'true' : 'false');

  const status = document.createElement('span');
  status.className = 'ca-tool-status ca-status-' + (entry._deleted ? 'deleted' : entry.status);
  status.textContent = entry._deleted ? '⊘' : entry.status === 'done' ? '✓' : entry.status === 'error' ? '✕' : entry.status === 'remote_placeholder' ? '⊘' : '';
  head.appendChild(status);

  if (entry.turn > 0) {
    const turn = document.createElement('span');
    turn.className = 'ca-tool-turn';
    turn.textContent = 'Turn ' + entry.turn;
    head.appendChild(turn);
  }

  const name = document.createElement('span');
  name.className = 'ca-tool-name';
  name.textContent = entry.tool;
  head.appendChild(name);

  const meta = document.createElement('span');
  meta.className = 'ca-tool-meta';
  if (entry._deleted) meta.textContent = 'deleted';
  else if (entry.status === 'running') meta.textContent = 'running…';
  else if (entry.status === 'error') meta.textContent = entry.errorType || 'error';
  else if (entry.status === 'remote_placeholder') meta.textContent = 'remote device';
  else meta.textContent = (entry.durationMs != null) ? entry.durationMs + 'ms' : 'done';
  head.appendChild(meta);

  // Copy-all button for the whole tool call (skip for remote placeholders and deleted)
  if (entry.status !== 'remote_placeholder' && !entry._deleted) {
    const fullText = 'Tool: ' + entry.tool + '\nArguments:\n' + _fmtArgs(entry.args) + '\nResult:\n' + (entry.result || '(empty)');
    const copyAllBtn = _makeCopyBtn(fullText, 'tool call');
    head.appendChild(copyAllBtn);
  }

  // Delete button — shown on live rows. Deleted rows get restore + permanent delete instead.
  if (entry._deleted) {
    // Will be injected below after the row is fully built
  } else if (entry.status !== 'remote_placeholder') {
    const delBtn = _makeToolDeleteBtn(entry, row);
    head.appendChild(delBtn);
  }

  const caret = document.createElement('span');
  caret.className = 'ca-tool-caret';
  caret.setAttribute('aria-hidden', 'true');
  caret.textContent = '›';
  head.appendChild(caret);

  row.appendChild(head);

  const body = document.createElement('div');
  body.className = 'ca-tool-body';

  // ── Saved output (full LLM response with tool calls) ──
  if (entry._savedOutput) {
    const outputLbl = document.createElement('div');
    outputLbl.className = 'ca-tool-label';
    outputLbl.textContent = 'LLM Output';
    body.appendChild(outputLbl);
    const outputWrap = document.createElement('div');
    outputWrap.className = 'ca-tool-pre-wrap';
    const outputPre = document.createElement('pre');
    outputPre.className = 'ca-tool-pre';
    let outputText = entry._savedOutput;
    try { outputText = JSON.stringify(JSON.parse(entry._savedOutput), null, 2); } catch (_) {}
    outputPre.textContent = outputText;
    outputWrap.appendChild(outputPre);
    outputWrap.appendChild(_makeCopyBtn(outputText, 'LLM output'));
    body.appendChild(outputWrap);
  }

  // ── Saved tool output (tool result from DB) ──
  if (entry._savedToolOutput) {
    const toolOutLbl = document.createElement('div');
    toolOutLbl.className = 'ca-tool-label';
    toolOutLbl.textContent = 'Tool Output (saved)';
    body.appendChild(toolOutLbl);
    const toolOutWrap = document.createElement('div');
    toolOutWrap.className = 'ca-tool-pre-wrap';
    const toolOutPre = document.createElement('pre');
    toolOutPre.className = 'ca-tool-pre';
    let toolOutText = entry._savedToolOutput;
    try { toolOutText = JSON.stringify(JSON.parse(entry._savedToolOutput), null, 2); } catch (_) {}
    toolOutPre.textContent = toolOutText;
    toolOutWrap.appendChild(toolOutPre);
    toolOutWrap.appendChild(_makeCopyBtn(toolOutText, 'tool output'));
    body.appendChild(toolOutWrap);
  }

  // ── Saved tool metadata ──
  if (entry._savedToolMetadata) {
    const metaLbl = document.createElement('div');
    metaLbl.className = 'ca-tool-label';
    metaLbl.textContent = 'Tool Metadata';
    body.appendChild(metaLbl);
    const metaWrap = document.createElement('div');
    metaWrap.className = 'ca-tool-pre-wrap';
    const metaPre = document.createElement('pre');
    metaPre.className = 'ca-tool-pre';
    let metaText = entry._savedToolMetadata;
    try { metaText = JSON.stringify(JSON.parse(entry._savedToolMetadata), null, 2); } catch (_) {}
    metaPre.textContent = metaText;
    metaWrap.appendChild(metaPre);
    metaWrap.appendChild(_makeCopyBtn(metaText, 'tool metadata'));
    body.appendChild(metaWrap);
  }

  const argLbl = document.createElement('div');
  argLbl.className = 'ca-tool-label';
  argLbl.textContent = 'Arguments';
  body.appendChild(argLbl);
  const argWrap = document.createElement('div');
  argWrap.className = 'ca-tool-pre-wrap';
  const argPre = document.createElement('pre');
  argPre.className = 'ca-tool-pre';
  argPre.textContent = _fmtArgs(entry.args);
  argWrap.appendChild(argPre);
  argWrap.appendChild(_makeCopyBtn(_fmtArgs(entry.args), 'arguments'));
  body.appendChild(argWrap);

  const resLbl = document.createElement('div');
  resLbl.className = 'ca-tool-label';
  resLbl.textContent = 'Result';
  body.appendChild(resLbl);

  if (entry.status === 'running') {
    const waiting = document.createElement('pre');
    waiting.className = 'ca-tool-pre';
    waiting.textContent = 'Waiting…';
    body.appendChild(waiting);
  } else if (typeof entry.result === 'string' && (
    entry.result.startsWith('/api/v1/content/') || entry.result.startsWith('/screenshots/')
  )) {
    const img = document.createElement('img');
    img.className = 'ca-tool-img';
    if (entry.result.startsWith('/api/v1/content/')) {
      fetchUrl(`activity-screenshot:${entry.result}`, entry.result).then((url) => {
        if (url) img.src = url;
      });
    } else {
      img.src = entry.result;
    }
    img.alt = entry.tool + ' result';
    body.appendChild(img);
  } else {
    const resWrap = document.createElement('div');
    resWrap.className = 'ca-tool-pre-wrap';
    const resPre = document.createElement('pre');
    resPre.className = 'ca-tool-pre';
    resPre.textContent = (entry.result && entry.result.length) ? entry.result : '(empty)';
    resWrap.appendChild(resPre);
    resWrap.appendChild(_makeCopyBtn((entry.result && entry.result.length) ? entry.result : '(empty)', 'result'));
    body.appendChild(resWrap);
  }

  row.appendChild(body);

  // For deleted entries on reload, inject restore + permanent-delete buttons
  if (entry._deleted && entry._detailMsgId != null && entry._detailIdx != null) {
    _injectToolDeletedActions(row, entry._detailMsgId, entry._detailIdx);
  }

  return row;
}

function renderPanel() {
  if (!panelEl) return;
  panelEl.textContent = '';
  if (toolCalls.length === 0) {
    const empty = document.createElement('div');
    empty.className = 'ca-tool-head';
    empty.style.cursor = 'default';
    empty.textContent = 'No tool calls this turn.';
    panelEl.appendChild(empty);
    return;
  }
  toolCalls.forEach((entry, i) => panelEl.appendChild(_buildRow(entry, i)));
}

// ── Panel open / close ──────────────────────────────────────────────────────

function openPanel() {
  if (expanded || !panelEl) return;
  expanded = true;
  renderPanel();
  panelEl.hidden = false;
  if (rootEl) rootEl.classList.add('expanded');
  if (barEl) barEl.setAttribute('aria-expanded', 'true');
  // Hide suggested replies while the tool-call panel is open
  const suggestRow = document.getElementById('chat-suggest-row');
  if (suggestRow) suggestRow.style.display = 'none';

  _outsideHandler = (e) => {
    // Panel is a sibling of the bar now (both direct children of rootEl).
    // Close only if the click is outside BOTH the bar and the panel.
    const inside = (barEl && barEl.contains(e.target)) || (panelEl && panelEl.contains(e.target));
    if (!inside) closePanel();
  };
  _keyHandler = (e) => { if (e.key === 'Escape') closePanel(); };
  // Defer so the click that opened the panel doesn't immediately close it.
  setTimeout(() => {
    document.addEventListener('mousedown', _outsideHandler);
    document.addEventListener('keydown', _keyHandler);
  }, 0);
}

function closePanel() {
  if (!expanded) return;
  expanded = false;
  if (panelEl) panelEl.hidden = true;
  if (rootEl) rootEl.classList.remove('expanded');
  if (barEl) barEl.setAttribute('aria-expanded', 'false');
  // Restore suggested replies when panel closes
  const suggestRow = document.getElementById('chat-suggest-row');
  if (suggestRow) suggestRow.style.display = '';
  if (_outsideHandler) { document.removeEventListener('mousedown', _outsideHandler); _outsideHandler = null; }
  if (_keyHandler) { document.removeEventListener('keydown', _keyHandler); _keyHandler = null; }
}

function togglePanel() {
  if (toolCalls.length === 0) return; // nothing to show
  if (expanded) closePanel();
  else openPanel();
}

// ── Event → note mapping ────────────────────────────────────────────────────

// Map a non-tool live event to a short note. Returns null to leave the note
// as-is (so internal/no-op steps don't blank out a meaningful note mid-turn).
function eventToNote(event) {
  const type = event.type;
  if (type === 'stream') return 'Writing a message…';
  if (type === 'pipeline') {
    switch (event.step) {
      case 'load_context':              return 'Loading context';
      case 'prep_tools':                return 'Building tools';
      case 'prep_history':              return 'Loading history';
      case 'memory_search_start':       return 'Searching memory';
      case 'build_prompt':              return 'Preparing';
      case 'working_intro_start':       return 'Planning approach…';
      case 'working_intro_ready':       return 'Approach ready';
      case 'working_intro_skipped':     return 'Starting work…';
      case 'starter_handoff_applied':   return 'Applied Scout findings';
      case 'attachment':                return 'Reading attachment';
      case 'attachment_describe_start': return 'Looking at image';
      case 'load_tools':                return 'Building tools';
      case 'turn_start':
      case 'llm_call_start':            return 'Thinking…';
      case 'guardrail_blocked':         return 'Blocked for safety';
      case 'agent_delegation':          return 'Handing off';
      case 'memory_save_start':         return 'Saving memory';
      case 'closer_start':              return 'Sending to Closer…';
      case 'closer_audit_start':        return 'Closer auditing…';
      case 'closer_contract_start':     return 'Starting independent reviews…';
      case 'contract_start':            return 'Running contract review…';
      case 'contract_pass':             return 'Contract review passed';
      case 'contract_block':            return 'Contract review found changes';
      case 'contract_correction':       return 'Sending contract fixes back…';
      case 'contract_timeout':          return 'Contract review timed out';
      case 'contract_skipped':          return 'Contract review skipped';
      case 'contract_stale_discard':    return 'Discarded stale review';
      case 'closer_llm_start':          return 'Closer writing…';
      default:                          return null;
    }
  }
  return null;
}

// Ensure the indicator is live and update the note. Resting counts as "not
// active", so a meaningful event begins a fresh turn (resetting the tool list).
function _ensureActive(note) {
  _clearEndTimer();
  if (!active) { _resetForNewTurn(); _activate(); }
  // A re-light AFTER the turn's system event is background housekeeping (memory
  // save, turn hooks, …). Settle it on the short backstop, never the in-turn
  // watchdog, so it can't hang the chip; the step's `*_end` clears it sooner.
  if (_turnEnded) _armPostTurnSettle();
  else _armWatchdog();
  setNote(note);
}

// Signal session pre-warming (session-prewarm.js) that this session's run has
// just ended (response / error / interrupted). The pre-warm module pulls the
// finished transcript into the IndexedDB cache so re-opening the session is an
// instant, offline-capable cache hit. Fire-and-forget; no listeners = no-op.
function _dispatchTurnCompleted(sid) {
  const sessionId = sid || app.currentSessionId;
  // RECORDER-HOOK: recorder.js observes decisions only. Remove this try block and comment together.
  try {
    window.__recorderTrace?.('run.completed', {
      session_id: sessionId,
      run_id: _noteTurnId,
    });
  } catch (_) {}
  if (!sessionId) return;
  try {
    window.dispatchEvent(new CustomEvent('webagent-turn-completed', {
      detail: { sessionId },
    }));
  } catch (_) { /* CustomEvent unsupported — ignore */ }
}

function handleEvent(event) {
  // RECORDER-HOOK: plugins/recorder/frontend/recorder.js consumes this optional trace.
  // Removal: delete this comment and the following try block; keep application logic.
  try {
    window.__recorderTrace?.('render.activity.event', {
      type: event.type,
      step: event.step,
      session_id: event.session_id,
      run_id: event.turn_id,
      event_seq: event.session_seq,
    });
  } catch (_) {}

  if (!event || event.type === 'ping') return;

  // Defensive: agentWs.js already only forwards current-session events, but an
  // untagged event from elsewhere shouldn't hijack the indicator.
  const sid = event.session_id || event.sessionId || '';
  if (sid && app.currentSessionId && sid !== app.currentSessionId) return;
  // Tag the note with the session it belongs to (untagged → current session).
  // The transcript mirror uses this to refuse painting into another view.
  _noteSessionId = sid || app.currentSessionId;
  if (event.turn_id) _noteTurnId = String(event.turn_id);

  // Track the inference-turn number — turn_start (and several other in-loop
  // events) carry `turn`. tool_call/tool_result don't, so they inherit the
  // last value seen this exchange.
  // When the turn number INCREASES, it means the previous turn's tool calls
  // are complete — attach them to the last agent bubble before resetting.
  if (typeof event.turn === 'number' && event.turn > 0 && event.turn !== currentTurn) {
    const prevTurn = currentTurn;
    currentTurn = event.turn;
    _turnEnded = false;   // a numbered turn is live — leave post-turn settle mode
    // If we had tool calls from the previous turn, attach them now.
    if (prevTurn > 0 && toolCalls.length > 0 && app.attachToolCallsToLastBubble) {
      const calls = toolCalls.slice();
      app.attachToolCallsToLastBubble(calls, undefined, currentToolAnchor);
    }
    _resetForNewTurn();
  }

  const type = event.type;

  if (type === 'db' && event.activity_summary && app.applyDurableActivitySummary) {
    try { app.applyDurableActivitySummary(event.activity_summary, event); } catch (_) {}
  }

  // The db event is emitted after the assistant row containing this turn's
  // structured tool calls is persisted. Its sequence belongs to the durable
  // interaction (unlike tool_call/tool_result event sequence numbers), so it
  // is the authoritative anchor for the live combined tool-call line.
  if (type === 'db' && event.role === 'assistant' && event.id
      && (event.op === 'insert_interaction' || event.interaction_seq != null)) {
    currentToolAnchor = {
      id: event.id,
      interactionSeq: Number(
        event.interaction_seq != null ? event.interaction_seq : event.session_seq,
      ),
      turnId: event.turn_id || null,
      createdAt: event.created_at || event.emit_time,
    };
    if (!app._interactionAnchors) app._interactionAnchors = new Map();
    app._interactionAnchors.set(String(event.id), currentToolAnchor);
    return;
  }

  // System events end the turn. Mark _turnEnded so anything the backend emits
  // AFTER this (background memory save, turn hooks, …) is treated as post-turn
  // housekeeping that self-clears, rather than re-lighting the bar indefinitely.
  // Also clear the stop-pending flag so the next turn starts clean.
  if (type === 'response')    {
    _turnEnded = true; app._stopPending = false;
    if (typeof app.endRunControls === 'function') app.endRunControls(event.turn_id);
    stop(); _dispatchTurnCompleted(sid); return;
  }
  if (type === 'error')       {
    _turnEnded = true; app._stopPending = false;
    if (typeof app.endRunControls === 'function') app.endRunControls(event.turn_id);
    if (active) setNote('Error'); _endSoon(); _dispatchTurnCompleted(sid); return;
  }
  if (type === 'interrupted') {
    _turnEnded = true; app._stopPending = false;
    if (typeof app.endRunControls === 'function') app.endRunControls(event.turn_id);
    if (active) setNote('Stopped'); _endSoon(); _dispatchTurnCompleted(sid); return;
  }

  // In-turn signals can never be post-turn housekeeping. If one arrives while we
  // still think the turn ended — e.g. an event/automation run that reused the
  // prior turn number and emitted no fresh user_message — we're actually in a new
  // live turn, so drop settle mode and let it claim the full in-turn watchdog.
  if (type === 'stream'
      || (type === 'pipeline' && (event.step === 'turn_start' || event.step === 'llm_call_start'))) {
    // When a stop was requested (user clicked Stop), the backend may still stream
    // a few more tokens before it checks the interrupt flag. Don't let those
    // late-arriving events re-light the indicator or turn "Stopping…" back into
    // "Thinking…" / "Writing reply…".
    if (!app._stopPending) _turnEnded = false;
  }

  // A user message is the authoritative new-turn boundary (covers fresh sends,
  // interrupt-and-replace, and event-triggered runs) — always reset the list.
  if (type === 'user_message') {
    _clearEndTimer();
    _resetForNewTurn();
    app._activeToolGroupBubble = null;
    _turnEnded = false;       // fresh exchange — out of post-turn settle mode
    app._stopPending = false; // never carry a stale stop into a new turn
    app._turnHasBubble = false;   // new exchange — needs its own fresh bubble
    currentTurn = 0;          // new exchange — turn_start will set it to 1
    _noteTurnId = String(event.turn_id || event.id || event.interaction_id || '');
    _activate();
    _armWatchdog();
    setNote('Thinking…');
    return;
  }

  if (type === 'tool_call') {
    // Vision ingestion calls hold their args here and render out-of-band when the
    // result lands (see _renderSynthToolBubble), so they don't get wiped by the
    // turn-boundary resets that bracket ingestion. (Memory tools have no tool_call
    // event — their args ride memory_search_start / memory_save_end instead.)
    if (_SYNTH_TOOLS[event.tool]) {
      _pendingSynthArgs[event.tool] = event.args || {};
      _ensureActive(_turnPrefix() + 'Reading ' + (event.tool || 'image'));
      return;
    }
    // Secure SSH setup is a user handoff rendered in its own modal. Avoid also
    // presenting the same JSON payload as a second tool-call panel.
    if (_SECURE_CARD_HANDOFF_TOOLS.has(event.tool)) {
      _ensureActive(_turnPrefix() + 'Opening secure SSH connection…');
      return;
    }
    _ensureActive(_turnPrefix() + 'Toolcall ' + (event.tool || 'tool'));
    addToolCall(event.tool, event.args, event.tool_call_id);
    return;
  }
  if (type === 'tool_result') {
    // Synthetic tools that DO emit a tool_result (vision, memory_search) render
    // their own standalone bubble; args come from whatever was stashed at start.
    // Memory tools show as a small debug note in the transcript instead.
    if (_SYNTH_TOOLS[event.tool]) {
      _ensureActive(_turnPrefix() + (event.error ? 'Error ' : 'Done ') + (event.tool || 'image'));
      if (event.tool !== 'memory_search' && event.tool !== 'memory_save') {
        const info = {
          args: _pendingSynthArgs[event.tool], result: event.result,
          error: !!event.error, durationMs: event.duration_ms,
          errorType: event.error_type, id: event.id,
        };
        const call = _synthToolCall(event.tool, info);
        // A recovery retry is part of the run it resumed, so fold it into that
        // run's activity group like any other tool call; only with no group to
        // join does it render as its own standalone bubble.
        if (!(_RECOVERY_TOOLS[event.tool] && _attachRecoveryToOpenGroup(call))) {
          _renderSynthToolBubble(event.tool, info, call);
        }
      } else if (event.tool === 'memory_search') {
        _appendMemoryNote('Memory searched');
      }
      return;
    }
    if (_SECURE_CARD_HANDOFF_TOOLS.has(event.tool) && _maybeShowSecureCard(event)) {
      _ensureActive(_turnPrefix() + 'SSH connection panel opened');
      return;
    }
    _ensureActive(_turnPrefix() + (event.error ? 'Error ' : 'Done ') + (event.tool || 'tool'));
    resolveToolResult(event.tool, event.result, event.duration_ms, !!event.error,
                      event.error_type, event.tool_call_id);
    _maybeShowSecureCard(event);
    _maybeShowChatComponent(event);
    _maybeShowAgentProfileCard(event);
    return;
  }

  // Live estimate: while the LLM streams content, count output tokens in real-time
  if (type === 'stream' && typeof event.content === 'string') {
    _onStreamContent(event.content);
  }

  // The resolved model is known before the first token. Seed the live bubble's
  // footer here instead of waiting for llm_call_end after streaming finishes.
  if (type === 'pipeline' && event.step === 'llm_call_start') {
    if (event.model) {
      app._activeTurnModel = event.model;
      app._activeTurnDisplayName = event.display_name || '';
    }
    app._activeTurnEffort = event.effort || null;
  }

  // Capture token usage from pipeline llm_call_end events
  if (type === 'pipeline' && event.step === 'llm_call_end') {
    if (typeof event.input_tokens === 'number' || typeof event.output_tokens === 'number') {
      // A call that ran on a model with no published price → show n/a instead
      // of a misleading $0 (distinguish "free" from "we don't know").
      if (event.cost_source === 'unknown') _sessionHasUnknownPriced = true;
      addTokens(event.input_tokens || 0, event.output_tokens || 0, event.credit_units);
    }
    // This is the provider's exact prompt-token count for the call that just
    // completed — the single source of truth for the ctx readout. Adopt it
    // directly: it IS the actual context sent to the provider. That means the
    // readout reflects compaction immediately — after a fold the next call
    // reports the reduced prompt (summary cars replace the older turns) and
    // this drops to it, instead of holding a stale high-water mark.
    if (typeof event.input_tokens === 'number') {
      const inTok = Math.max(0, event.input_tokens);
      if (inTok > 0) {
        _contextTokens = inTok;   // exact count — the real context sent this call
        _contextRevision += 1;
      }
      _pendingCtxEstimate = 0;
      _renderCtxIndicator();
      // Rescale the composition bar to this call's exact prompt total.
      _renderContextComposition();
    }
    // Remember which model/effort ran this turn so the bubble footer can tag it
    // the moment the live answer finalizes (chat-stream.js reads these).
    if (event.model) app._activeTurnModel = event.display_name || event.model;
    app._activeTurnEffort = event.effort || null;
    // Fire the premium glow LIVE on the streaming bubble as soon as llm_call_end
    // reports the effort — don't wait for finalize. This is the earliest point the
    // model/effort are known; the streaming bubble is already in the DOM.
    if (event.effort) {
      const eff = String(event.effort).trim().toLowerCase();
      const streaming = document.querySelector('.chat-bubble.agent.streaming');
      if (streaming) streaming.classList.toggle('premium', eff === 'high');
    }
    // Alternate-engine agents (Local Claude Code) don't resolve through the app's
    // model — the run itself reports the real model it used. Adopt it for the
    // footer name + context window so they describe the actual Claude model,
    // not a placeholder (the load-time resolve only knew the *configured* model,
    // which is blank when the CLI picks Claude's own default).
    if (_altEngine && event.model) {
      _currentModelName = event.model;
      const win = _claudeCtxWindow(event.model);
      if (win) _modelContextLimit = win;
      _renderCtxIndicator();
      _renderModelBtn();
    }
  }

  // Live context gauge from pipeline context_status events. This is a chars/4
  // ESTIMATE of the assembled context, so it no longer drives the pill's ctx
  // readout — that stays on the provider-exact llm_call_end count (the actual
  // context sent, so compaction drops show immediately).
  // (chat-surface.js reads this event itself for its own context readout.)
  if (type === 'pipeline' && event.step === 'context_status') {
    // intentionally ignored here — keep the pill on exact counts only
  }

  // Live context COMPOSITION, emitted just before each LLM call with exactly the
  // messages about to be sent: prompt overhead + tool schemas, the compactable
  // older history, and the newest verbatim tail. Feeds the footer panel's stacked
  // bar; the flat context_status estimate above stays unused.
  if (type === 'pipeline' && event.step === 'context_budget') {
    _setContextComposition({
      prompt_tokens: Number(event.prompt_tokens) || 0,
      compactable_tokens: Number(event.compactable_tokens) || 0,
      verbatim_tokens: Number(event.verbatim_tokens) || 0,
      source: 'live',
    });
  }

  // Loop-node memory tools have no normal tool_call event, so they ride the
  // pipeline lifecycle steps. memory_search stashes its query from the start
  // step (the activity bar shows "Searching memory"). memory_save has no
  // tool_result at all. Neither renders a bubble in the chat transcript —
  // they're hidden. The completion-settle handler below clears the
  // "Saving memory" chip.
  if (type === 'pipeline') {
    if (event.step === 'memory_search_start') {
      _pendingSynthArgs['memory_search'] = { query: event.query, limit: event.limit };
    } else if (event.step === 'memory_save_end') {
      _appendMemoryNote('Memory saved');
    }
  }

  // Post-turn background steps (memory save, …) finish with an `*_end`/completion
  // pipeline step. Once the turn's system event has fired, treat that as a
  // settle signal so the bar clears the moment the work is actually done, instead
  // of lingering on its `*_start` note. Guarded by _turnEnded so in-turn `*_end`
  // steps (llm_call_end fires every LLM call) can't clear a live turn.
  if (_turnEnded && type === 'pipeline' && _isCompletionStep(event.step)) {
    if (active) _endSoon();   // brief flash of the note, then fade
    return;
  }

  const note = eventToNote(event);
  if (note == null) {
    // A post-turn sign of life must NOT re-arm the long in-turn watchdog — that
    // would re-pin a stuck note for 3 min. Keep the short settle ticking instead.
    if (active) { if (_turnEnded) _armPostTurnSettle(); else _armWatchdog(); }
    return;
  }
  // When a stop was requested, let the backend's activity events still tick over
  // (watchdog, token counters) but don't replace "Stopping…" with "Writing reply…"
  // or any other mid-stream note — only system events clear the stop state.
  if (app._stopPending) return;
  _ensureActive(note);
}

// ── Exported helpers for chat.js (bubble-attached tool panels) ──────────────
// Reuse the same accordion row rendering so bubble panels look identical to the
// live activity panel.

export { _buildRow as buildToolRow };

// ── Element acquisition ─────────────────────────────────────────────────────
// The chat panel partial (chat-side-panel.html) is injected asynchronously at
// boot (partialsReady). If initChatActivity() ran before it landed — an
// intermittent race — every lookup below returns null and the activity bar is
// silently dead for the whole page load (start()/setNote() no-op when textEl
// is missing). Re-acquiring lazily on the send/restore paths self-heals that
// load: by the time the user sends, the partial is in the DOM, the refs come
// back live, and progress notes render immediately — no refresh needed.
function _acquireElements() {
  rootEl = document.getElementById('chat-above-pill');
  pillEl = document.getElementById('chat-input-row');
  // The static fallback remains directly under #chat-above-pill until the
  // configured control builder places it into a row. Only a placed control is
  // allowed to render footer progress; otherwise it becomes a stray left-edge
  // third progress surface during hydration or an agent-specific layout.
  const activityBars = Array.from(document.querySelectorAll('#chat-activity-bar'));
  barEl = activityBars.find((el) => el.closest('[data-controls-row]')) || null;
  textEl = barEl ? barEl.querySelector('.chat-activity-text') : null;
  // Like the chip, the legacy panel is valid only when it belongs to the
  // configured activity control. Leaving its raw fallback mounted creates a
  // wide border/line beneath the composer while progress is active.
  panelEl = barEl?.closest('[data-element-origin="activity"]')
    ?.querySelector('.chat-activity-panel') || null;
  tokenBarEl = document.getElementById('chat-token-bar');
  tokensInEl = document.getElementById('chat-tokens-in');
  tokensOutEl = document.getElementById('chat-tokens-out');
  tokenSpinnerEl = document.getElementById('chat-token-spinner');
  footerLeftEl = document.getElementById('chat-pill-stats');
  modelBtnEl = document.getElementById('chat-model-btn');
  modelCtxEl = document.getElementById('chat-model-ctx');
  costEl = document.getElementById('chat-cost');
  return textEl;
}

// Idempotent init: element refs are re-acquired on every call (self-heals the
// boot race and any mid-session control rebuild), but the one-shot wiring
// (cost poll, resize observer, model-sync hook) runs only once so a re-init
// never double-registers intervals/listeners.
let _activityWired = false;

export function initChatActivity() {
  _acquireElements();
  // Always show the session cost (starts at $0), not gated on a non-zero total.
  _renderCost();
  if (_activityWired) return;
  _activityWired = true;

  // Periodic reconcile: the live total only self-heals on session load, so also
  // refresh it from /session-cost every few minutes and whenever the tab regains
  // focus — closes the SSE-drift window mid-session (catch-up only, never down).
  setInterval(() => { _fetchSessionCost(); }, 5 * 60 * 1000);
  document.addEventListener('visibilitychange', () => {
    if (document.visibilityState === 'visible') _fetchSessionCost();
  });

  // The agent Config tab's "Query CLI for latest model options" button broadcasts
  // this after a successful force re-query — drop our page cache so the next
  // footer open shows the freshly queried list immediately.
  window.addEventListener('engine-catalog-refreshed', () => {
    _engineCatalogCache = null;
    _engineCatalogCacheTs = 0;
  });

  // ── Model changer (footer row chevron buttons) ──────────────────────────
  // MUST be wired BEFORE refreshModelContext() so the wrapped version captures
  // the first model-resolve call. Also hooks the WebSocket handler and the
  // async model-list fetch to stay in sync.
  const _changerEl = document.getElementById('chat-model-changer');
  const _changerName = _changerEl ? _changerEl.querySelector('.cmc-name') : null;
  const _toastEl = document.getElementById('cmc-toast');

  /** Truncate a model id from the front — show the tail with a leading `…`. */
  function _shortTail(id, maxLen = 18) {
    if (!id) return '';
    const base = id.includes('/') ? id.split('/').pop() : id;
    if (base.length <= maxLen) return base;
    return '…' + base.slice(-(maxLen - 1));
  }

  function _syncModelChanger() {
      if (!_changerEl || !_changerName) return;
      // Alternate engine (Claude/Codex): show the harness's real model choice.
      if (_altEngine) {
        const catalog = _engineCatalogNow();
        if (!catalog) { _changerName.textContent = 'Model'; return; }
        _changerEl.classList.remove('saving');
        _changerEl.style.display = '';
        const m = _engineCurModel || '';
        let label = 'Default';
        if (m) {
          const hit = catalog.models.find(x => x.v === m)
            || (catalog.lane === 'claude_code' ? catalog.models.find(x => x.v === _claudeAliasOf(m)) : null);
          label = hit ? hit.label : _shortTail(m);
        }
        _changerName.textContent = label;
        return;
      }
      // Still loading — show spinner, not a misleading "Default" label.
      if (!_allSlots.length) {
        _changerEl.style.display = '';
        if (_modelListLoaded) {
          // The roster ANSWERED and offered nothing usable: this agent has no
          // Standard model role assigned. Show the error state instead of an
          // endless spinner so the failure is visible before a send is attempted.
          _changerEl.classList.remove('saving', 'agent-override');
          _changerEl.classList.add('no-model');
          _changerName.textContent = 'No model';
          _changerEl.title = 'Standard model role not assigned — pick a model in the selector or ask an admin to appoint one';
          return;
        }
        _changerEl.classList.add('saving');
        return;
      }
      _changerEl.classList.remove('saving');
      // Agent-driven override (Model Switcher: set_model / use_premium_model):
      // the pill is showing a model the USER didn't pick, so display the real
      // model in warning color. The backend clears the override at run end and
      // emits 'model_override' (active:false) → this branch drops → normal.
      if (_agentOverride) {
        _changerEl.classList.add('agent-override');
        _changerEl.style.display = '';
        _changerName.textContent = _shortTail(_agentOverride.model) || 'Model';
        return;
      }
      _changerEl.classList.remove('agent-override');
      const slot = _allSlots.find(s => {
        const ref = _slotRef(s);
        return ref === _currentSlotRef;
      });
      const model = (slot && slot.model) || '';
      if (!model) {
        _changerEl.style.display = '';
        _changerEl.classList.remove('agent-override');
        _changerEl.classList.add('no-model');
        // No slot resolved for this session: no usable Standard model is appointed
        // for this agent (or the stored pick is gone). Say exactly that instead of
        // the bare word 'Default', which read as "a default model is in force" —
        // this is a recoverable dead-end, not a working default. The picker still
        // offers every entitled row, and the server refuses the run until one is
        // picked (the same fatal error the usage-limit bubble uses).
        _changerName.textContent = 'No model';
        _changerEl.title = 'No Standard model configured — pick a model in this selector or ask an admin to appoint one';
        return;
      }
      _changerEl.classList.remove('no-model');
      _changerEl.style.display = '';
      // The footer uses the same flat-roster identity as the picker: the
      // selected model's display name, never its legacy role abbreviation.
      _changerName.textContent = _shortTail(model);
    }

  // Sync when model changes via picker selection (_selectModel at ~line 1023)
  const _origSelect = _selectModel;
  _selectModel = function(modelId) {
    const p = _origSelect.apply(this, arguments);
    if (p && p.then) p.then(ok => { if (ok) _syncModelChanger(); });
    return p;
  };

  function _showModelToast(text) {
    if (!_toastEl || !_changerEl) return;
    const rect = _changerEl.getBoundingClientRect();
    _toastEl.textContent = text;
    _toastEl.style.bottom = (window.innerHeight - rect.top + 6) + 'px';
    _toastEl.style.left = Math.max(4, Math.min(rect.left + rect.width / 2 - 80, window.innerWidth - 170)) + 'px';
    _toastEl.className = 'cmc-toast';
    _toastEl.style.display = 'block';
    void _toastEl.offsetWidth;
    _toastEl.classList.add('visible');
    clearTimeout(_toastEl._hideTimer);
    _toastEl._hideTimer = setTimeout(() => {
      _toastEl.classList.remove('visible');
      _toastEl.classList.add('hiding');
      setTimeout(() => { _toastEl.style.display = 'none'; _toastEl.className = 'cmc-toast'; }, 200);
    }, 2000);
  }

  /** Switch the active model slot and show saving → checked feedback on the
   *  changer button.  Returns the promise from _selectModel so callers can chain
   *  model-list syncing after the API confirms success. */
  async function _saveModelWithFeedback(slotRef) {
    if (!_changerEl) return;
    // Resolve the slot so we can show the label *after* the check animation.
    const slot = _allSlots.find(s => {
      const ref = _slotRef(s);
      return ref === slotRef;
    });
    const nextModel = (slot && slot.model) || '';

    _changerEl.classList.add('saving');
    const ok = await _selectModel(slotRef);
    if (ok) {
      _changerEl.classList.remove('saving');
      _changerEl.classList.add('checked');
      // Update the label to the new model while the check is shown — the check
      // covers the label anyway, so this is invisible. When .checked is removed
      // below the new label is already in place.
      _syncModelChanger();
      _showModelToast(_shortTail(nextModel));
      setTimeout(() => { _changerEl.classList.remove('checked'); }, 700);
    } else {
      _changerEl.classList.remove('saving');
    }
    return ok;
  }

  function _cycleModel(dir) {
    if (!_changerEl) return;
    // Alternate engine: cycle the harness's own model catalog.
    if (_altEngine) { _cycleEngineModel(dir); return; }
    _changerEl.classList.add('saving');
    // Always re-fetch the model list from the server before cycling, so the
    // chevrons always reflect the current agent config — reorders, additions,
    // and removals from the Config tab are picked up immediately, never cycling
    // onto a stale or removed model.
    _fetchModelsForPicker().then(() => _doCycle(dir, _allSlots));
  }
  function _doCycle(dir, list) {
    if (!list.length) { _changerEl.classList.remove('saving'); return; }
    const cur = _currentSlotRef || '';
    let idx = list.findIndex(s => {
      const ref = _slotRef(s);
      return ref === cur;
    });
    if (idx === -1) idx = 0;
    idx = (idx + dir + list.length) % list.length;
    const next = list[idx];
    if (!next) { _changerEl.classList.remove('saving'); return; }
    const nextRef = _slotRef(next);
    if (nextRef === cur) { _changerEl.classList.remove('saving'); return; }
    _saveModelWithFeedback(nextRef).then(ok => {
      if (!ok) _changerEl.classList.remove('saving');
    });
  }

  /** Cycle the alternate-engine model catalog (chevron arrows). Re-reads the
   *  agent's saved config + live CLI catalog first so the cycle never lands on
   *  a stale value. */
  async function _cycleEngineModel(dir) {
    const catalog = (await _getEngineCatalog()) || _engineCatalog();
    if (!catalog || !_changerEl) return;
    try {
      const uq = `?user_id=${encodeURIComponent(app.currentUserId || '')}`;
      const res = await fetch(`/api/v1/agents/${encodeURIComponent(app.currentAgentId || '')}${uq}`, { headers: { ...authHeaders() } });
      if (res.ok) {
        const a = await res.json();
        const lane = (a.agent || a)[catalog.lane] || {};
        _engineCurModel = String(lane.model || '').trim();
        _engineCurEffort = String(lane.effort || '').trim();
      }
    } catch (_) { /* best-effort */ }
    const cur = _engineCurModel || '';
    let idx = catalog.models.findIndex(m => m.v === cur);
    if (idx === -1) idx = 0;
    idx = (idx + dir + catalog.models.length) % catalog.models.length;
    const next = catalog.models[idx];
    if (next.v === cur) return;
    _changerEl.classList.add('saving');
    const ok = await _saveEngineField('model', next.v);
    if (ok) {
      _engineCurModel = next.v;
      _changerEl.classList.remove('saving');
      _changerEl.classList.add('checked');
      _syncModelChanger();
      _showModelToast(next.v || 'Default');
      setTimeout(() => _changerEl.classList.remove('checked'), 700);
    } else {
      _changerEl.classList.remove('saving');
    }
  }

  // ── Model list panel (click the changer label, opens the ordered roster) ──
  function _buildModelListPanel() {
    if (_modelListPanelEl) return;
    const panel = document.createElement('div');
    panel.className = 'chat-skill-picker chat-model-list-panel';
    panel.style.display = 'none';
    const header = document.createElement('div');
    header.className = 'csp-header';
    header.innerHTML = '<svg xmlns="http://www.w3.org/2000/svg" width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><path d="M21 16V8a2 2 0 0 0-1-1.73l-7-4a2 2 0 0 0-2 0l-7 4A2 2 0 0 0 3 8v8a2 2 0 0 0 1 1.73l7 4a2 2 0 0 0 2 0l7-4A2 2 0 0 0 21 16z"/><polyline points="3.27 6.96 12 12.01 20.73 6.96"/><line x1="12" y1="22.08" x2="12" y2="12"/></svg>Models';
    panel.appendChild(header);
    const list = document.createElement('div');
    list.className = 'csp-list';
    panel.appendChild(list);
    const contextButton = document.createElement('button');
    contextButton.type = 'button';
    contextButton.className = 'cml-context-btn';
    contextButton.textContent = 'Context compaction';
    contextButton.addEventListener('click', (event) => {
      event.stopPropagation();
      panel.style.display = 'none';
      _toggleModelPicker();
    });
    panel.appendChild(contextButton);

    // Persistent effort control: this sits outside the scrolling roster so it
    // remains visible at the panel bottom for whichever model is active.
    const effort = document.createElement('div');
    effort.className = 'cml-effort-bar';
    const ticks = document.createElement('span');
    ticks.className = 'cml-effort-ticks';
    ticks.setAttribute('aria-hidden', 'true');
    for (let i = 0; i < 4; i += 1) ticks.appendChild(document.createElement('i'));
    effort.appendChild(ticks);
    const slider = document.createElement('input');
    slider.className = 'cml-effort-slider';
    slider.type = 'range';
    slider.min = '0';
    slider.max = '3';
    // A half step is reserved for the visual-only Default state, positioned
    // midway between Low and Medium. User input is snapped to named levels.
    slider.step = '0.5';
    slider.setAttribute('aria-label', 'Reasoning effort');
    slider.addEventListener('input', () => {
      slider.classList.remove('is-default');
      slider.value = String(_effortSliderIndex(slider.value));
      _paintModelListEffort(slider);
    });
    slider.addEventListener('change', () => _commitModelListEffort(slider));
    effort.appendChild(slider);
    // Read-out naming the current selection. It is the visual counterpart of the
    // slider's aria-valuetext, so it stays aria-hidden to avoid a double
    // announcement, and is repainted by _paintModelListEffort from the same
    // single source of truth as the track.
    const readout = document.createElement('span');
    readout.className = 'cml-effort-label';
    readout.setAttribute('aria-hidden', 'true');
    effort.appendChild(readout);
    panel.appendChild(effort);
    document.body.appendChild(panel);
    _modelListPanelEl = panel;
    _modelListPanelList = list;
    _modelListPanelEffort = effort;
    document.addEventListener('click', (e) => {
      if (_modelListPanelEl && _modelListPanelEl.style.display !== 'none'
          && !_modelListPanelEl.contains(e.target)
          && e.target !== _changerEl
          && !(_changerEl && _changerEl.contains(e.target))) {
        _modelListPanelEl.style.display = 'none';
      }
    });
    panel.addEventListener('keydown', (e) => {
      if (e.key === 'Escape') { _modelListPanelEl.style.display = 'none'; }
    });
  }
  function _toggleModelListPanel() {
    // Alternate engines pick from the harness's own catalog — open that panel.
    if (_altEngine) { _toggleEnginePanel(); return; }
    if (!_modelListPanelEl) _buildModelListPanel();
    if (_modelListPanelEl.style.display !== 'none') {
      _modelListPanelEl.style.display = 'none';
      return;
    }
    // Close the old model picker if open
    if (_modelPickerEl) _modelPickerEl.style.display = 'none';
    const footer = document.getElementById('chat-footer-row') || document.getElementById('chat-input-row');
    if (!footer) return;
    const rect = footer.getBoundingClientRect();
    _modelListPanelEl.style.top = 'auto';
    _modelListPanelEl.style.bottom = (window.innerHeight - rect.top + 6) + 'px';
    _modelListPanelEl.style.left = 'auto';
    _modelListPanelEl.style.right = Math.max(4, Math.min(window.innerWidth - rect.right, window.innerWidth - 290)) + 'px';
    setTimeout(() => { _modelListPanelEl.style.display = 'block'; }, 0);
    // Show loading state, fetch models, then render (same pattern as _toggleModelPicker)
    _modelListPanelList.innerHTML = '<div class="csp-empty">Loading models…</div>';
    _fetchModelsForPicker().then(() => {
      _renderModelListPanel();
    });
  }
  function _getModelRole(slot) {
    return _slotRef(slot) === _currentSlotRef ? 'active' : 'plain';
  }

  const _modelRoleIcons = {
    standard: { symbol: 'S', title: 'Standard' },
    premium: { symbol: 'P', title: 'Premium' },
    image_in: { symbol: '👁', title: 'Vision' },
    image_out: { symbol: '🖼', title: 'Image generation' },
    system: { symbol: '⚙', title: 'System' },
    voice: { symbol: '🎤', title: 'Voice' },
  };

  // ── Persistent effort slider ─────────────────────────────────────────────
  // This is a panel-level control, not a model-row popover. It always controls
  // the active visible model and stays below the independently scrolling roster.
  const _modelEffortLevels = ['minimal', 'low', 'medium', 'high'];

  // User-facing names for the read-out under the slider. Positional and
  // bottom-up: the four named ticks read Low → Medium → High → Ultra Effort.
  const _modelEffortLabels = {
    minimal: 'Low Effort',
    low: 'Medium Effort',
    medium: 'High Effort',
    high: 'Ultra Effort',
  };
  const _defaultEffortLabel = 'Default Effort';

  async function _saveEffort(slotRef, effort) {
    try {
      const headers = { 'Content-Type': 'application/json', ...authHeaders() };
      const sid = app.currentSessionId || '';
      const payload = {
        slot_ref: slotRef,
        reasoning_effort: effort,
      };
      if (sid) {
        const response = await fetch(apiPath('/api/v2/chat/sessions/' + encodeURIComponent(sid) + '/model-effort'), {
          method: 'POST', headers,
          body: JSON.stringify(payload),
        });
        if (!response.ok) return false;
      }
      _allEffortMap[slotRef] = effort;
      return true;
    } catch (_) { return false; }
  }

  // The empty session state is intentionally distinct from Minimal. It sends no
  // provider effort hint and is displayed at the midpoint of the named range.
  const _defaultEffortSliderIndex = (_modelEffortLevels.length - 1) / 2;

  function _effortSliderIndex(value) {
    return Math.max(0, Math.min(_modelEffortLevels.length - 1, Math.round(Number(value) || 0)));
  }

  function _effortForModelSlot(slot) {
    if (!slot) return 'default';
    const entryRef = _slotRef(slot);
    let effort = _allEffortMap[entryRef];
    // Keep legacy role-keyed per-session effort values visible after models
    // became one ordered row per roster entry.
    if (!effort) {
      const roles = slot.roles || (slot.role ? [slot.role] : []);
      effort = roles.map(role => _allEffortMap['role:' + role]).find(Boolean);
    }
    effort = String(effort || 'default').toLowerCase();
    return _modelEffortLevels.includes(effort) ? effort : 'default';
  }

  function _paintModelListEffort(slider) {
    const isDefault = slider.classList.contains('is-default');
    const index = isDefault ? _defaultEffortSliderIndex : _effortSliderIndex(slider.value);
    const level = _modelEffortLevels[index];
    const label = isDefault ? _defaultEffortLabel : (_modelEffortLabels[level] || level);
    const progress = `${(index / (_modelEffortLevels.length - 1)) * 100}%`;
    slider.style.setProperty('--cml-effort-progress', progress);
    const bar = slider.closest('.cml-effort-bar');
    bar?.style.setProperty('--cml-effort-progress', progress);
    const spoken = isDefault ? `${label} — provider default` : label;
    slider.setAttribute('aria-valuetext', `Reasoning effort: ${spoken}`);
    slider.title = `Reasoning effort: ${spoken}`;
    const labelEl = bar && bar.querySelector('.cml-effort-label');
    if (labelEl) {
      labelEl.textContent = label;
      labelEl.classList.toggle('is-default', isDefault);
    }
  }

  function _renderModelListEffort() {
    const slider = _modelListPanelEffort && _modelListPanelEffort.querySelector('.cml-effort-slider');
    if (!slider) return;
    const slot = (_allSlots || []).find(s => _slotRef(s) === _currentSlotRef) || (_allSlots || [])[0];
    const disabled = !slot;
    slider.disabled = disabled;
    slider.dataset.slotRef = disabled ? '' : _slotRef(slot);
    const level = _effortForModelSlot(slot);
    const isDefault = level === 'default';
    slider.classList.toggle('is-default', isDefault);
    slider.value = String(isDefault ? _defaultEffortSliderIndex : _modelEffortLevels.indexOf(level));
    _paintModelListEffort(slider);
  }

  async function _commitModelListEffort(slider) {
    const slotRef = slider.dataset.slotRef || '';
    if (!slotRef) return;
    const level = _modelEffortLevels[_effortSliderIndex(slider.value)];
    // Disabling a native range blurs it. Restore keyboard focus after the
    // request settles so Arrow-key adjustments remain continuous.
    const restoreFocus = document.activeElement === slider;
    slider.disabled = true;
    const saved = await _saveEffort(slotRef, level);
    slider.disabled = false;
    if (!saved) _renderModelListEffort();
    if (restoreFocus && document.contains(slider)) slider.focus({ preventScroll: true });
  }

  function _renderModelListPanel() {
    if (!_modelListPanelList) return;
    const list = _allSlots || [];
    _modelListPanelList.innerHTML = '';
    if (!list.length) {
      _modelListPanelList.innerHTML = '<div class="csp-empty">No models configured</div>';
      _renderModelListEffort();
      return;
    }
    // One persisted roster order, with roles represented only by compact icons
    // on the affected model row. The active row is the sole highlighted state.
    list.forEach((s) => {
      const slotRef = _slotRef(s);
      const role = _getModelRole(s);
      const item = document.createElement('div');
      item.className = 'cml-item' + (role === 'active' ? ' cml-active csp-active' : '');
      item.dataset.slotRef = slotRef;
      const nameSpan = document.createElement('span');
      nameSpan.className = 'cml-name';
      nameSpan.textContent = _shortTail(s.model || '');
      item.appendChild(nameSpan);
      const roleIcons = s.roles || (s.role ? [s.role] : []);
      if (roleIcons.length) {
        const icons = document.createElement('span');
        icons.className = 'cml-role-icons';
        roleIcons.forEach((roleKey) => {
          const icon = _modelRoleIcons[roleKey];
          if (!icon) return;
          const marker = document.createElement('span');
          marker.className = 'cml-role-icon';
          marker.textContent = icon.symbol;
          marker.title = icon.title;
          marker.setAttribute('aria-label', icon.title);
          icons.appendChild(marker);
        });
        if (icons.childElementCount) item.appendChild(icons);
      }
      item.title = (s.model || '');
      item.addEventListener('click', async () => {
        const saved = await _saveModelWithFeedback(slotRef);
        if (saved) _renderModelListPanel();
      });
      _modelListPanelList.appendChild(item);
    });
    _renderModelListEffort();
  }
  // Sync panel + changer when models change
  const _origFetchForPanel = _fetchModelsForPicker;
  _fetchModelsForPicker = function() {
    const p = _origFetchForPanel.apply(this, arguments);
    if (p && p.then) p.then(() => {
      _syncModelChanger();
      if (_modelListPanelEl && _modelListPanelEl.style.display !== 'none') _renderModelListPanel();
    });
    return p;
  };

  if (_changerEl) {
    const leftChev = _changerEl.querySelector('.cmc-chev-left');
    const rightChev = _changerEl.querySelector('.cmc-chev-right');
    if (leftChev) leftChev.addEventListener('click', (e) => { e.stopPropagation(); _cycleModel(-1); });
    if (rightChev) rightChev.addEventListener('click', (e) => { e.stopPropagation(); _cycleModel(1); });
    // Click the main label to open the new model list panel (chevrons have stopPropagation)
    _changerEl.addEventListener('click', _toggleModelListPanel);
    _syncModelChanger();
    // Wrap refreshModelContext to sync changer after model resolves
    const _origRefresh = refreshModelContext;
    refreshModelContext = function() {
      const p = _origRefresh.apply(this, arguments);
      if (p && p.then) p.then(() => _syncModelChanger());
      else _syncModelChanger();
      return p;
    };
  }

  if (barEl) barEl.addEventListener('click', togglePanel);

  // Show the active model's context window / max output next to the counters.
  // Re-callable (exposed below) so Settings can refresh it after a model change.
  refreshModelContext();

  // Click footer left (token bar + ctx) to open the context panel. Alternate
  // engines (Local Claude Code / Codex) get their own panel (context + plan
  // usage + the harness's real model/effort choices), since the app's model
  // list doesn't apply to them.
  function _onFooterClick(e) {
    e.stopPropagation();
    if (_altEngine) _toggleEnginePanel();
    else _toggleModelPicker();
  }
  if (footerLeftEl) {
    footerLeftEl.style.cursor = 'pointer';
    footerLeftEl.addEventListener('click', _onFooterClick);
  }
  // Also wire the ctx element directly as a reliable fallback
  if (modelCtxEl) {
    modelCtxEl.style.cursor = 'pointer';
    modelCtxEl.addEventListener('click', _onFooterClick);
  }
  // Dedicated "switch model" button below the pill (clearer affordance than the
  // ctx chip) — opens the same picker.
  if (modelBtnEl) modelBtnEl.addEventListener('click', _onFooterClick);
  // Delegated accordion toggle: one listener survives panel re-renders.
  if (panelEl) {
    panelEl.addEventListener('click', (e) => {
      const head = e.target.closest('.ca-tool-head');
      if (!head) return;
      const row = head.closest('.ca-tool-row');
      if (!row || row.dataset.i == null) return;
      const idx = parseInt(row.dataset.i, 10);
      const entry = toolCalls[idx];
      if (!entry) return;
      entry.open = !entry.open;
      row.classList.toggle('open', entry.open);
      head.setAttribute('aria-expanded', entry.open ? 'true' : 'false');
    });
  }

  // Imperative hooks for chat.js (instant feedback on send / HTTP-error paths).
  app.chatActivityStart = start;
  app.chatActivityStop = stop;
  app.chatActivityRestore = chatActivityRestore;
  // Mock (ambient) notes — UI-only progress for the CLIENT's own background work.
  // They render in the footer activity element and obey the transcript mirror
  // preference exactly like a run note. See MOCK_ACTIVITY_NOTES above.
  app.chatActivityMockNote = mockActivityNote;
  app.chatActivityMockClear = mockActivityNoteClear;
  app.chatActivityMockLabel = mockActivityLabel;
  // Reconcile uses this to restore the generic "Working…" note only when the
  // server still has an active run but the chat activity indicator has gone quiet.
  app.chatActivityIsLive = () => active;
  // Exposed so loadSessionChat can reset activity state on EVERY switch path
  // (Sessions page / genui / optimizer stats set currentSessionId directly and
  // call loadSessionChat without going through switchToSession).
  app.chatActivitySessionChanged = chatActivitySessionChanged;
  // Let Settings re-pull the footer context indicator after a model change.
  app.refreshModelContext = refreshModelContext;
  // Exposed so session-load.js can self-heal cost + ctx right after each
  // navigation, instead of waiting up to 5 minutes for the poll to fire.
  app.fetchSessionCost = _fetchSessionCost;

  // Receive every current-session agent event from the per-user WebSocket.
  app._chatActivityHandler = handleEvent;
  // Wrap to sync model changer when model name arrives via WS
  const _origHandler = app._chatActivityHandler;
  app._chatActivityHandler = function(event) {
    const ret = _origHandler.apply(this, arguments);
    if (event && event.type === 'agent_model_roster_changed') {
      // An administrator changed this agent's persisted role assignments. The
      // next footer render must resolve the same live config the loop will use.
      if (String(event.agent_id || '') === String(app.currentAgentId || '')) {
        _allSlots = [];
        _currentSlotRef = '';
        Promise.resolve(_fetchModelsForPicker()).then(() => {
          _syncModelChanger();
          _renderModelBtn();
        }).catch(() => {});
      }
    } else if (event && event.type === 'model_override') {
      // Agent-driven model override turned on/off (Model Switcher tools, the
      // new-user-message reset, or the run-end backend cleanup). Update the
      // cached state and re-render both model selectors (warning color while
      // active, normal when cleared).
      const hadOverride = !!_agentOverride;
      // Identity of the model the run is LEAVING — the netting key. A coarse
      // 'override' pair collapsed two DIFFERENT models into "no change" and
      // cancelled the notice outright, so a second switch in a run (or any
      // switch made while already on a custom model) rendered nothing at all.
      const prevOverrideKey = hadOverride
        ? 'override:' + String((_agentOverride && _agentOverride.model) || '').trim().toLowerCase()
        : 'default';
      _agentOverride = event.active ? { model: event.model || '' } : null;
      Promise.resolve().then(() => { _syncModelChanger(); _renderModelBtn(); });
      // Live transcript notice for the switch, anchored to the running turn so
      // it lands between that turn's tool calls (mirrors the mode notice).
      // Replayed events are stale — never re-announce an old switch.
      try {
        if (!event.replayed && typeof app.notifyModelSwitch === 'function') {
          if (event.active && event.model) {
            app.notifyModelSwitch(event.label || 'Switched to a custom model', {
              initiator: 'agent', tool: 'set_model', model: event.model,
              turnId: event.turn_id || undefined,
              // Identity pair: nets against the group's starting selection and
              // cancels only on a genuine return to it.
              from: prevOverrideKey,
              state: 'override:' + String(event.model).trim().toLowerCase(),
            });
          } else if (!event.active && hadOverride) {
            // Only announce the revert when an override WAS active — the
            // backend now emits active:false only on a real reset, but stay
            // defensive against redundant clears.
            app.notifyModelSwitch('Reverted to standard model', {
              initiator: 'agent', tool: 'reset',
              turnId: event.turn_id || undefined,
              from: prevOverrideKey,
              state: 'default',
            });
          }
        }
      } catch (_) { /* best-effort */ }
    } else if (event && event.model) {
      Promise.resolve().then(() => _syncModelChanger());
    }
    return ret;
  };
  // Let sessions.js update the ctx indicator after loading message history.
  app.setContextFromMessages = setContextFromMessages;
  app.setContextTokens = setContextTokens;
  app.formatContextTokens = function(tokens) {
    const cfg = _statsConfig();
    const entry = cfg && cfg.find(item => item && item.type === 'ctx');
    return _fmtCtxNum(Number(tokens), entry && entry.decimals != null ? entry.decimals : null)
      .replace(/\.0(?=[kM]$)/, '');
  };
  app.setSessionUsage = setSessionUsage;
  // Per-bubble "more" menu reads the SAME ctx readout the pill renders
  // (_renderCtxIndicator): displayed ctx (real count + thinking ramp), the
  // model's window, the model name, and pre-formatted labels so the menu text
  // matches the pill exactly (same _fmtCtxNum / decimals rules).
  app.getContextStats = function() {
    let entry = null;
    const cfg = _statsConfig();
    if (cfg) entry = cfg.find(e => e.type === 'ctx') || cfg.find(e => e.type === 'ctx-max');
    const decimals = entry ? entry.decimals : null;
    return {
      ctx: _displayedCtx(),
      rawCtx: _contextTokens || 0,
      max: _modelContextLimit || 0,
      model: _currentModelName || '',
      ctxLabel: _fmtCtxNum(_displayedCtx(), decimals),
      maxLabel: _fmtCtxNum(_modelContextLimit, decimals),
    };
  };
  // Per-bubble "more" menu Model row: resolve the model's display name from
  // the same flat roster used by the footer picker. Roles are metadata icons,
  // not a competing set of visible position labels.
  app.getModelRosterInfo = function(modelId) {
    if (!modelId || !_allSlots || !_allSlots.length) return null;
    const short = (name) => String(name || '').includes('/')
      ? String(name).slice(String(name).lastIndexOf('/') + 1)
      : String(name || '');
    const target = short(modelId);
    const slot = _allSlots.find(s => {
      if (!s || !s.model) return false;
      return String(s.model) === String(modelId) || short(s.model) === target;
    });
    if (!slot) return null;
    return { position: short(slot.model), ref: _slotRef(slot) };
  };
}

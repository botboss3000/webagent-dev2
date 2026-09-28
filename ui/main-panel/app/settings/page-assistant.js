'use strict';

/**
 * Page-assistant context engine — the shared brain behind the per-page "advanced
 * chat pill" assistants (App Settings, Agent Settings, …).
 *
 * A page-assistant pill floats at the bottom of an admin config page and hands the
 * user's request to WebAgent the MANAGER. Two things make it "context-aware":
 *   • the pill's PLACEHOLDER follows the visible or hovered `data-pa-area`,
 *     animated like a typewriter through LLM-generated example requests; and
 *   • on send the user's words are WRAPPED with that page's `intro` + the active
 *     area's `prompt` before being handed to the agent.
 * All the text (placeholders, idea hints, prompts) lives in
 * app/defaults/app-prompts.json → page_assistants.<page> (served by
 * GET /api/v1/app-prompts; rotating ideas via POST /app-prompts/page-suggestions).
 *
 * This module is a per-instance FACTORY (not module singletons) because each
 * consumer needs its own state bound to its OWN input element — App Settings'
 * input has a fixed id, but Agent Settings' pill (buildAbilitySearchPill) is built
 * in JS and its input has no stable id. So createPageAssistant() takes the input
 * ELEMENT directly and returns a controller.
 *
 * Two pill shapes, one engine:
 *   • COMPOSER mode (`wireComposer: true`) — a static HTML pill (App Settings'
 *     #ac-pa-bar-row). The engine owns send/Enter/Tab/voice/uploads and calls the
 *     injected `onSend({message, attachmentIds, title})`.
 *   • ATTACHED mode (`wireComposer: false`) — the JS-built buildAbilitySearchPill
 *     (Agent Settings), which already wires its own send/voice/uploads/filter. The
 *     engine owns only config-load + placeholder swap + suggestions/typewriter +
 *     Tab-to-fill, and exposes buildMessage()/currentArea()/title() so the caller's
 *     own onChatSend can assemble + send the context-aware message.
 *
 * Breadcrumbs: callers ui/main-panel/app/settings/app-settings/app-settings.js and
 * ui/main-panel/app/settings/agent-settings/agent-settings.js; the chat-widget
 * spawn lives in ui/chat-widget/js/chat-widget.js (spawnWebagentPageChat); the pill
 * geometry/skin in ui/shared/css/app1.css + index.css.
 */

import { apiPath } from '../../../shared/js/config.js';
import { _fetch } from './utils.js';
import { wireChatPillUploads, startSpeechDictation, isVoiceInputSupported, uploadPendingAttachments } from '../../../shared/js/attachments.js';

const _PA_TYPE_MS = 42;     // per-character type speed (a little jitter added)
const _PA_DELETE_MS = 18;   // per-character delete speed (snappier than typing)
const _PA_HOLD_MS = 2800;   // how long a fully-typed idea stays before deleting
const _PA_GAP_MS = 380;     // blank beat between deleting one idea and typing next

function _withSearchPrefix(value) {
  let text = String(value || '')
    .replace(/^Search(?: abilities)?,?\s*(?:or\s*)?/i, '');
  if (/^[A-Z][a-z]/.test(text)) text = text[0].toLowerCase() + text.slice(1);
  return `Search or ${text}`;
}

/**
 * @param {object} opts
 * @param {string}      opts.page          - app-prompts page key ('app_settings' | 'agent_settings')
 * @param {HTMLElement} opts.section       - section element the mouseover listener is bound to
 * @param {HTMLElement} opts.input         - the pill's <textarea> element
 * @param {function}    [opts.resolvePage] - (areaElement) => app-prompts page key
 * @param {string}      [opts.fallbackPlaceholder] - text after the "Search or " prefix
 * @param {boolean}     [opts.wireComposer=true] - own the whole composer (static-HTML pill)
 *
 *  Composer-mode only:
 * @param {HTMLElement} [opts.send]        - send button
 * @param {HTMLElement} [opts.voice]       - voice button
 * @param {HTMLElement} [opts.row]         - the .chat-pill row (gets .has-text)
 * @param {HTMLElement} [opts.previewBar]  - attachment preview bar
 * @param {Array}       [opts.pending]     - pending-attachments array
 * @param {function}    [opts.onSend]      - ({message, attachmentIds, title}) => void
 * @param {function}    [opts.onInput]     - (text) => void, live input hook (search)
 *
 * @returns {{init, buildMessage, currentArea, currentSuggestion, title,
 *            updatePlaceholder, startTyper, resetTyper}}
 */
export function createPageAssistant(opts = {}) {
  const {
    page,
    section,
    input,
    resolvePage = null,
    fallbackPlaceholder = null,
    wireComposer = true,
    send = null,
    voice = null,
    row = null,
    previewBar = null,
    pending = [],
    onSend = null,
    onInput = null,
  } = opts;

  let configs = {};      // page key -> { title, default_placeholder, intro, areas }
  let activePage = page;
  let area = null;       // visible/hovered area key (drives placeholder + prompt)
  let wired = false;
  const suggestCache = {}; // page:area -> { items:[str], idx, loading, done }
  // A suggestion owns the whole type → hold → delete cycle. Location changes
  // only affect the next cycle, preventing pointer movement from interrupting it.
  const typer = {
    timer: null, location: null, target: '', pos: 0, phase: 'type',
  };

  // ── Config ────────────────────────────────────────────────────────────────
  async function loadConfig() {
    try {
      const res = await _fetch(apiPath('/api/v1/app-prompts'));
      if (res.ok) {
        const data = await res.json();
        configs = data.page_assistants || {};
        for (const [location, entry] of Object.entries(suggestCache)) {
          if (entry.items.length) continue;
          const separator = location.indexOf(':');
          entry.items = fallbackSuggestions(
            location.slice(0, separator),
            location.slice(separator + 1),
          );
        }
      }
    } catch (_) { /* keep null — fall back to the static markup placeholder */ }
    if (!typer.target) updatePlaceholder();
    if (area) fetchSuggestions(activePage, area);
  }

  function activeConfig() { return configs[activePage] || null; }

  function title() {
    const config = activeConfig();
    return (config && config.title) || 'WebAgent';
  }

  // ── Suggestions (rotating example requests per area) ────────────────────────
  // The FULL example request currently being typed/held for the hovered area (or
  // null when none have loaded). Tab-to-fill uses this, so it always inserts the
  // complete line even mid-type.
  function currentSuggestion() {
    if (typer.target) return typer.target;
    const location = activePage && area ? `${activePage}:${area}` : null;
    const entry = location && suggestCache[location];
    if (entry && entry.items && entry.items.length) {
      return entry.items[entry.idx % entry.items.length];
    }
    return null;
  }

  function fallbackSuggestions(targetPage, targetArea) {
    const configured = configs[targetPage]?.areas?.[targetArea]?.fallbacks;
    return Array.isArray(configured)
      ? configured.filter(item => typeof item === 'string' && item.trim())
      : [];
  }

  // Static fallback placeholder (the area's `placeholder` hint). The typewriter
  // owns the placeholder once generated ideas exist; this shows before they load
  // or if generation failed.
  function updatePlaceholder() {
    if (!input) return;
    const config = activeConfig();
    const areas = (config && config.areas) || {};
    const a = area && areas[area];
    const text = (a && a.placeholder)
      || fallbackPlaceholder
      || (config && config.default_placeholder)
      || 'ask how to configure something or customize the app';
    input.placeholder = _withSearchPrefix(text);
  }

  // Ask the backend for a few example requests for one area. Cached per area, so
  // each section costs at most one LLM call per page visit. Best-effort.
  async function fetchSuggestions(targetPage, a) {
    // _fetch supplies the auth token directly. currentUserId can lag behind
    // token restoration during startup, which used to suppress suggestions.
    if (!a) return;
    if (!targetPage) return;
    const location = `${targetPage}:${a}`;
    let entry = suggestCache[location];
    if (entry && (entry.loading || entry.done)) return;  // in flight / already done
    entry = suggestCache[location] = {
      items: fallbackSuggestions(targetPage, a),
      idx: 0,
      loading: true,
      done: false,
    };
    try {
      const res = await _fetch(apiPath('/api/v1/app-prompts/page-suggestions'), {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ page: targetPage, area: a, count: 5 }),
      });
      if (res.ok) {
        const data = await res.json();
        const generated = Array.isArray(data.suggestions)
          ? data.suggestions.filter(s => typeof s === 'string' && s.trim())
          : [];
        if (generated.length) entry.items = generated;
      }
    } catch (_) { /* keep the configured location fallbacks */ }
    entry.loading = false;
    entry.done = true;
  }

  // ── Typewriter ──────────────────────────────────────────────────────────────
  function startTyper() {
    if (typer.timer) return;
    typerTick();
  }

  function resetTyper() {
    if (typer.timer) { clearTimeout(typer.timer); typer.timer = null; }
    typer.location = null;
    typer.target = '';
    typer.pos = 0;
    typer.phase = 'type';
  }

  function typerTick() {
    if (!input) { typer.timer = null; return; }
    // User is composing → placeholder is hidden; don't churn, just wait.
    if (input.value.trim()) {
      typer.timer = setTimeout(typerTick, 700);
      return;
    }

    // Adopt the latest detected location only between complete animation cycles.
    if (!typer.target) {
      const location = activePage && area ? `${activePage}:${area}` : null;
      const entry = location && suggestCache[location];
      const items = entry && entry.items;
      if (!items || !items.length) {
        updatePlaceholder();
        typer.timer = setTimeout(typerTick, 500);
        return;
      }
      typer.location = location;
      typer.target = items[entry.idx % items.length] || '';
      typer.pos = 0;
      typer.phase = 'type';
    }

    const target = typer.target;
    let delay;
    if (typer.phase === 'type') {
      if (typer.pos < target.length) {
        typer.pos++;
        delay = _PA_TYPE_MS + Math.random() * 45;   // human-ish jitter
      } else {
        typer.phase = 'hold';
        delay = _PA_HOLD_MS;
      }
    } else if (typer.phase === 'hold') {
      typer.phase = 'delete';
      delay = _PA_DELETE_MS;
    } else { // 'delete'
      if (typer.pos > 0) {
        typer.pos--;
        delay = _PA_DELETE_MS;
      } else {
        const completedEntry = suggestCache[typer.location];
        if (completedEntry?.items.length) {
          completedEntry.idx = (completedEntry.idx + 1) % completedEntry.items.length;
        }
        typer.location = null;
        typer.target = '';
        typer.phase = 'type';
        delay = _PA_GAP_MS;
      }
    }

    input.placeholder = _withSearchPrefix(target.slice(0, typer.pos));
    typer.timer = setTimeout(typerTick, delay);
  }

  // ── Message assembly ────────────────────────────────────────────────────────
  // page context (intro) + the hovered area's prompt + the user's own words.
  function buildMessage(text) {
    const cfg = activeConfig() || {};
    const a = (cfg.areas || {})[area] || null;
    const parts = [];
    if (cfg.intro) parts.push(cfg.intro);
    if (a && a.prompt) {
      parts.push((a.label ? `[Area: ${a.label}]\n` : '') + a.prompt);
    }
    parts.push('The admin asked:\n' + (text || ''));
    return parts.join('\n\n');
  }

  function currentArea() { return area; }

  // ── Composer-mode helpers (static-HTML pill only) ───────────────────────────
  function clearPending() {
    for (const entry of pending) {
      if (entry._objectUrl) URL.revokeObjectURL(entry._objectUrl);
    }
    pending.length = 0;
    if (previewBar) { previewBar.innerHTML = ''; previewBar.classList.add('ac-pa-hidden'); }
  }

  async function composerSend() {
    if (!input) return;
    const text = (input.value || '').trim();
    // Upload any locally-pending files before sending.
    const attachmentIds = await uploadPendingAttachments(pending);
    if (!text && !attachmentIds.length) return;

    const message = buildMessage(text);
    input.value = '';
    if (typeof onInput === 'function') onInput('');
    clearPending();
    sync();

    try {
      if (typeof onSend === 'function') {
        await onSend({ message, attachmentIds, title: title() });
      }
    } catch (e) {
      console.warn('page-assistant: send failed', e);
    }
  }

  // ── Wiring ──────────────────────────────────────────────────────────────────
  // Tab-to-fill is wired in BOTH modes (the attached pill's own keydown only
  // handles Enter→send, so Tab is ours regardless).
  function wireTabToFill() {
    if (!input) return;
    input.addEventListener('keydown', (e) => {
      if (e.key === 'Tab' && !e.shiftKey && !input.value.trim()) {
        const s = currentSuggestion();
        if (s) {
          e.preventDefault();
          input.value = s;
          // Fire `input` so whichever pill owns this field updates its armed state
          // (composer mode → our sync(); attached pill → its own updateArmed/filter).
          input.dispatchEvent(new Event('input', { bubbles: true }));
        }
      }
    });
  }

  // Composer mode: own send button / Enter / voice / uploads + the has-text swap.
  function sync() {
    if (!input) return;
    const has = !!input.value.trim() || pending.length > 0;
    if (row) row.classList.toggle('has-text', has);
    if (send) send.disabled = !has;
    if (row?.classList.contains('chat-pill-1line')) {
      input.style.height = '38px';
      if (has && input.value) {
        input.style.height = Math.min(input.scrollHeight, 132) + 'px';
      }
      input.style.overflowY = has && input.scrollHeight > 132 ? 'auto' : 'hidden';
    }
  }

  function wireComposerControls() {
    if (send) send.addEventListener('click', () => { composerSend(); });
    if (input) {
      input.addEventListener('input', () => {
        sync();
        if (typeof onInput === 'function') onInput(input.value);
      });
      input.addEventListener('keydown', (e) => {
        if (e.key === 'Enter' && !e.shiftKey) { e.preventDefault(); composerSend(); }
      });
    }
    if (voice) {
      // Where NO voice path can work (insecure context / unsupported browser),
      // force send-only instead of an erroring mic button.
      if (!isVoiceInputSupported()) {
        if (row) row.classList.add('no-voice');
        voice.hidden = true;
        voice.style.setProperty('display', 'none', 'important');
      } else {
        voice.addEventListener('click', () => startSpeechDictation(voice, input));
      }
    }
    if (previewBar) {
      wireChatPillUploads(row, input, {
        previewBar,
        pending,
        onChange: (p) => {
          previewBar.classList.toggle('ac-pa-hidden', p.length === 0);
          sync();
        },
      });
    }
    sync();
  }

  // Location follows scroll/focus and also accepts hover as an immediate override.
  function setLocation(hit) {
    if (!hit) return;
    const nextArea = hit.dataset.paArea;
    const nextPage = (typeof resolvePage === 'function' && resolvePage(hit)) || page;
    if (!nextArea || !nextPage || (nextArea === area && nextPage === activePage)) return;
    activePage = nextPage;
    area = nextArea;
    fetchSuggestions(activePage, area);
    if (!typer.target) updatePlaceholder();
    startTyper();
  }

  function areaBounds(hit) {
    const parts = [
      hit.querySelector(':scope > .ac-category-summary'),
      hit.querySelector(':scope > .ac-category-body'),
    ].filter(Boolean);
    if (!parts.length) parts.push(hit);
    const rects = parts.map(part => part.getBoundingClientRect())
      .filter(rect => rect.height > 0 || rect.width > 0);
    if (!rects.length) return null;
    return {
      top: Math.min(...rects.map(rect => rect.top)),
      bottom: Math.max(...rects.map(rect => rect.bottom)),
    };
  }

  function detectVisibleLocation() {
    if (!section) return;
    const content = section.id === 'app-config-content'
      ? section
      : section.closest('#app-config-content');
    const scroller = content?.closest('.inst-grid.carousel') || content || section;
    const viewport = scroller.getBoundingClientRect();
    const line = viewport.top + Math.min(120, viewport.height * 0.28);
    let best = null;
    let bestVisible = 0;
    for (const hit of section.querySelectorAll('[data-pa-area]')) {
      const bounds = areaBounds(hit);
      if (!bounds) continue;
      if (bounds.top <= line && bounds.bottom > line) {
        best = hit;
        break;
      }
      const visible = Math.max(0,
        Math.min(bounds.bottom, viewport.bottom) - Math.max(bounds.top, viewport.top));
      if (visible > bestVisible) {
        bestVisible = visible;
        best = hit;
      }
    }
    if (best) setLocation(best);
  }

  function wireLocationTracking() {
    if (!section) return;
    section.addEventListener('mouseover', (e) => {
      const hit = e.target.closest('[data-pa-area]');
      if (hit) setLocation(hit);
    });
    const content = section.id === 'app-config-content'
      ? section
      : section.closest('#app-config-content');
    const scroller = content?.closest('.inst-grid.carousel') || content || section;
    let scrollFrame = null;
    scroller.addEventListener('scroll', () => {
      if (scrollFrame) return;
      scrollFrame = requestAnimationFrame(() => {
        scrollFrame = null;
        detectVisibleLocation();
      });
    }, { passive: true });
    input?.addEventListener('focus', detectVisibleLocation);
    requestAnimationFrame(detectVisibleLocation);
  }

  function init() {
    if (wired) return;
    if (!input) return;
    wired = true;
    wireTabToFill();
    if (wireComposer) wireComposerControls();
    wireLocationTracking();
    loadConfig();
  }

  return {
    init,
    buildMessage,
    currentArea,
    currentSuggestion,
    title,
    updatePlaceholder,
    startTyper,
    resetTyper,
  };
}

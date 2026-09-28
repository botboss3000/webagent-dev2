'use strict';

// ── Chat launcher — shared button that opens a chat widget ──────────────────
// createChatLauncher(options) builds a PER-INSTANCE launcher: an icon button
// (floating "FAB" by default, or embedded inline in a page's own layout) that
// opens a chat widget on click. On hover (or first click, for touch) it can
// reveal up to four CORNER buttons around the button, each configurable with an
// icon, tooltip and action type — one of the built-ins (sessions, new_session,
// close_all, open_all, element_pickup) or a page-supplied custom action.
//
// This factory is the shared component behind:
//   • the global WebAgent launcher — webagent-launcher.js is now a THIN WRAPPER
//     that delegates everything here (identical behavior, so it's the
//     regression test that proves the component works);
//   • any page that wants its OWN configured launcher — a Gen UI page via its
//     widget.json, a catalog page via a `widget` block in its page.json, or any
//     page via the <chat-launcher> custom element exported below.
// All state is per-instance, so several launchers can coexist.
//
// Widget behavior is whatever createChatWidget accepts (title, agent, session
// contract, transformMessage, onDone…); the launcher supplies agent resolution
// + the open/close wiring. Styling lives in ui/chat-widget/chat-widget.css
// (CHAT-LAUNCHER-SYNC — keep every launcher's look in one place).

import { app } from '../../shared/js/state.js';
import { apiPath } from '../../shared/js/config.js';
import { authHeaders } from '../../shared/js/left-login.js';
import { icon } from '../../shared/js/icons.js';
import { buildFingerprint } from '../../shared/js/element-fingerprint.js';
import {
  createChatWidget,
  getOpenWidgets,
  closeAllWidgets,
  restoreAllMinimized,
} from './chat-widget.js';

const DEFAULT_HOVER_DELAY_MS = 200;

// The default corner-button set, matching the old chat_ui.json launcher block.
const DEFAULT_CORNER_BUTTONS = {
  enabled: true,
  hover_delay_ms: 200,
  top_left:     { enabled: true, type: 'sessions',       icon: 'list',       tooltip: 'Show sessions' },
  top_right:    { enabled: true, type: 'new_session',    icon: 'plus',       tooltip: 'New session' },
  bottom_left:  { enabled: true, type: 'element_pickup', icon: 'crosshair',  tooltip: 'Toggle element pickup — agent sees what the dot points at' },
  bottom_right: { enabled: true, type: 'open_all',       icon: 'maximize-2', tooltip: 'Open all minimized' },
};

// Deep-clone the buried corner-button objects so a provider's Object.assign or a
// caller's setConfig never mutates the module-level constant OR the caller's own
// source object (e.g. a GenUI page window.__GENUI_WIDGET). This clone is a
// defensive copy at factory-entry time; the factory itself owns cornerConfig.
function _cloneCornerConfig(src) {
  if (!src || src === false) return src;
  return {
    ...src,
    top_left:     src.top_left     ? { ...src.top_left     } : src.top_left,
    top_right:    src.top_right    ? { ...src.top_right    } : src.top_right,
    bottom_left:  src.bottom_left  ? { ...src.bottom_left  } : src.bottom_left,
    bottom_right: src.bottom_right ? { ...src.bottom_right } : src.bottom_right,
  };
}

// ── Animated bot SVG (Lucide bot paths with animation wrappers) ─────────────

/**
 * Returns an inline <svg> of the Lucide bot icon with animation hooks.
 * Uses EXACT Lucide bot paths — same icon the default agent uses.
 *   - Antenna wrapped in .wla-antenna (spin, bob, wiggle)
 *   - Each eye line wrapped in .wla-eye-blink (blink via scaleY)
 */
function _botSvg(size) {
  const s = size || 27;
  return `<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 24 24" class="wla-bot-svg" width="${s}" height="${s}" fill="none"
    stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true">
  <!-- antenna (base at 12,8) -->
  <g class="wla-antenna" style="transform-origin:12px 8px">
    <path d="M12 8V4H8"/>
  </g>
  <!-- head -->
  <rect width="16" height="12" x="4" y="8" rx="2"/>
  <!-- ears -->
  <path d="M2 14h2"/>
  <path d="M20 14h2"/>
  <!-- left eye — outer <g> shifts (glance), inner <g> scales (blink) -->
  <g class="wla-eye-look wla-eye-look-l">
    <g class="wla-eye-blink wla-eye-blink-l" style="transform-origin:9px 14px">
      <path d="M9 13v2"/>
    </g>
  </g>
  <!-- right eye — same nested structure -->
  <g class="wla-eye-look wla-eye-look-r">
    <g class="wla-eye-blink wla-eye-blink-r" style="transform-origin:15px 14px">
      <path d="M15 13v2"/>
    </g>
  </g>
</svg>`;
}

// ── Bot icon animation controller ──────────────────────────────────────────

function _startBotAnimations(button) {
  const reduceMotion = window.matchMedia('(prefers-reduced-motion: reduce)');

  function _els() {
    const svg = button.querySelector('.wla-bot-svg');
    if (!svg) return null;
    return {
      leftLook: svg.querySelector('.wla-eye-look-l'),
      rightLook: svg.querySelector('.wla-eye-look-r'),
      leftBlink: svg.querySelector('.wla-eye-blink-l'),
      rightBlink: svg.querySelector('.wla-eye-blink-r'),
      antenna: svg.querySelector('.wla-antenna'),
    };
  }

  let running = true;
  let timer = null;

  // Full teardown: stop the loop, disconnect the class observer, and remove the
  // reduceMotion listener so they don't outlive the button.
  function stop() {
    running = false;
    if (timer) { cancelAnimationFrame(timer); timer = null; }
    if (dragObserver) { dragObserver.disconnect(); }
    reduceMotion.removeEventListener('change', onMotionChange);
  }

  function _r(min, max) { return min + Math.random() * (max - min); }
  function _pick(arr) { return arr[Math.floor(Math.random() * arr.length)]; }
  function _sleep(ms) { return new Promise(r => setTimeout(r, ms)); }

  function _blinkOnce(e) {
    return new Promise(resolve => {
      if (!e || reduceMotion.matches) { resolve(); return; }
      e.leftBlink.classList.add('blinking');
      e.rightBlink.classList.add('blinking');
      setTimeout(() => {
        e.leftBlink.classList.remove('blinking');
        e.rightBlink.classList.remove('blinking');
        resolve();
      }, 220);
    });
  }

  function _setGlance(e, lx, rx, durMs) {
    if (!e) return;
    const t = `${durMs}ms ease`;
    e.leftLook.style.transition = `transform ${t}`;
    e.rightLook.style.transition = `transform ${t}`;
    e.leftLook.style.transform = `translateX(${lx}px)`;
    e.rightLook.style.transform = `translateX(${rx}px)`;
  }

  function _resetGlance(e) {
    if (!e) return;
    _setGlance(e, 0, 0, 400);
  }

  async function animLookAround(e) {
    if (!e || reduceMotion.matches) return;
    const dur = _r(1200, 2800);
    const lx = _r(-1.8, 1.8), rx = _r(-1.8, 1.8);
    _setGlance(e, lx, rx, dur);
    await _sleep(dur);
  }

  async function animBlink(e) { await _blinkOnce(e); }

  async function animDoubleBlink(e) {
    await _blinkOnce(e);
    await _sleep(120);
    await _blinkOnce(e);
  }

  async function animAntennaSpin(e) {
    if (!e || reduceMotion.matches) return;
    const dur = _r(500, 1600);
    const deg = _pick([-360, -450, 360, 450]);
    e.antenna.style.transition = `transform ${dur}ms ease-in-out`;
    e.antenna.style.transform = `rotate(${deg}deg)`;
    await _sleep(dur);
    e.antenna.style.transition = 'none';
    e.antenna.style.transform = 'rotate(0deg)';
    e.antenna.offsetHeight; // force reflow
    e.antenna.style.transition = '';
  }

  async function animAntennaBob(e) {
    if (!e || reduceMotion.matches) return;
    const dur = _r(400, 1000);
    const dy = _r(1.5, 4);
    e.antenna.style.transition = `transform ${dur * 0.35}ms ease-out`;
    e.antenna.style.transform = `translateY(-${dy}px)`;
    await _sleep(dur * 0.4);
    e.antenna.style.transition = `transform ${dur * 0.65}ms ease-in`;
    e.antenna.style.transform = 'translateY(0px)';
    await _sleep(dur * 0.7);
    e.antenna.style.transition = '';
  }

  async function animAntennaWiggle(e) {
    if (!e || reduceMotion.matches) return;
    const dur = 280;
    e.antenna.style.transition = `transform ${dur * 0.25}ms ease`;
    e.antenna.style.transform = 'rotate(-14deg)';
    await _sleep(dur * 0.25);
    e.antenna.style.transition = `transform ${dur * 0.5}ms ease`;
    e.antenna.style.transform = 'rotate(14deg)';
    await _sleep(dur * 0.5);
    e.antenna.style.transition = `transform ${dur * 0.25}ms ease`;
    e.antenna.style.transform = 'rotate(0deg)';
    await _sleep(dur * 0.3);
    e.antenna.style.transition = '';
  }

  const ANIMS = [
    { fn: animLookAround,   weight: 20 },
    { fn: animBlink,        weight: 10 },
    { fn: animDoubleBlink,  weight: 4  },
    { fn: animAntennaSpin,  weight: 8  },
    { fn: animAntennaBob,   weight: 7  },
    { fn: animAntennaWiggle,weight: 5  },
  ];
  const totalW = ANIMS.reduce((s, a) => s + a.weight, 0);

  async function loop() {
    if (!running || !button.isConnected) return;
    const e = _els();
    if (!e) return;

    let roll = Math.random() * totalW;
    let chosen = ANIMS[0];
    for (const a of ANIMS) {
      roll -= a.weight;
      if (roll <= 0) { chosen = a; break; }
    }

    try { await chosen.fn(e); } catch (_) { /* one bad anim shouldn't kill the loop */ }

    if (!running || !button.isConnected) return;
    const gapMs = _r(600, 3200);
    const start = performance.now();
    function waitFrame(now) {
      if (!running || !button.isConnected) return;
      if (now - start >= gapMs) {
        timer = null;
        loop();
      } else {
        timer = requestAnimationFrame(waitFrame);
      }
    }
    timer = requestAnimationFrame(waitFrame);
  }

  const startDelay = _r(800, 2000);
  const startTime = performance.now();
  function startFrame(now) {
    if (!running || !button.isConnected) return;
    if (now - startTime >= startDelay) {
      timer = null;
      loop();
    } else {
      timer = requestAnimationFrame(startFrame);
    }
  }
  timer = requestAnimationFrame(startFrame);

  const dragObserver = new MutationObserver(() => {
    if (button.classList.contains('dragging')) {
      running = false;
      if (timer) { cancelAnimationFrame(timer); timer = null; }
    } else if (!running && document.body.contains(button)) {
      running = true;
      loop();
    }
  });
  dragObserver.observe(button, { attributes: true, attributeFilter: ['class'] });

  function onMotionChange() {
    const e = _els();
    if (e) { _resetGlance(e); }
  }
  reduceMotion.addEventListener('change', onMotionChange);

  return stop;
}

// ── Icon magnet (pointer-attracted icon) ───────────────────────────────────

function _wireIconMagnet(button) {
  const reduceMotion = window.matchMedia('(prefers-reduced-motion: reduce)');
  const reach = 3.5;
  const radius = 150;
  let frame = 0;
  let pointer = null;

  const update = () => {
    frame = 0;
    if (!pointer || reduceMotion.matches || button.classList.contains('dragging')) {
      button.style.setProperty('--launcher-icon-x', '0px');
      button.style.setProperty('--launcher-icon-y', '0px');
      return;
    }
    const rect = button.getBoundingClientRect();
    const dx = pointer.x - (rect.left + rect.width / 2);
    const dy = pointer.y - (rect.top + rect.height / 2);
    const distance = Math.hypot(dx, dy);
    const strength = Math.max(0, 1 - distance / radius);
    const scale = distance ? reach * strength / distance : 0;
    button.style.setProperty('--launcher-icon-x', `${dx * scale}px`);
    button.style.setProperty('--launcher-icon-y', `${dy * scale}px`);
  };

  const schedule = () => {
    if (!frame) frame = requestAnimationFrame(update);
  };

  return (event) => {
    pointer = { x: event.clientX, y: event.clientY };
    schedule();
  };
}

// ── Drag (fixed launchers only) ─────────────────────────────────────────────

function _wireDrag(button, opts) {
  const { onDrag, onFinish, onResize, place } = opts;
  let start = null;

  const onDown = (event) => {
    if (event.button !== 0) return;
    const rect = button.getBoundingClientRect();
    start = { x: event.clientX, y: event.clientY, left: rect.left, top: rect.top, moved: false };
    button.setPointerCapture(event.pointerId);
    button.classList.add('dragging');
    onDrag();
  };
  const onMove = (event) => {
    if (!start) return;
    const dx = event.clientX - start.x;
    const dy = event.clientY - start.y;
    if (!start.moved && Math.hypot(dx, dy) < 5) return;
    if (!start.moved) { start.moved = true; }
    place(button, start.left + dx, start.top + dy);
  };
  const finish = (event) => {
    if (!start) return;
    const moved = start.moved;
    start = null;
    button.classList.remove('dragging');
    try { button.releasePointerCapture(event.pointerId); } catch (_) {}
    if (moved && onFinish) onFinish();
  };
  const onWinResize = () => {
    const rect = button.getBoundingClientRect();
    place(button, rect.left, rect.top);
  };

  button.addEventListener('pointerdown', onDown);
  button.addEventListener('pointermove', onMove);
  button.addEventListener('pointerup', finish);
  button.addEventListener('pointercancel', finish);
  window.addEventListener('resize', onWinResize);

  return () => {
    button.removeEventListener('pointerdown', onDown);
    button.removeEventListener('pointermove', onMove);
    button.removeEventListener('pointerup', finish);
    button.removeEventListener('pointercancel', finish);
    window.removeEventListener('resize', onWinResize);
  };
}

function _placeFixed(button, left, top) {
  const edge = 8;
  const rect = button.getBoundingClientRect();
  left = Math.max(edge, Math.min(innerWidth - rect.width - edge, left));
  top = Math.max(edge, Math.min(innerHeight - rect.height - edge, top));
  Object.assign(button.style, { left: `${left}px`, top: `${top}px`, right: 'auto', bottom: 'auto' });
}

// ── Factory ─────────────────────────────────────────────────────────────────

/**
 * Create a launcher button that opens a chat widget.
 * @param {object} opts
 * @param {HTMLElement} [opts.mountEl=document.body]  where the button is appended
 * @param {string} [opts.id]                          button id (e.g. 'webagent-launcher')
 * @param {string} [opts.position='fixed']            'fixed' (floating FAB) | 'inline' (in-page)
 * @param {string} [opts.icon='bot']                  'bot' (animated) | Lucide icon name | raw SVG/HTML string
 * @param {number} [opts.iconSize=27]                 icon px for the bot / lucide icon
 * @param {string} [opts.ariaLabel]                   aria-label for the button
 * @param {string} [opts.label='']                    optional text label beside the icon
 * @param {boolean} [opts.showLabel=false]            render the label
 * @param {string|null} [opts.agentId]                direct agent id for the widget
 * @param {Function|null} [opts.ensureAgent]          async () => agentId — caller handles ability setup
 * @param {Function|null} [opts.resolveAgent]         async () => agent record {id,name,icon}
 * @param {object|null} [opts.cornerButtons]          corner config (same shape as chat_ui.json launcher.corner_buttons); null → default set, false → none
 * @param {Function|null} [opts.cornerButtonsProvider] async () => corner config (fetched at init, like the old profile fetch)
 * @param {object} [opts.cornerActions]               custom corner action types: { typeName: (btn, ctx) => void }
 * @param {object} [opts.widget]                      options passed to createChatWidget (title, session contract, transformMessage, onDone…)
 * @param {boolean} [opts.draggable]                  default true for 'fixed'
 * @param {string|null} [opts.storageKey]             localStorage key for the saved position
 * @param {number} [opts.hoverDelayMs=200]            hover delay before the corner menu shows
 * @param {boolean} [opts.iconMagnet]                 pointer-attracted icon (default true for fixed)
 * @param {boolean} [opts.elementPickup=false]        wire the element-pickup corner action + app hooks
 * @param {'12oclock'|'surroundings'} [opts.pickupMode]  '12oclock' (default) samples the element above the widget centre; 'surroundings' samples N/E/S/W at a radius (~60px) and combines the fingerprints
 * @param {Function|null} [opts.onOpen]               (widget) => void — after the widget opens
 * @param {Function|null} [opts.onClose]              () => void — after the widget closes
 * @returns {{el:HTMLElement, button:HTMLElement, open:Function, close:Function, destroy:Function,
 *            toggleElementPickup:Function, setConfig:Function, get widget():object|null}}
 */
export function createChatLauncher(opts = {}) {
  const cfg = { ...opts };
  const mountEl = cfg.mountEl || document.body;
  const position = cfg.position === 'inline' ? 'inline' : 'fixed';
  const draggable = cfg.draggable !== undefined ? !!cfg.draggable : (position === 'fixed');
  const magnet = cfg.iconMagnet !== undefined ? !!cfg.iconMagnet : (position === 'fixed');
  const storageKey = cfg.storageKey || null;
  const hoverDelayMs = cfg.hoverDelayMs || DEFAULT_HOVER_DELAY_MS;
  // Defensive clone: the factory OWNS cornerConfig; the caller's source object
  // (and the module-level DEFAULT_CORNER_BUTTONS) are never mutated. The
  // provider below overwrites PER-SLOT objects (top_left etc.) via Object.assign,
  // which is safe because we own the top-level wrapper and its leaf objects.
  const cornerConfig = (cfg.cornerButtons !== undefined)
    ? _cloneCornerConfig(cfg.cornerButtons)
    : _cloneCornerConfig(DEFAULT_CORNER_BUTTONS);

  let destroyed = false;
  let agent = null;
  let widget = null;

  // Per-instance hover-menu / popup state (old module globals moved here).
  let menu = null;
  let sessionsPopup = null;
  let sessionsAbort = null;
  let hoverTimer = null;
  let hoverLeaveTimer = null;
  let clickRevealed = false;
  let clickOutsideHandler = null;
  let pickupActive = false;
  let pickupRAF = null;
  const pickupMode = cfg.pickupMode || '12oclock';
  const PICKUP_SURROUNDINGS_RADIUS = 60;  // px offset for N/E/S/W samples
  let stopBotAnim = null;

  const cleanups = [];

  // _rebuildHoverMenu attaches its own document/button listeners; each rebuild
  // tears down the PREVIOUS set so repeated calls (cornerButtonsProvider
  // resolved late, setConfig) don't stack orphaned handlers.
  let _menuCleanups = [];

  // ── Button ──

  const button = document.createElement('button');
  button.type = 'button';
  if (cfg.id) button.id = cfg.id;
  button.className = 'chat-launcher' + (position === 'inline' ? ' chat-launcher--inline' : ' chat-launcher--fixed');
  if (cfg.id === 'webagent-launcher') button.classList.add('webagent-launcher'); // back-compat styling alias
  button.setAttribute('aria-label', cfg.ariaLabel || 'Open WebAgent chat');

  function renderIcon() {
    button.innerHTML = '';
    if (cfg.icon === 'bot' || cfg.icon == null) {
      button.innerHTML = _botSvg(cfg.iconSize || 27);
      if (stopBotAnim) { try { stopBotAnim(); } catch (_) {} stopBotAnim = null; }
      stopBotAnim = _startBotAnimations(button);
    } else if (typeof cfg.icon === 'string' && cfg.icon.indexOf('<') !== -1) {
      button.innerHTML = cfg.icon; // raw SVG / HTML
    } else {
      button.innerHTML = icon(cfg.icon || 'message-circle', { size: (cfg.iconSize || 24) + 'px' });
    }
    if (cfg.showLabel && cfg.label) {
      const lab = document.createElement('span');
      lab.className = 'chat-launcher-label';
      lab.textContent = cfg.label;
      button.appendChild(lab);
    }
    if (cfg.elementPickup) {
      const dot = document.createElement('span');
      dot.className = 'wla-pickup-dot';
      dot.setAttribute('aria-hidden', 'true');
      button.appendChild(dot);
    }
  }
  renderIcon();
  mountEl.appendChild(button);

  // ── Agent resolution ──

  async function _resolveAgent() {
    if (cfg.agentId) return { id: cfg.agentId, name: cfg.ariaLabel || 'WebAgent', icon: 'bot' };
    if (cfg.ensureAgent) {
      const id = await cfg.ensureAgent();
      return { id, name: cfg.ariaLabel || 'WebAgent', icon: 'bot' };
    }
    if (cfg.resolveAgent) {
      const rec = await cfg.resolveAgent();
      if (rec) return rec;
    }
    return { id: null, name: 'WebAgent', icon: 'bot' };
  }

  // ── Widget ──

  function _widgetOpts(agentRecord) {
    const w = { ...(cfg.widget || {}) };
    if (cfg.agentId) w.agentId = cfg.agentId;
    else if (cfg.ensureAgent) w.ensureAgent = cfg.ensureAgent;
    else if (agentRecord && agentRecord.id) {
      w.agentId = agentRecord.id;
      if (w.title == null) w.title = agentRecord.name || 'WebAgent';
      if (w.iconName == null) w.iconName = agentRecord.icon || 'bot';
    }
    // Chain the caller's own onClose (from widget.onClose) with the launcher's
    // mandatory hide/show wiring so the user's callback isn't silently lost.
    const userClose = w.onClose || null;
    w.onClose = () => {
      widget = null;
      button.hidden = false;
      if (typeof userClose === 'function') { try { userClose(); } catch (_) {} }
      if (typeof cfg.onClose === 'function') { try { cfg.onClose(); } catch (_) {} }
    };
    return w;
  }

  function open() {
    if (destroyed) return Promise.resolve(null);
    if (widget && widget.el && widget.el.isConnected) return Promise.resolve(widget);
    return _resolveAgent().then((agentRecord) => {
      agent = agentRecord;
      if (destroyed) return null;
      button.hidden = true;
      widget = createChatWidget(_widgetOpts(agentRecord));
      widget.open();
      if (typeof cfg.onOpen === 'function') { try { cfg.onOpen(widget); } catch (_) {} }
      return widget;
    });
  }

  function close() {
    if (widget && typeof widget.close === 'function') { try { widget.close(); } catch (_) {} }
    widget = null;
    button.hidden = false;
  }

  // ── Corner actions ──

  const builtinActions = {
    new_session: () => {
      const existing = getOpenWidgets().find(w => w.el && w.el.isConnected);
      if (existing) return; // a widget is already open — don't stack silent ones
      _resolveAgent().then((agentRecord) => {
        if (destroyed) return;
        button.hidden = true;
        widget = createChatWidget(_widgetOpts(agentRecord));
        widget.open();
      });
    },
    sessions: (btn) => _toggleSessions(btn),
    close_all: () => { closeAllWidgets(); _hideSessionsPopup(); },
    open_all: () => { restoreAllMinimized(); _hideSessionsPopup(); },
    element_pickup: (btn) => {
      if (typeof app !== 'undefined' && app && typeof app._toggleElementPickup === 'function') {
        const next = !app.elementPickupActive;
        app._toggleElementPickup();
        btn.classList.toggle('--active', next);
      }
    },
  };

  function handleCornerAction(type, btn) {
    const custom = (cfg.cornerActions || {})[type];
    if (typeof custom === 'function') {
      try { custom(btn, api); } catch (_) {}
      return;
    }
    const builtin = builtinActions[type];
    if (builtin) { try { builtin(btn); } catch (_) {} }
  }

  // ── Sessions popup ──

  function _toggleSessions(btn) {
    if (sessionsPopup && sessionsPopup.classList.contains('show')) {
      _hideSessionsPopup();
      return;
    }
    if (!sessionsPopup) {
      sessionsPopup = document.createElement('div');
      sessionsPopup.className = 'chat-launcher-sessions-popup webagent-launcher-sessions-popup';
      sessionsPopup.innerHTML = '<span class="wlsp-empty">Loading sessions…</span>';
      mountEl.appendChild(sessionsPopup);
    }
    const btnRect = btn.getBoundingClientRect();
    sessionsPopup.style.left = btnRect.left + 'px';
    sessionsPopup.style.top = (btnRect.bottom + 4) + 'px';
    sessionsPopup.classList.add('show');
    _fetchSessionsList();
  }

  function _hideSessionsPopup() {
    if (sessionsPopup) {
      sessionsPopup.classList.remove('show');
      sessionsPopup.innerHTML = '';
    }
    if (sessionsAbort) { sessionsAbort.abort(); sessionsAbort = null; }
  }

  // Render the popup rows from an already-fetched session list (hybrid cache
  // or server). Each row carries data-sid for the click-through.
  function _renderSessionsList(sessions) {
    if (!sessionsPopup || !sessionsPopup.classList.contains('show')) return;
    if (!sessions.length) {
      sessionsPopup.innerHTML = '<span class="wlsp-empty">No sessions yet</span>';
      return;
    }
    let html = '';
    for (const s of sessions) {
      const title = s.title || 'New Session';
      const time = _fmtRelTime(s.updated_at || s.created_at);
      const status = s.run_status || '';
      const dotClass = status === 'running' ? 'wlsp-dot running'
        : status === 'done' ? 'wlsp-dot done'
        : 'wlsp-dot';
      const sid = s.id;
      html += `<div class="webagent-launcher-sessions-item" data-sid="${sid}"><span class="${dotClass}"></span><span class="wlsp-title">${_esc(title)}</span><span class="wlsp-time">${time}</span></div>`;
    }
    sessionsPopup.innerHTML = html;
    sessionsPopup.querySelectorAll('.webagent-launcher-sessions-item').forEach(el => {
      el.addEventListener('click', () => {
        const sid = el.dataset.sid;
        if (!sid) return;
        _hideSessionsPopup();
        _openSessionWidget(sid);
      });
    });
  }

  async function _fetchSessionsList() {
    if (!sessionsPopup) return;
    if (!app.currentUserId) {
      sessionsPopup.innerHTML = '<span class="wlsp-empty">Sign in to see sessions</span>';
      return;
    }
    // Hybrid-cache first: the adapter serves the session list INSTANTLY from
    // IndexedDB and refreshes it in the background — no network wait on the
    // popup (a slow mobile link would otherwise stall every open). Falls back
    // to the server fetch below when the cache isn't available.
    try {
      const mod = await import('/ui/chat/js/storage/storage-adapter.js');
      const sa = mod && mod.storageAdapter;
      if (sa && sa.isHybrid) {
        const cached = await sa.listSessions(app.currentUserId);
        if (Array.isArray(cached) && cached.length) {
          _renderSessionsList(cached.slice(0, 30));
          return;
        }
      }
    } catch (_) { /* fall through to the server fetch */ }

    if (sessionsAbort) sessionsAbort.abort();
    sessionsAbort = new AbortController();
    try {
      const token = localStorage.getItem('auth_token');
      let url = `/api/v1/db/sessions?db=user.db&user_id=${encodeURIComponent(app.currentUserId)}&limit=30`;
      // The launcher popup renders title/time/status only — never manifest
      // fields — so skip the per-session manifest computation (see /sessions
      // include_manifest=0; the dropdown does the same).
      url += `&include_manifest=0`;
      if (token) url += `&token=${encodeURIComponent(token)}`;
      const res = await fetch(apiPath(url), { signal: sessionsAbort.signal });
      if (!res.ok) throw new Error('Fetch failed');
      const data = await res.json();
      const sessions = (data.sessions || []).slice(0, 30);
      if (!sessionsPopup || !sessionsPopup.classList.contains('show')) return;
      _renderSessionsList(sessions);
    } catch (e) {
      if (e.name === 'AbortError') return;
      if (sessionsPopup && sessionsPopup.classList.contains('show')) {
        sessionsPopup.innerHTML = '<span class="wlsp-empty">Failed to load sessions</span>';
      }
    } finally {
      sessionsAbort = null;
    }
  }

  async function _openSessionWidget(sid) {
    const existing = getOpenWidgets().find(w => w.sessionId === sid);
    if (existing) {
      if (existing.minimized) existing.restore();
      return;
    }
    // Prefer the main app's cache-aware switch: zero server round trips here,
    // and the transcript renders from IndexedDB when the hybrid cache has it
    // (the widget's own fallback below fetches /api/v1/db/sessions/{id} first —
    // two round trips on a slow mobile link).
    if (window.__switchToSession) {
      _hideSessionsPopup();
      try { window.__switchToSession(sid); } catch (_) {}
      return;
    }
    try {
      const token = localStorage.getItem('auth_token');
      let url = `/api/v1/db/sessions/${encodeURIComponent(sid)}?db=user.db`;
      if (token) url += `&token=${encodeURIComponent(token)}`;
      const res = await fetch(apiPath(url));
      if (!res.ok) return;
      const data = await res.json();
      const session = data.session || data;
      const agentId = session.agent_id || null;
      let targetAgent = agentId;
      if (!targetAgent) {
        const a = await _resolveAgent();
        targetAgent = a.id;
      }
      if (destroyed) return;
      button.hidden = true;
      widget = createChatWidget({
        title: session.title || 'Session',
        iconName: 'bot',
        agentId: targetAgent,
        initialMessage: '',
        onClose: () => { widget = null; button.hidden = false; },
      });
      widget.open();
      _hideSessionsPopup();
      if (window.__switchToSession) {
        window.__switchToSession(sid);
      } else if (app && typeof app.loadSessionChat === 'function') {
        widget.close();
        app.loadSessionChat(sid);
      }
    } catch (_) { /* silently fail */ }
  }

  // ── Hover corner menu ──

  function _rebuildHoverMenu() {
    if (menu && menu.parentNode) menu.parentNode.removeChild(menu);
    menu = null;
    _menuCleanups.forEach((fn) => { try { fn(); } catch (_) {} });
    _menuCleanups = [];
    const cornerCfg = cornerConfig === false ? null : (cornerConfig || DEFAULT_CORNER_BUTTONS);
    if (!cornerCfg || cornerCfg.enabled === false) return;

    const m = document.createElement('div');
    m.className = 'chat-launcher-hover-menu webagent-launcher-hover-menu';
    if (cfg.id) m.id = cfg.id + '-hover-menu';
    mountEl.appendChild(m);
    menu = m;

    const delay = cornerCfg.hover_delay_ms || hoverDelayMs;

    const corners = [
      { key: 'top_left',     cls: '--tl' },
      { key: 'top_right',    cls: '--tr' },
      { key: 'bottom_left',  cls: '--bl' },
      { key: 'bottom_right', cls: '--br' },
    ];

    for (const { key, cls } of corners) {
      const btnCfg = cornerCfg[key];
      if (!btnCfg || btnCfg.enabled === false) continue;
      const btn = document.createElement('button');
      btn.type = 'button';
      btn.className = 'chat-launcher-corner-btn webagent-launcher-corner-btn ' + cls;
      btn.title = btnCfg.tooltip || '';
      btn.setAttribute('aria-label', btnCfg.tooltip || '');
      btn.innerHTML = icon(btnCfg.icon || 'circle', { size: '12px' });
      btn.dataset.cornerType = btnCfg.type || '';
      btn.addEventListener('click', (e) => {
        e.stopPropagation();
        handleCornerAction(btnCfg.type, btn);
      });
      m.appendChild(btn);
    }

    let magnetPointer = null;

    const onMove = (e) => {
      magnetPointer = { x: e.clientX, y: e.clientY };
      checkProximity(magnetPointer, delay);
    };
    const onLeave = () => {
      magnetPointer = null;
      scheduleHide(300);
    };

    document.addEventListener('pointermove', onMove, { passive: true });
    document.addEventListener('pointerleave', onLeave);
    _menuCleanups.push(() => {
      document.removeEventListener('pointermove', onMove);
      document.removeEventListener('pointerleave', onLeave);
    });

    button.addEventListener('pointerenter', () => {
      if (hoverLeaveTimer) { clearTimeout(hoverLeaveTimer); hoverLeaveTimer = null; }
      scheduleShow(delay);
    });
    button.addEventListener('pointerleave', (e) => {
      const related = e.relatedTarget;
      if (related && (related === menu || menu.contains(related))) return;
      scheduleHide(300);
    });

    menu.addEventListener('pointerenter', () => {
      if (hoverLeaveTimer) { clearTimeout(hoverLeaveTimer); hoverLeaveTimer = null; }
    });
    menu.addEventListener('pointerleave', (e) => {
      const related = e.relatedTarget;
      if (related === button || (related && related.closest?.('#webagent-launcher'))) return;
      scheduleHide(300);
    });
  }

  function checkProximity(pointer, delay) {
    if (!menu) return;
    const rect = button.getBoundingClientRect();
    const cx = rect.left + rect.width / 2;
    const cy = rect.top + rect.height / 2;
    const dist = Math.hypot(pointer.x - cx, pointer.y - cy);
    const threshold = Math.max(rect.width, rect.height) + 40;
    if (dist <= threshold) {
      if (button.classList.contains('dragging')) return;
      if (hoverLeaveTimer) { clearTimeout(hoverLeaveTimer); hoverLeaveTimer = null; }
      scheduleShow(delay);
    } else {
      scheduleHide(300);
    }
  }

  function scheduleShow(delay) {
    if (hoverTimer) return;
    if (menu && menu.classList.contains('show')) return;
    if (button.classList.contains('dragging')) return;
    hoverTimer = setTimeout(() => {
      hoverTimer = null;
      if (button.classList.contains('dragging')) return;
      positionMenu();
      if (menu) menu.classList.add('show');
    }, delay);
  }

  function scheduleHide(delay) {
    if (hoverTimer) { clearTimeout(hoverTimer); hoverTimer = null; }
    if (!menu || !menu.classList.contains('show')) return;
    if (hoverLeaveTimer) return;
    hoverLeaveTimer = setTimeout(() => {
      hoverLeaveTimer = null;
      if (menu) menu.classList.remove('show');
      clickRevealed = false;
      if (clickOutsideHandler) {
        document.removeEventListener('pointerdown', clickOutsideHandler);
        clickOutsideHandler = null;
      }
      _hideSessionsPopup();
    }, delay);
  }

  function positionMenu() {
    if (!menu) return;
    const r = button.getBoundingClientRect();
    menu.style.left = r.left + 'px';
    menu.style.top = r.top + 'px';
    menu.style.width = r.width + 'px';
    menu.style.height = r.height + 'px';
  }

  // ── Click-to-reveal then open (works on touch where hover doesn't) ──

  function onButtonClick() {
    if (dragged) { dragged = false; return; }
    if (!clickRevealed && menu && !menu.classList.contains('show')) {
      clickRevealed = true;
      positionMenu();
      menu.classList.add('show');
      if (clickOutsideHandler) document.removeEventListener('pointerdown', clickOutsideHandler);
      clickOutsideHandler = (e) => {
        if (e.target === button || button.contains(e.target)) return;
        if (menu && menu.contains(e.target)) return;
        menu.classList.remove('show');
        clickRevealed = false;
        document.removeEventListener('pointerdown', clickOutsideHandler);
        clickOutsideHandler = null;
      };
      setTimeout(() => {
        if (clickOutsideHandler) document.addEventListener('pointerdown', clickOutsideHandler);
      }, 0);
      return;
    }
    clickRevealed = false;
    open();
  }

  let dragged = false;

  // ── Element pickup (optional) ──

  function _setPickup(on) {
    pickupActive = on;
    app.elementPickupActive = on;
    app.elementPickupMode = pickupMode;
    app.elementPickupFingerprint = null;
    const dot = button.querySelector('.wla-pickup-dot');
    if (dot) dot.classList.toggle('active', on);
    if (on) {
      if (!pickupRAF) pickupRAF = requestAnimationFrame(_pickupLoop);
    } else {
      if (pickupRAF) { cancelAnimationFrame(pickupRAF); pickupRAF = null; }
    }
  }

  function _pickupLoop() {
    if (!pickupActive) { pickupRAF = null; return; }
    try {
      const rect = button.getBoundingClientRect();
      const cx = rect.left + rect.width / 2;
      const cy = rect.top + rect.height / 2;
      if (pickupMode === 'surroundings') {
        // Sample four cardinal directions at the configured radius, skipping
        // the button itself. Build a composite fingerprint with all hits.
        const dirs = [
          { key: 'north', x: cx, y: cy - PICKUP_SURROUNDINGS_RADIUS },
          { key: 'east',  x: cx + PICKUP_SURROUNDINGS_RADIUS, y: cy },
          { key: 'south', x: cx, y: cy + PICKUP_SURROUNDINGS_RADIUS },
          { key: 'west',  x: cx - PICKUP_SURROUNDINGS_RADIUS, y: cy },
        ];
        const hits = {};
        let anyHit = false;
        for (const d of dirs) {
          const el = document.elementFromPoint(d.x, d.y);
          if (el && el !== button && !button.contains(el)) {
            hits[d.key] = buildFingerprint(el, d.x, d.y);
            anyHit = true;
          }
        }
        if (anyHit) {
          app.elementPickupFingerprint = {
            intent: 'Element pickup (surroundings)',
            mode: 'surroundings',
            directions: hits,
            centre: { x: Math.round(cx), y: Math.round(cy) },
            radius: PICKUP_SURROUNDINGS_RADIUS,
          };
        }
      } else {
        // 12-o'clock: sample the element directly above the widget centre.
        const px = cx;
        const py = rect.top;
        const el = document.elementFromPoint(px, py);
        if (el && el !== button && !button.contains(el)) {
          app.elementPickupFingerprint = buildFingerprint(el, px, py);
        }
      }
    } catch (_) { /* rAF loop — best-effort */ }
    pickupRAF = requestAnimationFrame(_pickupLoop);
  }

  // Only ONE launcher at a time may own the global elementPickup toggle (the
  // first one to request it wins; subsequent launchers silently skip the hook).
  // Destroying a launcher that set it clears it; a launcher that never set it
  // (because another one already owned it) leaves it untouched.
  let _pickupOwner = false;
  if (cfg.elementPickup && typeof app !== 'undefined' && app && !app._toggleElementPickup) {
    _pickupOwner = true;
    app._toggleElementPickup = () => _setPickup(!pickupActive);
  }

  // ── Wiring ──

  button.addEventListener('click', onButtonClick);

  if (magnet) {
    const magnetMove = _wireIconMagnet(button);
    const onDocMove = (e) => magnetMove(e);
    const onDocLeave = () => { magnetMove({ clientX: -9999, clientY: -9999 }); };
    document.addEventListener('pointermove', onDocMove, { passive: true });
    document.addEventListener('pointerleave', onDocLeave);
    cleanups.push(() => {
      document.removeEventListener('pointermove', onDocMove);
      document.removeEventListener('pointerleave', onDocLeave);
    });
  }

  if (draggable && position === 'fixed') {
    cleanups.push(_wireDrag(button, {
      onDrag: () => {
        dragged = true;
        if (hoverTimer) { clearTimeout(hoverTimer); hoverTimer = null; }
        if (hoverLeaveTimer) { clearTimeout(hoverLeaveTimer); hoverLeaveTimer = null; }
        if (menu) {
          menu.classList.remove('show');
          menu.style.transition = 'none';
        }
        clickRevealed = false;
        if (clickOutsideHandler) {
          document.removeEventListener('pointerdown', clickOutsideHandler);
          clickOutsideHandler = null;
        }
      },
      onFinish: () => {
        if (menu) menu.style.transition = '';
        if (storageKey) _savePosition();
      },
      place: _placeFixed,
    }));
    _restorePosition();
  }

  // Fetch the corner config lazily (old behavior: read from ui-config profile).
  if (typeof cfg.cornerButtonsProvider === 'function') {
    cfg.cornerButtonsProvider().then((cfgFromProfile) => {
      if (destroyed) return;
      if (cfgFromProfile) Object.assign(cornerConfig, cfgFromProfile);
      _rebuildHoverMenu();
    }).catch(() => { if (!destroyed) _rebuildHoverMenu(); });
  } else {
    _rebuildHoverMenu();
  }

  // ── Position persistence (fixed) ──

  function _savePosition() {
    const rect = button.getBoundingClientRect();
    try {
      localStorage.setItem(storageKey, JSON.stringify({
        x: rect.left / Math.max(1, innerWidth - rect.width),
        y: rect.top / Math.max(1, innerHeight - rect.height),
      }));
    } catch (_) {}
  }

  function _restorePosition() {
    if (!storageKey) return;
    try {
      const saved = JSON.parse(localStorage.getItem(storageKey) || 'null');
      if (!saved || !Number.isFinite(saved.x) || !Number.isFinite(saved.y)) return;
      requestAnimationFrame(() => {
        if (destroyed) return;
        const rect = button.getBoundingClientRect();
        _placeFixed(button, saved.x * (innerWidth - rect.width), saved.y * (innerHeight - rect.height));
      });
    } catch (_) {}
  }

  // ── Public API ──

  function setConfig(next = {}) {
    if (next.widget) Object.assign(cfg.widget = cfg.widget || {}, next.widget);
    if (next.cornerButtons) {
      Object.assign(cornerConfig, next.cornerButtons);
      _rebuildHoverMenu();
    }
    if (next.cornerActions) Object.assign(cfg.cornerActions = cfg.cornerActions || {}, next.cornerActions);
    if (next.icon) { cfg.icon = next.icon; renderIcon(); }
    if (next.ariaLabel) { cfg.ariaLabel = next.ariaLabel; button.setAttribute('aria-label', next.ariaLabel); }
    if (next.label !== undefined) { cfg.label = next.label; renderIcon(); }
    if (next.showLabel !== undefined) { cfg.showLabel = !!next.showLabel; renderIcon(); }
  }

  function toggleElementPickup() {
    if (cfg.elementPickup) _setPickup(!pickupActive);
  }

  function destroy() {
    if (destroyed) return;
    destroyed = true;
    try { close(); } catch (_) {}
    if (stopBotAnim) { try { stopBotAnim(); } catch (_) {} stopBotAnim = null; }
    if (pickupRAF) { cancelAnimationFrame(pickupRAF); pickupRAF = null; }
    _menuCleanups.forEach((fn) => { try { fn(); } catch (_) {} });
    _menuCleanups = [];
    cleanups.forEach((fn) => { try { fn(); } catch (_) {} });
    cleanups.length = 0;
    if (menu && menu.parentNode) menu.parentNode.removeChild(menu);
    if (sessionsPopup && sessionsPopup.parentNode) sessionsPopup.parentNode.removeChild(sessionsPopup);
    if (button.parentNode) button.parentNode.removeChild(button);
    if (cfg.elementPickup && _pickupOwner && typeof app !== 'undefined' && app) {
      try { app._toggleElementPickup = null; } catch (_) {}
    }
  }

  const api = {
    el: button,
    button,
    open,
    close,
    destroy,
    setConfig,
    toggleElementPickup,
    get widget() { return widget; },
  };
  return api;
}

// ── Helpers ─────────────────────────────────────────────────────────────────

function _fmtRelTime(dateStr) {
  if (!dateStr) return '';
  const now = Date.now();
  const d = new Date(dateStr.endsWith('Z') ? dateStr : dateStr + 'Z');
  const diffMs = now - d.getTime();
  if (diffMs < 0) return 'just now';
  const sec = Math.floor(diffMs / 1000);
  if (sec < 60) return 'just now';
  const min = Math.floor(sec / 60);
  if (min < 60) return `${min}m ago`;
  const hrs = Math.floor(min / 60);
  if (hrs < 24) return `${hrs}h ago`;
  const days = Math.floor(hrs / 24);
  if (days < 7) return `${days}d ago`;
  return d.toLocaleDateString(undefined, { month: 'short', day: 'numeric' });
}

function _esc(str) {
  const div = document.createElement('div');
  div.textContent = String(str);
  return div.innerHTML;
}

// ── <chat-launcher> custom element ──────────────────────────────────────────
// Declarative form: drop <chat-launcher icon="bot" agent="<id>"></chat-launcher>
// into any page in the MAIN document (not inside a Gen UI shadow — the widget
// layer lives on <body>, so embed the element in the page that owns the button).
// Attributes: icon, agent (agent id), label, position, draggable, src (URL to a
// JSON config file — the factory opts minus mountEl/el). The element exposes
// `.launcher` (the factory API) and destroys it on disconnect.

export class ChatLauncherElement extends HTMLElement {
  connectedCallback() {
    this._launcher = null;
    const cfg = this._readConfig();
    if (!cfg) return;
    // The element owns the launcher's mount: fixed launchers escape it via
    // position:fixed anyway; inline launchers sit right where the element is.
    cfg.mountEl = this;
    try {
      this._launcher = createChatLauncher(cfg);
      this.dispatchEvent(new CustomEvent('chat-launcher-ready', { detail: this._launcher }));
    } catch (e) {
      console.error('chat-launcher mount failed', e);
    }
  }

  disconnectedCallback() {
    if (this._launcher && typeof this._launcher.destroy === 'function') {
      try { this._launcher.destroy(); } catch (_) {}
    }
    this._launcher = null;
  }

  _readConfig() {
    const cfg = {};
    if (this.id) cfg.id = this.id;
    if (this.hasAttribute('icon')) cfg.icon = this.getAttribute('icon');
    if (this.hasAttribute('agent')) cfg.agentId = this.getAttribute('agent');
    if (this.hasAttribute('label')) cfg.label = this.getAttribute('label');
    if (this.hasAttribute('show-label')) cfg.showLabel = this.getAttribute('show-label') !== 'false';
    if (this.hasAttribute('position')) cfg.position = this.getAttribute('position') === 'inline' ? 'inline' : 'fixed';
    if (this.hasAttribute('draggable')) cfg.draggable = this.getAttribute('draggable') !== 'false';
    if (this.hasAttribute('aria-label')) cfg.ariaLabel = this.getAttribute('aria-label');
    if (this.hasAttribute('storage-key')) cfg.storageKey = this.getAttribute('storage-key');

    const src = this.getAttribute('src');
    if (src) {
      // JSON config file wins for everything it specifies; attributes are the
      // lightweight declarative overrides for the common fields above.
      let loaded = null;
      try {
        loaded = JSON.parse(src);
      } catch (_) { /* not inline JSON — treat as URL */ }
      if (loaded && typeof loaded === 'object') {
        return { ...loaded, ...cfg };
      }
      // async URL fetch: build after load
      this._mountFromUrl(src, cfg);
      return null;
    }
    // Inline JSON child: <chat-launcher><script type="application/json">…</script></chat-launcher>
    const inline = this.querySelector('script[type="application/json"]');
    if (inline) {
      try {
        const parsed = JSON.parse(inline.textContent || '{}');
        if (parsed && typeof parsed === 'object') return { ...parsed, ...cfg };
      } catch (_) {}
    }
    return cfg;
  }

  _mountFromUrl(url, attrCfg) {
    fetch(url, { credentials: 'same-origin' })
      .then((r) => { if (!r.ok) throw new Error('http ' + r.status); return r.json(); })
      .then((json) => {
        if (!this.isConnected) return;
        try {
          this._launcher = createChatLauncher({ ...(json || {}), ...attrCfg, mountEl: this });
          this.dispatchEvent(new CustomEvent('chat-launcher-ready', { detail: this._launcher }));
        } catch (e) {
          console.error('chat-launcher mount failed', e);
        }
      })
      .catch((e) => console.error('chat-launcher config fetch failed', e));
  }

  get launcher() { return this._launcher; }
}

if (typeof customElements !== 'undefined' && !customElements.get('chat-launcher')) {
  customElements.define('chat-launcher', ChatLauncherElement);
}

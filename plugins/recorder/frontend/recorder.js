'use strict';

// ── Client render recorder (browser flight recorder) ──────────────────────────
//
// Records what the BROWSER actually rendered and felt — one chat baseline,
// frame-grouped DOM differences, lag / long-task stalls, JS errors, console
// warnings and failed/slow network calls — and ships them to
// /api/v1/recordings, stamped with
// session_seq so each row joins exactly to the interaction / diagnostics row
// beside it on the server. This is the client-side blind spot the server logs
// can never see (an event arrives + logs clean, then the JS that renders it
// throws or stutters).
//
// OFF by default and strictly local to this loaded document. The admin header
// control supplies a short-lived lease bound to this document's client_id and
// recording_id. Other tabs never receive that start event or lease, so they stay
// passive (no observers, no listeners, no flush).
//
// A capture takes one HTML baseline of the chat, then records only nodes/text/
// attributes that differ. Mutations are grouped at requestAnimationFrame
// boundaries so related changes share a precise frame timestamp and operation
// order. Bounded buffers report evidence gaps; recording has no total time limit.
//
// Design rule: the recorder must never throw into the app and must never become
// the source of the lag it is measuring. Every hook is try/wrapped; the baseline
// and mutation payloads are size-capped; any truncation is explicit evidence.

import { app } from '/ui/shared/js/state.js';
import { randomUUID } from '/ui/shared/js/uuid.js';
import { authHeaders } from '/ui/shared/js/left-login.js';

import { createWitnesses } from './witnesses.js';
import { _messageCache } from '/ui/chat/js/chat-message-cache.js';

import { BATCH_BYTES, bytes, batches, textParts } from './transport.js';
import { Prebuffer, createMirror } from './ring.js';
import { createUploader } from './uploader.js';

const INGEST_URL = '/api/v1/recordings';

let _clientId = null;           // fresh document id supplied by the header
let _leaseToken = '';
let _leaseExpiresAt = 0;
let _seq = 0;
let _buffer = [];
let _bufferBytes=0;
let _lastStopResult=null;
let _uploader=null, _lastJob=null;
let _ring=null, _mirror=null, _prepared=null, _ringTimer=null;
let _pendingMirrorPatches=[];
let _bufferRoot=null;
let _active = false;            // currently capturing?
let _cfg = null;                // server-provided capture knobs
let _flushTimer = null;
let _captureId = null;
let _captureStartedAt = 0;
let _witnesses=null;
let _sampleTimer=null;

// Teardown handles for everything we install, so we can fully stop on disable.
let _mutObserver = null;
let _perfObserver = null;
let _origFetch = null;
let _origConsole = {};          // { warn, error }
const _winListeners = [];       // [ [type, fn] ]

let _pendingMutationOps = [];
let _mutationFrameHandle = null;
let _mutationFrame = 0;
let _capturedMutationBytes = 0;
let _capturedMutationOps = 0;
let _mutationOverflowed = false;
let _pendingMutationLimitReason = null;
let _nextNodeId = 1;
let _nodeIds = new WeakMap();

// ── Helpers ───────────────────────────────────────────────────────────────────

function _nowIso() { return new Date().toISOString(); }

function _ensureClientId() {
  if (!_clientId) _clientId = randomUUID();
  return _clientId;
}

// Common correlation coordinates pulled live from app state at capture time.
function _coords() {
  let sessionSeq = null;
  try {
    const sid = app.currentSessionId;
    if (sid && app.lastSessionSeq && typeof app.lastSessionSeq[sid] === 'number') {
      sessionSeq = app.lastSessionSeq[sid];
    }
  } catch (_) {}
  return {
    session_id: (app && app.currentSessionId) || null,
    session_seq: sessionSeq,
    user_id: (app && app.currentUserId) || null,
    agent_id: (app && app.currentAgentId) || null,
    client_id: _ensureClientId(),
    document_id: _ensureClientId(),
    url: location.pathname + location.search,
  };
}

// Push one record onto the outgoing buffer. Never throws.
function _record(kind, fields) {
  if (!_active) return;
  if ((_uploader?.queuedRows || 0)>=10000) { _health('outbox-backpressure-dropped-events'); return; }
  try {
    if(kind==='console' || kind==='js_error')fields=_redactDiagnostic(fields);
    if (_ring) {
      // Keep complete logical rows in memory; transport fragmentation happens
      // only after promotion, so eviction can never orphan payload parts.
      const row=JSON.parse(JSON.stringify({kind,ts:_nowIso(),elapsed_ms:recorderElapsedMs(),..._coords(),...fields}));
      const patches=kind==='mutation'?_pendingMirrorPatches.splice(0):[];
      _ring.push([row],patches,bytes(row)*2+bytes(patches)*2+128);
      return;
    }
    // Large HTML is losslessly linked to its parent event in small pieces.
    // Editable fields have already been masked before serialization.
    if(fields?.html && fields.html.length>24000) {
      const chunks=textParts(fields.html), payload_id=randomUUID(), parts=chunks.length;
      _record(kind,{...fields,html:undefined,detail:{...fields.detail,payload_id,html_parts:parts}});
      for(let part=0;part<parts;part++) _record('html.part',{...fields,
        label:'DEBUG-TAG:html-part',html:chunks[part],...(fields.elapsed_ms===undefined?{}:{elapsed_ms:fields.elapsed_ms}),ts:fields.ts || _nowIso(),
        detail:{payload_id,part,parts,original_kind:kind}});
      return;
    }
    if(kind==='checkpoint' && bytes(fields)>BATCH_BYTES-8192) {
      const {elements,...metadata}=fields.detail;
      const parts=batches(elements, BATCH_BYTES-16384),checkpoint_id=randomUUID();
      parts.forEach((elements,part)=>_record('checkpoint.part',{...fields,label:fields.label,
        detail:{checkpoint_id,part,parts:parts.length,metadata,elements}}));
      return;
    }
    const row=Object.assign({
      kind,ts:_nowIso(),seq:_seq++,recording_id:_captureId,
      elapsed_ms:_captureStartedAt?Math.max(0,Math.round(performance.now()-_captureStartedAt)):0,
    },_coords(),fields || {});
    const size=bytes(row)+1;
    if(_buffer.length && _bufferBytes+size>BATCH_BYTES-2048) _flush(false);
    _buffer.push(row);_bufferBytes+=size;
    if (_buffer.length >= 200 || _bufferBytes>=BATCH_BYTES-2048) _flush(false);
  } catch (_) {}
}

// ── Flush ───────────────────────────────────────────────────────────────────--

const _healthReasons=new Set();
function _health(reason,resolved=false,recordingId=_captureId) {
  if(reason==='upload-pending') {
    if(recordingId===_captureId) {
      if(resolved)_healthReasons.delete(reason);else _healthReasons.add(reason);
    }
    window.dispatchEvent(new CustomEvent('render-recorder-health',{detail:{reason,resolved,recording_id:recordingId}}));
    return;
  }
  if(recordingId===_captureId && _healthReasons.has(reason))return;
  if(recordingId===_captureId)_healthReasons.add(reason);
  if(_active && recordingId===_captureId && (_uploader?.queuedRows || 0)<10000)
    _record('capture.gap',{label:'capture-gap',level:'warning',detail:{reason}});
  window.dispatchEvent(new CustomEvent('render-recorder-health',{detail:{reason,recording_id:recordingId}}));
}
function _flush(useBeacon,retryNow=false) {
  if(_ring || !_uploader)return Promise.resolve(true);
  const records=_buffer.splice(0);_bufferBytes=0;
  return _uploader.flush(records,useBeacon,retryNow);
}

// ── Capture: JS errors ─────────────────────────────────────────────────────--

function _installErrorCapture() {
  const onErr = (e) => {
    try {
      const src = e.filename ? (e.filename.split('/').pop() + ':' + e.lineno + ':' + e.colno) : 'unknown';
      _record('js_error', {
        level: 'error',
        label: String(e.message || 'error').slice(0, 300),
        detail: { source: src, stack: (e.error && e.error.stack) ? String(e.error.stack).slice(0, 4000) : null },
      });
    } catch (_) {}
  };
  const onRej = (e) => {
    try {
      const reason = e.reason;
      const msg = (reason && reason.message) ? reason.message : String(reason);
      _record('js_error', {
        level: 'error',
        label: ('unhandledrejection: ' + msg).slice(0, 300),
        detail: { stack: (reason && reason.stack) ? String(reason.stack).slice(0, 4000) : null },
      });
    } catch (_) {}
  };
  window.addEventListener('error', onErr);
  window.addEventListener('unhandledrejection', onRej);
  _winListeners.push(['error', onErr], ['unhandledrejection', onRej]);
}

// ── Capture: console warnings/errors ───────────────────────────────────────--

function _installConsoleCapture() {
  ['warn', 'error'].forEach((lvl) => {
    _origConsole[lvl] = console[lvl];
    console[lvl] = function (...args) {
      try {
        const text = args.map((a) => {
          if (typeof a === 'string') return a;
          try { return JSON.stringify(a); } catch (_) { return String(a); }
        }).join(' ').slice(0, 600);
        // Skip our own error-banner echo to avoid recursion noise.
        if (!text.startsWith('[WebAgent JS error]')) {
          _record('console', { level: lvl === 'warn' ? 'warning' : 'error', label: text });
        }
      } catch (_) {}
      return _origConsole[lvl].apply(console, args);
    };
  });
}

// ── Capture: network (fetch) ───────────────────────────────────────────────--

function _installNetworkCapture() {
  _origFetch = window.fetch;
  const slowMs = (_cfg && _cfg.network_slow_ms) || 2500;
  window.fetch = function (input, init) {
    let url = '';
    try { url = (typeof input === 'string') ? input : (input && input.url) || ''; } catch (_) {}
    // Never record the recorder's own intake/config traffic (feedback loop).
    try { url = new URL(url, location.href).pathname; } catch (_) { url = '[unknown]'; }
    const ours = url.indexOf('/api/v1/recordings') !== -1;
    const t0 = (performance && performance.now) ? performance.now() : Date.now();
    const p = _origFetch.apply(this, arguments);
    if (ours) return p;
    return p.then((resp) => {
      try {
        const dt = ((performance && performance.now) ? performance.now() : Date.now()) - t0;
        const method = (init && init.method) || 'GET';
        // Successful fast requests matter for warm-cache and server-load audits.
        _record('network', {
            level: resp.ok ? (dt >= slowMs ? 'warning' : 'info') : 'error',
            label: `${method} ${resp.status} ${url}`.slice(0, 400),
            value_num: Math.round(dt),
            detail: { ok: resp.ok, status: resp.status, slow: dt >= slowMs, source:'fetch', cache_provenance:'unknown' },
        });
      } catch (_) {}
      return resp;
    }).catch((err) => {
      try {
        const dt = ((performance && performance.now) ? performance.now() : Date.now()) - t0;
        const method = (init && init.method) || 'GET';
        _record('network', {
          level: 'error',
          label: `${method} FAILED ${url}`.slice(0, 400),
          value_num: Math.round(dt),
          detail: { error: String(err && err.message || err) },
        });
      } catch (_) {}
      throw err;
    });
  };
}

// ── Capture: lag / long tasks ──────────────────────────────────────────────--

function _installLagCapture() {
  try {
    if (typeof PerformanceObserver === 'undefined') return;
    const minMs = (_cfg && _cfg.longtask_min_ms) || 80;
    _perfObserver = new PerformanceObserver((list) => {
      for (const entry of list.getEntries()) {
        try {
          if (entry.duration >= minMs) {
            _record('lag', {
              level: entry.duration >= 250 ? 'warning' : 'info',
              label: `longtask ${Math.round(entry.duration)}ms`,
              value_num: Math.round(entry.duration),
            });
          }
        } catch (_) {}
      }
    });
    _perfObserver.observe({ entryTypes: ['longtask'] });
  } catch (_) {}

  // One-shot navigation timing baseline (DOMContentLoaded / load durations).
  try {
    const nav = performance.getEntriesByType && performance.getEntriesByType('navigation')[0];
    if (nav) {
      _record('nav', {
        label: 'navigation-timing',
        value_num: Math.round(nav.duration || 0),
        detail: {
          dom_content_loaded: Math.round(nav.domContentLoadedEventEnd || 0),
          load_event: Math.round(nav.loadEventEnd || 0),
          type: nav.type,
        },
      });
    }
  } catch (_) {}
}

// ── Capture: one chat baseline + frame-coalesced DOM differences ───────────--

const DECORATIVE_BACKGROUND_SELECTOR = [
  '#stargaze-bg',
  '.stargaze-genui',
  '.stargaze-overlay',
  '#cursor-glow',
  '[data-wa-bg-surface]',
].join(',');
const RECORDER_INTERNAL_SELECTOR = '#render-recorder-control,[data-recorder-dialog]';

function _excludeDecorativeBackgrounds() {
  return !(_cfg && _cfg.capture_decorative_animations === true);
}

function _isDecorativeNode(node) {
  if (!_excludeDecorativeBackgrounds() || !node) return false;
  try {
    const el = node.nodeType === Node.ELEMENT_NODE ? node : node.parentElement;
    return !!(el && (el.matches(DECORATIVE_BACKGROUND_SELECTOR)
      || el.closest(DECORATIVE_BACKGROUND_SELECTOR)));
  } catch (_) {
    return false;
  }
}

function _isExcludedCaptureNode(node) {
  if (!node) return false;
  try {
    const el = node.nodeType === Node.ELEMENT_NODE ? node : node.parentElement;
    if (el && (el.matches(RECORDER_INTERNAL_SELECTOR) || el.closest(RECORDER_INTERNAL_SELECTOR))) {
      return true;
    }
  } catch (_) {}
  return _isDecorativeNode(node);
}

function _stripDecorativeClone(clone) {
  if (!clone) return clone;
  try {
    if (clone.matches && clone.matches(RECORDER_INTERNAL_SELECTOR)) return null;
    if (clone.querySelectorAll) {
      clone.querySelectorAll(RECORDER_INTERNAL_SELECTOR).forEach((node) => node.remove());
    }
    if (!_excludeDecorativeBackgrounds()) return clone;
    if (clone.matches && clone.matches(DECORATIVE_BACKGROUND_SELECTOR)) return null;
    if (clone.querySelectorAll) {
      clone.querySelectorAll(DECORATIVE_BACKGROUND_SELECTOR).forEach((node) => node.remove());
    }
  } catch (_) {}
  return clone;
}

function _snapshotRoot() {
  if (_cfg && _cfg.capture_whole_page) return document.body;
  return document.getElementById('chat-messages-inner')
    || document.getElementById('chat-messages');
}

function _takeSnapshot(reason) {
  if (!_active || _ring) return;
  try {
    const root = _snapshotRoot();
    if (!root) return;
    const clone = _stripDecorativeClone(root.cloneNode(true));
    if (!clone) return;
    _maskSensitiveClone(clone);
    let html = clone.outerHTML || '';
    const truncated = false; // Transport splits this baseline without dropping HTML.
    _capturedMutationBytes += html.length;
    _record('snapshot', {
      label: reason || 'snapshot',
      value_num: html.length,
      html,
      detail: {
        truncated,
        root: root.id || root.tagName,
        scroll_y: window.scrollY,
        viewport: window.innerWidth + 'x' + window.innerHeight,
        theme: document.body.classList.contains('light-mode') ? 'light' : 'dark',
      },
    });
  } catch (_) {}
}

function _clip(value, max, required=false) {
  const text = value == null ? '' : String(value);
  if (required && text.length > max) _health('payload-truncated');
  return text.length <= max ? text : text.slice(0, max) + `… [+${text.length - max} chars]`;
}

function _nodeId(node) {
  if (!node) return null;
  let id = _nodeIds.get(node);
  if (!id) {
    id = _nextNodeId++;
    _nodeIds.set(node, id);
  }
  return id;
}

// A compact, agent-readable identity. Recorder ids remain stable when a live
// node is removed and reinserted, which makes accidental moves obvious.
function _redactDiagnostic(fields) {
  const secrets=[];
  try {
    for(const el of document.querySelectorAll('input,textarea,[contenteditable="true"]')){
      const value=el.value || el.textContent;
      if(value)secrets.push(String(value));
    }
  }catch(_){}
  const redact=value=>{
    if(typeof value==='string'){for(const secret of secrets)value=value.split(secret).join('[masked]');return value;}
    if(Array.isArray(value))return value.map(redact);
    if(value && typeof value==='object')return Object.fromEntries(Object.entries(value).map(([k,v])=>[k,redact(v)]));
    return value;
  };
  return redact(fields);
}
function _sensitiveNode(node) {
  const el=node?.nodeType===1?node:node?.parentElement;
  return !!el?.closest?.('input,textarea,[contenteditable="true"]');
}
function _safeText(node) {
  if(_sensitiveNode(node))return '[masked]';
  try {
    if(!node?.querySelector?.('input,textarea,[contenteditable="true"]'))return node?.textContent || '';
    const clone=node.cloneNode(true);_maskSensitiveClone(clone);return clone.textContent || '';
  }catch(_){return '';}
}
function _nodeRef(node) {
  if (!node) return null;
  if (node.nodeType === Node.TEXT_NODE) {
    return {
      recorder_id: _nodeId(node),
      type: 'text',
      parent_recorder_id: _nodeId(node.parentNode),
      text: _clip(_safeText(node), 160),
    };
  }
  if (node.nodeType !== Node.ELEMENT_NODE) {
    return { recorder_id: _nodeId(node), type: `node-${node.nodeType}` };
  }
  return {
    recorder_id: _nodeId(node),
    tag: node.tagName.toLowerCase(),
    id: node.id || null,
    class: _clip(node.className || '', 180) || null,
    turn_id: node.dataset ? (node.dataset.turnId || null) : null,
    session_seq: node.dataset ? (node.dataset.sessionSeq || null) : null,
    component: node.dataset ? (node.dataset.component || null) : null,
    load_state: node.dataset ? (node.dataset.loadState || null) : null,
  };
}

function _maskSensitiveClone(root) {
  try {
    const nodes = [];
    if (root.matches && root.matches('input,textarea,[contenteditable="true"]')) nodes.push(root);
    if (root.querySelectorAll) nodes.push(...root.querySelectorAll('input,textarea,[contenteditable="true"]'));
    for (const el of nodes) {
      const type = String(el.getAttribute && el.getAttribute('type') || '').toLowerCase();
      const sensitive = true; // Editable values are never recorder evidence.
      if (sensitive) {
        if (el.hasAttribute && el.hasAttribute('value')) el.setAttribute('value', '[masked]');
        if (el.tagName === 'TEXTAREA' || el.getAttribute('contenteditable') === 'true') el.textContent = '[masked]';
      }
    }
  } catch (_) {}
  return root;
}

function _nodeHtml(node) {
  try {
    const max = Math.max(500, Number((_cfg && _cfg.mutation_node_max_bytes) || 12000));
    if (node.nodeType === Node.TEXT_NODE) return _safeText(node);
    if (node.nodeType !== Node.ELEMENT_NODE) return '';
    const clone = _stripDecorativeClone(node.cloneNode(true));
    if (!clone) return '';
    _maskSensitiveClone(clone);
    return clone.outerHTML || '';
  } catch (_) {
    return '';
  }
}

function _childIndex(parent, node) {
  try { return Array.prototype.indexOf.call(parent.childNodes, node); } catch (_) { return -1; }
}

function _componentLedger() {
  try {
    const root = _snapshotRoot();
    if (!root) return [];
    return Array.from(root.children)
      .filter((node) => !_isExcludedCaptureNode(node))
      .map((node, index) => ({
      index,
      ..._nodeRef(node),
      text: _clip(_safeText(node), 80),
      }));
  } catch (_) {
    return [];
  }
}

function _mutationOp(m) {
  if (_isExcludedCaptureNode(m.target) || _sensitiveNode(m.target)) return null;
  if (m.type === 'attributes') {
    return {
      op: 'attribute',
      target: _nodeRef(m.target),
      name: m.attributeName,
      old_value: _clip(m.oldValue, 500),
      value: _clip(m.target.getAttribute(m.attributeName), 500),
    };
  }
  if (m.type === 'characterData') {
    return {
      op: 'text',
      target: _nodeRef(m.target),
      old_value: _clip(m.oldValue, 1000),
      value: _clip(m.target.textContent, 1000),
    };
  }
  if (m.type === 'childList') {
    const addedNodes = Array.from(m.addedNodes).filter((node) => !_isExcludedCaptureNode(node));
    const removedNodes = Array.from(m.removedNodes).filter((node) => !_isExcludedCaptureNode(node));
    if (!addedNodes.length && !removedNodes.length) return null;
    return {
      op: 'children',
      parent: _nodeRef(m.target),
      previous: _isExcludedCaptureNode(m.previousSibling) ? null : _nodeRef(m.previousSibling),
      next: _isExcludedCaptureNode(m.nextSibling) ? null : _nodeRef(m.nextSibling),
      added: addedNodes.map((node) => ({
        index: _childIndex(m.target, node),
        node: _nodeRef(node),
        html: _nodeHtml(node),
      })),
      removed: removedNodes.map((node) => ({
        node: _nodeRef(node),
        html: _nodeHtml(node),
      })),
    };
  }
  return null;
}

function _markMutationOverflow(reason) {
  if (_mutationOverflowed) return;
  _mutationOverflowed = true;
  try { if (_mutObserver) _mutObserver.disconnect(); } catch (_) {}
  _record('meta', {
    level: 'warning',
    label: 'mutation-capture-limit',
    detail: { reason, operations: _capturedMutationOps, bytes: _capturedMutationBytes },
  });
}

function _emitMutationFrame() {
  _mutationFrameHandle = null;
  if (!_active || !_pendingMutationOps.length || _mutationOverflowed) return;
  const operations = _pendingMutationOps;
  _pendingMutationOps = [];
  _mutationFrame += 1;

  const maxBatchBytes = Math.max(4000, Number((_cfg && _cfg.mutation_batch_max_bytes) || 100000));
  const maxCaptureBytes = Infinity;
  const hasOrderChange = operations.some((op) => op.op === 'children');
  let chunk = [];
  let chunkBytes = 0;
  let chunkIndex = 0;

  const emit = () => {
    if (!chunk.length || _mutationOverflowed) return;
    const payload = JSON.stringify({
      version: 1,
      frame: _mutationFrame,
      chunk: chunkIndex++,
      at_ms: Math.max(0, Math.round(performance.now() - _captureStartedAt)),
      operations: chunk,
      component_order: hasOrderChange && chunkIndex === 1 ? _componentLedger() : undefined,
    });
    if (_capturedMutationBytes + payload.length > maxCaptureBytes) {
      _markMutationOverflow('capture_max_bytes');
      return;
    }
    _capturedMutationBytes += payload.length;
    _capturedMutationOps += chunk.length;
    _record('mutation', {
      label: 'mutation-frame',
      value_num: chunk.length,
      html: payload,
      detail: { frame: _mutationFrame, chunk: chunkIndex - 1, operations: chunk.length, bytes: payload.length },
    });
    chunk = [];
    chunkBytes = 0;
  };

  for (const op of operations) {
    const opBytes = JSON.stringify(op).length;
    if (chunk.length && chunkBytes + opBytes > maxBatchBytes) emit();
    if (_mutationOverflowed) break;
    chunk.push(op);
    chunkBytes += opBytes;
  }
  emit();
  _sampleWitnesses(false);
  _checkpoint('mutation-frame');
  if (_pendingMutationLimitReason) {
    const reason = _pendingMutationLimitReason;
    _pendingMutationLimitReason = null;
    _markMutationOverflow(reason);
  }
}

function _scheduleMutationFrame() {
  if (_mutationFrameHandle != null) return;
  if (window.requestAnimationFrame) _mutationFrameHandle = window.requestAnimationFrame(_emitMutationFrame);
  else _mutationFrameHandle = window.setTimeout(_emitMutationFrame, 16);
}

function _onMutations(mutations) {
  try {
    if(_ring && _mirror)try{_pendingMirrorPatches.push(..._mirror.patches(mutations));}
    catch(_){_mirror=null;_pendingMirrorPatches=[];_ring.limitations.add('prebuffer-baseline-memory-limit');}
    for (const mutation of mutations) {
      let node=mutation.target.nodeType===1?mutation.target:mutation.target.parentElement;
      while(node) { _fingerprints.delete(node); node=node.parentElement; }
    }
    const maxOps = Infinity;
    for (const m of mutations) {
      if (_capturedMutationOps + _pendingMutationOps.length >= maxOps) {
        _pendingMutationLimitReason = 'capture_max_operations';
        try { if (_mutObserver) _mutObserver.disconnect(); } catch (_) {}
        break;
      }
      const op = _mutationOp(m);
      if (op) _pendingMutationOps.push(op);
    }
    if (_pendingMutationOps.length) _scheduleMutationFrame();
    else if(_ring && _pendingMirrorPatches.length){const patches=_pendingMirrorPatches.splice(0);_ring.push([],patches,bytes(patches));}
  } catch (_) {}
}

function _installSnapshotCapture() {
  try {
    const root = _snapshotRoot();
    if (!root) {
      _record('meta', { level: 'warning', label: 'chat-root-not-found' });
      return;
    }
    _mutObserver = new MutationObserver(_onMutations);
    _mutObserver.observe(root, {
      childList: true,
      subtree: true,
      attributes: true,
      attributeOldValue: true,
      characterData: true,
      characterDataOldValue: true,
    });
    // The only normal full snapshot. All subsequent rows contain differences.
    _takeSnapshot('baseline');
  } catch (_) {}
}


// Semantic checkpoints are observations, not the renderer's expectations.
// Missing DOM ownership stays null. Rules must block where evidence is absent.
function _fingerprint(text) {
  let hash = 2166136261;
  for (let i=0;i<text.length;i++) hash = Math.imul(hash ^ text.charCodeAt(i),16777619);
  return (hash >>> 0).toString(16);
}
function _semanticText(el) {
  const bodies=[...el.querySelectorAll('.md-body,.bubble-body,.ca-activity-entry-body')];
  return bodies.length?bodies.map(node=>node.textContent || '').join('\n'):(el.textContent || '');
}
let _fingerprints=new WeakMap();
let _styles=new WeakMap();
let _checkpointLastAt=0;
function _geometry(el) {
  const rect = el.getBoundingClientRect();
  let visible = rect.width > 0 && rect.height > 0;
  for (let parent=el;visible && parent;parent=parent.parentElement) {
    let style=_styles.get(parent);
    if (!style) { style=getComputedStyle(parent); _styles.set(parent,style); }
    visible = !parent.hidden && style.display!=='none' && style.visibility!=='hidden' && Number(style.opacity)!==0;
  }
  const scroller=app._chatScroller;
  const clip=scroller?.contains?.(el)?scroller.getBoundingClientRect():null;
  const viewport_visible=visible && rect.bottom>Math.max(0,clip?.top||0) && rect.top<Math.min(innerHeight,clip?.bottom||innerHeight) && rect.right>0 && rect.left<innerWidth;
  return {visible,viewport_visible,in_viewport:viewport_visible,rect:{x:rect.x,y:rect.y,width:rect.width,height:rect.height}};
}
function _counts(el,kind) {
  if (kind!=='activity') return {};
  const label=el.querySelector('.bubble-tool-calls-label');
  const panel=el.querySelector('.bubble-tool-calls-panel');
  if (!label || !panel) return {};
  const text=label.textContent || '';
  const counters={};
  for (const [key,expression] of [['tools',/(\d+) tools?\b/],['messages',/(\d+) messages?\b/],['compactions',/(\d+) compactions?\b/]]) {
    const match=text.match(expression); counters[key]=match?Number(match[1]):0;
  }
  const rows={tools:0,messages:0,compactions:0};
  // Collapsed persisted groups intentionally defer their row DOM to save RAM.
  // Measure the same semantic entries used by the visible heading in that
  // state; once expanded, the direct panel rows remain the witness.
  const deferred=Array.isArray(panel.__summaryCalls) ? panel.__summaryCalls : null;
  if (deferred) {
    for (const entry of deferred) {
      if (!entry) continue;
      if (entry.notice==='compaction') rows.compactions++;
      else if (entry.kind==='progress' || entry.kind==='response') { if (entry.previewExcluded!==true) rows.messages++; }
      else if (entry.kind!=='system') rows.tools++;
    }
  } else {
    for (const row of panel.children) {
      if (row.matches('.ca-compaction-row')) rows.compactions++;
      else if (row.matches('.ca-progress-row,.ca-response-row')) { if (row.__entry?.previewExcluded!==true) rows.messages++; }
      else if (!row.matches('.ca-system-row')) rows.tools++;
    }
  }
  return {counters,rows};
}


// Independent browser witnesses: sample rendered nodes, never renderer success
// flags. Same-frame progress/content and hidden-tab intervals remain incomplete.
const _compactionWitnesses=new Map();
let _progressWitness=null;
const _CONTENT_SELECTOR='.chat-bubble.agent:not(.activity-group),.bubble-tool-calls-preview,.bubble-tool-calls-panel > .ca-tool-row';
function _contentIdentity(el) {
  return el.dataset?.msgId || el.dataset?.activityEntryId || el.dataset?.detailMsgId || el.dataset?.turnId || 'node-'+_nodeId(el);
}
function _beginProgressWitness(detail) {
  if(_progressWitness && detail.run_id && _progressWitness.run_id===detail.run_id &&
      _progressWitness.session_id===detail.session_id) return;
  const root=_snapshotRoot();
  _progressWitness={session_id:detail.session_id,run_id:detail.run_id,
    observed_from_send:!!root && document.visibilityState!=='hidden',
    progress_ms:null,first_content_ms:null,
    before:new Set(root?[...root.querySelectorAll(_CONTENT_SELECTOR)].map(_contentIdentity):[])};
  requestAnimationFrame(()=>{if (_active) {_sampleWitnesses(false);_checkpoint('send-witness');}});
}
function _sampleWitnesses(initial=false) {
  if (!_active) return;
  try {
    const root=_snapshotRoot();
    if (!root) return;
    _styles=new WeakMap();
    const at=recorderElapsedMs();
    const seen=new Set();
    for(const el of root.querySelectorAll('.system-compaction-transient')) {
      const id=el.dataset.compactionInteractionId || 'node-'+_nodeId(el);
      seen.add(id);
      let witness=_compactionWitnesses.get(id);
      if(!witness) {
        witness={started_at_ms:at,complete:!initial,session_id:app.currentSessionId};
        _compactionWitnesses.set(id,witness);
      }
      if(document.visibilityState==='hidden' || !_geometry(el).viewport_visible) witness.complete=false;
    }
    for(const [id,witness] of _compactionWitnesses) {
      if(seen.has(id)) continue;
      if(witness.session_id!==app.currentSessionId || document.visibilityState==='hidden') witness.complete=false;
      _record('compaction.notice',{label:'DEBUG-TAG:compaction.notice',detail:{
        interaction_id:id,outcome:'success',visible_duration_ms:witness.complete?at-witness.started_at_ms:null,
        observed_duration_ms:at-witness.started_at_ms,complete:witness.complete,
        session_id:witness.session_id,
      }});
      _compactionWitnesses.delete(id);
    }
    const witness=_progressWitness;
    if(!witness || witness.session_id!==app.currentSessionId || witness.first_content_ms!==null) return;
    if(document.visibilityState==='hidden') witness.observed_from_send=false;
    if(witness.progress_ms===null) {
      const notes=[...root.querySelectorAll('.live-activity-note'),...document.querySelectorAll('.chat-activity-text')];
      if(notes.some(el=>(el.textContent || '').trim() && _geometry(el).viewport_visible)) witness.progress_ms=at;
    }
    for(const el of root.querySelectorAll(_CONTENT_SELECTOR)) {
      if(witness.before.has(_contentIdentity(el)) || !_geometry(el).viewport_visible) continue;
      if(el.matches('.ca-system-row')) continue;
      const body=el.querySelector('.bubble-body,.llm-section,.md-body,.ca-activity-entry-body');
      const text=body?.textContent || (el.matches('.ca-tool-row,.bubble-tool-calls-preview')?el.textContent:
        [...el.childNodes].filter(node=>node.nodeType===3).map(node=>node.textContent).join(''));
      if(!(text || '').trim()) continue;
      witness.first_content_ms=at;
      // Co-observation cannot establish paint order; the rule must block.
      if(witness.progress_ms===at) witness.observed_from_send=false;
      break;
    }
  } catch (_) { _health('timing-witness-incomplete'); }
}
function _progressEvidence() {
  if(!_progressWitness || _progressWitness.session_id!==app.currentSessionId) return undefined;
  const {session_id,run_id,observed_from_send,progress_ms,first_content_ms}=_progressWitness;
  return {session_id,run_id,observed_from_send,progress_ms,first_content_ms};
}

function _checkpoint(reason) {
  if (!_active) return;
  try {
    const started=performance.now();
    if (reason==='mutation-frame' && started-_checkpointLastAt<100) {
      if (!_checkpointTimer) _checkpointTimer=setTimeout(()=>{_checkpointTimer=null;_checkpoint('coalesced-render');},100);
      return;
    }
    _checkpointLastAt=started;
    _sampleWitnesses(false);
    _styles=new WeakMap();
    const root=_snapshotRoot();
    const sid=app.currentSessionId;
    // Seed known identity from source records, including a transcript loaded
    // before recording began. Do not assign the current session to unknown DOM.
    for(const msg of (_messageCache.get(sid)?.messages || [])) {
      if(msg.id) _owners.set(String(msg.id),{session_id:sid});
    }
    if (!root) { _health('chat-root-not-found'); return; }
    const allNodes=[...root.querySelectorAll('.chat-bubble,.bubble-tool-calls-panel > .ca-tool-row,.bubble-tool-calls-preview')];
    const nodes=allNodes.slice(0,2000);
    if (nodes.length!==allNodes.length) _health('checkpoint-element-limit');
    const transcriptBubbles=[...root.children].filter(child=>child.matches('.chat-bubble'));
    const elements=nodes.map(el=>{
      const group=el.closest('.activity-group');
      const transcriptBubble=el.closest('.chat-bubble');
      const transcriptIndex=transcriptBubble?.parentElement===root ? transcriptBubbles.indexOf(transcriptBubble) : null;
      const ds=el.dataset;
      const parent=group?.dataset || {};
      let fingerprint=_fingerprints.get(el);
      if(!fingerprint) {
        // RECORDER-HOOK: content identity excludes mutable timestamps/actions.
        // Source DOM text is observed independently; no renderer success flag.
        const text=_semanticText(el);
        fingerprint={text_length:text.length,text_hash:_fingerprint(text)};_fingerprints.set(el,fingerprint); }
      const id=el.classList.contains('activity-group')?(ds.runKey || ds.activityTurnId):
        (ds.msgId || ds.detailMsgId || ds.activityEntryId || ds.turnId);
      const kind=el.classList.contains('user')?'user':el.classList.contains('activity-group')?'activity':
        el.classList.contains('bubble-tool-calls-preview')?'preview':el.classList.contains('terminal-closer')?'closer':el.classList.contains('error')?'failure':el.classList.contains('ca-progress-row')?'progress':
        el.classList.contains('ca-tool-row') && !el.classList.contains('ca-system-row') && !el.classList.contains('ca-response-row')?'tool':'content';
      // RECORDER-HOOK: compare the collapsed preview with its actual durable
      // detail DOM. This observes content loss without trusting a renderer flag.
      let previewEvidence={};
      if(kind==='preview' && el.__sourceRow) {
        const text=_semanticText(el.__sourceRow);
        const body=el.querySelector('.ca-activity-entry-body') || el;
        const style=getComputedStyle(body);
        previewEvidence={source_text_hash:_fingerprint(text),source_text_length:text.length,
          text_clamped:(parseInt(style.webkitLineClamp,10)>0) || style.textOverflow==='ellipsis'};
      }
      return {id:kind+':'+(id || 'node-'+_nodeId(el)),stable_id:!!id,kind,
        role:el.classList.contains('user')?'user':'assistant',
        run_id:ds.ownerRun || ds.activityTurnId || parent.activityTurnId || null,
        segment_id:ds.runKey || parent.runKey || null,
        owner_id:(ds.ownerRun || ds.activityTurnId || parent.activityTurnId)?
          'user:'+(ds.ownerRun || ds.activityTurnId || parent.activityTurnId):null,
        session_id:ds.sessionId || parent.sessionId || _owners.get(String(id))?.session_id ||
          _owners.get(String(ds.ownerRun || ds.activityTurnId || parent.activityTurnId))?.session_id || null,
        interaction_seq:ds.sessionSeq?Number(ds.sessionSeq):null,
        finalized:kind==='closer' || el.__entry?._finalized===true?true:null,...fingerprint,
        turn_anchor:kind==='user' && el.classList.contains('turn-anchor'),
        transcript_index:transcriptIndex,
        ..._counts(el,kind),...previewEvidence,
        ..._geometry(el)};
    });
    const stop=document.querySelector('#chat-stop-btn');
    const scroll=app._chatScroller || root.parentElement;
    const terminalEvidence=[];
    // Loaded finalized closers are evidence even if capture began after send.
    // New live terminal regions still wait for the completion settling boundary.
    const loadedRuns=new Set(elements.filter(el=>el.kind==='closer' && el.run_id).map(el=>el.run_id));
    if(!app.isProcessing && !app._runControlsActive) for(const run_id of loadedRuns) {
      if(!_terminalRuns.has(run_id)) terminalEvidence.push({run_id,settled:true});
    }
    if(reason==='terminal-settled') {
      for(const run of _terminalRuns.values()) {
        if(run.reported || run.session_id!==app.currentSessionId || performance.now()-run.at<1000) continue;
        const ownerPresent=elements.some(el=>el.kind==='user' && el.id==='user:'+run.run_id);
        const groupVisible=elements.some(el=>el.kind==='activity' && el.run_id===run.run_id && el.viewport_visible);
        terminalEvidence.push({run_id:run.run_id,settled:true,
          outcome_region_complete:ownerPresent && groupVisible,
          explicit_failure:elements.some(el=>el.kind==='failure' && el.run_id===run.run_id && el.visible)});

        run.reported=true;
      }
    }
    const userTurnAnchor=typeof app.getUserTurnAnchorState==='function' ? app.getUserTurnAnchorState() : null;
    const detail={
      progress_before_content:_progressEvidence(),
      reason,elements,document_id:_clientId,active_session_id:app.currentSessionId || null,
      user_turn_anchor:userTurnAnchor,
      terminal_runs:terminalEvidence,
      // RECORDER-HOOK: the UI keeps Stop active across ownership transfers
      // using _runControlsActive even while isProcessing is false.
      run_state:_terminalSession && _terminalSession===app.currentSessionId?'completed':
        ((app.isProcessing || app._runControlsActive)?'running':'idle'),stop_visible:stop?_geometry(stop).visible:false,
      complete:nodes.length===allNodes.length,viewport:{width:innerWidth,height:innerHeight,device_pixel_ratio:devicePixelRatio,
        visibility:document.visibilityState,theme:document.body.classList.contains('light-mode')?'light':'dark'},
      scroll:scroll?{top:scroll.scrollTop,height:scroll.scrollHeight,client_height:scroll.clientHeight}:null,
      transient_count:root.querySelectorAll('.system-compaction-transient,.system-compaction-progress').length,
      overhead_ms:performance.now()-started,
    };
    _witnesses?.checkpoint(detail);
    _record('checkpoint',{label:'DEBUG-TAG:render-checkpoint',detail});
  } catch (_) { _health('checkpoint-failed'); }
}
const _owners=new Map();
const _terminalRuns=new Map();
let _terminalSession=null;
function _trace(kind, detail) {
  // Callers emit IDs/lengths only, never raw WS events or request credentials.
  try {
    _witnesses?.trace(kind);
    if (detail.interaction_id && detail.session_id) _owners.set(String(detail.interaction_id),{session_id:detail.session_id});
    if (kind==='run.completed') { _terminalSession=detail.session_id;
      if (detail.run_id) _terminalRuns.set(String(detail.run_id),{run_id:String(detail.run_id),session_id:detail.session_id,at:performance.now()});
      setTimeout(()=>_checkpoint('terminal-settled'),1100);
    }
    if (kind==='run.started') { _beginProgressWitness(detail); _terminalSession=null;if(detail.run_id) _terminalRuns.delete(String(detail.run_id)); }
    _record('trace',{label:'DEBUG-TAG:'+kind,detail:{event:kind,...detail}});
  } catch (_) {}
}
export function recorderElapsedMs() {
  return _captureStartedAt ? Math.max(0,Math.round(performance.now()-_captureStartedAt)) : 0;
}

// ── Start / stop ──────────────────────────────────────────────────────────--

function _start(data) {
  if (_active || !data || !data.client_id || (!data.armed && (!data.lease_token || !data.recording_id))) return;
  _cfg = data.config || {};
  _clientId = data.client_id;
  _captureId = data.recording_id;
  _leaseToken = data.lease_token;
  _leaseExpiresAt = Date.parse(data.lease_expires_at || '') || 0;
  _captureStartedAt = performance.now();
  _pendingMutationOps = [];
  _pendingMirrorPatches=[];
  _mutationFrameHandle = null;
  _mutationFrame = 0;
  _capturedMutationBytes = 0;
  _capturedMutationOps = 0;
  _mutationOverflowed = false;
  _pendingMutationLimitReason = null;
  _nextNodeId = 1;
  _nodeIds = new WeakMap();
  _fingerprints = new WeakMap();

  _active = true;
  _lastStopResult=null;
  _seq = 0;

  _healthReasons.clear();
  _buffer=[];_bufferBytes=0;
  _uploader=data.armed?null:createUploader({recordingId:_captureId,clientId:_clientId,token:_leaseToken,health:_health});
  if(data.armed)_initializeRing();
  _owners.clear(); _terminalRuns.clear(); _terminalSession=null;
  _progressWitness=null; _compactionWitnesses.clear();
  _witnesses=createWitnesses({record:_record,geometry:_geometry,captureId:_captureId,persist:!data.armed,requestCheckpoint:()=>_checkpoint('recovery-before')});
  window.__recorderTrace = _trace;
  _sampleWitnesses(true);
  _checkpoint('capture-start');
  const configuredDuration = Number(_cfg.capture_duration_ms);
  const durationMs = Number.isFinite(configuredDuration) ? Math.max(0, configuredDuration) : 0;
  const root = _snapshotRoot();
  _record('meta', {
    label: 'capture-start',
    detail: {
      ua: navigator.userAgent,
      duration_ms: durationMs || null,
      unlimited: durationMs === 0,
      root: root && (root.id || root.tagName),
      decorative_backgrounds: !_excludeDecorativeBackgrounds(),
    },
  });

  if (_cfg.capture_js_errors !== false) _installErrorCapture();
  if (_cfg.capture_console !== false) _installConsoleCapture();
  if (_cfg.capture_network !== false) _installNetworkCapture();
  if (_cfg.capture_lag !== false) _installLagCapture();
  if (_cfg.capture_snapshots !== false) _installSnapshotCapture();

  // RECORDER-HOOK: footer controls, theme changes and quiet recovery do not
  // mutate the transcript. Sample them while enabled; no timer remains off.
  _sampleTimer=setInterval(()=>_checkpoint('periodic-observation'),500);
  const flushMs = (_cfg.flush_interval_ms) || 5000;
  if(!data.armed)_flushTimer = setInterval(() => _flush(false), flushMs);
  // Duration is owned by toggle.js so stop flushes and finalizes the report.

  // Flush hard when the tab is hidden or unloading — that's exactly when the
  // bug-before-close evidence would otherwise be lost.
  const onHide = () => { if (document.visibilityState === 'hidden') { _sampleWitnesses(false); _flush(true); } };
  const onPageHide = () => {
    _record('meta',{label:'document-pagehide',detail:{pending_rows:_buffer.length}});
    // _flush(true) also queues a bounded unload beacon before async storage.
    _flush(true);
  };
  document.addEventListener('visibilitychange', onHide);
  window.addEventListener('pagehide', onPageHide);
  _winListeners.push(['pagehide', onPageHide]);
  // (visibilitychange is on document; tracked separately for teardown)
  _recorderHideHandler = onHide;
  const onScroll = () => { if (!_checkpointTimer) _checkpointTimer=setTimeout(() => { _checkpointTimer=null; _checkpoint('viewport-change'); },150); };
  window.addEventListener('scroll',onScroll,true);
  _scrollHandler=onScroll;
  window.addEventListener('resize',onScroll);
  _winListeners.push(['resize',onScroll]);
}

let _recorderHideHandler = null;
let _checkpointTimer=null;
let _scrollHandler=null;

function _detachHooks() {
  _active = false;
  _witnesses?.stop(); _witnesses=null;
  clearInterval(_sampleTimer); _sampleTimer=null;
  delete window.__recorderTrace;
  window.removeEventListener('scroll',_scrollHandler,true);
  clearTimeout(_checkpointTimer); _checkpointTimer=null;
  try { if (_mutObserver) _mutObserver.disconnect(); } catch (_) {}
  _mutObserver = null;
  try { if (_perfObserver) _perfObserver.disconnect(); } catch (_) {}
  _perfObserver = null;
  if (_origFetch) { try { window.fetch = _origFetch; } catch (_) {} _origFetch = null; }
  ['warn', 'error'].forEach((lvl) => {
    if (_origConsole[lvl]) { try { console[lvl] = _origConsole[lvl]; } catch (_) {} }
  });
  _origConsole = {};
  for (const [type, fn] of _winListeners) {
    try { window.removeEventListener(type, fn); } catch (_) {}
  }
  _winListeners.length = 0;
  if (_recorderHideHandler) {
    try { document.removeEventListener('visibilitychange', _recorderHideHandler); } catch (_) {}
    _recorderHideHandler = null;
  }
  if (_flushTimer) { clearInterval(_flushTimer); _flushTimer = null; }
  clearInterval(_ringTimer);_ringTimer=null;
}
export function detachRecorder(reason) {
  if(!_active)return _lastJob;
  if (_mutationFrameHandle != null) {
    try { window.cancelAnimationFrame(_mutationFrameHandle); } catch (_) {}
    try { clearTimeout(_mutationFrameHandle); } catch (_) {}
    _mutationFrameHandle = null;
  }
  try { if(_mutObserver)_onMutations(_mutObserver.takeRecords());_emitMutationFrame(); _checkpoint('capture-stop'); } catch (_) {}
  _record('meta', {
    label: 'capture-stop',
    detail: {
      reason: reason || 'disabled',
      duration_ms: Math.max(0, Math.round(performance.now() - _captureStartedAt)),
      mutation_frames: _mutationFrame,
      mutation_operations: _capturedMutationOps,
      captured_bytes: _capturedMutationBytes,
      overflowed: _mutationOverflowed,
    },
  });
  _detachHooks();
  if(_ring) {
    _ring=null;_mirror=null;_prepared=null;_pendingMirrorPatches=[];
    _captureId=null;_captureStartedAt=0;_uploader=null;
    return null;
  }
  const recording_id=_captureId,last_seq=_seq-1,uploader=_uploader;
  _flush(false);
  const job={recording_id,last_seq,renew:token=>uploader.renew(token),
    async finish(){
      const acknowledged=await uploader.drain();
      const result={acknowledged:acknowledged===true,last_seq,recording_id};
      if(_lastJob===job)_lastStopResult=result;
      return result;
    }};
  _lastJob=job;_captureId=null;_captureStartedAt=0;_leaseToken='';_leaseExpiresAt=0;
  _uploader=null;_pendingMutationOps=[];
  return job;
}
export async function stopRecorder(reason) {
  const job=_active?detachRecorder(reason):_lastJob;
  return job?job.finish():_lastStopResult;
}

function _initializeRing() {
  clearInterval(_ringTimer);
  const root=_snapshotRoot();_bufferRoot=root;
  let mirrorLimited=false;
  try{_mirror=root?createMirror(root,{id:_nodeId,excluded:_isExcludedCaptureNode,sensitive:_sensitiveNode}):null;}
  catch(_){_mirror=null;mirrorLimited=true;}
  let baselineBytes=_mirror?.size() || 0;
  const tooLarge=baselineBytes>32*1024*1024;
  if(tooLarge){_mirror=null;baselineBytes=0;}
  _ring=new Prebuffer({baselineBytes,advance:patches=>{
    const size=_mirror?.advance(patches) || 0;
    if(_mirror?.invalid){_mirror=null;_ring?.limitations.add('prebuffer-mirror-invalid-patch');return 0;}
    if(size>32*1024*1024){_mirror=null;_ring?.limitations.add('prebuffer-baseline-memory-limit');return 0;}
    return size;
  }});
  if(tooLarge || mirrorLimited)_ring.limitations.add('prebuffer-baseline-memory-limit');
  if(!root)_ring.limitations.add('prebuffer-chat-root-not-found');
  _ringTimer=setInterval(()=>{
    if(!_ring)return;
    _refreshBufferRoot();
    if(!_ring)return;
    _ring.trim();
    // Bound auxiliary identity/witness maps during arbitrarily long arming.
    if(_owners.size>10000)_owners.clear();
    if(_terminalRuns.size>1000)_terminalRuns.clear();
    if(_compactionWitnesses.size>1000)_compactionWitnesses.clear();
  },500);
}
function _refreshBufferRoot() {
  if(!_ring || _prepared || _snapshotRoot()===_bufferRoot)return;
  // Partials may mount after auto-arm, or replace the chat root on navigation.
  // Restart the browser window at this real baseline boundary, reporting warmup
  // rather than claiming five seconds of DOM evidence that did not exist.
  try{_mutObserver?.disconnect();}catch(_){}
  _mutObserver=null;_pendingMirrorPatches=[];_pendingMutationOps=[];
  _initializeRing();
  if(_cfg.capture_snapshots!==false)_installSnapshotCapture();
}
// RECORDER-PREBUFFER-HOOK: header lifecycle only. Arming creates no archive,
// no outbox writes and no uploads. Uninstall/disarm tears down all observers.
export function armRecorder(data) {
  if(_active)return;
  _start({...data,armed:true,recording_id:null,lease_token:''});
}
export function disarmRecorder() {
  if(_ring)detachRecorder('disarmed');
}
export function prepareRecorderCapture() {
  if(!_ring)return null;
  _refreshBufferRoot();
  try{if(_mutObserver)_onMutations(_mutObserver.takeRecords());_emitMutationFrame();}catch(_){}
  _prepared={..._ring.freeze(),trigger_at:_nowIso()};
  return {..._prepared};
}
export function cancelPreparedCapture() {
  if(!_ring)return;
  _prepared=null;_ring.pinned=false;delete _ring.trigger;_ring.trim();
}
function _promote(data) {
  if(!_prepared)prepareRecorderCapture();
  // RECORDER-PROMOTION-BOUNDARY: drain handshake mutations into the frozen
  // window before switching sinks. Their mirror patches must never reach a
  // subsequent collector whose node IDs start from one again.
  try{if(_mutObserver)_onMutations(_mutObserver.takeRecords());_emitMutationFrame();}catch(_){}
  if(_pendingMirrorPatches.length){
    const patches=_pendingMirrorPatches.splice(0);_ring.push([],patches,bytes(patches)*2+128);
  }
  const ring=_ring,prepared={..._prepared,...ring.coverage()};
  const armedOrigin=_captureStartedAt;
  const origin=ring.boundary,html=_mirror?.html() || '';
  const groups=ring.groups;
  _ring=null;_mirror=null;_prepared=null;_pendingMirrorPatches=[];clearInterval(_ringTimer);_ringTimer=null;
  _captureId=data.recording_id;_leaseToken=data.lease_token;
  _captureStartedAt=origin;_seq=0;_buffer=[];_bufferBytes=0;
  _uploader=createUploader({recordingId:_captureId,clientId:_clientId,token:_leaseToken,health:_health});
  if(html)_record('snapshot',{label:'prebuffer-baseline',html,elapsed_ms:0,ts:new Date(Date.parse(prepared.trigger_at)-prepared.available_ms).toISOString(),
    detail:{truncated:false,prebuffer:true,root:_snapshotRoot()?.id}});
  for(const group of groups)for(const row of group.rows) {
    const {kind,...fields}=row;
    const offset=origin-armedOrigin;
    if(fields.detail?.progress_before_content) {
      const evidence=fields.detail.progress_before_content;
      for(const key of ['progress_ms','first_content_ms'])if(evidence[key]!=null){
        if(evidence[key]<offset)evidence.observed_from_send=false;
        evidence[key]=Math.max(0,evidence[key]-offset);
      }
    }
    if(kind==='mutation' && fields.html)try {
      const payload=JSON.parse(fields.html);payload.at_ms=Math.max(0,payload.at_ms-offset);fields.html=JSON.stringify(payload);
    }catch(_){}
    _record(kind,{...fields,elapsed_ms:Math.max(0,Math.round(group.at-origin))});
  }
  const offset=origin-armedOrigin;
  if(_progressWitness)for(const key of ['progress_ms','first_content_ms'])if(_progressWitness[key]!=null){
    if(_progressWitness[key]<offset)_progressWitness.observed_from_send=false;
    _progressWitness[key]=Math.max(0,_progressWitness[key]-offset);
  }
  for(const witness of _compactionWitnesses.values()){
    if(witness.started_at_ms<offset)witness.complete=false;
    witness.started_at_ms=Math.max(0,witness.started_at_ms-offset);
  }
  _record('meta',{label:'prebuffer-trigger',detail:{prebuffer:prepared},elapsed_ms:prepared.trigger_elapsed_ms});
  // Coverage limits describe optional history; warm-up is not lost live evidence.
  // GenUI uses prebuffer metadata without downgrading the whole capture.
  _witnesses?.stop();
  _witnesses=createWitnesses({record:_record,geometry:_geometry,captureId:_captureId,requestCheckpoint:()=>_checkpoint('recovery-before')});
  _flushTimer=setInterval(()=>_flush(false),_cfg.flush_interval_ms || 5000);
  _checkpoint('recording-trigger');
}

// ── Document-local control lifecycle ───────────────────────────────────────--

function _applyLocalState(data) {
  const enabled = !!(data && data.enabled);
  if (!enabled) {
    if (_active && !_ring) stopRecorder('disabled');
    return;
  }
  if(_active && _ring){
    if(data.client_id===_clientId && data.recording_id && data.lease_token)_promote(data);
    return;
  }
  if(!_active && _lastJob?.recording_id===data.recording_id) {
    // A stopped capture awaiting upload/finalization must not restart observers.
    if(data.lease_token) _lastJob.renew(data.lease_token);
    return;
  }
  if (_active) {
    // Lease renewal must not create a second baseline or reinstall hooks.
    if (data.client_id !== _clientId || data.recording_id !== _captureId) {
      stopRecorder('lease-binding-changed');
      return;
    }
    if (data.lease_token) {_leaseToken = data.lease_token;_uploader?.renew(data.lease_token);}
    _leaseExpiresAt = Date.parse(data.lease_expires_at || '') || _leaseExpiresAt;
    return;
  }
  _start(data);
}

let _initialized = false;

export function initRecorder() {
  if (_initialized) return;
  _initialized = true;
  // Session switching publishes phase timings after the new projection commits.
  // Persist them in the active render recording so performance regressions can
  // be compared with the mutation frames from the same journey.
  window.addEventListener('session-switch-timing', (event) => {
    if (!_active) return;
    _record('meta', {
      label: 'session-switch-timing',
      detail: (event && event.detail) || {},
    });
  });
  window.addEventListener('render-recorder-changed', (event) => {
    _applyLocalState(event.detail || {});
  });
  // Covers a very fast click/reload restoration that completed before deferred
  // recorder initialization attached the event listener.
  if (window.__renderRecorderState) _applyLocalState(window.__renderRecorderState);
}

// Screenshot markers bind to the last browser evidence sequence at capture time.
export function recorderEventSeq() { return Math.max(-1,_seq-1); }

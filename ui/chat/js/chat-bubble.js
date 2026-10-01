'use strict';

// Chat bubble DOM — bubble creation, Markdown rendering (marked + DOMPurify),
// linkification, relative timestamps. Sets app.addChatBubble / app._renderMarkdownBody.
// Module map for this folder: ui/chat/js/README.md.

import { _refreshLucideIcons, _esc } from '../../shared/js/dom-utils.js';
import { copyText } from '../../shared/js/clipboard.js';
import { app } from '../../shared/js/state.js';
import { getAgentChatUi } from '../../shared/js/app-prompts.js';
import { renderAttachmentElement } from '../../shared/js/attachments.js';
import * as mediaCache from '../../shared/js/media-cache.js';
import { sortTranscriptCanonical } from './transcript-order.js';
import { parseClosingEnvelope } from './closing-message.js';

// ── Mid-run OPENINGS are transcript boundaries ──────────────────────────────
// A mid-run OPENING is the pre-tool progress message (the `pre_tool_progress_message` gate) or
// the assertions contract's introduction: a genuine `main` reply the run posts
// BEFORE it finishes, carrying `pre_tool_progress_message_sent` / `assertions_announced` in its
// durable metadata. It is not the run's answer, and the work that follows it is
// not a child of the work before it — the durable projection splits its activity
// counters at every such row (db_viewer.py `_compact_activity_rows` flushes at
// any non-activity row). The live transcript must split there too, or the whole
// run collapses into ONE disclosure, later entries land ABOVE the opening, and
// the opening itself reads as the run's closer.
//
// The mark is a data attribute, written by chat-stream's
// `_publishMidRunAnnouncement` (live + reconcile) and by session-load's phased
// builder (reload). One vocabulary, three readers: the ordering authority below,
// chat-stream's live segment boundary, and session-load's durable renderer.
export function isMidRunOpeningEl(el) {
  return !!(el && el.dataset && el.dataset.midRunOpening);
}

/** The user-turn id a mid-run opening belongs to ('' when it carries none). */
export function openingOwnerOf(el) {
  return (el && el.dataset && el.dataset.midRunOpeningOwner) || '';
}

/** Stamp a bubble as a mid-run opening — see isMidRunOpeningEl. */
export function markMidRunOpening(el, ownerTurnId) {
  if (!el || !el.dataset) return el;
  el.dataset.midRunOpening = '1';
  if (ownerTurnId) el.dataset.midRunOpeningOwner = String(ownerTurnId);
  return el;
}

/**
 * True when an activity group belongs to a run phase that an opening has already
 * CLOSED: an opening for the same owner (or an unstamped one) sits between the
 * group and the next user row. Such a group is finished — it must never adopt
 * later entries, be re-adopted as the live group, or be coalesced with the phase
 * below the opening. Without that rule the pre-opening group keeps absorbing the
 * run's work, so the whole run reads as ONE disclosure with its opening stranded
 * at the tail — and the phase that followed the opening is dragged back ABOVE it.
 */
export function activityGroupPrecedesOpening(group, owner) {
  if (!group || !group.isConnected) return false;
  let node = group.nextElementSibling;
  while (node) {
    if (node.classList && node.classList.contains('chat-bubble')
        && node.classList.contains('user')) return false;
    if (isMidRunOpeningEl(node)) {
      const stamped = openingOwnerOf(node);
      if (!owner || !stamped || String(stamped) === String(owner)) return true;
    }
    node = node.nextElementSibling;
  }
  return false;
}

function _markdownReady() {
  return !!(window.marked && typeof window.marked.parse === 'function'
         && window.DOMPurify && typeof window.DOMPurify.sanitize === 'function');
}

function _highlightCodeBlocks(root) {
  try {
    if (window.Prism && typeof window.Prism.highlightAllUnder === 'function') {
      window.Prism.highlightAllUnder(root);
    }
  } catch (_) { /* syntax highlighting is best-effort */ }
}

function _addCodeBlockCopyButtons(root) {
  root.querySelectorAll('pre').forEach((pre) => {
    const wrapper = document.createElement('div');
    wrapper.className = 'md-code-block';
    pre.replaceWith(wrapper);
    wrapper.appendChild(pre);

    const button = document.createElement('button');
    button.type = 'button';
    button.className = 'md-code-copy';
    button.title = 'Copy code';
    button.setAttribute('aria-label', 'Copy code');
    button.innerHTML = '<i data-lucide="copy"></i>';
    button.addEventListener('click', async () => {
      try {
        await copyText(pre.textContent || '');
        button.classList.remove('is-failed');
        button.classList.add('is-copied');
        button.title = 'Copied';
        button.setAttribute('aria-label', 'Code copied');
        button.innerHTML = '<i data-lucide="check"></i>';
      } catch (_) {
        button.classList.remove('is-copied');
        button.classList.add('is-failed');
        button.title = 'Copy failed';
        button.setAttribute('aria-label', 'Copy code failed');
        button.innerHTML = '<i data-lucide="x"></i>';
      }
      _refreshLucideIcons(button);
      window.setTimeout(() => {
        button.classList.remove('is-copied', 'is-failed');
        button.title = 'Copy code';
        button.setAttribute('aria-label', 'Copy code');
        button.innerHTML = '<i data-lucide="copy"></i>';
        _refreshLucideIcons(button);
      }, 1200);
    });
    wrapper.appendChild(button);
  });
  _refreshLucideIcons(root);
}

function _isProtectedContentUrl(value) {
  try { return new URL(value, window.location.origin).pathname.startsWith('/api/v1/content/'); }
  catch (_) { return false; }
}

function _hydrateProtectedContent(root) {
  root.querySelectorAll('img[src],audio[src],video[src],source[src]').forEach((node) => {
    const src = node.getAttribute('src') || '';
    if (!_isProtectedContentUrl(src)) return;
    node.removeAttribute('src');
    mediaCache.fetchUrl('markdown:' + src, src).then((url) => {
      if (url) { node.src = url; node.parentElement?.load?.(); }
    });
  });
  root.querySelectorAll('a[href]').forEach((anchor) => {
    const href = anchor.getAttribute('href') || '';
    if (!_isProtectedContentUrl(href)) return;
    anchor.removeAttribute('target');
    anchor.addEventListener('click', async (event) => {
      event.preventDefault();
      const response = await fetch(href);
      if (!response.ok) return;
      const objectUrl = URL.createObjectURL(await response.blob());
      const download = document.createElement('a');
      download.href = objectUrl; download.download = anchor.textContent?.trim() || 'download';
      download.click(); setTimeout(() => URL.revokeObjectURL(objectUrl), 1000);
    });
  });
}

function _renderMarkdownBody(text, highlight) {
  if (!text || !text.trim() || !_markdownReady()) return null;
  let html;
  try {
    html = window.marked.parse(text, { gfm: true, breaks: true });
  } catch (_) { return null; }
  const body = document.createElement('div');
  body.className = 'md-body';
  body.innerHTML = window.DOMPurify.sanitize(html, { FORBID_ATTR: ['style'] });
  body.querySelectorAll('a[href]').forEach((a) => {
    a.target = '_blank';
    a.rel = 'noopener noreferrer';
  });
  _hydrateProtectedContent(body);
  if (highlight !== false) _highlightCodeBlocks(body);
  _addCodeBlockCopyButtons(body);
  return body;
}

// Fill a container (bubble or turn-section) with rendered Markdown or linkified text.
function _fillAgentBubble(container, text, highlight) {
  const body = _renderMarkdownBody(text, highlight);
  if (body) {
    container.appendChild(body);
    container.classList.add('md');
    container.__mdSource = text;
    return true;
  }
  container.appendChild(linkifyText(text || ''));
  container.classList.remove('md');
  container.__mdSource = null;
  return false;
}

const URL_RE = /https?:\/\/[^\s<>"]+/g;

function linkifyText(text) {
  const frag = document.createDocumentFragment();
  let last = 0;
  let match;
  URL_RE.lastIndex = 0;
  while ((match = URL_RE.exec(text)) !== null) {
    if (match.index > last) frag.appendChild(document.createTextNode(text.slice(last, match.index)));
    const a = document.createElement('a');
    a.href = match[0];
    a.textContent = match[0];
    a.target = '_blank';
    a.rel = 'noopener noreferrer';
    frag.appendChild(a);
    last = match.index + match[0].length;
  }
  if (last < text.length) frag.appendChild(document.createTextNode(text.slice(last)));
  return frag;
}

/** Apply chat_ui.json user-message truncation after a user bubble enters the DOM. */
export function applyUserMessageTruncation(bubble) {
  requestAnimationFrame(() => {
    if (!bubble?.isConnected || bubble.querySelector(':scope > .user-message-expand')) return;
    const cfg = getAgentChatUi()?.chat_common?.user_message_truncation || {};
    if (cfg.enabled === false) return;
    const requestedLines = Number.parseInt(cfg.max_lines, 10);
    const maxLines = Number.isFinite(requestedLines) && requestedLines > 0
      ? Math.min(requestedLines, 100)
      : 3;
    const expandLabel = typeof cfg.expand_label === 'string' && cfg.expand_label.trim()
      ? cfg.expand_label.trim()
      : 'Click to see more';
    const collapseLabel = typeof cfg.collapse_label === 'string' && cfg.collapse_label.trim()
      ? cfg.collapse_label.trim()
      : 'Show less';
    // User turn actions can wrap raw content in .turn-section before this frame.
    const body = bubble.querySelector(':scope > .bubble-body, :scope > .turn-section > .bubble-body');
    if (!body) return;
    const lineHeight = parseFloat(getComputedStyle(body).lineHeight) || 20;
    if (body.scrollHeight <= lineHeight * maxLines + 1) return;
    bubble.style.setProperty('--user-message-max-lines', String(maxLines));
    bubble.classList.add('user-message-clamped');
    const toggle = document.createElement('button');
    toggle.type = 'button';
    toggle.className = 'user-message-expand';
    toggle.textContent = expandLabel;
    toggle.setAttribute('aria-expanded', 'false');
    toggle.addEventListener('click', () => {
      const expanded = bubble.classList.toggle('user-message-expanded');
      toggle.textContent = expanded ? collapseLabel : expandLabel;
      toggle.setAttribute('aria-expanded', String(expanded));
    });
    const contentHost = body.closest(':scope > .turn-section') || body;
    contentHost.after(toggle);
  });
}

// Compact renderer for typed subscription-limit rows; raw provider payloads never reach the DOM.
function renderUsageLimitNotice(bubble, metadata) {
  let meta = metadata || {}; if (typeof meta === 'string') { try { meta = JSON.parse(meta) || {}; } catch (_) { meta = {}; } }
  const body = bubble?.querySelector(':scope > .bubble-body'); if (!body) return;
  body.replaceChildren();
  const title = document.createElement('div'); title.className = 'usage-limit-title'; title.textContent = 'Usage limit reached';
  const detail = document.createElement('div'); detail.className = 'usage-limit-detail'; const reset = Number(meta.resets_at);
  detail.textContent = Number.isFinite(reset) && reset > 0 ? 'Your work is preserved. Automatic retries are paused until ' + new Date(reset * 1000).toLocaleString() + '.' : 'Your work is preserved. Automatic retries are paused; try again later.';
  body.append(title, detail);
}

/** Format a UTC timestamp string (e.g. "2024-01-15 10:30:00") to an epoch ms. */
function _parseCreatedAt(str) {
  if (!str) return NaN;
  if (typeof str === 'number') return Number.isFinite(str) ? str : NaN;
  const raw = String(str).trim();
  if (!raw) return NaN;
  // SQLite stores UTC as `YYYY-MM-DD HH:MM:SS[.fff]` without a zone.  Remote
  // stores may return a normal ISO value that already has Z or an offset; do not
  // append a second zone suffix to those values.
  const iso = raw.replace(' ', 'T');
  const zoned = /(?:Z|[+-]\d{2}:?\d{2})$/i.test(iso) ? iso : iso + 'Z';
  const parsed = Date.parse(zoned);
  return Number.isFinite(parsed) ? parsed : NaN;
}

/** Apply an authoritative interactions.created_at value to a rendered bubble. */
function _setBubbleCreatedAt(bubble, createdAt) {
  if (!bubble) return;
  const createdAtMs = _parseCreatedAt(createdAt);
  if (Number.isFinite(createdAtMs)) {
    bubble.setAttribute('data-created-at', createdAtMs);
    bubble.removeAttribute('data-time-error');
  } else {
    bubble.removeAttribute('data-created-at');
    bubble.setAttribute(
      'data-time-error',
      createdAt ? `Invalid database created_at: ${createdAt}` : 'Database created_at is missing',
    );
  }
  // Update time elements in both legacy footers and new gutters
  const times = bubble.querySelectorAll(':scope .bubble-time');
  times.forEach(time => {
    if (Number.isFinite(createdAtMs)) {
      time.setAttribute('data-created-at', createdAtMs);
      time.removeAttribute('data-time-error');
      time.removeAttribute('title');
      time.textContent = _formatRelativeTime(createdAtMs);
    } else if (app.isDebug) {
      const error = bubble.getAttribute('data-time-error');
      time.removeAttribute('data-created-at');
      time.setAttribute('data-time-error', error);
      time.title = error;
      time.textContent = `time error: ${error}`;
    }
  });
  // Live nodes can exist before their durable session_seq is known. Re-run the
  // fallback placement as soon as a useful timestamp arrives; the sequence
  // path below will replace this provisional placement once DB-tail catches up.
  if (bubble.isConnected) _reorderTranscriptCanonical();
}

let _nextRenderOrder = 1;

function _renderOrder(el) {
  if (!el) return Number.MAX_SAFE_INTEGER;
  const existing = Number(el.dataset && el.dataset.renderOrder);
  if (Number.isFinite(existing)) return existing;
  const order = _nextRenderOrder++;
  if (el.dataset) el.dataset.renderOrder = String(order);
  return order;
}

/**
 * Rebuild the live transcript order without letting clock skew override durable
 * interaction order. Sequenced nodes always stay ordered by session_seq. Nodes
 * that have not reached persistence yet are placed into the sequenced timeline
 * by created_at, then move to their authoritative slot when their seq arrives.
 */
function _reorderTranscriptCanonical() {
  if (!app.chatMessages || app.chatMessages.dataset.publicConversation === 'true') return;
  const nodes = Array.from(app.chatMessages.children).filter(el =>
    el.classList && (
      el.classList.contains('chat-bubble')
      || el.classList.contains('chat-bubble-placeholder')
    ),
  );
  if (nodes.length < 2) return;

  const key = el => {
    const seqRaw = el.getAttribute('data-session-seq');
    const createdAtRaw = el.getAttribute('data-created-at');
    const seq = seqRaw == null ? NaN : Number(seqRaw);
    const createdAt = createdAtRaw == null ? NaN : Number(createdAtRaw);
    return {
      el,
      seq: Number.isFinite(seq) ? seq : null,
      createdAt: Number.isFinite(createdAt) ? createdAt : null,
      order: _renderOrder(el),
    };
  };
  const keyed = nodes.map(key);
  // The cache and DOM must make the same recovery decision. Before recovery,
  // this remains sequence-first with timestamp placement for provisional rows;
  // after a flagged drift, it becomes timestamp-first with stable tie-breakers.
  const ordered = sortTranscriptCanonical(keyed.map(item => ({
    id: item.el.dataset.msgId || ('render-' + item.order),
    session_seq: item.seq,
    created_at: item.createdAt,
    _item: item,
  })), {
    timestampOrderRecovery: app.timestampOrderRecovery === true,
  }).map(item => item._item);

  // A live Scout preview belongs immediately after its USER prompt. It has no
  // durable interaction sequence because it is intentionally ephemeral, so its
  // owner—not its arrival time—determines its transcript position.
  for (const item of [...ordered]) {
    const ownerId = item.el.dataset && item.el.dataset.scoutOwner;
    if (!ownerId || !item.el.classList.contains('scout-preview')) continue;
    const current = ordered.indexOf(item);
    const owner = ordered.findIndex(candidate => candidate.el.classList.contains('user')
      && (candidate.el.dataset.msgId === ownerId || candidate.el.dataset.turnId === ownerId));
    if (current === -1) continue;
    if (owner === -1) {
      // A pre-send mode/model notice group can still carry the PROMPT's
      // provisional id while the run's entries already use the durable one, so no
      // user row matches the stamp. Never fall through to the createdAt/render
      // -order sort here — it would hoist the notice ABOVE the message it belongs
      // to. Pin the group to the prompt it currently follows instead.
      const prev = item.el.previousElementSibling;
      if (prev && prev.classList && prev.classList.contains('chat-bubble')
          && prev.classList.contains('user')
          && !item.el.querySelector('.ca-tool-row:not(.ca-system-row)')) {
        ordered.splice(current, 1);
        const promptIdx = ordered.findIndex(candidate => candidate.el === prev);
        if (promptIdx !== -1) ordered.splice(promptIdx + 1, 0, item);
      }
      continue;
    }
    if (current === owner + 1) continue;
    ordered.splice(current, 1);
    const relocatedOwner = ordered.findIndex(candidate => candidate.el.classList.contains('user')
      && (candidate.el.dataset.msgId === ownerId || candidate.el.dataset.turnId === ownerId));
    ordered.splice(relocatedOwner + 1, 0, item);
  }

  // Provisional LIVE activity groups carry data-activity-turn-id = the owning
  // USER interaction id. Keep those after that prompt's Scout preview, when one
  // exists, so the transcript reads user → Scout → tools. Persisted segments
  // must remain in durable sequence order: a closer/summary can legitimately
  // divide multiple segments with one owner.
  //
  // A mid-run OPENING (pre-tool progress message / introduction — isMidRunOpeningEl)
  // divides the run exactly like a durable summary row does: the group carrying
  // the work published AFTER the opening is pinned directly below it, never
  // dragged back up to the prompt. Without this the opening is hoisted above its
  // own run's work and reads as the run's closer.
  const baseIndexFor = ownerId => {
    const scout = ordered.findIndex(candidate => candidate.el.classList.contains('scout-preview')
      && candidate.el.dataset.scoutOwner === ownerId);
    return scout !== -1 ? scout : ordered.findIndex(candidate => candidate.el.classList.contains('user')
      && (candidate.el.dataset.msgId === ownerId || candidate.el.dataset.turnId === ownerId));
  };
  const anchorIndexFor = (ownerId, from) => {
    let anchor = from;
    for (let i = from + 1; i < ordered.length; i += 1) {
      const el = ordered[i].el;
      if (el.classList.contains('user')) break;
      if (!isMidRunOpeningEl(el)) continue;
      const stamped = openingOwnerOf(el);
      if (stamped && String(stamped) !== String(ownerId)) continue;
      anchor = i;
    }
    return anchor;
  };
  for (const item of [...ordered]) {
    const ownerId = item.el.dataset && item.el.dataset.activityTurnId;
    if (!ownerId || !item.el.classList.contains('activity-group')) continue;
    if (item.seq !== null || item.el.dataset.activitySegmentId) continue;
    // A phase an opening has already closed keeps its chronological slot ABOVE
    // that opening; only the run's current phase is pinned directly below it.
    // Without this both phases of the run would be dragged under the opening in
    // arrival order, putting the pre-opening work below the post-opening work.
    if (activityGroupPrecedesOpening(item.el, ownerId)) continue;
    const current = ordered.indexOf(item);
    const base = baseIndexFor(ownerId);
    if (current === -1 || base === -1) continue;
    if (current === anchorIndexFor(ownerId, base) + 1) continue;
    ordered.splice(current, 1);
    // Re-resolve both anchors in the CURRENT array before re-inserting.
    const relocated = baseIndexFor(ownerId);
    if (relocated === -1) { ordered.splice(0, 0, item); continue; }
    ordered.splice(anchorIndexFor(ownerId, relocated) + 1, 0, item);
  }

  // Each run's terminal closer must sit IMMEDIATELY after its activity group,
  // anchored by the shared owner run/interaction sequence (data-activity-turn-id
  // / data-owner-run). session_seq ordering alone can detach a closer (or a
  // non-folded reload answer) above/away from its group, so enforce adjacency
  // here — the single ordering authority for the transcript. A group with no
  // closer (still running / no final answer) is left as-is.
  _groupClosersByOwner(ordered);

  if (ordered.every((item, index) => nodes[index] === item.el)) return;
  for (const item of ordered) app.chatMessages.appendChild(item.el);
  _regroupBubbles();
}

/**
 * Enforce "activity group IMMEDIATELY BEFORE its one closer" across the ordered
 * transcript. Each activity-group is keyed by its owner run (data-activity-turn-id),
 * its closer is keyed by the SAME owner run. For every group that has a closer in
 * the list, move that closer to be the group's immediate next sibling (removing
 * any stray/duplicate closer for the same run). Groups with no closer are left
 * untouched, preserving distinct progress step rows and their counters.
 */
function _groupClosersByOwner(ordered) {
  if (!ordered || ordered.length < 2) return;
  const keyOf = el => el && el.dataset
    && (el.dataset.runKey || el.dataset.activityTurnId || el.dataset.ownerRun || '');
  const isCloser = el => el && el.classList && el.classList.contains('terminal-closer');
  const isGroup = el => el && el.classList && el.classList.contains('activity-group');

  // EXACT per-run key → { groupEl, closerEls: [] }. A closer is only attached to a
  // group when it shares the SAME exact run key (data-run-key / data-activity-turn-id).
  // We NEVER match by nearest/current group across a user boundary, and we never
  // promote/merge when the key is missing or differs — an orphan final with no
  // matching group is left as ordinary prose (its own run keeps its group+closer).
  const runs = new Map();
  ordered.forEach(item => {
    const el = item.el;
    if (!isGroup(el) && !isCloser(el)) return;
    const key = keyOf(el);
    if (!key) return;
    if (!runs.has(key)) runs.set(key, { groupEl: null, closerEls: [] });
    const run = runs.get(key);
    // A run can legitimately own MORE THAN ONE group — a mid-run opening (plan
    // announcement / introduction) divides it into "before" and "after" phases
    // — and the closer belongs after the LAST one, which is where the run's
    // terminal answer actually lands. Preferring the first group would yank the
    // closer up above the opening.
    if (isGroup(el)) run.groupEl = el;
    else if (isCloser(el)) run.closerEls.push(el);
  });

  const fixes = [];
  for (const [, run] of runs) {
    if (!run.groupEl || !run.closerEls.length) continue;
    const keep = run.closerEls[run.closerEls.length - 1]; // newest text wins
    const stray = run.closerEls.filter(c => c !== keep);
    fixes.push({ groupEl: run.groupEl, keep, stray });
  }
  if (!fixes.length) return;

  // Higher groups first so an earlier splice never shifts a group we already
  // fixed. Recompute the group's live index each pass.
  fixes.sort((a, b) => {
    const ia = ordered.findIndex(x => x.el === a.groupEl);
    const ib = ordered.findIndex(x => x.el === b.groupEl);
    return ib - ia;
  });
  for (const {groupEl, keep, stray} of fixes) {
    // Remove stray/duplicate closers (higher indices first) so they never
    // re-emerge between this group and its canonical closer.
    const strayIdx = stray
      .map(el => ordered.findIndex(x => x.el === el)).filter(i => i !== -1)
      .sort((a, b) => b - a);
    for (const idx of strayIdx) ordered.splice(idx, 1);
    // Locate group and kept closer in the CURRENT array.
    let gIdx = ordered.findIndex(x => x.el === groupEl);
    let kIdx = ordered.findIndex(x => x.el === keep);
    if (gIdx === -1 || kIdx === -1 || kIdx === gIdx + 1) continue;
    // Move keep to immediately after the group.
    const [node] = ordered.splice(kIdx, 1);
    gIdx = ordered.findIndex(x => x.el === groupEl);
    ordered.splice(gIdx + 1, 0, node);
  }
}

/** Stamp a durable interaction sequence and keep transcript nodes ordered. */
function _setBubbleSessionSeq(bubble, sessionSeq) {
  if (!bubble || !app.chatMessages || !Number.isFinite(sessionSeq)) return;
  bubble.setAttribute('data-session-seq', String(sessionSeq));
  _renderOrder(bubble);
  _reorderTranscriptCanonical();
}

/** Format an elapsed duration since a given epoch ms into a relative-time label. */
function _formatRelativeTime(createdAtMs, now = Date.now()) {
  if (!Number.isFinite(createdAtMs)) return 'time unavailable';
  const elapsed = Math.max(0, now - createdAtMs);
  const totalSec = Math.floor(elapsed / 1000);
  if (totalSec < 60) return totalSec + 's';
  const totalMin = Math.floor(totalSec / 60);
  if (totalMin < 60) return totalMin + 'm';
  const hours = Math.floor(totalMin / 60);
  const mins = totalMin % 60;
  return hours + 'h ' + mins + 'm';
}

function _isBubbleTimeVisible(el) {
  if (!el || !el.isConnected || !el.getClientRects().length) return false;
  const rect = el.getBoundingClientRect();
  return rect.bottom > 0 && rect.right > 0
    && rect.top < window.innerHeight && rect.left < window.innerWidth;
}

function _nextRelativeTimeBoundary(createdAtMs, now) {
  const elapsed = Math.max(0, now - createdAtMs);
  const unitMs = elapsed < 60 * 1000 ? 1000 : 60 * 1000;
  return Math.max(1, unitMs - (elapsed % unitMs));
}

/**
 * Refresh rendered, visible bubble times and return the next label-change delay.
 * A single caller schedules that wake-up, so off-screen and minute-aged rows do
 * not cause needless per-second DOM writes.
 */
function _refreshAllBubbleTimes() {
  const now = Date.now();
  let nextDelay = Infinity;
  const scope = app.chatMessages || document;
  scope.querySelectorAll('.bubble-time').forEach(el => {
    if (!_isBubbleTimeVisible(el)) return;
    const ts = el.getAttribute('data-created-at');
    if (ts) {
      const createdAtMs = Number(ts);
      if (!Number.isFinite(createdAtMs)) return;
      const text = _formatRelativeTime(createdAtMs, now);
      if (el.textContent !== text) el.textContent = text;
      nextDelay = Math.min(nextDelay, _nextRelativeTimeBoundary(createdAtMs, now));
    } else if (app.isDebug && el.hasAttribute('data-time-error')) {
      const errorText = `time error: ${el.getAttribute('data-time-error')}`;
      if (el.textContent !== errorText) el.textContent = errorText;
    }
  });
  return Number.isFinite(nextDelay) ? nextDelay : null;
}

// ── Consecutive-bubble grouping ──────────────────────────────────────────────
// Back-to-back agent text bubbles "join up" into one visual stack (tight gap +
// squared touching corners) so a single multi-part reply reads as one block.
// They stay separate when a long time has passed between them (the agent picking
// the thread back up later), or when a non-text bubble (tool-only, error) breaks
// the run. Tuning lives here; the look is driven by the .grouped-* classes in
// app1.css. Applied incrementally as each bubble is appended (bubbles arrive in
// order on both live stream and session load).
const GROUP_GAP_MS = 3 * 60 * 1000; // join only if <3 min apart, else separate

function _bubbleGroupable(bubble) {
  if (!bubble || !bubble.classList || !bubble.classList.contains('agent')) return false;
  // Only plain agent prose joins — tool pills, errors, deleted, and the
  // closer 'Closer' lane stand alone (the close-out must read as its own
  // separate bubble, not a continuation of the agent's reply).
  return !bubble.classList.contains('tool-only')
      && !bubble.classList.contains('error')
      && !bubble.classList.contains('interrupted')
      && !bubble.classList.contains('deleted')
      && !bubble.classList.contains('summary-bubble');
}

function _bubblesJoin(a, b) {
  if (!_bubbleGroupable(a) || !_bubbleGroupable(b)) return false;
  const ta = Number(a.getAttribute('data-created-at')) || 0;
  const tb = Number(b.getAttribute('data-created-at')) || 0;
  if (!ta || !tb) return false;
  return Math.abs(tb - ta) <= GROUP_GAP_MS;
}

// Re-evaluate a bubble's join state against the one directly above it.
function _applyGrouping(bubble) {
  if (!bubble) return;
  const prev = bubble.previousElementSibling;
  const join = !!(prev && prev.classList && prev.classList.contains('chat-bubble') && _bubblesJoin(prev, bubble));
  bubble.classList.toggle('grouped-cont', join);    // joined to the bubble above
  if (prev && prev.classList && prev.classList.contains('chat-bubble')) {
    prev.classList.toggle('grouped-open', join);    // joined to the bubble below
  }
}

// Full rescan — for callers that rebuild the list out of band (reconcile, etc.).
function _regroupBubbles() {
  if (!app.chatMessages) return;
  const bubbles = app.chatMessages.querySelectorAll(':scope > .chat-bubble');
  for (const b of bubbles) _applyGrouping(b);
}

// Strip the machine closing envelope (the trailing `FLAG: <name>` line and the
// short call-to-action that rides with it) so it is never painted as transcript
// copy. The envelope STAYS in the stored content — it is what makes the flag
// durable — so this runs at PAINT time only, never against the durable text.
//
// Tolerant of a stream caught MID-envelope: a trailing PARTIAL marker ('F',
// 'FL', 'FLA', 'FLAG', with or without its colon) standing alone at the end of a
// line is the envelope starting to arrive, so it is hidden too and the machine
// line can never flash while the tokens land. The marker must follow a newline
// (so there is a statement in front of it) and a lone word like 'FAQ:' or 'For'
// never matches — the tail has to be a strict prefix of 'FLAG:'. A complete
// envelope resolves to its statement; envelope-only text (no statement) is left
// untouched.
//
// Called from the bubble-creation choke point AND from every live writer that
// bypasses it (chat-stream's `_setBubbleText` and `_buildActivityEntryRow`).
// A live writer that skips this prints the raw envelope until a reload
// re-projects the row from the durable content.
export function stripClosingEnvelope(text) {
  const raw = String(text == null ? '' : text);
  if (raw.indexOf('FLAG:') === -1) {
    const partial = raw.match(/\n[ \t]*F(?:L(?:A(?:G)?)?)?:?[ \t]*$/);
    return partial ? raw.slice(0, partial.index).replace(/\s+$/, '') : raw;
  }
  const parsed = parseClosingEnvelope(raw);
  if (parsed.valid && parsed.statement) return parsed.statement;
  return raw;
}

function addChatBubble(role, text, extraClass, imageUrl, turnId, msgId, createdAt) {
  const extraClasses = new Set(String(extraClass || '').split(/\s+/).filter(Boolean));
  const isToolOnly = extraClasses.has('tool-only');
  const isStreaming = extraClasses.has('streaming');
  // A mid-run notice is an ordinary agent bubble in a distinct durable lane:
  // no visual treatment, label, or adjacent-agent merge that could erase the
  // activity boundary it represents.
  const isNotice = extraClasses.has('notice');

  // Closing-message envelope (see ui/chat/js/closing-message.js). The promoted
  // final message ends with a `FLAG:` line and a short call-to-action; this
  // strips the machine envelope so the bubble shows ONLY the statement. The flag
  // is NOT rendered here — it surfaces on the session status chip and the
  // notification pill, which read the durable metadata.closing_message record
  // the loop writes. Applied HERE, in the one bubble-CREATION choke point, so
  // the cold load, reconcile and virtual-scroll paths all agree. The live
  // repaint paths (chat-stream's _setBubbleText / _buildActivityEntryRow) call
  // the same helper — see stripClosingEnvelope.
  if (role === 'agent' && !isToolOnly && !extraClasses.has('published')) text = stripClosingEnvelope(text);
  // Any real text/user bubble ends the current tool-only group, so the next
  // tool-only turn starts a fresh grouped bubble instead of merging.
  if (!isToolOnly && !isStreaming) app._activeToolGroupBubble = null;

  // ── Agent turn merging ──────────────────────────────────────────────────
  // Consecutive agent text messages merge into ONE bubble per turn. A user
  // message closes the turn; the next agent text starts a fresh bubble.
  // Error / interrupted / placeholder / summary / terminal-closer bubbles do
  // NOT participate in merging (the closer close-out is its own separate
  // bubble). `terminal-closer` must be excluded explicitly: a summary or
  // reconcile re-delivery that reaches this lane would otherwise merge the
  // reply into the open agent turn as a SECOND identical section, showing the
  // same live message twice until a reload re-projects it from the durable row.
  if (role === 'user') {
    app._agentTurnBubble = null;
  }

  const isMergeable = role === 'agent'
    && !extraClasses.has('published')
    && !isToolOnly
    && extraClass !== 'error'
    && extraClass !== 'interrupted'
    && extraClass !== 'session-placeholder'
    && extraClass !== 'summary-bubble'
    && extraClass !== 'terminal-closer'
    && !isNotice
    && text && text.trim();

  if (isMergeable && app._agentTurnBubble && app._agentTurnBubble.isConnected) {
    // Append a new LLM section to the open agent turn bubble.
    const target = app._agentTurnBubble;
    const idx = target.querySelectorAll(':scope > .turn-section.llm-section').length;
    const section = document.createElement('div');
    section.className = 'turn-section llm-section';
    section.dataset.sectionIdx = String(idx);
    if (turnId) section.dataset.turnId = turnId;
    if (msgId) section.dataset.msgId = msgId;
    _fillAgentBubble(section, text, extraClass !== 'streaming');
    target.appendChild(section);
    if (section.__mdSource) target.__mdSource = section.__mdSource;
    if (turnId) target.setAttribute('data-turn-id', turnId);
    if (msgId) target.setAttribute('data-msg-id', msgId);
    if (createdAt) _setBubbleCreatedAt(target, createdAt);
    // Wire the PREVIOUS LLM section (the one before this new one) so its gutter exists
    const allLLM = target.querySelectorAll(':scope > .turn-section.llm-section');
    if (allLLM.length >= 2 && typeof app._wireLLMSection === 'function') {
      app._wireLLMSection(allLLM[allLLM.length - 2], target);
    }
    return target;
  }

  const bubble = document.createElement('div');
  bubble.className = 'chat-bubble ' + role + (extraClass ? ' ' + extraClass : '');
  if (turnId) bubble.setAttribute('data-turn-id', turnId);
  if (msgId) bubble.setAttribute('data-msg-id', msgId);
  {
    _setBubbleCreatedAt(bubble, createdAt);
  }
  if (role === 'user') {
    const label = document.createElement('span');
    label.className = 'label';
    label.textContent = 'You';
    bubble.appendChild(label);
  }
  // Agent responses, including terminal closers, retain the same sender heading.
  if (role === 'agent' && !isToolOnly && !isNotice && extraClass !== 'session-placeholder'
      && extraClass !== 'summary-bubble') {
    const label = document.createElement('span');
    label.className = 'label';
    label.textContent = 'Agent';
    bubble.appendChild(label);
  }
  if (role === 'info') {
    const body = document.createElement('div');
    body.className = 'bubble-body';
    body.appendChild(linkifyText(text));
    bubble.appendChild(body);
  } else if (role === 'agent' && extraClass !== 'error') {
    // Wrap agent content in an LLM turn-section
    const section = document.createElement('div');
    section.className = 'turn-section llm-section';
    section.dataset.sectionIdx = '0';
    if (turnId) section.dataset.turnId = turnId;
    if (msgId) section.dataset.msgId = msgId;
    _fillAgentBubble(section, text, extraClass !== 'streaming');
    bubble.appendChild(section);
    // Also track mdSource on the bubble for copy/speak helpers
    if (section.__mdSource) bubble.__mdSource = section.__mdSource;
  } else if (role === 'agent' && extraClass === 'error') {
    const body = document.createElement('div');
    body.className = 'bubble-body';
    body.appendChild(linkifyText(text));
    bubble.appendChild(body);
  } else if (role === 'user') {
    const body = document.createElement('div');
    body.className = 'bubble-body';
    body.appendChild(linkifyText(text));
    bubble.appendChild(body);
  }
  if (imageUrl) {
    const img = document.createElement('img');
    img.src = imageUrl;
    img.style.maxWidth = '100%';
    img.style.maxHeight = '400px';
    img.style.borderRadius = '8px';
    img.style.marginTop = '8px';
    img.style.border = '1px solid var(--border)';
    const target = bubble.querySelector(':scope > .turn-section .md-body, :scope > .bubble-body, :scope > .md-body') || bubble;
    target.appendChild(img);
  }
  if (role === 'agent' && window.__streamAttachments && extraClass === 'has-attachments') {
    for (const att of window.__streamAttachments) {
      const el = renderAttachmentElement(att);
      if (el) bubble.appendChild(el);
    }
    window.__streamAttachments = null;
  }
  // Track this as the open agent turn bubble for merging subsequent messages.
  // (streaming bubbles start empty but should still anchor the turn.)
  // The summary bubble never becomes the merge anchor — it is a standalone lane.
  if (role === 'agent' && !isToolOnly && !isNotice && extraClass !== 'error'
      && extraClass !== 'interrupted' && extraClass !== 'summary-bubble') {
    app._agentTurnBubble = bubble;
  }

  app.chatMessages.appendChild(bubble);
  if (role === 'user') applyUserMessageTruncation(bubble);
  _renderOrder(bubble);
  _reorderTranscriptCanonical();
  _applyGrouping(bubble);
  // Footer is a bubble invariant, not a caller responsibility. The renderer is
  // idempotent, so stream/reconcile finalizers can safely enrich it again when
  // durable metadata (model, status, or a turn id) arrives.
  if (typeof app._addBubbleActions === 'function') {
    app._addBubbleActions(bubble);
  }
  // Follow the tail while automatic scrolling is armed. A manual transcript
  // gesture releases the lock, so _scrollToBottomIfNear no-ops until it is
  // armed again. Session-load's windowed render repositions afterwards, so it
  // never fights an anchored restore.
  if (typeof app._scrollToBottomIfNear === 'function') {
    app._scrollToBottomIfNear(app._chatScroller || (app.chatMessages && app.chatMessages.parentElement));
  }
  return bubble;
}

function addRecoveryNotice(recovery = {}) {
  if (!app.chatMessages) return null;
  const bubble = document.createElement('details');
  bubble.className = 'chat-bubble agent recovery-notice';
  bubble.setAttribute('data-created-at', Date.now());
  const summary = document.createElement('summary');
  summary.textContent = 'Recovered and resumed';
  bubble.appendChild(summary);
  const details = document.createElement('dl');
  const fields = [
    ['Stopped because', recovery.stop_cause],
    ['Issue', recovery.issue],
    ['Recovery', recovery.trigger],
    ['Attempt', recovery.attempt ? `${recovery.attempt}${recovery.max_attempts ? ` of ${recovery.max_attempts}` : ''}` : '—'],
    ['Stopped at', recovery.stopped_at],
    ['Recovered at', recovery.recovered_at],
  ];
  for (const [label, value] of fields) {
    if (!value) continue;
    const term = document.createElement('dt');
    term.textContent = label;
    const description = document.createElement('dd');
    description.textContent = String(value);
    details.append(term, description);
  }
  bubble.appendChild(details);
  app.chatMessages.appendChild(bubble);
  _applyGrouping(bubble);
  return bubble;
}

function updateLastBubble(text, extraClass, imageUrl) {
  const bubbles = app.chatMessages.querySelectorAll('.chat-bubble.agent');
  const last = bubbles[bubbles.length - 1];
  if (!last) return;
  while (last.firstChild) last.removeChild(last.firstChild);
  // Wrap in LLM section
  const section = document.createElement('div');
  section.className = 'turn-section llm-section';
  section.dataset.sectionIdx = '0';
  const isMd = _fillAgentBubble(section, text, extraClass !== 'streaming');
  last.appendChild(section);
  if (section.__mdSource) last.__mdSource = section.__mdSource;
  if (imageUrl) {
    const img = document.createElement('img');
    img.src = imageUrl;
    img.style.maxWidth = '100%';
    img.style.maxHeight = '400px';
    img.style.borderRadius = '8px';
    img.style.marginTop = '8px';
    img.style.border = '1px solid var(--border)';
    section.appendChild(img);
  }
  if (window.__streamAttachments && extraClass === 'has-attachments') {
    for (const att of window.__streamAttachments) {
      const el = renderAttachmentElement(att);
      if (el) last.appendChild(el);
    }
    window.__streamAttachments = null;
  }
  if (extraClass) last.className = 'chat-bubble agent ' + extraClass;
  else last.classList.remove('streaming');
  if (isMd) last.classList.add('md');
  // The class rewrite above wipes any grouped-* state — re-derive it (e.g. a
  // streaming bubble that just turned into an error must drop out of its group).
  _applyGrouping(last);
  // scroll handled by caller
}

// Expose on app immediately so session-load.js / stream modules can use them
app.addChatBubble = addChatBubble;
app.addRecoveryNotice = addRecoveryNotice;
app.updateLastBubble = updateLastBubble;
app._linkifyText = linkifyText;
app._renderMarkdownBody = _renderMarkdownBody;
app.renderUsageLimitNotice = renderUsageLimitNotice;
app._regroupBubbles = _regroupBubbles;
app._setBubbleCreatedAt = _setBubbleCreatedAt;
app._setBubbleSessionSeq = _setBubbleSessionSeq;
app._reorderTranscriptCanonical = _reorderTranscriptCanonical;
app.isMidRunOpeningEl = isMidRunOpeningEl;
app.openingOwnerOf = openingOwnerOf;
app.markMidRunOpening = markMidRunOpening;
app.isMidRunOpeningEl = isMidRunOpeningEl;
app.markMidRunOpening = markMidRunOpening;
app.openingOwnerOf = openingOwnerOf;
app.activityGroupPrecedesOpening = activityGroupPrecedesOpening;

export {
  addChatBubble,
  addRecoveryNotice,
  updateLastBubble,
  _fillAgentBubble,
  linkifyText,
  _esc as escapeHtml,
  _renderMarkdownBody,
  renderUsageLimitNotice,
  _formatRelativeTime,
  _refreshAllBubbleTimes,
  _parseCreatedAt,
  _setBubbleCreatedAt,
  _setBubbleSessionSeq,
  _reorderTranscriptCanonical,
  _regroupBubbles,
  _applyGrouping,
  // NOTE: isMidRunOpeningEl / markMidRunOpening / openingOwnerOf /
  // activityGroupPrecedesOpening are declared above with an inline `export`
  // (like applyUserMessageTruncation) and must NOT be repeated here. An ESM
  // module cannot export the same name twice: repeating them is a SyntaxError
  // that fails the WHOLE chat module graph, so the panel never boots.
};

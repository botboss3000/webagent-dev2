'use strict';

// Chat element: local_changes — working-tree summary panel (the new design).
// Consumes the /chat-changes data feed (ui/chat/js/chat-changes.js) via the
// 'chat-changes:updated' CustomEvent — the feed does the single fetch, this
// panel just renders. Hidden until the 'changes' chip below the pill
// (ui/chat/elements/changes.js) toggles it open; the chip's count badge shows
// when there are changes.
// Row markup: .chat-change-row/.chat-change-path/.chat-change-stat (shared
// with ui/shared/css/app1.css).
// Enabled via data/config/chat_ui.json → active_footer.above_pill rows.
// The commit button (top right, admin-gated like the feed) reveals a small
// note field + send button; submitting commits exactly this session's files
// via POST /api/v1/github/chat-changes/commit, then re-refreshes.
// Band size is configurable: controls.local_changes.width / max_lines in
// chat_ui.json (applied below as --lc-width / --lc-max-lines). The user may
// also pull the grip upward to persist a taller per-session list height.

import { app } from '../../../shared/js/state.js';
import { authHeaders } from '../../../shared/js/left-login.js';
import { kvRead, kvWrite } from '../../../shared/js/kv-ui-state.js';

// ── Mobile keyboard preservation ───────────────────────────────────────────
// Taps inside this panel (grip drag/click, commit/send buttons) must not
// dismiss the keyboard while the chat pill is focused: focusable taps blur
// the pill, and some mobile browsers (older iOS Safari) ignore the
// pointerdown preventDefault that normally keeps focus put. So the panel
// cancels the focus-shift default itself AND, as a bulletproof fallback,
// hands focus back to the pill after the tap once the click has run.
// Editable fields (the commit-note input) legitimately take focus — they
// are exempt. Works alongside the global keepPillFocusOnFooterTap guard in
// ui/shared/js/dom-utils.js; the local copy is immune to zone/mount
// reordering and adds the focus-restore fallback.
const _isEditable = (el) => {
  if (!el || el.nodeType !== 1) return false;
  const tag = el.tagName.toLowerCase();
  return tag === 'input' || tag === 'textarea' || tag === 'select' || !!el.isContentEditable;
};
const _chatInputEl = () => (app && app.chatInput) || document.getElementById('chat-input');

export function html(cfg = {}) {
  return `<div class="chat-local-changes" data-element-name="local_changes" hidden>
    <button type="button" class="chat-local-changes-grip" title="Drag up to resize the changes panel; click to hide it" aria-label="Resize the changes panel or hide it" aria-expanded="true">
      <div class="chat-local-changes-grip-line"></div>
    </button>
    <div class="chat-local-changes-head">
      <span class="chat-local-changes-count">0</span>
      <button type="button" class="chat-local-changes-commit" title="Commit these files" hidden>
        <i data-lucide="git-commit-horizontal" style="width:13px;height:13px;"></i>
      </button>
    </div>
    <div class="chat-local-changes-list" aria-label="Checked-out and changed files in this session"></div>
    <form class="chat-local-changes-form" hidden>
      <input type="text" class="chat-local-changes-note" placeholder="Commit note" maxlength="2000" autocomplete="off" />
      <button type="submit" class="chat-local-changes-send" title="Commit"><i data-lucide="send" style="width:12px;height:12px;"></i></button>
    </form>
    <div class="chat-local-changes-status" hidden></div>
  </div>`;
}

export function init(el, cfg = {}) {
  const list = el.querySelector('.chat-local-changes-list');
  if (!list) return;
  const count = el.querySelector('.chat-local-changes-count');
  const commitBtn = el.querySelector('.chat-local-changes-commit');
  const form = el.querySelector('.chat-local-changes-form');
  const note = el.querySelector('.chat-local-changes-note');
  const send = el.querySelector('.chat-local-changes-send');
  const status = el.querySelector('.chat-local-changes-status');
  const grip = el.querySelector('.chat-local-changes-grip');

  // ── Resize / minimize via the top grab bar ───────────────────────────────
  // Pulling the panel UP grows its file list; pulling down shrinks it. The
  // chosen height is per-session, while a click still offers a compact,
  // keyboard-accessible minimized state.
  let minimized = false;
  const _lcStorageKey = () => `chat_lc_minimized_${app.currentSessionId || ''}`;
  const _lcHeightStorageKey = () => `chat_lc_list_height_${app.currentSessionId || ''}`;
  const _minimumListHeight = () => {
    const lineHeight = Number.parseFloat(getComputedStyle(list).lineHeight);
    return Number.isFinite(lineHeight) ? lineHeight : 16;
  };
  const _maximumListHeight = () => Math.max(_minimumListHeight(), Math.min(480, window.innerHeight * 0.5));
  const _clampListHeight = (value) => Math.min(_maximumListHeight(), Math.max(_minimumListHeight(), value));
  const _applyListHeight = (value, persist = true) => {
    const height = _clampListHeight(value);
    el.style.setProperty('--lc-list-height', `${height}px`);
    if (persist) kvWrite('chat:lcListHeight:' + (app.currentSessionId || ''), _lcHeightStorageKey(), String(height));
  };
  const _syncListHeight = () => {
    const saved = Number.parseFloat(kvRead('chat:lcListHeight:' + (app.currentSessionId || ''), _lcHeightStorageKey()));
    if (Number.isFinite(saved)) _applyListHeight(saved, false);
    else el.style.removeProperty('--lc-list-height');
  };
  const _applyMinimized = (value) => {
    minimized = !!value;
    el.classList.toggle('minimized', minimized);
    if (grip) {
      grip.setAttribute('aria-expanded', String(!minimized));
      grip.title = minimized ? 'Show the changes panel' : 'Drag up to resize the changes panel; click to hide it';
      grip.setAttribute('aria-label', minimized ? 'Show the changes panel' : 'Resize the changes panel or hide it');
    }
    if (minimized) {
      // The collapse hides everything but the grip. If focus was inside the
      // panel body (commit-note field / buttons), the browser blurs it and
      // the keyboard drops — re-anchor focus on the chat pill to keep the
      // keyboard engaged.
      const input = _chatInputEl();
      const ae = document.activeElement;
      if (input && ae && el.contains(ae)) input.focus({ preventScroll: true });
    }
    kvWrite('chat:lcMinimized:' + (app.currentSessionId || ''), _lcStorageKey(), minimized ? '1' : '0');
  };
  const _syncPanelState = () => {
    const saved = kvRead('chat:lcMinimized:' + (app.currentSessionId || ''), _lcStorageKey());
    _applyMinimized(saved === '1');
    _syncListHeight();
  };

  if (grip) {
    let _dragStartY = 0;
    let _dragStartHeight = 0;
    let _dragWasMinimized = false;
    let _didDrag = false;
    const _dragThreshold = 4;

    grip.addEventListener('pointerdown', (e) => {
      _dragStartY = e.clientY;
      _dragStartHeight = list.getBoundingClientRect().height || _minimumListHeight();
      _dragWasMinimized = minimized;
      _didDrag = false;
      grip.setPointerCapture(e.pointerId);
      e.preventDefault();
    });

    grip.addEventListener('pointermove', (e) => {
      if (!grip.hasPointerCapture(e.pointerId)) return;
      const dy = _dragStartY - e.clientY; // positive = pulling up / growing taller
      if (!_didDrag && Math.abs(dy) > _dragThreshold) {
        _didDrag = true;
        // An upward pull from the compact state restores the panel and begins
        // growing it immediately; a simple click continues to restore it.
        if (_dragWasMinimized && dy > 0) {
          _applyMinimized(false);
          _dragStartHeight = _minimumListHeight();
        }
      }
      if (!_didDrag || (_dragWasMinimized && minimized)) return;
      _applyListHeight(_dragStartHeight + dy);
    });

    const _endGripDrag = (e) => {
      if (grip.hasPointerCapture(e.pointerId)) grip.releasePointerCapture(e.pointerId);
    };
    grip.addEventListener('pointerup', _endGripDrag);
    grip.addEventListener('pointercancel', _endGripDrag);

    // Click remains the simple hide/show affordance; movement is strictly resize.
    grip.addEventListener('click', (e) => {
      if (_didDrag) {
        e.preventDefault();
        e.stopPropagation();
        return;
      }
      e.preventDefault();
      _applyMinimized(!minimized);
    });

    _syncPanelState();
  }

  // ── Keep the mobile keyboard engaged while interacting with the panel ──
  // See the module-level comment above. Only focusable taps (button /
  // [role=button] / a / [tabindex]) can blur the pill, so only those are
  // intercepted; plain row taps keep working (and stay selectable).
  let _pillWasFocused = false;
  const _focusStealer = (t) => t && typeof t.closest === 'function' && t.closest('button, [role="button"], a, [tabindex]');
  const _restorePillFocus = () => {
    const input = _chatInputEl();
    if (!input || !_pillWasFocused) return;
    if (document.activeElement === input) return;
    if (_isEditable(document.activeElement)) return; // commit-note field took focus legitimately
    input.focus({ preventScroll: true });
  };
  el.addEventListener('pointerdown', (e) => {
    _pillWasFocused = !!_chatInputEl() && document.activeElement === _chatInputEl();
    if (!_pillWasFocused || _isEditable(e.target) || !_focusStealer(e.target)) return;
    e.preventDefault(); // stop the focus-shift default; the click still runs
  }, true);
  el.addEventListener('mousedown', (e) => {
    if (!_pillWasFocused || _isEditable(e.target) || !_focusStealer(e.target)) return;
    e.preventDefault(); // desktop browsers shift focus on mousedown
  }, true);
  // Fallback for browsers that ignore the pointerdown cancel: the tap blurred
  // the pill anyway — put focus back now that the click has run, so the
  // keyboard stays up. Both events are listened to for idempotent coverage.
  el.addEventListener('pointerup', _restorePillFocus, true);
  el.addEventListener('click', _restorePillFocus, true);

  // Config-driven sizing — chat_ui.json → active_footer.above_pill.controls.local_changes.
  if (cfg.width) el.style.setProperty('--lc-width', cfg.width);
  if (cfg.max_lines != null) el.style.setProperty('--lc-max-lines', cfg.max_lines);

  let files = [];
  let canCommit = false;
  let isAdmin = false;

  // ── Show/hide driven by the 'changes' chip below the pill ──
  // The panel is hidden until the user opens it — the chip's count badge
  // signals there is something to see. The chip dispatches
  // chat-control:toggle-changes; opening also un-collapses a prior
  // grip-minimize and refreshes the list once.
  let userHidden = true;
  const updateVisibility = () => {
    const next = userHidden || !files.length;
    // Only write when the value actually differs. MutationObserver delivers
    // records for the `hidden` attribute even when the value is unchanged
    // (the IDL reflection re-sets the attribute), so writing every time makes
    // the observer re-trigger itself in an endless microtask loop. The guard
    // breaks that: after the first write the next observer callback compares
    // equal and stops.
    if (!!el.hidden !== next) el.hidden = next;
  };
  // The config system re-shows controls on every re-apply (_showControl sets
  // el.hidden = false), which would pop the panel open against the user's
  // will — re-assert the computed visibility whenever the hidden attribute
  // changes outside our control. The equality guard above keeps this safe.
  const _visObserver = new MutationObserver(() => updateVisibility());
  _visObserver.observe(el, { attributes: true, attributeFilter: ['hidden'] });
  const onToggleChanges = () => {
    userHidden = !userHidden;
    if (!userHidden) {
      if (minimized) _applyMinimized(false);
      if (typeof app.refreshSessionChanges === 'function') {
        try { app.refreshSessionChanges(); } catch (_) {}
      }
    }
    updateVisibility();
  };
  document.addEventListener('chat-control:toggle-changes', onToggleChanges);

  const setStatus = (text, isError = false) => {
    if (!status) return;
    status.textContent = text || '';
    status.hidden = !text;
    status.classList.toggle('error', !!isError);
  };

  const render = (next = []) => {
    files = [...new Map((next || []).map((file) => [file.path, file])).values()];
    list.replaceChildren();
    if (!files.length) {
      el.hidden = true;
      return;
    }
    for (const file of files) {
      const row = document.createElement('div');
      row.className = 'chat-change-row';
      const path = document.createElement('span');
      path.className = 'chat-change-path';
      path.textContent = file.path;
      const stat = document.createElement('span');
      stat.className = 'chat-change-stat';
      const lifecycle = file.checkout_status || (file.conflict ? 'Conflict' : 'Changed');
      const lifecycleClass = lifecycle.toLowerCase().replace(/[^a-z]+/g, '-');
      row.dataset.checkoutStatus = lifecycleClass;
      row.setAttribute('aria-label', file.path + ', ' + lifecycle);
      row.title = file.violation || (
        file.checkout_owner && !file.checked_out
          ? lifecycle + '; lease held by session ' + file.checkout_owner
          : file.checkout_expires_at ? lifecycle + '; lease expires ' + file.checkout_expires_at : lifecycle
      );
      const badge = document.createElement('span');
      badge.className = 'chat-change-lifecycle chat-change-lifecycle-' + lifecycleClass;
      badge.textContent = lifecycle;
      stat.append(badge);
      if (file.added || file.removed) {
        const lineStats = document.createElement('span');
        lineStats.className = 'chat-change-line-stats';
        const add = document.createElement('span');
        add.className = 'chat-change-stat-add';
        add.textContent = file.added ? '+' + file.added : '';
        const del = document.createElement('span');
        del.className = 'chat-change-stat-del';
        del.textContent = file.removed ? '-' + file.removed : '';
        lineStats.append(add);
        if (file.added && file.removed) lineStats.append(' ');
        lineStats.append(del);
        stat.append(lineStats);
      }
      if (file.manual_checkin_allowed) {
        const checkin = document.createElement('button');
        checkin.type = 'button';
        checkin.className = 'chat-change-manual-checkin';
        checkin.textContent = 'Check in';
        checkin.title = 'Manually check in the lease held by another session';
        checkin.setAttribute('aria-label', 'Manually check in ' + file.path + ' from another session');
        checkin.addEventListener('click', async (event) => {
          event.preventDefault();
          event.stopPropagation();
          if (!app.currentSessionId) return;
          checkin.disabled = true;
          try {
            const res = await fetch('/api/v1/github/chat-changes/checkin', {
              method: 'POST',
              headers: { ...authHeaders(), 'Content-Type': 'application/json' },
              body: JSON.stringify({ session_id: app.currentSessionId, paths: [file.path] }),
            });
            const body = await res.json().catch(() => ({}));
            if (!res.ok) throw new Error(body.detail || body.message || 'Check-in failed.');
            setStatus('Checked in ' + file.path + '. The prior session must reread it before reuse.');
            if (typeof app.refreshSessionChanges === 'function') await app.refreshSessionChanges();
          } catch (err) {
            setStatus(err.message || 'Check-in failed.', true);
          } finally {
            checkin.disabled = false;
          }
        });
        stat.append(checkin);
      }
      row.classList.toggle('conflict', !!file.conflict);
      row.classList.toggle('violation', lifecycle === 'Violation');
      row.append(path, stat);
      list.append(row);
    }
    canCommit = files.some((f) => !!f.status && !f.conflict);
    if (count) count.textContent = files.length + ' file' + (files.length === 1 ? '' : 's');
    if (commitBtn) commitBtn.hidden = !(isAdmin && canCommit);
    updateVisibility();
  };

  // Admin gate — the commit endpoint requires admin (same check the data
  // feed uses in initChatChanges before it starts polling).
  fetch('/api/v1/github/check-access', { headers: { ...authHeaders() } })
    .then((r) => r.json().catch(() => ({})))
    .then((d) => {
      isAdmin = !!d.is_admin;
      if (commitBtn) commitBtn.hidden = !(isAdmin && canCommit);
    })
    .catch(() => {});

  commitBtn?.addEventListener('click', (e) => {
    e.stopPropagation();
    if (!form) return;
    form.hidden = !form.hidden;
    if (!form.hidden) {
      setStatus('');
      note?.focus();
    }
  });

  form?.addEventListener('submit', async (e) => {
    e.preventDefault();
    e.stopPropagation();
    if (!canCommit || !app.currentSessionId) return;
    if (send) send.disabled = true;
    try {
      const res = await fetch('/api/v1/github/chat-changes/commit', {
        method: 'POST',
        headers: { ...authHeaders(), 'Content-Type': 'application/json' },
        body: JSON.stringify({
          session_id: app.currentSessionId,
          paths: files.filter((f) => !!f.status && !f.conflict).map((f) => f.path),
          message: note?.value || '',
        }),
      });
      const body = await res.json().catch(() => ({}));
      if (!res.ok) throw new Error(body.detail || body.message || `Commit failed (${res.status})`);
      setStatus(body.status === 'committed' ? `Committed ${body.hash}.` : (body.message || 'Nothing to commit.'));
      if (note) note.value = '';
      if (form) form.hidden = true;
      if (typeof app.refreshSessionChanges === 'function') {
        try { await app.refreshSessionChanges(); } catch (_) {}
      }
    } catch (err) {
      setStatus(err.message || 'Commit failed.', true);
    } finally {
      if (send) send.disabled = false;
    }
  });

  let received = false;
  const onUpdated = (e) => {
    received = true;
    // Re-read per-session state — keeps the minimize and resized-height
    // preferences in sync when the element persists across session switches.
    _syncPanelState();
    render(e.detail && e.detail.files);
  };
  document.addEventListener('chat-changes:updated', onUpdated);

  // Boot-order backstop: the chat-changes feed (ui/chat/js/chat-changes.js)
  // starts before this element mounts, so a restored session's first broadcast
  // may fire before we're listening. If no event has arrived shortly after
  // mount, pull once via the feed's refresh — it re-broadcasts.
  let destroyed = false;
  const backstop = setTimeout(() => {
    if (!destroyed && !received && typeof app.refreshSessionChanges === 'function') {
      try { app.refreshSessionChanges(); } catch (_) {}
    }
  }, 500);

  el.__localChangesCleanup = () => {
    destroyed = true;
    clearTimeout(backstop);
    _visObserver.disconnect();
    document.removeEventListener('chat-changes:updated', onUpdated);
    document.removeEventListener('chat-control:toggle-changes', onToggleChanges);
  };
}

export function destroy(el) {
  if (typeof el.__localChangesCleanup === 'function') el.__localChangesCleanup();
}

export function style() { return ''; }

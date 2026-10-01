'use strict';

// Session dropdown controller — the session selector chip in the chat header.
// Centralized home: ui/chat/elements/session-dropdown/ (index.js = element
// contract, controller.js = this file, list.js = session data + rendering).
//
// Mounted two ways, both idempotent (per root element, guarded by a WeakMap):
//   1. the element's init() — when the chat-controls loader creates the
//      control dynamically, and
//   2. session-init.js at boot — when the static chat-side-panel.html markup is
//      present (the loader REUSES existing DOM and does NOT call init()).
//
// mountSessionDropdown(root) returns { open, close, isOpen, destroy } and
// stores the handle on root._sessionDropdownController so header code
// (new-session flows, etc.) can close the menu without importing internals.
//
// Owns: open/close/position/animation, the loading skeleton, keyboard
// dismissal, trigger wiring (click/Enter/space/long-press/kebab/delete), the
// per-row "more" popup, the menu click delegation (switch/delete/bin/visibility/
// checkbox/expand/search/manage footer), row reordering and long-press rename.

import { app } from '../../../shared/js/state.js';
import { apiPath } from '../../../shared/js/config.js';
import { icon } from '../../../shared/js/icons.js';
import { copyText } from '../../../shared/js/clipboard.js';
import { advanceDeleteBtn, resetDeleteBtn } from '../../../shared/js/delete-control.js';
import { makeRowsReorderable, attachRowLongPress } from '../../../shared/js/ordering.js';
import { storageAdapter } from '../../js/storage/storage-adapter.js';
import { authHeaders } from '../../../shared/js/left-login.js';
import {setNotificationUnread} from '../../core/notifications.js';
import { activateNeedsInputChip } from '../../../shared/js/needs-input-chip.js';
import { positionKebabPopup } from '../../../shared/js/dom-utils.js';
import { createChatWidget } from '../../../chat-widget/js/chat-widget.js';
// Screen wake lock — the ⋯ menu's "always-on display" switch drives the shared,
// owner-scoped lock under the 'app-setting' owner (the same owner the app-wide
// App Functions setting uses, so the two controls can never fight; Gen UI keeps
// its own 'genui' owner). See ui/shared/js/screen-wake.js.
import { isAlwaysOnEnabled, setAlwaysOnDisplay, isWakeLockSupported } from '../../../shared/js/screen-wake.js';
import {
  _sessionsCache,
  populateSessionSelect,
  refreshSessionMetadata,
  _renderSessionRows,
  _setTriggerLabel,
  _setShowHidden, _getShowHidden,
  _setCheckboxMode, _getCheckboxMode,
  _selectedSessions, _clearSelected,
  toggleSessionGroup,
  _setSearchQuery, _getSearchQuery,
  _setBinMode, _getBinMode,
  _setFlagFilter, _getFlagFilter,
  _scanMessagesForQuery, _clearMsgSearch,
  _scheduleSearchCommit, _cancelPendingSearchCommit,
  configureSessionDropdown, getSessionDropdownConfig,
  _formatRelativeTime, _isSessionRead,
  sessionDropdownStore,
} from './list.js';
import {
  switchToSession,
  deleteSession,
  togglePin,
  toggleHidden,
  startRename,
  _headerRenameSession,
  autoRenameSession,
} from '../../js/session-core.js?v=515';

const _mounts = new WeakMap(); // root el -> { handle, destroy }

export function mountSessionDropdown(root, opts = {}) {
  if (!root) return null;
  const cfg = configureSessionDropdown(opts);
  // The menu is portaled to body while open. A remount (including the React
  // island handoff) must not leave an old body-owned menu sharing this ID.
  // There is one session selector per chat shell, so detached copies are stale.
  for (const staleMenu of document.querySelectorAll('#session-dropdown-menu')) {
    if (!root.contains(staleMenu) && staleMenu.dataset.reactManaged !== 'true') staleMenu.remove();
  }
  const trigger = root.querySelector('.session-dropdown-trigger');
  const triggerParts = {
    agent_icon: root.querySelector('#session-dropdown-icon'),
    status: root.querySelector('#session-dropdown-status'),
    row_actions: root.querySelector('#session-dropdown-kebab'),
    delete: root.querySelector('#session-dropdown-delete'),
  };
  for (const [name, el] of Object.entries(triggerParts)) {
    if (!el) continue;
    const hidden = cfg.trigger[name] === false;
    el.hidden = hidden;
    if (hidden) el.style.setProperty('display', 'none', 'important');
    else el.style.removeProperty('display');
  }
  if (trigger) trigger.setAttribute('aria-label', opts.aria_label || 'Show sessions');
  const configuredMenu = root.querySelector('.session-dropdown-menu')
    || document.getElementById('session-dropdown-menu');
  if (configuredMenu) {
    configuredMenu.style.width = cfg.menu.width || '';
    configuredMenu.style.maxHeight = cfg.menu.max_height || '';
  }
  if (_mounts.has(root)) return _mounts.get(root).handle;
  const menu = root.querySelector('.session-dropdown-menu')
    || document.getElementById('session-dropdown-menu');
  if (!menu) return null;

  const dropdown = root;
  let _openRequestId = 0;
  let _keyboardDismissRequested = false;
  let _closeAnimationTimer = 0;
  let _openAnimationTimer = 0;
  let _lastToggleTime = 0;
  // The React island deliberately remounts this controller after hydration.
  // Keep its persistent wrapper listeners disposable across that handoff.
  const _dropdownEventAbort = new AbortController();

  // Sync status row. When data-syncing is set, a "Syncing…" row is shown at the
  // BOTTOM of the menu, immediately above the manage footer (it used to be a
  // ::before pinned to the top). A real element keeps it in the right place no
  // matter which code path re-renders the menu or toggles the flag.
  function _syncStatusRow() {
    if (!menu) return;
    const syncing = menu.dataset.syncing === 'true';
    const existing = menu.querySelector(':scope > .session-dropdown-sync');
    if (!syncing) {
      if (existing) existing.remove();
      return;
    }
    let sync = existing;
    if (!sync) {
      sync = document.createElement('div');
      sync.className = 'session-dropdown-sync';
      sync.textContent = 'Syncing…';
    }
    const footer = menu.querySelector(':scope > .session-manage-row');
    if (footer) {
      if (footer.previousElementSibling !== sync) footer.before(sync);
    } else if (menu.lastElementChild !== sync) {
      menu.appendChild(sync);
    }
  }
  // React to both data-syncing toggles and menu re-renders (rows/footer are
  // rebuilt by list.js) so the sync row is always positioned above the footer.
  const _syncRowObserver = new MutationObserver(_syncStatusRow);
  _syncRowObserver.observe(menu, { attributes: true, attributeFilter: ['data-syncing'], childList: true });
  // Ensure it is correct if the menu already carries data-syncing at mount time.
  _syncStatusRow();

  function _isEditableElement(el) {
    if (!el || el === document.body) return false;
    return el.matches?.('input, textarea, select, [contenteditable="true"]') || false;
  }

  function _viewportHeight() {
    return window.visualViewport?.height || window.innerHeight;
  }

  // Focus is the reliable keyboard signal. Some Android WebViews shrink both
  // visualViewport and innerHeight, so comparing those two values can falsely
  // report that the keyboard is already closed.
  function _dismissKeyboardBeforeOpen() {
    const active = document.activeElement;
    const hadEditableFocus = _isEditableElement(active)
      || document.body.classList.contains('chat-pill-focused');
    if (!hadEditableFocus) return false;

    _keyboardDismissRequested = true;
    active?.blur?.();
    document.body.classList.remove('chat-pill-focused');
    return true;
  }

  function _waitForKeyboardDismiss() {
    const vv = window.visualViewport;
    const startHeight = _viewportHeight();
    let lastHeight = startHeight;
    let lastChangeAt = performance.now();
    // Whether the viewport has actually moved since the blur. False on desktop
    // (blurring an input never resizes the viewport) — lets the wait finish in
    // ~120ms instead of the full 450ms settle window when there is no keyboard.
    let sawResize = false;
    let finished = false;
    let frame = 0;
    let timeout = 0;

    return new Promise(resolve => {
      const finish = () => {
        if (finished) return;
        finished = true;
        cancelAnimationFrame(frame);
        clearTimeout(timeout);
        vv?.removeEventListener('resize', noteResize);
        window.removeEventListener('resize', noteResize);
        if (menu._deferredOpenCleanup === finish) menu._deferredOpenCleanup = null;
        requestAnimationFrame(() => requestAnimationFrame(resolve));
      };
      const noteResize = () => {
        sawResize = true;
        lastHeight = _viewportHeight();
        lastChangeAt = performance.now();
      };
      const check = now => {
        const height = _viewportHeight();
        if (Math.abs(height - lastHeight) > 1) {
          lastHeight = height;
          lastChangeAt = now;
          // A measured height change also counts as motion (covers browsers
          // that resize without firing a resize event).
          sawResize = true;
        }
        const viewportGrew = height > startHeight + 40;
        const quietFor = now - lastChangeAt;
        // Fast path (desktop / no keyboard): nothing moved within a short
        // grace window — there is no keyboard to wait for, open immediately.
        if (!sawResize && quietFor >= 120) { finish(); return; }
        // Keyboard collapse confirmed: wait until the viewport has grown AND
        // settled, or (motion without growth) the full settle window.
        if ((viewportGrew && quietFor >= 120) || (sawResize && quietFor >= 450)) {
          finish();
          return;
        }
        frame = requestAnimationFrame(check);
      };

      vv?.addEventListener('resize', noteResize);
      window.addEventListener('resize', noteResize);
      frame = requestAnimationFrame(check);
      timeout = setTimeout(finish, 2500);
      menu._deferredOpenCleanup = finish;
    });
  }

  // The menu must have lightweight, stable geometry before its opening
  // animation starts. Render only phantom rows here: cache reads and row
  // rendering wait until the opening transition has fully settled.
  // SESSIONS-SKELETON (breadcrumb) — three places paint session-list skeletons
  // and must stay in sync on row count + line-width ranges. THIS function is
  // the reference: 6–15 rows, each line 30–100% wide. The sessions table
  // mirrors it in ui/main-panel/agents/sessions/js/sessions-page.js
  // (_renderSkeletonRows — first content column fixed 500px) and the static
  // phantom rows in ui/main-panel/agents/agents.html (same classes + ranges).
  function _renderSessionMenuSkeleton() {
    if (!menu) return;
    menu.replaceChildren();
    menu.dataset.loading = 'true';
    menu.setAttribute('aria-busy', 'true');
    // Randomized phantom rows — 6–15 rows, each line 30–100% wide — so the
    // skeleton reads as a real, ragged list of session titles instead of a
    // uniform, predictable stack. (Breadcrumb: see SESSIONS-SKELETON above.)
    const rowCount = 6 + Math.floor(Math.random() * 10); // 6–15 rows
    for (let i = 0; i < rowCount; i++) {
      const row = document.createElement('div');
      row.className = 'session-dropdown-loading-row';
      row.setAttribute('aria-hidden', 'true');
      const lineWidth = 30 + Math.floor(Math.random() * 71); // 30–100%
      row.innerHTML = `
        <span class="session-dropdown-loading-icon"></span>
        <span class="session-dropdown-loading-line" style="max-width: ${lineWidth}%"></span>
      `;
      menu.appendChild(row);
    }
  }

  function _prepareSessionMenuContent() {
    if (app.currentUserId) {
      _renderSessionMenuSkeleton();
    } else {
      menu.replaceChildren();
      delete menu.dataset.loading;
      menu.removeAttribute('aria-busy');
      const empty = document.createElement('div');
      empty.className = 'session-dropdown-empty';
      empty.textContent = 'No sessions yet';
      menu.appendChild(empty);
    }
  }

  // The menu is moved to document.body while open to escape clipped header
  // ancestors, so it no longer follows its trigger automatically. Keep this
  // geometry update separate from the open animation so a window or chat-panel
  // resize can re-anchor the already-open menu without restarting that animation.
  function _positionOpenMenu() {
    if (!menu) return;

    const rect = dropdown.getBoundingClientRect();
    const viewport = window.visualViewport;
    const viewportTop = viewport?.offsetTop || 0;
    const viewportHeight = viewport?.height || window.innerHeight;
    const viewportBottom = viewportTop + viewportHeight;
    const spaceBelow = viewportBottom - rect.bottom;
    const spaceAbove = rect.top - viewportTop;
    const flipUp = spaceBelow < 180 && spaceAbove > spaceBelow;

    menu.style.top = flipUp ? 'auto' : rect.bottom + 'px';
    menu.style.bottom = flipUp ? (window.innerHeight - rect.top) + 'px' : 'auto';
    const viewportCap = Math.max(100, Math.floor(viewportHeight * 0.66));
    menu._availableHeight = Math.min(viewportCap, Math.max(100, Math.floor((flipUp ? spaceAbove : spaceBelow) - 8)));

    // Keep the detached menu flush with the session chip on every layout,
    // including mobile and while the chat panel is being dragged.
    menu.style.left = rect.left + 'px';
    menu.style.right = 'auto';
    menu.style.width = rect.width + 'px';
    menu.style.minWidth = '0';
    const ddStyle = getComputedStyle(dropdown);
    menu.style.maxWidth = ddStyle.maxWidth !== 'none' ? ddStyle.maxWidth : 'none';
    menu.style.transform = '';
    menu.classList.toggle('flip-up', flipUp);
    dropdown.classList.toggle('flip-up', flipUp);
  }

  /** Position and show the menu (called after the keyboard is gone). */
  function _positionAndShowMenu() {
    if (!menu) return;
    clearTimeout(_closeAnimationTimer);
    clearTimeout(_openAnimationTimer);
    _closeAnimationTimer = 0;
    _openAnimationTimer = 0;
    menu._closeAnimation?.cancel();
    menu._closeAnimation = null;
    menu.style.maxHeight = '';
    menu.style.overflow = '';
    // Clear a stale transition lock (an interrupted close sets transition:none)
    // and any stale measured height so every open starts from a clean slate.
    menu.style.transition = '';
    menu.style.removeProperty('--session-menu-open-height');
    // Move to body so it escapes overflow-clipped ancestors
    for (const staleMenu of document.querySelectorAll('#session-dropdown-menu')) {
      if (staleMenu !== menu && staleMenu.parentNode === document.body && staleMenu.dataset.reactManaged !== 'true') staleMenu.remove();
    }
    if (menu.dataset.reactManaged !== 'true' && menu.parentNode !== document.body) document.body.appendChild(menu);

    menu.style.position = 'fixed';
    // Flush against the chip (no gap): when open the chip is the panel's
    // title row, so the menu attaches directly to its edge.
    _positionOpenMenu();

    // Now reveal. _positionOpenMenu() has already applied the correct
    // flip-up state to both the detached menu and its trigger.
    menu.hidden = false;
    menu.dataset.state = 'opening';
    menu.classList.remove('closing');
    dropdown.classList.add('open');
    _resetAllDeleteButtons();
  }

  async function openMenu() {
    if (!menu) return;
    const requestId = ++_openRequestId;
    const shouldWait = _keyboardDismissRequested || _dismissKeyboardBeforeOpen();
    _keyboardDismissRequested = false;
    if (shouldWait) {
      await _waitForKeyboardDismiss();
      if (requestId !== _openRequestId) return;
    }
    // Do this before revealing the menu. The following measurement can now
    // never observe an empty list while the async session fetch is pending.
    _prepareSessionMenuContent();
    _positionAndShowMenu();
    requestAnimationFrame(() => {
      if (requestId !== _openRequestId || menu.dataset.state !== 'opening') return;
      const availableHeight = menu._availableHeight || menu.scrollHeight;
      // The loading shell grows to the same final cap as the populated list.
      // It must not derive its opening height from the randomized skeleton.
      const targetHeight = availableHeight;
      menu.style.setProperty('--session-menu-open-height', `${targetHeight}px`);
      const rows = Array.from(menu.children);
      const stagger = rows.length ? Math.min(0.045, 0.3 / rows.length) : 0.035;
      rows.forEach((row, index) => {
        row.style.animationDelay = `${(index * stagger).toFixed(3)}s`;
      });
      // Match the tool-call disclosure: establish the collapsed frame, then
      // transition to the measured list height on the following frame.
      menu.offsetHeight;
      requestAnimationFrame(() => {
        if (requestId !== _openRequestId || menu.dataset.state !== 'opening') return;
        menu.classList.add('opening');
        menu.dataset.state = 'open';
        // DEBUG-TAG: drop-down has actually opened
        console.log('DEBUG-TAG: [session-dropdown] opened', { requestId, rows: menu.children.length });
        const totalMs = rows.length * stagger * 1000 + 180;
        _openAnimationTimer = setTimeout(() => {
          menu.classList.remove('opening');
          rows.forEach(row => { row.style.animationDelay = ''; });
          _openAnimationTimer = 0;
          // The shell is now fully open. Only at this point may cache reads,
          // DOM row construction, or network revalidation compete for the
          // main thread, avoiding a visibly interrupted open animation.
          if (requestId === _openRequestId && menu.dataset.state === 'open') _loadSessionList();
        }, totalMs);
      });
    });
  }

  async function _loadSessionList() {
    if (!app.currentUserId) return;
    const userId = app.currentUserId;
    menu.dataset.syncing = 'true';
    menu.setAttribute('aria-busy', 'true');
    sessionDropdownStore.setLoadState('syncing');
    try {
      // First resolve the shared memory snapshot or IndexedDB mirror. In
      // normal mode with no local snapshot this falls through to the server,
      // leaving the honest skeleton in place. In every warm path the rows are
      // usable immediately and this awaited work is only local storage.
      const paintedFromCache = await populateSessionSelect(userId, { preferCache: true });

      // A cached snapshot is deliberately stale-while-revalidate. Normal mode
      // verifies it with one authoritative list read after it is visible.
      // Hybrid listSessions already owns one background refresh and dispatches
      // sessions-delta; browser-authority mode has no server handshake. Starting
      // another handshake here would multiply one open into equivalent work.
      if (paintedFromCache && !storageAdapter.isBrowser) {
        await populateSessionSelect(userId);
      }
    } finally {
      delete menu.dataset.syncing;
      menu.removeAttribute('aria-busy');
      sessionDropdownStore.setLoadState('idle');
    }
  }

  function _finishMenuClose() {
    clearTimeout(_closeAnimationTimer);
    _closeAnimationTimer = 0;
    if (menu.dataset.state !== 'closing') return;
    menu.hidden = true;
    delete menu.dataset.state;
    menu.classList.remove('flip-up');
    // Return the menu to its original parent so next open's body-append works
    if (menu.dataset.reactManaged !== 'true' && menu.parentNode !== dropdown) {
      dropdown.appendChild(menu);
    }
    // Clear inline position styles so the next open recalculates fresh
    menu.style.position = '';
    menu.style.top = '';
    menu.style.left = '';
    menu.style.bottom = '';
    menu.style.transform = '';
    menu.style.width = '';
    menu.style.minWidth = '';
    menu.style.maxWidth = '';
    menu.style.maxHeight = '';
    menu.style.overflow = '';
    menu.style.transition = '';
    menu.style.removeProperty('--session-menu-open-height');
    menu._availableHeight = 0;
    menu.classList.remove('opening', 'closing');
    menu._closeAnimation?.cancel();
    menu._closeAnimation = null;
  }

  function closeMenu({ immediate = false } = {}) {
    if (!menu) return;
    // A pending search-input debounce must not fire after the menu closes —
    // it would commit a half-typed query the user thought they abandoned.
    _cancelPendingSearchCommit();
    _openRequestId++;
    dropdown.classList.remove('open');
    dropdown.classList.remove('flip-up');
    // Clean up the keyboard-deferred open handler if it's pending
    menu._deferredOpenCleanup?.();
    if (!menu.hidden) {
      clearTimeout(_openAnimationTimer);
      _openAnimationTimer = 0;
      menu.classList.remove('opening');
      const rows = Array.from(menu.children);
      const stagger = rows.length ? Math.min(0.035, 0.18 / rows.length) : 0.025;
      // Lock the current rendered height before transitioning it to zero.
      menu.style.maxHeight = `${menu.getBoundingClientRect().height}px`;
      menu.style.overflow = 'hidden';
      menu.offsetHeight;
      rows.reverse().forEach((row, index) => {
        row.style.animationDelay = `${(index * stagger).toFixed(3)}s`;
      });
      menu.classList.add('closing');
      menu.dataset.state = 'closing';
      clearTimeout(_closeAnimationTimer);
      if (immediate) {
        _finishMenuClose();
      } else if (window.matchMedia?.('(prefers-reduced-motion: reduce)').matches) {
        _finishMenuClose();
      } else if (typeof menu.animate !== 'function') {
        requestAnimationFrame(() => {
          if (menu.dataset.state === 'closing') menu.style.maxHeight = '0px';
        });
        _closeAnimationTimer = setTimeout(_finishMenuClose, 380);
      } else {
        const startHeight = menu.getBoundingClientRect().height;
        const endTranslate = menu.classList.contains('flip-up') ? '4px' : '-4px';
        // Suppress CSS transitions so they don't fight the WAAPI animation.
        menu.style.transition = 'none';
        const cornerRadius = getComputedStyle(menu).borderTopLeftRadius || '14px';
        menu._closeAnimation = menu.animate([
          {
            maxHeight: `${startHeight}px`,
            clipPath: `inset(0 0 0 0 round ${cornerRadius})`,
            opacity: 1,
            transform: 'translateY(0)',
          },
          {
            maxHeight: '0px',
            clipPath: menu.classList.contains('flip-up')
              ? `inset(100% 0 0 0 round ${cornerRadius})`
              : `inset(0 0 100% 0 round ${cornerRadius})`,
            opacity: 0,
            transform: `translateY(${endTranslate})`,
          },
        ], {
          duration: 340,
          easing: 'cubic-bezier(.4, 0, .2, 1)',
          fill: 'forwards',
        });
        menu._closeAnimation.onfinish = _finishMenuClose;
        menu._closeAnimation.oncancel = () => {};
        // Fallback for embedded browsers that expose animate() but fail to
        // deliver the finish callback when the tab loses visibility.
        _closeAnimationTimer = setTimeout(_finishMenuClose, 450);
      }
    }
    _resetAllDeleteButtons();
    _closeRowActions();
    // Reset manage mode so the dropdown reopens in the normal (non-hidden) view.
    _setShowHidden(false);
    // Reset the footer search too — the dropdown must reopen with an empty
    // search field showing the full session list, never the previous results.
    _setSearchQuery('');
    _clearMsgSearch();
  }

  function _resetDeleteBtn(btn) {
    resetDeleteBtn(btn, { title: 'Delete session' });
  }

  function _resetAllDeleteButtons() {
    document.querySelectorAll('.session-row-delete').forEach(_resetDeleteBtn);
  }

  // Only ONE delete affordance may be armed at a time. Arming the trigger chip's
  // trash, the footer's delete-all / delete-selected, an opt-in row trash
  // (menu.row_delete) or the "more" panel's Delete session row drops every OTHER
  // armed control back to its resting state, so a second click can never land on
  // a stale hazard. Shared here so every call site agrees
  // (SHARED-DELETE-CONTROL, delete-control.js).
  function _resetArmedDeletes(keep) {
    document.querySelectorAll('.session-row-delete[data-state="warning"]').forEach(other => {
      if (other !== keep) resetDeleteBtn(other, { title: 'Delete session' });
    });
    document.querySelectorAll('.session-row-actions .session-row-action.danger[data-state="warning"]').forEach(other => {
      if (other !== keep) _resetMenuDeleteBtn(other);
    });
  }

  // The "more" panel's Delete session row is an icon + LABEL button, but the
  // shared reset paints an icon-only button (correct for the bare trash icons
  // it was written for). Re-apply the row's resting markup after any reset; the
  // transient states get their label back from CSS instead — see the
  // .session-row-actions .session-row-action.danger[data-state] rules in
  // shared/css/app1.css (tagged SHARED-DELETE-CONTROL).
  const _menuDeleteRestHtml = () => `${icon('trash-2', { size: '14px' })} Delete session`;
  function _resetMenuDeleteBtn(btn) {
    if (!btn) return;
    resetDeleteBtn(btn, { title: 'Delete session', size: '14px' });
    btn.innerHTML = _menuDeleteRestHtml();
  }

  // Failed delete from the panel: show the reason in place of the row's label
  // for a couple of seconds (same "borrow the name space" feedback the row's own
  // trash button uses via _showRowError), then fall back to the resting row.
  function _flashMenuDeleteError(btn, msg) {
    btn.innerHTML = `${icon('alert-triangle', { size: '14px' })} ${msg}`;
    clearTimeout(btn._delErrTimer);
    btn._delErrTimer = setTimeout(() => _resetMenuDeleteBtn(btn), 2500);
  }

  // The "more" (⋯) panel's Delete session row uses the SAME two-click
  // affordance as the row's own trash button and the Agents → Sessions page
  // kebab menu (advanceDeleteBtn): click one arms it (one delete pulse → hazard
  // icon + warning label), click two commits the delete. The panel deliberately
  // STAYS OPEN between the two clicks — so the confirm lands on the same
  // control — and stays open through the delete pulse, so a further click can
  // restore the session, exactly like the row's trash (onUndo).
  function handleMenuDeleteClick(btn, sid, row) {
    advanceDeleteBtn(btn, {
      size: '14px', spinSize: '14px',
      onArm: (b) => _resetArmedDeletes(b),
      onConfirm: async () => {
        const result = await deleteSession(sid, { retries: 1 });
        // On success the row stays in the "deleting" pulse so a further click
        // can restore it (onUndo) — same contract as the row's trash button.
        if (result.ok) return;
        const msg = result.error || 'Delete failed';
        // The panel is body-mounted, so borrow the row's name space when the
        // panel was opened from a row kebab; otherwise show it in the panel row.
        if (row && row.isConnected) _showRowError(row, msg);
        else _flashMenuDeleteError(btn, msg);
        _resetMenuDeleteBtn(btn);
      },
      onUndo: async () => {
        await _restoreFromBin(sid);
        _resetMenuDeleteBtn(btn);
      },
    });
  }

  function handleDeleteClick(btn, sid) {
    advanceDeleteBtn(btn, {
      onArm: (b) => _resetArmedDeletes(b),
      onConfirm: async () => {
        const result = await deleteSession(sid, { retries: 1 });
        if (!result.ok) {
          // Show error in the session name space — replace the title text, revert
          // after a couple seconds so the user can retry.
          const row = btn.closest('.session-row');
          if (row) {
            _showRowError(row, result.error || 'Delete failed');
          }
          resetDeleteBtn(btn, { title: 'Delete session', size: '14px' });
        }
        // On success the delete commits (session → recycled). The dropdown row
        // stays in the "deleting" pulse until the menu is next refreshed, so a
        // second click (onUndo) can restore the session before it's dropped.
      },
      onUndo: async () => {
        // Undo the in-flight delete: restore the session (recycled → active).
        // The button shows a green pulsing triangle until the restore resolves.
        await _restoreFromBin(sid);
        resetDeleteBtn(btn, { title: 'Delete session', size: '14px' });
      },
    });
  }

  // Show an ephemeral error message in the session row's title span, then
  // revert to the original session name after ~2.5s. Uses design-system
  // colours via a class — no hard-coded hex.
  function _showRowError(row, msg, durationMs) {
    const titleSpan = row.querySelector('.session-row-title');
    if (!titleSpan) return;
    const orig = titleSpan.dataset._delErrOrig || titleSpan.innerHTML;
    titleSpan.dataset._delErrOrig = orig;
    titleSpan.textContent = msg;
    titleSpan.classList.add('session-row-title-error');
    titleSpan.title = msg;
    clearTimeout(titleSpan._delErrTimer);
    titleSpan._delErrTimer = setTimeout(() => {
      titleSpan.innerHTML = titleSpan.dataset._delErrOrig || orig;
      titleSpan.classList.remove('session-row-title-error');
      titleSpan.title = 'Hold to rename';
      delete titleSpan.dataset._delErrOrig;
    }, durationMs || 2500);
  }

  // ── Shared "Renaming…" feedback (header trigger + dropdown rows) ─────────
  // Auto rename swaps the session NAME for a spinner + "Renaming…" in BOTH the
  // header trigger label and the dropdown row's title — one code path, one
  // look. The agent icon (a child of each row's title span, a sibling of the
  // header label) is left untouched, so the only thing that changes is the
  // name itself.

  function _setRenamingState(el, on) {
    if (!el) return;
    if (on) {
      el.dataset._renOrig = el.innerHTML;
      el.dataset._renOrigTitle = el.title || '';
      const iconEl = el.querySelector(':scope > .session-row-agent-icon');
      if (iconEl) {
        el.innerHTML = '';
        el.appendChild(iconEl);
        el.insertAdjacentHTML('beforeend',
          '<span class="session-row-sep"> </span>' +
          icon('loader-2', { size: '14px', className: 'session-radial-loader' }) +
          ' Renaming\u2026');
      } else {
        el.innerHTML = icon('loader-2', { size: '14px', className: 'session-radial-loader' }) + ' Renaming\u2026';
      }
      el.title = 'Generating name\u2026';
    } else if (el.dataset._renOrig !== undefined) {
      el.innerHTML = el.dataset._renOrig;
      el.title = el.dataset._renOrigTitle;
      delete el.dataset._renOrig;
      delete el.dataset._renOrigTitle;
    }
  }

  // ── Shared "Refreshing…" feedback (dropdown rows) ────────────────────────
  // Mirrors _setRenamingState: swaps the row's session NAME for a spinner +
  // "Refreshing…" while the row re-syncs from the server DB, then restores.
  // Uses its own data keys so it can't collide with a rename snapshot.
  function _setRefreshingState(el, on) {
    if (!el) return;
    if (on) {
      el.dataset._refOrig = el.innerHTML;
      el.dataset._refOrigTitle = el.title || '';
      const iconEl = el.querySelector(':scope > .session-row-agent-icon');
      if (iconEl) {
        el.innerHTML = '';
        el.appendChild(iconEl);
        el.insertAdjacentHTML('beforeend',
          '<span class="session-row-sep"> </span>' +
          icon('loader-2', { size: '14px', className: 'session-radial-loader' }) +
          ' Refreshing\u2026');
      } else {
        el.innerHTML = icon('loader-2', { size: '14px', className: 'session-radial-loader' }) + ' Refreshing\u2026';
      }
      el.title = 'Refreshing from server\u2026';
    } else if (el.dataset._refOrig !== undefined) {
      el.innerHTML = el.dataset._refOrig;
      el.title = el.dataset._refOrigTitle;
      delete el.dataset._refOrig;
      delete el.dataset._refOrigTitle;
    }
  }

  // Set a session name in a title element, keeping any agent icon inside it.
  function _setSessionName(el, name) {
    if (!el) return;
    const iconEl = el.querySelector(':scope > .session-row-agent-icon');
    if (iconEl) {
      el.innerHTML = '';
      el.appendChild(iconEl);
      el.insertAdjacentHTML('beforeend', '<span class="session-row-sep"> </span>');
      el.appendChild(document.createTextNode(name));
    } else {
      el.textContent = name;
    }
  }

  // Drop any saved renaming snapshot (used after _setTriggerLabel re-renders
  // the header label from the cache, so a stale snapshot can't linger).
  function _clearRenamingSaved(el) {
    if (!el) return;
    delete el.dataset._renOrig;
    delete el.dataset._renOrigTitle;
  }

  function handleDeleteAllClick(btn) {
    advanceDeleteBtn(btn, {
      size: '15px', spinSize: '15px',
      onArm: () => _resetArmedDeletes(),
      onConfirm: async () => {
        // Spare pinned and hidden sessions; delete the rest of the visible list.
        const targets = _sessionsCache.filter(s => !s.pinned && !s.hidden).map(s => s.id);
        for (const sid of targets) {
          const result = await deleteSession(sid, { retries: 1 });
          if (!result.ok) {
            console.warn('Delete-all: failed to delete', sid, result.error);
            // Don't abort the batch — log and continue. The surviving row will
            // still be visible after the batch for the user to retry individually.
          }
        }
      },
    });
  }

  function handleDeleteSelectedClick(btn) {
    advanceDeleteBtn(btn, {
      size: '15px', spinSize: '15px',
      onConfirm: async () => {
        const targets = [..._selectedSessions];
        for (const sid of targets) {
          const result = await deleteSession(sid, { retries: 1 });
          if (!result.ok) {
            console.warn('Delete-selected: failed to delete', sid, result.error);
          }
        }
        _clearSelected();
        _setCheckboxMode(false);
        if (app.currentUserId) populateSessionSelect(app.currentUserId);
      },
    });
  }

  // Restore a recycled session from the bin (POST /sessions/{id}/restore),
  // then refetch so the row moves back to the live list (or disappears from a
  // bin-only view). Same endpoint as the Sessions page restore; the current
  // search/bin scope is preserved across the refetch.
  async function _restoreFromBin(sid) {
    if (!sid) return;
    const token = localStorage.getItem('auth_token');
    try {
      let url = `/api/v1/db/sessions/${encodeURIComponent(sid)}/restore?db=user.db`;
      if (token) url += `&token=${encodeURIComponent(token)}`;
      const res = await fetch(apiPath(url), { method: 'POST' });
      if (!res.ok) {
        console.warn('Restore failed for', sid, res.status);
        return;
      }
    } catch (err) {
      console.warn('Restore failed for', sid, err);
      return;
    }
    if (app.currentUserId) {
      await populateSessionSelect(app.currentUserId);
      const q = _getSearchQuery().trim();
      if (q.length >= 2) _scanMessagesForQuery(q);
    }
  }

  // ── Trigger events ──
  // Listen on the DROPDOWN WRAPPER (#session-dropdown), not the trigger button,
  // so clicks work even when CSS applies pointer-events: none to the trigger
  // (before data-loaded is set). Mirrors the agent-name dropdown pattern which
  // listens on .chat-header-name-row.
  let _lpTimer = null, _lpStartX = 0, _lpStartY = 0;
  dropdown.addEventListener('pointerdown', (e) => {
    if (e.target.closest('.header-delete-btn, .header-plus-btn, .session-dropdown-status, .session-needs-input-chip')) return;
    if (e.pointerType === 'mouse' && e.button !== 0) return;
    _lpStartX = e.clientX; _lpStartY = e.clientY;
    _lpTimer = setTimeout(() => {
      _lpTimer = null;
      if (!getSessionDropdownConfig().trigger.rename_on_hold) return;
      e.preventDefault();
      _headerRenameSession();
    }, 500);
  }, { signal: _dropdownEventAbort.signal });
  dropdown.addEventListener('pointermove', (e) => {
    if (!_lpTimer) return;
    if (Math.abs(e.clientX - _lpStartX) > 8 || Math.abs(e.clientY - _lpStartY) > 8) {
      clearTimeout(_lpTimer); _lpTimer = null;
    }
  }, { signal: _dropdownEventAbort.signal });
  dropdown.addEventListener('pointerup', () => { if (_lpTimer) { clearTimeout(_lpTimer); _lpTimer = null; } }, { signal: _dropdownEventAbort.signal });
  dropdown.addEventListener('pointercancel', () => { if (_lpTimer) { clearTimeout(_lpTimer); _lpTimer = null; } }, { signal: _dropdownEventAbort.signal });
  dropdown.addEventListener('click', (e) => {
    if (e.target.closest('.header-delete-btn, .header-plus-btn')) return;
    // A rename input may have just replaced the label (via long-press), so
    // e.target could be the old detached span — check the DOM for any input.
    // The menu may be body-attached during open, so check both dropdown and menu.
    if (e.target.closest('.session-row-title-input') || dropdown.querySelector('.session-row-title-input') || menu.querySelector('.session-row-title-input')) return;
    // Kebab / delete inside the trigger: don't toggle the dropdown menu.
    if (e.target.closest('#session-dropdown-kebab, #session-dropdown-delete')) {
      e.stopPropagation();
      return;
    }
    // The collapsed title bar carries the current session's own flag CHIP
    // (list.js _setTriggerLabel). Its click belongs to the chip's three-stage
    // cycle — note → armed → confirm — exactly as in the open menu (the menu's
    // own listener further down), so it is consumed here BEFORE the toggle: the
    // chip is a special condition that acts on the session, while every other
    // click in the bar still opens/closes the menu.
    const titleChip = e.target.closest('.session-needs-input-chip');
    if (titleChip) {
      e.stopPropagation();
      e.stopImmediatePropagation();
      const chipSid = titleChip.dataset.sessionId;
      if (chipSid) {
        // Same verb contract as the menu chip: the coloured unread prompt marks
        // the session read, the grey read-but-unfinished chip puts it back to
        // unread so the coloured flag returns.
        const toUnread = titleChip.dataset.needsInputVerb === 'unread';
        void activateNeedsInputChip(titleChip, () => (
          toUnread ? markSessionUnread(titleChip, chipSid) : markSessionRead(titleChip, chipSid)
        ));
      }
      return;
    }
    e.stopPropagation();
    // Ignore rapid double-clicks and clicks during animation.
    const now = Date.now();
    // The title row is a true toggle.  Prefer the menu's explicit lifecycle
    // state over its measured height: during an opening transition the latter
    // can be transiently zero even though the menu is visibly present.
    const menuIsOpen = !menu.hidden
      && (menu.dataset.state === 'opening' || menu.dataset.state === 'open');
    if (menuIsOpen) {
      _lastToggleTime = Date.now();
      closeMenu();
      if (e.detail > 0) e.target.closest('.session-dropdown-trigger')?.blur();
      return;
    }
    if (now - _lastToggleTime < 350 || menu.dataset.state === 'closing') return;
    _lastToggleTime = now;
    // DEBUG-TAG: click → open/close trail for the session drop-down
    console.log('DEBUG-TAG: [session-dropdown] click', { open: true, state: menu.dataset.state });
    _dismissKeyboardBeforeOpen();
    openMenu();
    // Pointer clicks leave the button/span focused in some browsers, whose
    // native focus ring makes this compact control appear to grow and shift.
    // Drop pointer-acquired focus after toggling; keyboard focus is preserved.
    if (e.detail > 0) {
      e.target.closest('.session-dropdown-trigger')?.blur();
    }
  }, { signal: _dropdownEventAbort.signal });
  dropdown.addEventListener('keydown', (e) => {
    if (e.key === 'Enter' || e.key === ' ') {
      // Don't toggle if the focus is on the kebab, delete, or a rename input.
      // The title bar's flag CHIP is the same: Enter/Space must reach the chip
      // BUTTON's own activation (which the click handler above routes into the
      // shared cycle), not open the menu. The menu's chips already answer the
      // keyboard this way — that listener only intercepts the search input —
      // so this guard is what keeps the two surfaces identical for a user who
      // never touches the mouse.
      if (e.target.closest('#session-dropdown-kebab, #session-dropdown-delete, .session-row-title-input, .session-needs-input-chip')) return;
      e.preventDefault();
      const menuIsOpen = !menu.hidden
        && (menu.dataset.state === 'opening' || menu.dataset.state === 'open');
      if (menuIsOpen) closeMenu();
      else if (menu.dataset.state !== 'closing') openMenu();
    }
  }, { signal: _dropdownEventAbort.signal });
  // ── Trigger kebab: open the row-actions popup (pin/unpin) ──────────
  // Prefer the ids from the static/element markup; fall back to the kebab
  // inside the trigger (legacy chat-controls markup has no ids).
  const triggerKebab = root.querySelector('#session-dropdown-kebab')
    || root.querySelector(':scope > .session-dropdown-trigger .session-row-kebab');
  const triggerDelete = root.querySelector('#session-dropdown-delete')
    || root.querySelector(':scope > .session-dropdown-trigger .session-row-delete');
  if (triggerKebab) {
    triggerKebab.addEventListener('click', (e) => {
      e.stopPropagation();
      if (!getSessionDropdownConfig().trigger.row_actions) return;
      const sid = triggerKebab.dataset.id || app.currentSessionId;
      const open = document.querySelector('.session-row-actions[data-source="session-list"]');
      if (open && open.dataset.id === sid) { _closeRowActions(); return; }
      _closeRowActions();
      if (sid) _openRowActions(sid, triggerKebab);
    });
  }
  if (triggerDelete) {
    triggerDelete.addEventListener('click', (e) => {
      e.stopPropagation();
      if (!getSessionDropdownConfig().trigger.delete) return;
      const sid = triggerDelete.dataset.id || app.currentSessionId;
      if (sid) handleDeleteClick(triggerDelete, sid);
    });
  }

  // ── Per-row "more" (⋯) popup: pin/unpin toggle ─────────────────────────────
  // Body-mounted floating menu anchored under the kebab. Mirrors the genui page
  // kebab popup (.session-row-actions / .session-row-action). The row's
  // Agent / Session id (compact, click-to-copy the full id — same helper as the
  // chat header more menu) sits at the BOTTOM, under the actions; the two-click
  // delete stays on the row's own trash button.
  let _rowActionsEl = null;
  function _closeRowActions() {
    if (_rowActionsEl) { _rowActionsEl.remove(); _rowActionsEl = null; }
    document.removeEventListener('pointerdown', _onRowActionsOutside, true);
    document.removeEventListener('keydown', _onRowActionsEsc, true);
    window.removeEventListener('resize', _closeRowActions);
  }
  function _onRowActionsOutside(e) {
    if (_rowActionsEl && !_rowActionsEl.contains(e.target) && !e.target.closest('.session-row-kebab')) {
      _closeRowActions();
    }
  }
  function _onRowActionsEsc(e) { if (e.key === 'Escape') _closeRowActions(); }
  function _openRowActions(sid, kebabBtn) {
    const menuCfg = getSessionDropdownConfig().menu;
    if (!menuCfg.row_actions) return;
    const sess = _sessionsCache.find(s => s.id === sid);
    const pinned = !!(sess && sess.pinned);
    const isRead = _isSessionRead(sess);
    // Keep the persisted read timestamp visible even after newer activity makes
    // the session unread. It tells the user exactly when they last caught up.
    const lastSeen = sess?.read_at ? _formatRelativeTime(sess.read_at) : '';
    const readActions =
      `<div class="session-row-action session-row-read-info" role="status">${icon('clock-3', { size: '14px' })} Last seen${lastSeen ? ` ${lastSeen}` : ' never'}</div>` +
      (isRead
        ? `<button class="session-row-action" data-action="mark-unread">${icon('mail-open', { size: '14px' })} Mark unread</button>`
        : `<button class="session-row-action" data-action="mark-read">${icon('check-circle', { size: '14px' })} Mark read</button>`);
    const row = kebabBtn.closest('.session-row');
    const popup = document.createElement('div');
    popup.className = 'session-row-actions';
    popup.dataset.id = sid;
    popup.dataset.source = 'session-list';
    // Always-on display switch — a REAL checkbox switch (reusing the shared
    // .conn-toggle from app3.css) because the user reads it as a switch, not an
    // action. It flips the per-device override of the app-wide App Functions
    // setting and drives the shared wake lock immediately. When the browser
    // cannot hold a wake lock at all (needs https + a supported browser) the row
    // is inert and says so, rather than pretending to work. See screen-wake.js.
    const _aodSupported = isWakeLockSupported();
    const alwaysOnRow = menuCfg.always_on_display
      ? `<label class="session-row-action session-row-toggle${_aodSupported ? '' : ' disabled'}"`
        + ` data-action="always-on" title="${_aodSupported
            ? 'Keep this device\u2019s screen on while the app is open'
            : 'This browser cannot keep the screen on \u2014 needs https and a supported browser'}">`
        + `${icon('sun-moon', { size: '14px' })}<span class="session-row-toggle-label">Always-on display</span>`
        + (_aodSupported
            ? `<span class="conn-toggle-wrap"><input type="checkbox" class="conn-toggle"${isAlwaysOnEnabled() ? ' checked' : ''}><span class="conn-toggle-track"></span></span>`
            : `<span class="session-row-action-tag">Not supported</span>`)
        + `</label>`
      : '';
    popup.innerHTML =
      (menuCfg.pinning ? `<button class="session-row-action" data-action="pin">${icon(pinned ? 'pin-off' : 'pin', { size: '14px' })} ${pinned ? 'Unpin session' : 'Pin session'}</button>` : '') +
      (menuCfg.auto_rename ? `<button class="session-row-action" data-action="auto-rename">${icon('wand-2', { size: '14px' })} Auto rename</button>` : '') +
      (menuCfg.rename ? `<button class="session-row-action" data-action="rename">${icon('pencil', { size: '14px' })} Rename session</button>` : '') +
      (menuCfg.open_in_widget ? `<button class="session-row-action" data-action="open-in-widget">${icon('messages-square', { size: '14px' })} Open in widget</button>` : '') +
      `<button class="session-row-action" data-action="refresh">${icon('refresh-cw', { size: '14px' })} Refresh session</button>` +
      (menuCfg.start_scratchpad ? `<button class="session-row-action" data-action="start-scratchpad">${icon('notepad-text', { size: '14px' })} Start a scratchpad</button>` : '') +
      readActions +
      alwaysOnRow +
      (menuCfg.delete ? `<button class="session-row-action danger" data-action="delete" title="Delete session">${_menuDeleteRestHtml()}</button>` : '');
    // Always-on display: bind on `change`, not the shared click handler. A
    // <label> wrapping the checkbox toggles the box itself, so read the box's
    // settled state here and push it through the one setter that both persists
    // the per-device preference and drives the wake lock. The popup stays open
    // so the user sees the switch land; the row is inert when the browser
    // cannot hold a lock (see screen-wake.js).
    const _aodBox = popup.querySelector('.session-row-toggle .conn-toggle');
    if (_aodBox) {
      _aodBox.addEventListener('change', (e) => {
        e.stopPropagation();
        setAlwaysOnDisplay(_aodBox.checked);
      });
    }
    // Agent / Session id rows — compact "A: <first8>" / "S: <first8>" at the
    // bottom of the popup, laid out like the action buttons above (label at
    // the icon column, id left-aligned at the description column — one
    // continuous string, no pill container). Click copies the FULL id; the
    // confirmation shows the full word ("Agent ID copied ✓"), mirroring the
    // chat header more menu (session-init.js).
    const info = document.createElement('div');
    info.className = 'session-row-actions-info';
    const agentId = (sess && sess.agent_id) || app.currentAgentId || '';
    const sessionId = sid || '';
    const mkIdRow = (label, fullLabel, v) => {
      const r = document.createElement('div');
      r.className = 'session-row-action session-row-id';
      r.title = `Copy ${fullLabel} id`;
      r.tabIndex = 0;
      const key = document.createElement('span');
      key.className = 'session-row-id-key';
      key.textContent = label;
      const val = document.createElement('span');
      val.className = 'session-row-id-val';
      val.textContent = v ? v.slice(0, 8) : '—';
      r.append(key, val);

      // Click-to-copy the FULL id value with green check feedback
      r.addEventListener('click', (e) => {
        e.stopPropagation();
        copyText(v).then(() => {
          r.classList.add('copied');
          const orig = val.textContent;
          val.textContent = `${fullLabel} ID copied \u2713`;
          setTimeout(() => {
            r.classList.remove('copied');
            val.textContent = orig;
          }, 1200);
        }).catch(() => {});
      });

      return r;
    };
    if (menuCfg.agent_id) info.appendChild(mkIdRow('A:', 'Agent', agentId));
    if (menuCfg.session_id) info.appendChild(mkIdRow('S:', 'Session', sessionId));
    // Headless engine session id (Codex thread / Claude session) — the id to
    // recall inside the CLI (`codex exec resume <id>` / `claude --resume <id>`)
    // when the headless run misbehaves. Comes from the sessions API's
    // engine_thread_id (sessions.metadata codex_thread_id / claude_session_id).
    const engThreadId = (sess && sess.engine_thread_id) || '';
    const engLabel = (sess && sess.agent_engine === 'codex') ? 'Codex'
      : ((sess && sess.agent_engine === 'claude_code') ? 'Claude' : '');
    if (menuCfg.engine_thread_id && engThreadId && engLabel) {
      info.append(mkIdRow(engLabel + ':', engLabel, engThreadId));
    }
    if (info.childElementCount) popup.appendChild(info);
    document.body.appendChild(popup);
    // Position under the kebab, right-aligned, clamped to the VISUAL viewport.
    // The shared helper flips above only when it fits, never lets the panel
    // leave the top edge, and caps the height — so a short or keyboard-shrunk
    // viewport can't push the panel out of view. Keep in lockstep with the
    // Sessions-page copy (DESIGN SYNC CONTRACT).
    positionKebabPopup(popup, kebabBtn);
    popup.addEventListener('click', (e) => {
      e.stopPropagation();
      const actionBtn = e.target.closest('.session-row-action');
      if (!actionBtn) return;
      if (actionBtn.dataset.action === 'pin') { togglePin(sid); _closeRowActions(); }
      else if (actionBtn.dataset.action === 'auto-rename') {
        _closeRowActions();
        console.log('[auto-rename] clicked for session', sid);
        // Commonized feedback: the header trigger and the dropdown rows share
        // one look — the agent icon stays put and the session NAME is swapped
        // for a spinner + "Renaming…" in both places.
        const titleSpan = row && row.querySelector('.session-row-title');
        const origText = titleSpan ? (titleSpan.textContent || '').trim() : '';
        const dropdown = document.getElementById('session-dropdown');
        const headerLabel = document.getElementById('session-dropdown-label');
        const isCurrent = sid === app.currentSessionId;
        _setRenamingState(titleSpan, true);
        if (isCurrent) {
          if (dropdown) dropdown.dataset.titling = 'true';
          _setRenamingState(headerLabel, true);
        }
        autoRenameSession(sid).then((r) => {
          const newTitle = r && r.title ? String(r.title).trim() : '';
          // Success only if the server produced a NEW name — an unchanged title
          // (the LLM returned nothing) is a failure, not a silent no-op.
          const changed = r.ok && newTitle && newTitle !== origText;
          if (changed) {
            // Swap in the new name immediately (icon kept) — don't wait for
            // the next populateSessionSelect cycle.
            if (titleSpan) {
              _setRenamingState(titleSpan, false);
              _setSessionName(titleSpan, newTitle);
              titleSpan.title = 'Hold to rename';
            }
            // Also update the cache entry so the header label + any other
            // view sees it on the next render.
            const sess = _sessionsCache.find(s => s.id === sid);
            if (sess) sess.title = newTitle;
            if (isCurrent) {
              if (dropdown) delete dropdown.dataset.titling;
              _clearRenamingSaved(headerLabel);
              _setTriggerLabel();
            }
          } else {
            console.log('[auto-rename] failed — result:', r);
            // Restore the original title, then flash the error briefly in its
            // place (~1s) before the old name comes back.
            if (titleSpan) _setRenamingState(titleSpan, false);
            if (isCurrent) {
              if (dropdown) delete dropdown.dataset.titling;
              _clearRenamingSaved(headerLabel);
              _setTriggerLabel();
            }
            if (row) {
              const msg = r.message || r.error || (newTitle ? 'Name unchanged' : 'Could not generate a name');
              _showRowError(row, msg, 1200);
            }
          }
        }).catch((_err) => {
          if (titleSpan) _setRenamingState(titleSpan, false);
          if (isCurrent) {
            if (dropdown) delete dropdown.dataset.titling;
            _clearRenamingSaved(headerLabel);
            _setTriggerLabel();
          }
          if (row) _showRowError(row, 'Auto rename failed', 1200);
        });
      }
      else if (actionBtn.dataset.action === 'rename') { if (row) { _closeRowActions(); startRename(sid, row); } else { _headerRenameSession(); _closeRowActions(); } }
      else if (actionBtn.dataset.action === 'open-in-widget') {
        const session = _sessionsCache.find(s => s.id === sid);
        if (!session) return;
        createChatWidget({
          title: session.title || 'Session',
          iconName: 'messages-square',
          agentId: session.agent_id || app.currentAgentId || null,
          sessionId: session.id,
          hydrateExistingSession: true,
        }).open();
        _closeRowActions();
      }
      else if (actionBtn.dataset.action === 'refresh') { _closeRowActions(); _refreshSession(sid, row); }
      else if (actionBtn.dataset.action === 'start-scratchpad') {
        _closeRowActions();
        // Close the dropdown too: this action exists to reveal a NEW header tab,
        // and the open menu sits on top of both the tab strip and the panel, so
        // leaving it up would hide the very thing the click just created.
        if (typeof closeMenu === 'function') closeMenu();
        startScratchpad(sid);
      }
      else if (actionBtn.dataset.action === 'mark-unread') {
        markSessionUnread(actionBtn, sid).then((changed) => { if (changed) _closeRowActions(); });
      }
      else if (actionBtn.dataset.action === 'mark-read') {
        markSessionRead(actionBtn, sid).then((changed) => { if (changed) _closeRowActions(); });
      }
      // Two-click delete (SHARED-DELETE-CONTROL): the first click only ARMS this
      // row, so the panel must stay open for the confirm click to land on it.
      else if (actionBtn.dataset.action === 'delete') { handleMenuDeleteClick(actionBtn, sid, row); }
    });
    _rowActionsEl = popup;
    // Defer so the click that opened the popup doesn't immediately close it.
    setTimeout(() => {
      document.addEventListener('pointerdown', _onRowActionsOutside, true);
      document.addEventListener('keydown', _onRowActionsEsc, true);
      window.addEventListener('resize', _closeRowActions);
    }, 0);
  }

  // ── Start a scratchpad ───────────────────────────────────────────────────
  // Creates this session's Scratchpad document — a user-owned set of notes, each
  // row individually shared with the agent — if it does not exist yet, then
  // reveals its header tab. The component id is stable, so a second click
  // reopens the same scratchpad instead of creating a duplicate.
  async function startScratchpad(sid) {
    if (!sid) return;
    const isCurrent = sid === app.currentSessionId;
    try {
      const listUrl = apiPath('/api/v1/chat/components?session_id=' + encodeURIComponent(sid));
      const resp = await fetch(listUrl, { headers: authHeaders() });
      const existing = resp.ok
        ? ((await resp.json()).components || []).find(c => c.id === 'scratchpad')
        : null;
      if (!existing) {
        const create = await fetch(listUrl, {
          method: 'POST',
          headers: { 'Content-Type': 'application/json', ...authHeaders() },
          body: JSON.stringify({ component: {
            id: 'scratchpad', type: 'scratchpad', title: 'Scratchpad', placement: 'sticky',
            data: { rows: [{ text: '', shared: false }] },
          } }),
        });
        if (!create.ok) throw new Error('HTTP ' + create.status);
      }
      if (isCurrent) {
        // Reload the panel from the server (renders the Scratchpad card and
        // fires the chip refresh via chat-components-changed), then open it.
        if (typeof app.loadChatComponents === 'function') await app.loadChatComponents(sid);
        if (typeof app.componentPanelShow === 'function') app.componentPanelShow('scratchpad');
      }
    } catch (err) {
      console.warn('Start a scratchpad failed:', err);
    }
  }

  // ── Per-row Refresh: re-sync THIS session from the server DB ─────────────
  // 1) Pull the authoritative session metadata into the local mirror and
  //    repaint the dropdown cache/list (refreshSessionMetadata → its internal
  //    populateSessionSelect refills _sessionsCache, re-renders the rows and
  //    refreshes the header trigger label). 2) If this session's transcript is
  //    currently open in the chat panel, force a clean reload from the DB
  //    (refreshTranscript(true) drops the message cache and re-fetches, so the
  //    visible chat matches the server — same path as the header refresh
  //    button). The row title shows a spinner + "Refreshing…" while in flight.
  async function _refreshSession(sid, row) {
    const titleSpan = row && row.querySelector('.session-row-title');
    _setRefreshingState(titleSpan, true);
    try {
      if (app.currentUserId) {
        await refreshSessionMetadata(app.currentUserId);
      }
      if (sid === app.currentSessionId && typeof app.refreshTranscript === 'function') {
        await app.refreshTranscript(true);
      }
      _setRefreshingState(titleSpan, false);
    } catch (err) {
      console.warn('Session refresh failed:', err);
      _setRefreshingState(titleSpan, false);
      if (row && row.isConnected) _showRowError(row, 'Refresh failed', 1500);
    }
  }

  async function markSessionRead(button, sessionId) {
    if (!button || !sessionId || button.dataset.pending === '1') return false;
    button.dataset.pending = '1';
    button.disabled = true;
    try {
      if (await setNotificationUnread(sessionId,false)) {
        window.dispatchEvent(new CustomEvent('session:visited', {
          detail: { sessionId, visitedAt: new Date().toISOString() },
        }));
        return true;
      }
      const response = await fetch(apiPath(`/api/v1/db/sessions/${encodeURIComponent(sessionId)}/read?db=user.db&user_id=${encodeURIComponent(app.currentUserId || '')}`), {
        method: 'POST',
        headers: authHeaders(),
      });
      if (!response.ok) throw new Error(`HTTP ${response.status}`);
      const payload = await response.json().catch(() => ({}));
      window.dispatchEvent(new CustomEvent('session:visited', {
        detail: { sessionId, visitedAt: payload.read_at || new Date().toISOString() },
      }));
      return true;
    } catch (error) {
      console.warn('Mark read failed:', error);
      const row = button.closest('.session-row');
      if (row && row.isConnected) _showRowError(row, 'Could not mark read', 1800);
      else button.title = 'Could not mark read';
      return false;
    } finally {
      if (button.isConnected) {
        button.disabled = false;
        delete button.dataset.pending;
      }
    }
  }

  async function markSessionUnread(button, sessionId) {
    if (!button || !sessionId || button.dataset.pending === '1') return false;
    button.dataset.pending = '1';
    button.disabled = true;
    try {
      if (await setNotificationUnread(sessionId,true)) {
        window.dispatchEvent(new CustomEvent('session:unread', {
          detail: { sessionId, unreadAt: new Date().toISOString() },
        }));
        return true;
      }
      const response = await fetch(apiPath(`/api/v1/db/sessions/${encodeURIComponent(sessionId)}/unread?db=user.db&user_id=${encodeURIComponent(app.currentUserId || '')}`), {
        method: 'POST',
        headers: authHeaders(),
      });
      if (!response.ok) throw new Error(`HTTP ${response.status}`);
      const payload = await response.json().catch(() => ({}));
      const readAt = _sessionsCache.find((session) => session.id === sessionId)?.read_at || null;
      window.dispatchEvent(new CustomEvent('session:unread', {
        detail: { sessionId, readAt, unreadAt: payload.unread_at || new Date().toISOString() },
      }));
      return true;
    } catch (error) {
      console.warn('Mark unread failed:', error);
      const row = button.closest('.session-row');
      if (row && row.isConnected) _showRowError(row, 'Could not mark unread', 1800);
      else button.title = 'Could not mark unread';
      return false;
    } finally {
      if (button.isConnected) {
        button.disabled = false;
        delete button.dataset.pending;
      }
    }
  }

  if (menu) {
    // Remember the row chosen on pointer-down. Background session/Portal
    // refreshes rebuild this menu; without this guard a rebuild between down
    // and click can leave a different row under the pointer and open the wrong
    // conversation.
    menu.addEventListener('pointerdown', (e) => {
      const row = e.target.closest('.session-row');
      menu.dataset.pointerSessionId = row?.dataset.id || '';

      // Rows are rebuilt by cache/server refreshes. Handle ⋯ on pointer-down so
      // the action survives a refresh that detaches the button before its click
      // can bubble to this delegated listener. Mark this physical button so its
      // follow-up click only consumes the duplicate; keyboard clicks still use
      // the click path below.
      const kebabBtn = e.target.closest('.session-row-kebab');
      if (kebabBtn && (e.pointerType !== 'mouse' || e.button === 0)) {
        kebabBtn.dataset.pointerHandled = '1';
        const sid = kebabBtn.dataset.id;
        const open = document.querySelector('.session-row-actions[data-source="session-list"]');
        if (open && open.dataset.id === sid) _closeRowActions();
        else {
          _closeRowActions();
          if (sid) _openRowActions(sid, kebabBtn);
        }
        e.preventDefault();
        e.stopPropagation();
        return;
      }

      // Handle avatar disclosure at pointer-down, matching the kebab's
      // rebuild-safe path. This prevents the global click-away listener from
      // closing the dropdown before a parent family can expand.
      const avatarToggle = e.target.closest('.session-row-agent-toggle');
      if (avatarToggle && (e.pointerType !== 'mouse' || e.button === 0)) {
        // The tree render replaces the button synchronously. Keep this marker on
        // the persistent menu rather than the detached button so its matching
        // synthetic click cannot toggle the freshly rendered disclosure again.
        menu.dataset.pointerGroupId = avatarToggle.dataset.id || '';
        menu.dataset.pointerGroupAt = String(Date.now());
        toggleSessionGroup(avatarToggle.dataset.id);
        e.preventDefault();
        e.stopPropagation();
        return;
      }
    });
    // Middle-click opens the conversation independently, without selecting it
    // or closing this menu. Row controls retain their own action semantics.
    menu.addEventListener('auxclick', (e) => {
      if (e.button !== 1) return;
      const pointerSessionId = menu.dataset.pointerSessionId || '';
      delete menu.dataset.pointerSessionId;
      if (e.target.closest('button, input, select, textarea, [role="button"], [contenteditable="true"], .session-needs-input-chip')) return;
      const row = e.target.closest('.session-row[data-id]');
      if (!row || row.classList.contains('recycled')) return;
      if (pointerSessionId && pointerSessionId !== row.dataset.id) return;
      const sid = row.dataset.id;
      if (!sid) return;
      const session = _sessionsCache.find(s => s.id === sid);
      const agentId = row.dataset.agent || session?.agent_id || app.currentAgentId;
      const url = new URL(window.location.pathname, window.location.origin);
      url.searchParams.set('tab', 'agents');
      if (agentId) url.searchParams.set('agent', agentId);
      url.searchParams.set('session', sid);
      e.preventDefault();
      e.stopPropagation();
      window.open(url.href, '_blank', 'noopener');
    });
    menu.addEventListener('click', (e) => {
      e.stopPropagation();
      const needsInputChip = e.target.closest('.session-needs-input-chip');
      if (needsInputChip) {
        // A portaled menu can outlive a remount briefly; no second delegated
        // listener may consume this physical click as an immediate confirm.
        e.stopImmediatePropagation();
        const sid = needsInputChip.dataset.sessionId;
        if (sid) {
          // The chip's verb rides in its own dataset (shared chip module,
          // ui/shared/js/needs-input-chip.js): the COLOURED unread prompt marks
          // the session read, while the GREY read-but-unfinished chip puts it
          // back to unread so the coloured flag (and the notification row)
          // return. Read from the chip, not from a local flag, so a re-render
          // between the two clicks cannot flip the meaning of the second one.
          const toUnread = needsInputChip.dataset.needsInputVerb === 'unread';
          void activateNeedsInputChip(needsInputChip, () => (
            toUnread ? markSessionUnread(needsInputChip, sid) : markSessionRead(needsInputChip, sid)
          ));
        }
        return;
      }
      const pointerSessionId = menu.dataset.pointerSessionId || '';
      delete menu.dataset.pointerSessionId;
      // Opt-in per-row trash (menu.row_delete, off by default). The trigger
      // chip's own trash (#session-dropdown-delete) is wired separately below.
      const delBtn = e.target.closest('.session-row-delete');
      if (delBtn) {
        const row = delBtn.closest('.session-row');
        const sid = row && row.dataset.id;
        if (sid) handleDeleteClick(delBtn, sid);
        return;
      }
      // Per-row "more" (⋯) kebab — opens a small popup with the pin/unpin toggle.
      // A second click on the same kebab closes it (toggle behaviour).
      const kebabBtn = e.target.closest('.session-row-kebab');
      if (kebabBtn) {
        // Pointer activation was handled above to survive an async row rebuild.
        // Consume its synthetic click; a keyboard click has no marker and still
        // opens the same action menu through this path.
        if (kebabBtn.dataset.pointerHandled === '1') {
          delete kebabBtn.dataset.pointerHandled;
          return;
        }
        const sid = kebabBtn.dataset.id;
        const open = document.querySelector('.session-row-actions[data-source="session-list"]');
        if (open && open.dataset.id === sid) { _closeRowActions(); return; }
        _closeRowActions();
        if (sid) _openRowActions(sid, kebabBtn);
        return;
      }
      // Footer: toggle show-hidden (manage mode) — reveals hidden rows and
      // swaps row trash buttons for eye toggles.
      const eyeBtn = e.target.closest('.session-manage-eye');
      if (eyeBtn) {
        _setShowHidden(!_getShowHidden());
        if (app.currentUserId) populateSessionSelect(app.currentUserId);
        return;
      }
      // Footer: checkbox mode toggle — adds checkboxes to each row for batch delete.
      const checkboxToggle = e.target.closest('.session-manage-checkbox-toggle');
      if (checkboxToggle) {
        _setCheckboxMode(!_getCheckboxMode());
        _clearSelected();
        if (app.currentUserId) populateSessionSelect(app.currentUserId);
        return;
      }
      // Footer: delete selected sessions (two-click confirm).
      const delSelectedBtn = e.target.closest('.session-manage-delete-selected');
      if (delSelectedBtn) {
        handleDeleteSelectedClick(delSelectedBtn);
        return;
      }
      // Footer: recycle-bin toggle — narrows the list to binned sessions.
      const binBtn = e.target.closest('.session-manage-bin');
      if (binBtn) {
        _setBinMode(!_getBinMode());
        if (app.currentUserId) populateSessionSelect(app.currentUserId);
        return;
      }
      // Footer: FLAG chip — narrows the list to sessions that carry a status
      // flag, hiding the idle ones. Same predicate the row chips use; the
      // pressed state is re-interpolated by the re-render below.
      const flagFilterBtn = e.target.closest('.session-manage-flag-filter');
      if (flagFilterBtn) {
        _setFlagFilter(!_getFlagFilter());
        if (app.currentUserId) populateSessionSelect(app.currentUserId);
        return;
      }
      // Footer: clear-search button (×) — empties the query and refetches.
      const clearBtn = e.target.closest('.session-manage-search-clear');
      if (clearBtn) {
        _setSearchQuery('');
        _clearMsgSearch();
        const input = menu.querySelector('.session-manage-search-input');
        if (input) { input.value = ''; input.focus(); }
        if (app.currentUserId) populateSessionSelect(app.currentUserId);
        return;
      }
      // Bin badge on a recycled row — two-click restore (mirrors the Sessions
      // page chip): first click arms ("Restore?"), second restores. Caught
      // before the row-switch handler so clicking it never opens the session.
      const binChip = e.target.closest('.session-row-bin');
      if (binChip) {
        const sid = binChip.dataset.id;
        if (!sid) return;
        if (!binChip.classList.contains('armed')) {
          binChip.classList.add('armed');
          binChip.textContent = 'Restore?';
          binChip.title = 'Click again to restore this session';
          clearTimeout(binChip._disarmTimer);
          binChip._disarmTimer = setTimeout(() => {
            binChip.classList.remove('armed');
            binChip.textContent = 'bin';
            binChip.title = 'In recycling bin — click to restore';
          }, 3000);
          return;
        }
        clearTimeout(binChip._disarmTimer);
        binChip.classList.remove('armed');
        binChip.textContent = 'bin';
        binChip.title = 'In recycling bin — click to restore';
        _restoreFromBin(sid);
        return;
      }
      // Per-row visibility (eye) toggle in manage mode.
      const visBtn = e.target.closest('.session-row-visibility');
      if (visBtn) {
        const sid = visBtn.dataset.id;
        if (sid) toggleHidden(sid);
        return;
      }
      // Per-row checkbox toggle in checkbox mode.
      const rowCheckbox = e.target.closest('.session-row-checkbox');
      if (rowCheckbox) {
        const sid = rowCheckbox.dataset.id;
        if (_selectedSessions.has(sid)) {
          _selectedSessions.delete(sid);
        } else {
          _selectedSessions.add(sid);
        }
        _renderSessionRows();
        return;
      }
      // The parent agent avatar is the family disclosure control. Caught before
      // row selection so expanding the tree never opens the parent session.
      const expandBtn = e.target.closest('.session-row-agent-toggle');
      if (expandBtn) {
        // Pointer activation toggled the group above. Consume only its matching
        // follow-up click; keyboard activation never sets this menu-level marker.
        const pointerAge = Date.now() - Number(menu.dataset.pointerGroupAt || 0);
        if (menu.dataset.pointerGroupId === expandBtn.dataset.id && pointerAge < 1000) {
          delete menu.dataset.pointerGroupId;
          delete menu.dataset.pointerGroupAt;
          return;
        }
        // A synchronously replaced button may not deliver its synthetic click
        // to the menu. Do not let that stale marker consume a later activation.
        delete menu.dataset.pointerGroupId;
        delete menu.dataset.pointerGroupAt;
        toggleSessionGroup(expandBtn.dataset.id);
        return;
      }
      if (e.target.closest('.session-row-title-input')) return;
      // In checkbox mode, clicking a row toggles its checkbox instead of switching.
      const row = e.target.closest('.session-row');
      if (row) {
        // Recycled rows are restore-only (via their bin badge) — never switch
        // into a binned session from the dropdown.
        if (row.classList.contains('recycled')) return;
        if (_getCheckboxMode()) {
          const sid = row.dataset.id;
          if (_selectedSessions.has(sid)) {
            _selectedSessions.delete(sid);
          } else {
            _selectedSessions.add(sid);
          }
          _renderSessionRows();
          return;
        }
        switchToSession(pointerSessionId || row.dataset.id); closeMenu();
      }
    });
    // Search bar (footer): debounced input → refetch + content scan. The menu
    // is re-rendered wholesale on every poll/refetch, so listen on the menu
    // container and look up the live input each time. The debounce lives in
    // list.js so any query change (including clearing via ×/Escape) cancels a
    // pending commit — a stale timer must never resurrect an old query after
    // the user cleared the box.
    menu.addEventListener('input', (e) => {
      const inp = e.target.closest('.session-manage-search-input');
      if (!inp) return;
      _scheduleSearchCommit(inp.value);
    });
    // Typing inside the search field must not toggle the menu (the dropdown
    // wrapper's Enter/Space handler never sees it — the menu is body-attached —
    // but the document Escape handler would close it). Escape clears the query
    // first, then closes on a second press.
    menu.addEventListener('keydown', (e) => {
      const inp = e.target.closest('.session-manage-search-input');
      if (!inp) return;
      e.stopPropagation();
      if (e.key === 'Escape') {
        if (inp.value) {
          inp.value = '';
          _setSearchQuery('');
          _clearMsgSearch();
          if (app.currentUserId) populateSessionSelect(app.currentUserId);
        } else {
          closeMenu();
        }
      }
    });
    makeRowsReorderable(menu, {
      // Binned (recycled) rows are restore-only — they sort by activity in the
      // bin, so they must not be draggable/reorderable like pinned rows.
      rowSelector: '.session-row.pinned:not(.session-child-row):not(.recycled)',
      handleSelector: '.session-row.pinned:not(.session-child-row):not(.recycled)',
      ignoreSelector: '.session-row-delete, .session-row-kebab, .session-row-title-input, .session-row-visibility, .session-row-agent-toggle, .session-row-checkbox',
      onReorder: async (orderedIds) => {
        if (window.__webagentOfflineReadOnly === true) {
          await populateSessionSelect(app.currentUserId);
          return;
        }
        if (!getSessionDropdownConfig().menu.pinning) return;
        orderedIds.forEach((id, index) => {
          const session = _sessionsCache.find(s => s.id === id);
          if (session) session.sort_order = index;
        });
        _renderSessionRows();
        _setTriggerLabel();
        try {
          await storageAdapter.reorderSessions(app.currentUserId, orderedIds);
        } catch (e) {
          console.warn('Failed to persist pinned session order:', e);
          // The optimistic DOM order is no longer trustworthy. Reload the local
          // source of truth (which is also the hybrid mirror) instead of leaving
          // a layout that silently snaps back on the next poll/restart.
          await populateSessionSelect(app.currentUserId);
        }
      },
    });
    attachRowLongPress(menu, {
      rowSelector: '.session-row:not(.session-child-row)',
      ignoreSelector: '.session-row-delete, .session-row-kebab, .session-row-title-input',
      // Only a long-press ON THE SESSION NAME opens rename — not the pin icon,
      // status dot, caret, or empty row gaps.
      requireSelector: '.session-row-title',
      onLongPress: (sid, row) => {
        if (window.__webagentOfflineReadOnly === true) return;
        if (getSessionDropdownConfig().menu.rename) startRename(sid, row);
      },
    });
  }

  // Guard: a rename input (from long-press) replaces its span with replaceWith(),
  // detaching the old span from the DOM. The click/focusin event that follows can
  // carry the detached span as e.target, which fails Node.contains(). Check the DOM.
  function _hasActiveRenameInput() {
    return !!(dropdown.querySelector('.session-row-title-input') || menu.querySelector('.session-row-title-input'));
  }

  let _repositionFrame = 0;
  const _scheduleOpenMenuReposition = () => {
    if (menu.hidden || menu.dataset.state === 'closing' || _repositionFrame) return;
    _repositionFrame = requestAnimationFrame(() => {
      _repositionFrame = 0;
      if (!menu.hidden && menu.dataset.state !== 'closing') _positionOpenMenu();
    });
  };
  const _visualViewport = window.visualViewport;
  window.addEventListener('resize', _scheduleOpenMenuReposition);
  _visualViewport?.addEventListener('resize', _scheduleOpenMenuReposition);
  _visualViewport?.addEventListener('scroll', _scheduleOpenMenuReposition);
  const _dropdownResizeObserver = typeof ResizeObserver !== 'undefined'
    ? new ResizeObserver(_scheduleOpenMenuReposition)
    : null;
  _dropdownResizeObserver?.observe(dropdown);

  const onDocClick = (e) => {
    // A stale mount's menu may have been replaced by a re-applied control —
    // only close menus that are actually in the document.
    if (!document.body.contains(menu)) return;
    if (_hasActiveRenameInput()) return;
    if (e.target.closest('.session-row-actions')) return;
    if (dropdown && !dropdown.contains(e.target) && !menu.contains(e.target)) closeMenu();
  };
  // Close the menu when focus lands outside the dropdown (e.g. clicking the
  // chat input pill).  The focus event fires before the footer-mode rebuild
  // in switchFooterMode, which can hide the target element and prevent the
  // click event from reaching the document handler above.
  const onDocFocusIn = (e) => {
    if (!document.body.contains(menu)) return;
    if (_hasActiveRenameInput()) return;
    if (e.target.closest('.session-row-actions')) return;
    if (dropdown && !dropdown.contains(e.target) && !menu.contains(e.target)) closeMenu();
  };
  const onDocKeydown = (e) => {
    if (!document.body.contains(menu)) return;
    if (e.key === 'Escape' && menu && !menu.hidden) closeMenu();
  };

  document.addEventListener('click', onDocClick);
  document.addEventListener('focusin', onDocFocusIn);
  document.addEventListener('keydown', onDocKeydown);

  function destroy() {
    _openRequestId++;               // invalidate any in-flight open
    _dropdownEventAbort.abort();
    clearTimeout(_lpTimer);
    closeMenu();
    if (_repositionFrame) cancelAnimationFrame(_repositionFrame);
    window.removeEventListener('resize', _scheduleOpenMenuReposition);
    _visualViewport?.removeEventListener('resize', _scheduleOpenMenuReposition);
    _visualViewport?.removeEventListener('scroll', _scheduleOpenMenuReposition);
    _dropdownResizeObserver?.disconnect();
    _syncRowObserver?.disconnect();
    document.removeEventListener('click', onDocClick);
    document.removeEventListener('focusin', onDocFocusIn);
    document.removeEventListener('keydown', onDocKeydown);
    _closeRowActions();
    if (root._sessionDropdownController === handle) root._sessionDropdownController = null;
  }

  const handle = {
    open: () => { openMenu(); },
    close: (options) => {
      _closeRowActions();
      closeMenu(options);
    },
    isOpen: () => !!menu && !menu.hidden,
    destroy,
  };
  root._sessionDropdownController = handle;
  _mounts.set(root, { handle, destroy });
  return handle;
}

export function unmountSessionDropdown(root) {
  const m = root && _mounts.get(root);
  if (m) {
    m.destroy();
    _mounts.delete(root);
  }
}

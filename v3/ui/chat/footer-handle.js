// SISTER-PANEL: V2-FOOTER-HANDLE — v2 chat-ui.js interaction, v3-only storage scope.
export function initFooterHandle() {
const app = { get currentSessionId() { return new URL(location.href).searchParams.get('session_id') || ''; } };
const kvRead = (_, key) => localStorage.getItem('v3:' + key);
const kvWrite = (_, key, value) => localStorage.setItem('v3:' + key, value);
  // ── Footer drag handle (abilities / device / mode chips) ──
  const _footerHandle = document.getElementById('chat-footer-handle');
  const _footerRow = document.getElementById('chat-footer-row');

  // Hide the handle entirely when below_pill has no visible content.
  // Check after the row has been populated by chat-controls-config.
  function _updateFooterHandleVisibility() {
    if (!_footerHandle || !_footerRow) return;
    const controls = _footerRow.querySelectorAll('[data-header-control], [data-element-origin]');
    const hasContent = Array.from(controls).some(
      el => el.style.display !== 'none' && !el.hidden
    );
    _footerHandle.style.display = hasContent ? '' : 'none';
  }
  _updateFooterHandleVisibility();
  // Re-check when chat controls are re-applied (agent switch, theme change)
  if (app._reapplyChatControls) {
    const _origReapply = app._reapplyChatControls;
    const _patchedReapply = function() {
      const result = _origReapply.apply(this, arguments);
      setTimeout(_updateFooterHandleVisibility, 50);
      return result;
    };
    app._reapplyChatControls = _patchedReapply;
  }

  function _saveFooterExpanded(expanded) {
    const sid = app.currentSessionId;
    if (sid) {
      kvWrite('chat:footerExpanded:' + sid, 'chat_footer_expanded_' + sid, expanded ? '1' : '0');
    }
  }

  function _loadFooterExpanded() {
    const sid = app.currentSessionId;
    if (sid) {
      const saved = kvRead('chat:footerExpanded:' + sid, 'chat_footer_expanded_' + sid);
      if (saved !== undefined && saved !== null) return saved === '1';
    }
    return true; // default to expanded
  }

  function _applyFooterExpanded(expanded) {
    if (_footerHandle) {
      _footerHandle.classList.toggle('expanded', expanded);
      _footerHandle.setAttribute('aria-expanded', String(expanded));
    }
    if (_footerRow) _footerRow.classList.toggle('expanded', expanded);
    _saveFooterExpanded(expanded);
  }

  function _toggleFooter() {
    const current = _footerRow ? _footerRow.classList.contains('expanded') : false;
    _applyFooterExpanded(!current);
  }

  if (_footerHandle && _footerRow) {
    // Apply saved state on init
    _applyFooterExpanded(_loadFooterExpanded());

    // ── Drag to reveal / hide with spring-back ──
    let _dragStartY = 0;
    let _dragOpen = false;
    let _dragOffset = 0;       // cumulative vertical drag offset (negative = up)
    const _dragThreshold = 48;  // px of vertical drag to trigger expand/collapse
    const _dragMax = 80;        // px of linear travel before rubber band kicks in
    const _rubberResistance = 40; // higher = stiffer band past the limit
    const _pillPortion = 0.2;   // fraction of rubber-banded offset applied to the pill edge
    const _pillEl = _footerHandle.parentElement;

    // Asymptotic rubber-band: beyond `limit`, the output grows slower and slower
    // rather than stopping abruptly. Gives a springy feel.
    function _rubberBand(value, limit, resistance) {
      const sign = value < 0 ? -1 : 1;
      const abs = Math.abs(value);
      if (abs <= limit) return value;
      return sign * (limit + (abs - limit) * resistance / ((abs - limit) + resistance));
    }

    _footerHandle.addEventListener('pointerdown', (e) => {
      _dragStartY = e.clientY;
      _dragOpen = _footerRow.classList.contains('expanded');
      _dragOffset = 0;
      _footerHandle.classList.remove('spring-back');
      _pillEl.classList.remove('spring-back');
      _footerHandle.setPointerCapture(e.pointerId);
      e.preventDefault();
    });

    _footerHandle.addEventListener('pointermove', (e) => {
      if (!_footerHandle.hasPointerCapture(e.pointerId)) return;
      const dy = _dragStartY - e.clientY; // positive = dragging up
      const banded = _rubberBand(dy, _dragMax, _rubberResistance);
      _dragOffset = banded;

      // Split the visual offset: the pill edge gets a portion, the handle gets
      // the rest. Since the handle sits inside the pill, splitting keeps the
      // handle at the correct total displacement when both move together.
      const pillOffset = banded * _pillPortion;
      const handleOffset = banded * (1 - _pillPortion);

      // The pill edge shows the stretch (rubber-band visible on the pill border)
      _pillEl.style.setProperty('--pill-drag-y', (-pillOffset) + 'px');
      // The handle tracks the finger more aggressively
      _footerHandle.style.setProperty('--handle-drag-y', (-handleOffset) + 'px');

      // Toggle when threshold crossed
      if (!_dragOpen && dy > _dragThreshold) {
        _applyFooterExpanded(true);
        _dragOpen = true;
        _dragStartY = e.clientY; // reset to avoid re-trigger
      } else if (_dragOpen && dy < -_dragThreshold) {
        _applyFooterExpanded(false);
        _dragOpen = false;
        _dragStartY = e.clientY;
      }
    });

    _footerHandle.addEventListener('pointerup', (e) => {
      if (!_footerHandle.hasPointerCapture(e.pointerId)) return;
      _footerHandle.releasePointerCapture(e.pointerId);
      // Spring back to origin — on both pill edge and handle
      _footerHandle.classList.add('spring-back');
      _footerHandle.style.removeProperty('--handle-drag-y');
      _pillEl.classList.add('spring-back');
      _pillEl.style.removeProperty('--pill-drag-y');
      _dragOffset = 0;
    });

    // Click to toggle (when there was no significant drag)
    _footerHandle.addEventListener('click', (e) => {
      if (Math.abs(_dragOffset) > 4) {
        e.preventDefault();
        e.stopPropagation();
        return;
      }
      e.preventDefault();
      const wasExpanded = _footerRow.classList.contains('expanded');
      _toggleFooter();
      // Impulse: boot the pill in the direction it should go, then spring-back
      // with overshoot so it bounces past the target and settles.
      //   Expanding  → pull UP  (negative) to reveal buttons, overshoot upward
      //   Collapsing → push DOWN (positive) to hide buttons, overshoot downward
      const impulse = wasExpanded ? 24 : -24;
      _footerHandle.classList.remove('spring-back', 'spring-back-click');
      _pillEl.classList.remove('spring-back', 'spring-back-click');
      _pillEl.style.setProperty('--pill-drag-y', impulse + 'px');
      _footerHandle.style.setProperty('--handle-drag-y', impulse + 'px');
      requestAnimationFrame(() => {
        _pillEl.classList.add('spring-back-click');
        _footerHandle.classList.add('spring-back-click');
        _pillEl.style.removeProperty('--pill-drag-y');
        _footerHandle.style.removeProperty('--handle-drag-y');
      });
    });
  }

  // Export for session switch reload
  app.reloadFooterExpanded = () => _applyFooterExpanded(_loadFooterExpanded());

document.addEventListener('v3:session-open', app.reloadFooterExpanded);
return app.reloadFooterExpanded;
}

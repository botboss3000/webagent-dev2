"use strict";

// Session-dropdown bootstrap. The static controller stays mounted for the
// trigger/menu lifecycle; React remains a local, idle-prefetched enhancement.
// This preserves the header's established click and focus contract.

import { mountSessionDropdown as mountLegacy, unmountSessionDropdown as unmountLegacy } from "./controller.js";

const _islands = new WeakMap();

function _disposeLegacy(root, handle) {
  try { handle?.close?.({ immediate: true }); } catch (_) { /* best effort */ }
  try { unmountLegacy(root); } catch (_) { /* already detached */ }
  for (const menu of document.querySelectorAll("#session-dropdown-menu")) {
    if (!root.contains(menu)) menu.remove();
  }
}

export function mountReactSessionDropdown(root, opts = {}) {
  if (!root) return null;
  if (_islands.has(root)) return _islands.get(root).handle;

  // The proven native controller is the live interaction owner. Never replace
  // its trigger after binding: that handoff can strand a visible, inert pill.
  const fallback = mountLegacy(root, opts);
  const state = { live: fallback, destroyed: false };
  const handle = {
    open: (...args) => state.live?.open?.(...args),
    close: (...args) => state.live?.close?.(...args),
    isOpen: () => state.live?.isOpen?.() || false,
    destroy: () => {
      state.destroyed = true;
      _disposeLegacy(root, fallback);
      _islands.delete(root);
    },
  };
  _islands.set(root, { handle, state });

  // Keep the self-hosted React artifact warm without allowing it to replace
  // the already-bound header DOM. Its rendering migration resumes only once
  // this interaction boundary has dedicated browser-journey coverage.
  const prefetchIsland = () => import("/ui/vendor/react-session-dropdown/react-session-dropdown.js?v=6")
    .catch((error) => {
      console.warn("React session dropdown preload unavailable; native selector remains active.", error);
    });
  if ("requestIdleCallback" in window) window.requestIdleCallback(prefetchIsland, { timeout: 1200 });
  else setTimeout(prefetchIsland, 0);

  root._sessionDropdownController = handle;
  return handle;
}

export function unmountReactSessionDropdown(root) {
  const island = root && _islands.get(root);
  if (island) island.handle.destroy();
}

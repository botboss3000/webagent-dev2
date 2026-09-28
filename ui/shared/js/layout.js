'use strict';

// Shared sidebar layout helper, extracted from the large files.js module so
// that leaf modules (e.g. ui/shared/js/tables.js) can read the mobile breakpoint
// WITHOUT importing files.js. That import was the "wrong-way" edge of a
// db/* <-> files.js dependency cycle; keeping this helper here lets both the
// file manager and the DB viewer share it without depending on each other.

/** True on narrow / mobile-width viewports. Mirrors the chat layout's
 *  breakpoint, deferring to the chat layout's own check when present. */
export function isMobileLayout() {
  if (typeof window.__isMobileChatLayout === 'function') return window.__isMobileChatLayout();
  return window.innerWidth <= 514;
}

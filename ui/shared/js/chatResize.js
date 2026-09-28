'use strict';

// Chat panel resize — drag handle to set the chat side-panel width, persisted to
// localStorage (chatPanelWidth). initChatResize().

const STORAGE_KEY = 'chatPanelWidth';
const MIN_WIDTH = 250;

export function initChatResize() {
  const handle = document.getElementById('chat-resize-handle');
  const chatPanel = document.getElementById('chat-panel');
  if (!handle || !chatPanel) return;

  // The chat-only layout begins once the two 250px panel floors and 14px
  // divider no longer fit side by side.
  const MOBILE_BP = 514;
  const MAIN_MIN_WIDTH = 250;
  const RESIZE_HANDLE_WIDTH = 14;
  const maxDesktopChatWidth = () => Math.max(
    MIN_WIDTH,
    window.innerWidth - MAIN_MIN_WIDTH - RESIZE_HANDLE_WIDTH,
  );
  const applySavedWidth = () => {
    const saved = localStorage.getItem(STORAGE_KEY);
    if (!saved) return;
    const w = parseInt(saved, 10);
    if (isNaN(w)) return;
    if (window.innerWidth <= MOBILE_BP) {
      chatPanel.style.width = '';
    } else {
      // Reserve the workspace floor before restoring a saved chat width. Without
      // this clamp, a previously wide chat column can reclaim its full pixel
      // width during a viewport resize and compress the main panel first.
      chatPanel.style.width = Math.min(maxDesktopChatWidth(), Math.max(MIN_WIDTH, w)) + 'px';
    }
  };
  applySavedWidth();
  window.addEventListener('resize', applySavedWidth);

  // Programmatic width set for the App-control ability (agent-driven, via a
  // `ui_command` WebSocket event). Clamped to the same bounds as a drag and
  // persisted, so it survives reloads. No-op on mobile (chat is full-width).
  window.__setChatPanelWidth = function (px) {
    const w = parseInt(px, 10);
    if (isNaN(w)) return;
    if (window.innerWidth <= MOBILE_BP) return;
    const clamped = Math.min(maxDesktopChatWidth(), Math.max(MIN_WIDTH, w));
    chatPanel.style.width = clamped + 'px';
    try { localStorage.setItem(STORAGE_KEY, String(clamped)); } catch (_) {}
  };

  let dragging = false;
  let startX = 0;
  let startWidth = 0;
  // Which side of the handle the chat sits on, captured at drag start. When the
  // admin moves the chat to the LEFT (App Settings → Chat panel reverses #stage)
  // the handle is to the RIGHT of the chat, so dragging right must GROW it — the
  // opposite of the default right-hand layout. Detect from live geometry so it
  // works however the side was set, with no setting to read.
  let chatOnLeft = false;

  handle.addEventListener('mousedown', (e) => {
    e.preventDefault();
    dragging = true;
    startX = e.clientX;
    startWidth = chatPanel.getBoundingClientRect().width;
    chatOnLeft = chatPanel.getBoundingClientRect().left < handle.getBoundingClientRect().left;
    handle.classList.add('resizing');
    document.body.style.cursor = 'col-resize';
    document.body.style.userSelect = 'none';
  });

  document.addEventListener('mousemove', (e) => {
    if (!dragging) return;
    const delta = chatOnLeft ? (e.clientX - startX) : (startX - e.clientX);
    const newWidth = Math.min(maxDesktopChatWidth(), Math.max(MIN_WIDTH, startWidth + delta));
    chatPanel.style.width = newWidth + 'px';
  });

  document.addEventListener('mouseup', (e) => {
    if (!dragging) return;
    dragging = false;
    handle.classList.remove('resizing');
    document.body.style.cursor = '';
    document.body.style.userSelect = '';
    localStorage.setItem(STORAGE_KEY, parseInt(chatPanel.style.width, 10));
  });
}

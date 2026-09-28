'use strict';

// Chat element: changes — chip in the below-pill footer row.
// Shows a count badge of files changed in this session and toggles the
// new-design local_changes panel (ui/chat/elements/local-changes/index.js)
// on click. Data comes from the chat-changes feed (ui/chat/js/chat-changes.js)
// via the 'chat-changes:updated' event.

export function html(cfg = {}) {
  return `<button type="button" class="chat-changes-btn" title="Show checked-out and changed files in this chat session" aria-pressed="false" data-element-name="changes">
    <i data-lucide="files" style="width:20px;height:20px;"></i>
    <span class="chat-changes-count" hidden>0</span>
  </button>`;
}

export function init(el, cfg = {}) {
  const badge = el.querySelector('.chat-changes-count');

  const onUpdated = (e) => {
    if (!badge) return;
    const n = e && e.detail && Array.isArray(e.detail.files) ? e.detail.files.length : 0;
    badge.textContent = String(n);
    badge.hidden = !n;
  };

  const onToggle = () => {
    const active = el.classList.toggle('active');
    el.setAttribute('aria-pressed', String(active));
  };

  el.addEventListener('click', (e) => {
    e.stopPropagation();
    document.dispatchEvent(new CustomEvent('chat-control:toggle-changes'));
  });

  document.addEventListener('chat-changes:updated', onUpdated);
  document.addEventListener('chat-control:toggle-changes', onToggle);

  el.__changesCleanup = () => {
    document.removeEventListener('chat-changes:updated', onUpdated);
    document.removeEventListener('chat-control:toggle-changes', onToggle);
  };
}

export function destroy(el) {
  if (typeof el.__changesCleanup === 'function') el.__changesCleanup();
}

export function style() { return ''; }

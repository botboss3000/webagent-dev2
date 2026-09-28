'use strict';

// Chat element: browser_notifications — enables or disables browser notifications
// for this app. Permission can only be requested from this direct click handler.

const STORAGE_KEY = 'webagent.browser_notifications.enabled';
let _popover = null;
let _anchor = null;

function supported() {
  return 'Notification' in window;
}

function enabled() {
  return localStorage.getItem(STORAGE_KEY) === 'true';
}

function setEnabled(value) {
  localStorage.setItem(STORAGE_KEY, String(value));
}

function closePopover() {
  if (_popover) _popover.remove();
  if (_anchor) _anchor.setAttribute('aria-expanded', 'false');
  _popover = null;
  _anchor = null;
  document.removeEventListener('click', onDocumentClick);
}

function onDocumentClick(event) {
  if (_popover && !_popover.contains(event.target) && !_anchor?.contains(event.target)) closePopover();
}

function positionPopover(popover, button) {
  const rect = button.getBoundingClientRect();
  popover.style.left = `${Math.max(8, rect.right - popover.offsetWidth)}px`;
  popover.style.top = `${Math.max(8, rect.top - popover.offsetHeight - 8)}px`;
}

function openBlockedPopover(button) {
  if (_popover && _anchor === button) {
    closePopover();
    return;
  }
  closePopover();

  const popover = document.createElement('section');
  popover.className = 'chat-browser-notifications-popover';
  popover.setAttribute('role', 'dialog');
  popover.setAttribute('aria-label', 'Browser notifications blocked');
  popover.innerHTML = `
    <strong>Notifications are blocked</strong>
    <p>Your browser did not show an approval panel. In this site’s browser settings, set Notifications to <b>Ask</b> or <b>Allow</b>, then select the bell again.</p>
    <button type="button" class="chat-browser-notifications-dismiss">Close</button>`;
  popover.querySelector('.chat-browser-notifications-dismiss').addEventListener('click', closePopover);
  document.body.appendChild(popover);
  positionPopover(popover, button);
  button.setAttribute('aria-expanded', 'true');
  _popover = popover;
  _anchor = button;

  // Defer so the opening click cannot immediately close this panel.
  setTimeout(() => document.addEventListener('click', onDocumentClick), 0);
}

function update(button) {
  if (!supported()) {
    button.disabled = true;
    button.classList.remove('is-active', 'is-blocked');
    button.setAttribute('aria-pressed', 'false');
    button.setAttribute('aria-label', 'Browser notifications are not supported');
    button.title = 'Browser notifications are not supported by this browser';
    return;
  }

  const permission = Notification.permission;
  const isEnabled = permission === 'granted' && enabled();
  const isDenied = permission === 'denied';

  // A denied permission is recoverable only in browser settings, but the control
  // remains available to explain that path and retry after settings change.
  button.disabled = false;
  button.classList.toggle('is-active', isEnabled);
  button.classList.toggle('is-blocked', isDenied);
  button.setAttribute('aria-pressed', String(isEnabled));
  button.setAttribute('aria-label', isDenied
    ? 'Browser notifications are blocked. Show help to enable them'
    : isEnabled
      ? 'Turn off browser notifications'
      : 'Turn on browser notifications');
  button.title = isDenied
    ? 'Notifications are blocked. Select for help.'
    : isEnabled
      ? 'Turn off browser notifications'
      : 'Turn on browser notifications';
}

export function html(cfg = {}) {
  const size = cfg.element_size || '16px';
  return `<button type="button" class="chat-browser-notifications-btn" title="Turn on browser notifications" aria-label="Turn on browser notifications" aria-pressed="false" aria-haspopup="dialog" aria-expanded="false" data-element-name="browser_notifications"><i data-lucide="bell" aria-hidden="true" style="width:${size};height:${size};"></i></button>`;
}

export function init(el) {
  update(el);
  el.addEventListener('click', async (event) => {
    event.stopPropagation();
    if (!supported() || el.disabled) return;

    if (Notification.permission === 'denied') {
      openBlockedPopover(el);
      return;
    }

    if (Notification.permission === 'granted') {
      setEnabled(!enabled());
      update(el);
      return;
    }

    const permission = await Notification.requestPermission();
    setEnabled(permission === 'granted');
    update(el);
    if (permission === 'denied') openBlockedPopover(el);
  });
}

export function destroy(el) {
  if (_anchor === el) closePopover();
}

export function style() { return ''; }

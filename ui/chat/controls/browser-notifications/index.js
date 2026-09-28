// Browser notification preference control for the active chat footer.
(function () {
  'use strict';

  const CONTROL_ID = 'browser_notifications';
  const STORAGE_KEY = 'webagent.browser_notifications.enabled';

  function notificationsSupported() {
    return 'Notification' in window;
  }

  function notificationsEnabled() {
    return localStorage.getItem(STORAGE_KEY) === 'true';
  }

  function setNotificationsEnabled(enabled) {
    localStorage.setItem(STORAGE_KEY, String(enabled));
  }

  function updateControl(button) {
    if (!button) return;

    if (!notificationsSupported()) {
      button.disabled = true;
      button.setAttribute('aria-pressed', 'false');
      button.setAttribute('aria-label', 'Browser notifications are not supported');
      button.title = 'Browser notifications are not supported by this browser';
      return;
    }

    const permission = Notification.permission;
    const enabled = permission === 'granted' && notificationsEnabled();
    const denied = permission === 'denied';

    button.disabled = denied;
    button.classList.toggle('is-active', enabled);
    button.setAttribute('aria-pressed', String(enabled));
    button.setAttribute('aria-label', enabled ? 'Turn off browser notifications' : 'Turn on browser notifications');
    button.title = denied
      ? 'Notifications are blocked. Change this in your browser settings.'
      : enabled
        ? 'Turn off browser notifications'
        : 'Turn on browser notifications';
  }

  async function toggleNotifications(button) {
    if (!notificationsSupported() || button.disabled) return;

    if (Notification.permission === 'granted') {
      setNotificationsEnabled(!notificationsEnabled());
      updateControl(button);
      return;
    }

    const permission = await Notification.requestPermission();
    setNotificationsEnabled(permission === 'granted');
    updateControl(button);
  }

  window.ChatFooterControls = window.ChatFooterControls || {};
  window.ChatFooterControls[CONTROL_ID] = {
    init({ element }) {
      const button = element.querySelector('.browser-notifications-control');
      if (!button || button.dataset.notificationsBound === 'true') return;

      button.dataset.notificationsBound = 'true';
      updateControl(button);
      button.addEventListener('click', () => {
        toggleNotifications(button).catch(() => updateControl(button));
      });
      window.addEventListener('focus', () => updateControl(button));
    },
  };
}());

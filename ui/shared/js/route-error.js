'use strict';

// Route-error surface — renders a safe, server-declared failure state in the
// normal app workspace. app/main.py injects window.__routeError only for routes
// that intentionally retain the shell (currently failed public-agent links).
// This module never infers route failures from the URL, preventing client-side
// guesses from overriding normal routing or authorization.

function _routeError() {
  const value = window.__routeError;
  return value && Number(value.status) === 404 && value.code === 'public_agent_not_found'
    ? value
    : null;
}

export function initRouteError() {
  if (!_routeError()) return false;
  const mainPanel = document.getElementById('main-panel');
  if (!mainPanel || document.getElementById('route-error-panel')) return false;

  document.body.classList.add('route-error-active');
  const panel = document.createElement('section');
  panel.id = 'route-error-panel';
  panel.className = 'route-error-panel';
  panel.setAttribute('role', 'main');
  panel.setAttribute('aria-labelledby', 'route-error-title');
  panel.innerHTML = `
    <div class="route-error-card">
      <span class="route-error-code" aria-hidden="true">404</span>
      <div class="route-error-copy">
        <p class="route-error-eyebrow">Link unavailable</p>
        <h1 id="route-error-title">This shared agent link isn’t available</h1>
        <p>The link may be incorrect, no longer shared, or have been disabled.</p>
      </div>
      <a class="route-error-action" href="/app">Go to WebAgent</a>
    </div>
  `;
  mainPanel.appendChild(panel);
  return true;
}

/**
 * Safety Splash — server restart confirmation lock.
 *
 * When the server shuts down, a "safety lock" flag is set. The server inlines
 * window.__SAFETY_LOCK into the HTML. If true, this script shows a full-screen
 * modal BEFORE the app loads. The admin must choose "Start Services" or
 * "Keep Shut Down" before any app JS runs.
 *
 * Loaded only when the lock is active, so it's zero-cost on normal boot.
 */
(function () {
  var KEY = 'wa_safety_checked';

  // If we already confirmed in this session (after a reload), skip.
  try { if (sessionStorage.getItem(KEY)) return; } catch (e) {}

  if (!window.__SAFETY_LOCK) return;

  // Hide page content until the admin decides.
  document.documentElement.style.opacity = '0';

  showSafetySplash(function (answer) {
    // answer: true = start services, false = keep shut down
    fetch('/api/v1/admin/safety-lock/confirm', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ start_services: answer })
    })
      .then(function (r) { return r.json(); })
      .then(function (d) {
        if (answer && d.services_started) {
          // Mark checked so reload doesn't re-trigger, then reload
          try { sessionStorage.setItem(KEY, '1'); } catch (e) {}
          window.location.reload();
        } else {
          // Stay on the minimal "shut down" page
          showShutdownPage();
        }
      })
      .catch(function () {
        // Network or auth error — show it on the splash
        var errEl = document.createElement('p');
        errEl.style.cssText = 'color:#e74c3c;margin-top:1rem;font-size:.9rem';
        errEl.textContent = 'Failed to confirm. Are you logged in as admin?';
        document.querySelector('#safety-splash div div').appendChild(errEl);
      });
  });

  function showSafetySplash(callback) {
    document.documentElement.style.opacity = '1';
    document.title = 'Safety Lock — WebAgent';
    document.body.innerHTML =
      '<div id="safety-splash" style="' +
      'position:fixed;inset:0;z-index:2147483000;display:flex;flex-direction:column;' +
      'align-items:center;justify-content:center;background:#0d0d1a;color:#e0e0e0;' +
      'font-family:system-ui,sans-serif;padding:2rem;text-align:center;' +
      '">' +
      '<div style="max-width:480px">' +
      '<h1 style="font-size:1.75rem;margin:0 0 .5rem">⚠ Server Restarted</h1>' +
      '<p style="color:#999;margin:0 0 1.5rem;line-height:1.5">' +
      'The server was shut down and has restarted. ' +
      'Background services (automations, sessions, event handlers) are currently paused. ' +
      'Do you want to start them now?</p>' +
      '<div style="display:flex;gap:1rem;justify-content:center;flex-wrap:wrap">' +
      '<button id="safety-start" style="' +
      'padding:.75rem 1.5rem;border:none;border-radius:8px;background:#6c5ce7;color:#fff;' +
      'font-size:1rem;cursor:pointer;font-weight:600' +
      '">Start Services</button>' +
      '<button id="safety-keep" style="' +
      'padding:.75rem 1.5rem;border:1px solid #444;border-radius:8px;background:transparent;' +
      'color:#ccc;font-size:1rem;cursor:pointer' +
      '">Keep Shut Down</button>' +
      '</div>' +
      '<p style="color:#555;font-size:.8rem;margin-top:2rem">' +
      'Only an admin user can start services. ' +
      'If you keep services shut down, restart the server to see this prompt again.</p>' +
      '</div>' +
      '</div>';
    document.getElementById('safety-start').onclick = function () { callback(true); };
    document.getElementById('safety-keep').onclick = function () { callback(false); };
  }

  function showShutdownPage() {
    document.title = 'Shut Down — WebAgent';
    document.documentElement.style.opacity = '1';
    document.body.style.cssText =
      'display:flex;align-items:center;justify-content:center;min-height:100vh;' +
      'font-family:system-ui,sans-serif;background:#0d0d1a;color:#e0e0e0;text-align:center;' +
      'padding:2rem;margin:0;';
    document.body.innerHTML =
      '<div>' +
      '<h1 style="font-size:1.5rem;margin-bottom:.5rem;color:#e0e0e0">Services are shut down</h1>' +
      '<p style="color:#999;margin:.25rem 0">The server is running but all background services are stopped.</p>' +
      '<p style="color:#999;margin:.25rem 0">Restart the server to see the safety prompt again.</p>' +
      '</div>';
  }
})();
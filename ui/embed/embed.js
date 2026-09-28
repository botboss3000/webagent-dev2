'use strict';

// WebAgent website embed loader — the ONE asset a customer references from their
// own site:
//   <script src="https://YOUR-APP/embed.js" data-agent="<uuid>" data-position="right" async></script>
//
// It injects a floating launcher button + a chat panel (an iframe pointing at
// /embed/<agent_id> on this app's origin). Everything lives inside a Shadow DOM
// so the host page's CSS can't bleed in and vice-versa. The launcher's accent
// and its very existence come from the agent's public embed config, so an owner
// who hasn't enabled the widget gets nothing rendered.
(function () {
  // Guard against double-inclusion (e.g. the snippet pasted twice).
  if (window.__webagentEmbedLoaded) return;
  window.__webagentEmbedLoaded = true;

  var script = document.currentScript;
  if (!script) {
    // async scripts: currentScript can be null — find ourselves by [data-agent].
    var all = document.querySelectorAll('script[data-agent]');
    script = all[all.length - 1];
  }
  if (!script) return;

  var agentId = script.getAttribute('data-agent');
  if (!agentId) { console.warn('[webagent-embed] missing data-agent'); return; }
  var position = (script.getAttribute('data-position') || 'right').toLowerCase() === 'left' ? 'left' : 'right';

  // Origin = where THIS script was served from, so all URLs stay same-origin.
  var origin;
  try { origin = new URL(script.src, location.href).origin; } catch (_) { origin = location.origin; }

  // ── Fetch public config: accent + whether the widget should show at all ──
  fetch(origin + '/api/v1/agents/' + encodeURIComponent(agentId) + '/embed')
    .then(function (r) { return r.ok ? r.json() : null; })
    .then(function (info) {
      if (!info || !info.embeddable) return;   // disabled or not anon-open → render nothing
      mount(info.config || {});
    })
    .catch(function () { /* network/agent gone — fail silent, no broken UI on host */ });

  function mount(cfg) {
    var accent = cfg.accent || '#4f46e5';
    var accentFg = contrast(accent);
    var widget = cfg.chat_ui || (cfg.views && cfg.views.widget) || {};

    var host = document.createElement('div');
    host.setAttribute('data-webagent-embed', agentId);
    (document.body || document.documentElement).appendChild(host);
    var root = host.attachShadow ? host.attachShadow({ mode: 'open' }) : host;

    var style = document.createElement('style');
    style.textContent = css(position, accent, accentFg, widget);
    root.appendChild(style);

    // Launcher button — renders the AGENT's configured icon (SVG served by the
    // app in the embed descriptor). Falls back to a chat bubble if the server
    // didn't supply one. No async icon-library load — the icon is always there.
    var btn = document.createElement('button');
    btn.type = 'button';
    btn.className = 'wa-launch';
    btn.setAttribute('aria-label', 'Open chat');
    btn.innerHTML = cfg.agent_icon_svg || '<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true"><path d="M21 11.5a8.38 8.38 0 0 1-.9 3.8 8.5 8.5 0 0 1-7.6 4.7 8.38 8.38 0 0 1-3.8-.9L3 21l1.9-5.7a8.38 8.38 0 0 1-.9-3.8 8.5 8.5 0 0 1 4.7-7.6 8.38 8.38 0 0 1 3.8-.9h.5a8.48 8.48 0 0 1 8 8v.5z"/></svg>';
    root.appendChild(btn);

    // Panel (holds the iframe; created lazily on first open)
    var panel = document.createElement('div');
    panel.className = 'wa-panel';
    root.appendChild(panel);

    ['n', 's', 'e', 'w', 'ne', 'nw', 'se', 'sw'].forEach(function (dir) {
      var handle = document.createElement('div');
      handle.className = 'wa-resize wa-resize-' + dir;
      handle.setAttribute('data-dir', dir);
      handle.setAttribute('aria-hidden', 'true');
      panel.appendChild(handle);
    });

    var iframe = null;
    var open = false;
    var dragged = false;
    var storageKey = 'webagent-launcher:' + origin + ':' + agentId;
    var panelStorageKey = storageKey + ':panel-size';

    restorePosition();
    restorePanelSize();
    wireDrag();
    wirePanelResize();

    function ensureIframe() {
      if (iframe) return;
      iframe = document.createElement('iframe');
      iframe.className = 'wa-frame';
      iframe.title = cfg.title || 'Chat';
      iframe.setAttribute('allow', 'clipboard-write');
      iframe.src = origin + '/embed/' + encodeURIComponent(agentId);
      panel.appendChild(iframe);
    }

    function setOpen(next) {
      open = next;
      if (open) ensureIframe();
      if (open) placePanel();
      panel.classList.toggle('wa-open', open);
      btn.classList.toggle('wa-hidden', open);
      btn.setAttribute('aria-expanded', open ? 'true' : 'false');
    }

    btn.addEventListener('click', function () {
      if (dragged) { dragged = false; return; }
      setOpen(true);
    });

    window.addEventListener('resize', function () {
      clampLauncher();
      clampPanelSize();
      if (open) placePanel();
    });

    // The iframe asks to close via postMessage (header ✕ button).
    window.addEventListener('message', function (e) {
      var d = e.data;
      if (e.origin !== origin || !d || d.source !== 'webagent-embed') return;
      if (d.action === 'close') setOpen(false);
    });

    function wireDrag() {
      var start = null;
      btn.addEventListener('pointerdown', function (e) {
        if (e.button !== 0) return;
        var r = btn.getBoundingClientRect();
        start = { x: e.clientX, y: e.clientY, left: r.left, top: r.top };
        dragged = false;
        btn.setPointerCapture(e.pointerId);
        btn.classList.add('wa-dragging');
      });
      btn.addEventListener('pointermove', function (e) {
        if (!start) return;
        var dx = e.clientX - start.x, dy = e.clientY - start.y;
        if (!dragged && Math.hypot(dx, dy) < 5) return;
        dragged = true;
        setLauncherPosition(start.left + dx, start.top + dy);
      });
      function finish(e) {
        if (!start) return;
        start = null;
        btn.classList.remove('wa-dragging');
        try { btn.releasePointerCapture(e.pointerId); } catch (_) {}
        if (dragged) savePosition();
      }
      btn.addEventListener('pointerup', finish);
      btn.addEventListener('pointercancel', finish);
    }

    function wirePanelResize() {
      var start = null;
      var edge = 8;
      var minWidth = Math.min(300, window.innerWidth - edge * 2);
      var minHeight = Math.min(320, window.innerHeight - edge * 2);

      panel.querySelectorAll('.wa-resize').forEach(function (handle) {
        handle.addEventListener('pointerdown', function (e) {
          if (e.button !== 0 || window.innerWidth <= 480) return;
          e.preventDefault();
          e.stopPropagation();
          var r = panel.getBoundingClientRect();
          start = {
            dir: handle.getAttribute('data-dir') || '',
            x: e.clientX, y: e.clientY,
            left: r.left, top: r.top, right: r.right, bottom: r.bottom,
            width: r.width, height: r.height,
          };
          handle.setPointerCapture(e.pointerId);
          panel.classList.add('wa-resizing');
        });
        handle.addEventListener('pointermove', function (e) {
          if (!start) return;
          var dx = e.clientX - start.x;
          var dy = e.clientY - start.y;
          var left = start.left;
          var top = start.top;
          var width = start.width;
          var height = start.height;
          if (start.dir.indexOf('e') !== -1) {
            width = Math.max(minWidth, Math.min(window.innerWidth - edge - start.left, start.width + dx));
          }
          if (start.dir.indexOf('w') !== -1) {
            width = Math.max(minWidth, Math.min(start.right - edge, start.width - dx));
            left = start.right - width;
          }
          if (start.dir.indexOf('s') !== -1) {
            height = Math.max(minHeight, Math.min(window.innerHeight - edge - start.top, start.height + dy));
          }
          if (start.dir.indexOf('n') !== -1) {
            height = Math.max(minHeight, Math.min(start.bottom - edge, start.height - dy));
            top = start.bottom - height;
          }
          panel.style.left = left + 'px';
          panel.style.top = top + 'px';
          panel.style.right = 'auto';
          panel.style.bottom = 'auto';
          panel.style.width = width + 'px';
          panel.style.height = height + 'px';
        });
        function finish(e) {
          if (!start) return;
          start = null;
          panel.classList.remove('wa-resizing');
          try { handle.releasePointerCapture(e.pointerId); } catch (_) {}
          savePanelSize();
        }
        handle.addEventListener('pointerup', finish);
        handle.addEventListener('pointercancel', finish);
      });
    }

    function savePanelSize() {
      var r = panel.getBoundingClientRect();
      try { localStorage.setItem(panelStorageKey, JSON.stringify({ width: r.width, height: r.height })); }
      catch (_) {}
    }

    function restorePanelSize() {
      try {
        var saved = JSON.parse(localStorage.getItem(panelStorageKey) || 'null');
        if (saved && Number.isFinite(saved.width) && Number.isFinite(saved.height)) {
          panel.style.width = saved.width + 'px';
          panel.style.height = saved.height + 'px';
        }
      } catch (_) {}
      clampPanelSize();
    }

    function clampPanelSize() {
      if (window.innerWidth <= 480) {
        panel.style.removeProperty('width');
        panel.style.removeProperty('height');
        return;
      }
      var r = panel.getBoundingClientRect();
      var maxWidth = Math.max(1, window.innerWidth - 24);
      var maxHeight = Math.max(1, window.innerHeight - 24);
      var width = Math.max(Math.min(300, maxWidth), Math.min(r.width || 400, maxWidth));
      var height = Math.max(Math.min(320, maxHeight), Math.min(r.height || 640, maxHeight));
      panel.style.width = width + 'px';
      panel.style.height = height + 'px';
    }

    function setLauncherPosition(left, top) {
      var edge = 8;
      var r = btn.getBoundingClientRect();
      var w = r.width || 58, h = r.height || 58;
      left = Math.max(edge, Math.min(window.innerWidth - w - edge, left));
      top = Math.max(edge, Math.min(window.innerHeight - h - edge, top));
      btn.style.left = left + 'px';
      btn.style.top = top + 'px';
      btn.style.right = 'auto';
      btn.style.bottom = 'auto';
      if (open) placePanel();
    }

    function clampLauncher() {
      var r = btn.getBoundingClientRect();
      setLauncherPosition(r.left, r.top);
    }

    function savePosition() {
      var r = btn.getBoundingClientRect();
      try {
        localStorage.setItem(storageKey, JSON.stringify({
          x: r.left / Math.max(1, window.innerWidth - r.width),
          y: r.top / Math.max(1, window.innerHeight - r.height)
        }));
      } catch (_) {}
    }

    function restorePosition() {
      try {
        var saved = JSON.parse(localStorage.getItem(storageKey) || 'null');
        if (saved && Number.isFinite(saved.x) && Number.isFinite(saved.y)) {
          requestAnimationFrame(function () {
            var r = btn.getBoundingClientRect();
            setLauncherPosition(saved.x * (window.innerWidth - r.width), saved.y * (window.innerHeight - r.height));
          });
        }
      } catch (_) {}
    }

    function placePanel() {
      if (window.innerWidth <= 480) {
        panel.style.left = '0';
        panel.style.top = '0';
        panel.style.right = 'auto';
        panel.style.bottom = 'auto';
        return;
      }
      var b = btn.getBoundingClientRect();
      var p = panel.getBoundingClientRect();
      var pw = p.width || Math.min(400, window.innerWidth - 40);
      var ph = p.height || Math.min(640, window.innerHeight - 40);
      var gap = 12, edge = 12;
      var left = Math.max(edge, Math.min(window.innerWidth - pw - edge, b.left + b.width / 2 - pw / 2));
      var above = b.top - ph - gap;
      var below = b.bottom + gap;
      var top = above >= edge ? above : Math.min(window.innerHeight - ph - edge, below);
      panel.style.left = left + 'px';
      panel.style.top = Math.max(edge, top) + 'px';
      panel.style.right = 'auto';
      panel.style.bottom = 'auto';
    }
  }

  // ── helpers ──
  function contrast(hex) {
    var h = (hex || '').replace('#', '');
    if (h.length === 3) h = h.split('').map(function (c) { return c + c; }).join('');
    if (h.length !== 6) return '#ffffff';
    var r = parseInt(h.slice(0, 2), 16) / 255, g = parseInt(h.slice(2, 4), 16) / 255, b = parseInt(h.slice(4, 6), 16) / 255;
    function lin(c) { return c <= 0.03928 ? c / 12.92 : Math.pow((c + 0.055) / 1.055, 2.4); }
    var L = 0.2126 * lin(r) + 0.7152 * lin(g) + 0.0722 * lin(b);
    return L > 0.5 ? '#1a1a24' : '#ffffff';
  }


  function css(pos, accent, accentFg, widget) {
    var launcher = widget.launcher || {};
    var panel = widget.panel || {};
    var bottom = cssValue(panel.bottom || launcher.bottom, '20px');
    var size = cssValue(launcher.size, '58px');
    var side = pos === 'left' ? 'left: 20px;' : 'right: 20px;';
    var panelWidth = cssValue(panel.width, '400px');
    var panelHeight = cssValue(panel.height, '640px');
    var panelRadius = cssValue(panel.radius, '14px');
    return [
      '.wa-launch{position:fixed;bottom:' + bottom + ';' + side + 'z-index:2147483000;display:inline-flex;align-items:center;justify-content:center;width:' + size + ';height:' + size + ';padding:0;border:1px solid rgba(255,255,255,.24);border-radius:50%;background:radial-gradient(circle at 32% 25%,rgba(255,255,255,.28),transparent 34%),' + accent + ';color:' + accentFg + ';cursor:grab;touch-action:none;user-select:none;box-shadow:0 0 18px ' + accent + ',0 0 44px color-mix(in srgb,' + accent + ' 35%,transparent),0 8px 26px rgba(0,0,0,.28);transition:transform .15s,box-shadow .15s;font-family:-apple-system,BlinkMacSystemFont,"Segoe UI",Roboto,sans-serif;animation:wa-glow 4s ease-in-out infinite;}',
      '.wa-launch svg{width:27px;height:27px;stroke-width:1.8;pointer-events:none;}',
      '.wa-launch:hover{transform:scale(1.06);box-shadow:0 0 24px ' + accent + ',0 0 58px color-mix(in srgb,' + accent + ' 42%,transparent),0 10px 30px rgba(0,0,0,.3);}',
      '.wa-launch.wa-dragging{cursor:grabbing;transform:scale(1.08);animation:none;}',
      '@keyframes wa-glow{0%,100%{filter:saturate(1);opacity:.94}50%{filter:saturate(1.2);opacity:1}}',
      // Keep the launcher box measurable while the panel is open. Resize and
      // iframe-load reflows call placePanel(); display:none would collapse its
      // bounding box to (0, 0) and jump a right-side panel to the upper-left.
      '.wa-launch.wa-hidden{visibility:hidden;pointer-events:none;}',
      '.wa-panel{position:fixed;bottom:' + bottom + ';' + side + 'z-index:2147483000;width:' + panelWidth + ';height:' + panelHeight + ';max-width:calc(100vw - 40px);max-height:calc(100vh - 40px);border-radius:' + panelRadius + ';overflow:hidden;box-shadow:0 12px 48px rgba(0,0,0,.3);opacity:0;transform:translateY(16px) scale(.98);pointer-events:none;transition:opacity .18s,transform .18s;background:#16100b;}',
      '.wa-panel.wa-open{opacity:1;transform:none;pointer-events:auto;}',
      '.wa-frame{position:relative;z-index:0;width:100%;height:100%;border:none;display:block;}',
      '.wa-resize{position:absolute;z-index:2;touch-action:none;user-select:none;}',
      '.wa-resize-n{left:12px;right:12px;top:0;height:7px;cursor:ns-resize;}',
      '.wa-resize-s{left:12px;right:12px;bottom:0;height:7px;cursor:ns-resize;}',
      '.wa-resize-e{top:12px;bottom:12px;right:0;width:7px;cursor:ew-resize;}',
      '.wa-resize-w{top:12px;bottom:12px;left:0;width:7px;cursor:ew-resize;}',
      '.wa-resize-ne{top:0;right:0;width:14px;height:14px;cursor:nesw-resize;}',
      '.wa-resize-nw{top:0;left:0;width:14px;height:14px;cursor:nwse-resize;}',
      '.wa-resize-se{right:0;bottom:0;width:14px;height:14px;cursor:nwse-resize;}',
      '.wa-resize-sw{left:0;bottom:0;width:14px;height:14px;cursor:nesw-resize;}',
      '.wa-panel.wa-resizing{transition:none;}',
      '.wa-panel.wa-resizing .wa-frame{pointer-events:none;}',
      '@media (max-width:480px){.wa-panel{width:100vw!important;height:100vh!important;max-width:100vw;max-height:100vh;bottom:0;right:0;left:0;border-radius:0;}.wa-resize{display:none;}}',
    ].join('');
  }

  function cssValue(value, fallback) {
    // Values are app-owned JSON, but reject characters that could break the
    // shadow-DOM stylesheet if the file is edited by hand.
    return typeof value === 'string' && /^[#(),.%\w\s+-]+$/.test(value) ? value : fallback;
  }
})();

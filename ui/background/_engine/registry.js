/* ============================================================
   Background registry  —  window.WA_BG (renderer-facing API)
   ------------------------------------------------------------
   This is core infrastructure, NOT a background. Every background
   plugin (a folder under ui/background/<id>/) ships an <id>.js that,
   when loaded, calls:

       window.WA_BG.register('<id>', { start: fn, stop: fn, refresh: fn });

   • start(genui)  — begin drawing into the shared <genui>. The
                      renderer owns its own sizing, listeners and
                      requestAnimationFrame loop.
   • stop()         — cancel the loop, remove its listeners, leave
                      the genui clean. Called when the background is
                      swapped out (theme flip or admin change).
   • refresh()      — OPTIONAL. Called when the admin edits the theme
                      colours live (Appearance panel); re-read the CSS
                      tokens so the running loop repaints in the new
                      palette. Safe to omit.

   The manager (manager.js) is the only consumer of whenReady();
   plugins only ever call register(). Loaded BEFORE manager.js so the
   global exists no matter when a renderer script finishes loading.
   ============================================================ */
(function () {
  if (window.WA_BG) return;  // idempotent — never clobber a live registry

  var renderers = {};   // id -> { start, stop }
  var waiters = {};     // id -> [callbacks awaiting registration]

  window.WA_BG = {
    /* Called by each background plugin's <id>.js on load. */
    register: function (id, api) {
      if (!id || !api || typeof api.start !== 'function') return;
      renderers[id] = api;
      var queued = waiters[id];
      if (queued) {
        delete waiters[id];
        for (var i = 0; i < queued.length; i++) {
          try { queued[i](api); } catch (e) { /* isolate one bad waiter */ }
        }
      }
    },

    /* Resolve when a renderer id is registered (now or later). */
    whenReady: function (id, cb) {
      if (renderers[id]) { cb(renderers[id]); return; }
      (waiters[id] || (waiters[id] = [])).push(cb);
    },

    /* True if a renderer id has registered. */
    has: function (id) { return !!renderers[id]; },

    _get: function (id) { return renderers[id] || null; }
  };
})();

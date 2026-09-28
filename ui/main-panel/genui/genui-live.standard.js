/**
 * GenUILive — standard live-data wiring for genui pages.
 *
 * Drop this snippet into any genui page that needs to stay in sync with its
 * data bag (data.json) as an agent writes to it, and optionally with agent
 * session activity for live "working…" indicators.
 *
 * Canonical file:  ui/main-panel/genui/genui-live.standard.js
 * Spec document:   docs/claude/genui-live-standard.md
 * Companion:       genui-state.standard.js (persistence across refreshes)
 *
 * Pattern:
 *   1. Paste this file verbatim (or embed its body).
 *   2. var live = GenUILive.init({slug:'home', user_id:'admin', onData: applyLiveData});
 *   3. function applyLiveData(fresh, prev) { … merge into STATE, re-render … }
 *   4. Optionally: live.watchSessions({api, getSessions: ()=>[...], onRun: onRunUpdate});
 *   5. Call live.destroy() in the page's cleanup hook.
 *
 * How it works:
 *   - Polls GET /api/v1/genui/{slug}/data?user_id=… (Bearer auth from localStorage).
 *   - Stable JSON diff between polls — onData fires only when something changed.
 *   - Adaptive interval: fast (~3s) while watched sessions have active runs;
 *     slow (~15s) when idle.  Instant wake on api.onSessionActivity().
 *   - Pauses polling when the tab is hidden (pagehide / visibilitychange).
 *   - No-ops gracefully when endpoints are down.
 *
 * Auth: reads localStorage 'auth_token' and 'auth_user_id'; falls back to
 * 'admin' when no user_id is stored (single-user local installs).
 */

(function (global) {
  'use strict';

  /** Default poll interval when no session is actively running (ms). */
  var IDLE_INTERVAL = 15000;
  /** Default poll interval when a watched session is in-flight (ms). */
  var FAST_INTERVAL = 3000;
  /** Minimum time between two onData calls (ms). */
  var COALESCE_MS = 1000;

  /** Read auth-bearing headers from localStorage. */
  function authHeaders() {
    var tok = null;
    try { tok = localStorage.getItem('auth_token'); } catch (_) {}
    if (!tok) return {};
    return { 'Authorization': 'Bearer ' + tok };
  }

  function authUserId(fallback) {
    try {
      var uid = localStorage.getItem('auth_user_id') || '';
      if (uid && uid.indexOf('anon_') !== 0) return uid;
    } catch (_) {}
    return fallback || 'admin';
  }

  /** Stable JSON stringify — sorted keys so diffs are meaningful. */
  function stableStr(obj) {
    try {
      return JSON.stringify(obj, function (_, v) {
        if (v && typeof v === 'object' && !Array.isArray(v)) {
          var keys = Object.keys(v).sort();
          var out = {};
          for (var i = 0; i < keys.length; i++) out[keys[i]] = v[keys[i]];
          return out;
        }
        return v;
      });
    } catch (_) { return ''; }
  }

  /** Fetch the current data bag from the server. */
  function fetchData(slug, uid, cb) {
    var url = '/api/v1/genui/' + encodeURIComponent(slug) + '/data?user_id=' + encodeURIComponent(uid);
    try {
      var xhr = new XMLHttpRequest();
      xhr.open('GET', url, true);
      var h = authHeaders();
      for (var k in h) { if (h.hasOwnProperty(k)) xhr.setRequestHeader(k, h[k]); }
      xhr.timeout = 10000;
      xhr.onload = function () {
        if (xhr.status === 200) {
          try {
            var body = JSON.parse(xhr.responseText);
            if (body && body.status === 'ok' && body.data) {
              cb(null, body.data);
            } else {
              cb(null, {});
            }
          } catch (_) { cb('parse'); }
        } else {
          cb('status:' + xhr.status);
        }
      };
      xhr.onerror = function () { cb('network'); };
      xhr.ontimeout = function () { cb('timeout'); };
      xhr.send();
    } catch (_) { cb('exception'); }
  }

  /**
   * Create a GenUILive instance for one page.
   *
   * opts:
   *   slug         — genui slug (required)
   *   user_id      — override user_id (default: from localStorage or 'admin')
   *   onData(data, prevData) — called when the bag changed (required)
   *   fastInterval — override FAST_INTERVAL (optional)
   *   idleInterval — override IDLE_INTERVAL (optional)
   *   coalesceMs   — override COALESCE_MS (optional)
   *
   * Returns: { watchSessions, refresh, destroy, getLastData }
   */
  function GenUILive(opts) {
    var o = opts || {};
    var slug = String(o.slug || '');
    var uid = o.user_id || authUserId('admin');
    var fastInterval = o.fastInterval || FAST_INTERVAL;
    var idleInterval = o.idleInterval || IDLE_INTERVAL;
    var coalesceMs = o.coalesceMs || COALESCE_MS;
    var onData = o.onData || function () {};

    if (!slug) {
      console.error('[GenUILive] slug is required');
      return { watchSessions: function () {}, refresh: function () {}, destroy: function () {}, getLastData: function () { return {}; } };
    }

    var _destroyed = false;
    var _timer = 0;
    var _lastData = null;
    var _lastDataStr = '';
    var _lastOnDataMs = 0;
    var _hidden = false;
    var _watchingSessions = false;
    var _apiObj = null;
    var _getSessionIds = null;
    var _onRunCb = null;
    var _activeSessions = {};   // sid -> { lastSeen, running }
    var _sessionActivityUnsub = null;

    /** Compute the current interval based on active sessions. */
    function computeInterval() {
      var hasActive = false;
      var keys = Object.keys(_activeSessions);
      for (var i = 0; i < keys.length; i++) {
        if (_activeSessions[keys[i]].running) { hasActive = true; break; }
      }
      return hasActive ? fastInterval : idleInterval;
    }

    /** Core poll — fetch data, diff, fire onData if changed. */
    function poll() {
      if (_destroyed) return;
      if (_hidden) { schedule(); return; }

      fetchData(slug, uid, function (err, data) {
        if (_destroyed) { schedule(); return; }
        if (err) {
          // Graceful degradation — just schedule the next poll.
          schedule();
          return;
        }

        _lastData = data;
        var s = stableStr(data);
        if (s !== _lastDataStr) {
          _lastDataStr = s;
          var now = Date.now();
          if (now - _lastOnDataMs >= coalesceMs) {
            _lastOnDataMs = now;
            try { onData(data, _lastData); } catch (e) {
              console.error('[GenUILive] onData error:', e);
            }
          }
        }
        schedule();
      });
    }

    function schedule() {
      if (_destroyed) return;
      clearTimeout(_timer);
      _timer = setTimeout(poll, computeInterval());
    }

    function wake() {
      if (_destroyed) return;
      clearTimeout(_timer);
      _timer = setTimeout(poll, 150);  // near-instant
    }

    // ── Visibility pause ────────────────────────────────────────────────────
    function onVisChange() {
      _hidden = document.visibilityState === 'hidden';
      if (!_hidden) {
        // Tab came back — poll immediately.
        clearTimeout(_timer);
        _timer = setTimeout(poll, 200);
      }
    }

    // ── Session watcher — live activity ↔ UI link ───────────────────────────
    function onSessionActivity(evt) {
      if (_destroyed) return;
      wake();
      // Track which sessions are active so computeInterval knows.
      if (evt && evt.session_id) {
        var entry = _activeSessions[evt.session_id];
        if (entry) {
          entry.lastSeen = Date.now();
          // A type===pipeline or tool_call means the agent is working.
          if (evt.type === 'tool_call' || evt.type === 'pipeline' || evt.type === 'response') {
            entry.running = true;
          }
        }
      }
      // On instant wake, also re-check run state so "running" → idle transitions
      // update within the next normal poll too.
    }

    // Run-state poll: cheap session-tail to see if a session's run is over.
    function checkRunState(sid, cb) {
      var url = '/api/v1/db/session-tail?session_id=' + encodeURIComponent(sid) + '&after_session_seq=0';
      try {
        var xhr = new XMLHttpRequest();
        xhr.open('GET', url, true);
        var h = authHeaders();
        for (var k in h) { if (h.hasOwnProperty(k)) xhr.setRequestHeader(k, h[k]); }
        xhr.timeout = 8000;
        xhr.onload = function () {
          if (xhr.status === 200) {
            try {
              var body = JSON.parse(xhr.responseText);
              cb(null, body.run || null);
            } catch (_) { cb('parse'); }
          } else {
            cb('status:' + xhr.status);
          }
        };
        xhr.onerror = function () { cb('network'); };
        xhr.ontimeout = function () { cb('timeout'); };
        xhr.send();
      } catch (_) { cb('exception'); }
    }

    function refreshRunStates() {
      if (!_getSessionIds) return;
      var sids;
      try { sids = _getSessionIds(); } catch (_) { return; }
      if (!sids || !sids.length) return;
      for (var i = 0; i < sids.length; i++) {
        (function (sid) {
          if (!sid) return;
          checkRunState(sid, function (err, run) {
            if (!_activeSessions[sid]) _activeSessions[sid] = { lastSeen: 0, running: false };
            if (!err && run) {
              _activeSessions[sid].running = !!run.active;
            }
            // Notify the page of current run state.
            if (_onRunCb) {
              try { _onRunCb(sid, run); } catch (_) {}
            }
          });
        })(sids[i]);
      }
    }

    // ── Public API ──────────────────────────────────────────────────────────
    var api = {
      /**
       * Link agent sessions to this live instance. The page calls this once
       * after init, passing the host api object and a function that returns
       * the current set of relevant session ids.
       *
       *   getSessions() — returns array of session ids to watch (e.g. all
       *                   items whose qa.status is in-flight).
       *   onRun(sid, run) — called with run state ({active, status, current_op})
       *                     so the page can update live indicators.
       */
      watchSessions: function (opts2) {
        var o2 = opts2 || {};
        _apiObj = o2.api || null;
        _getSessionIds = o2.getSessions || null;
        _onRunCb = o2.onRun || null;
        if (_apiObj && typeof _apiObj.onSessionActivity === 'function') {
          _sessionActivityUnsub = function () {};
          _apiObj.onSessionActivity(onSessionActivity);
        }
        _watchingSessions = true;
        // Initial run state check.
        refreshRunStates();
      },

      /** Force an immediate poll, ignoring the coalesce debounce. */
      refresh: function () {
        _lastOnDataMs = 0;
        wake();
      },

      /** Return the last seen data bag. */
      getLastData: function () {
        return _lastData || {};
      },

      /** Stop all polling and tear down the session-activity subscription. */
      destroy: function () {
        _destroyed = true;
        clearTimeout(_timer);
        _timer = 0;
        if (typeof _sessionActivityUnsub === 'function') _sessionActivityUnsub();
        _sessionActivityUnsub = null;
        _activeSessions = {};
        try { document.removeEventListener('visibilitychange', onVisChange); } catch (_) {}
      },
    };

    // ── Boot ────────────────────────────────────────────────────────────────
    document.addEventListener('visibilitychange', onVisChange);
    // Immediate first poll.
    poll();
    // Periodically refresh run states for watched sessions.
    setInterval(function () {
      if (_destroyed || !_watchingSessions) return;
      refreshRunStates();
    }, 5000);

    return api;
  }

  // ── Exports ───────────────────────────────────────────────────────────────
  global.GenUILive = GenUILive;

})(typeof window !== 'undefined' ? window : this);

/* ============================================================
   Background plugin: Floating particles  (soft drifting bokeh)
   ------------------------------------------------------------
   Soft glowing orbs drift slowly upward with a little sideways sway and
   gently part around the cursor. Lighter-weight than Stargaze. Colours
   come from the --particles-rgb token in particles.css.
   ============================================================ */
(function () {
  var RID = 'particles';
  var reduced = window.matchMedia && window.matchMedia('(prefers-reduced-motion: reduce)').matches;

  (function ensureCss() {
    if (document.querySelector('link[data-bg="' + RID + '"]')) return;
    var l = document.createElement('link');
    l.rel = 'stylesheet'; l.href = 'ui/background/particles/particles.css';
    l.setAttribute('data-bg', RID);
    // start() reads the palette synchronously on register — BEFORE this async
    // sheet (which defines --particles-rgb) has applied. Re-read once it lands so
    // the loop swaps the pre-CSS JS fallback (peach in light) for the real themed
    // token instead of staying stuck on the fallback all session.
    l.onload = function () { if (running) readPalette(); };
    document.head.appendChild(l);
  })();

  var REPEL = 90;          // cursor influence radius (px)

  var genui, ctx;
  var DPR = Math.min(window.devicePixelRatio || 1, 2);
  var W = 0, H = 0;
  var parts = [];
  var realPointer = { x: -9999, y: -9999, active: false };
  var bootPointer = { x: -9999, y: -9999, active: false };
  var running = false, rafId = 0, lastT = 0;
  var orbRgb = '180,200,255';
  var wasLight = null;

  function isLight() { return document.body && document.body.classList.contains('light-mode'); }

  function readPalette() {
    var cs = window.getComputedStyle(document.body);
    var x = (cs.getPropertyValue('--particles-rgb') || '').trim();
    orbRgb = x || (isLight() ? '255,170,110' : '180,200,255');
    wasLight = isLight();
  }

  function build() {
    parts = [];
    var n = Math.round((W * H) / 26000);
    if (n > 90) n = 90;
    for (var i = 0; i < n; i++) {
      parts.push({
        x: Math.random() * W,
        y: Math.random() * H,
        r: 6 + Math.random() * 26,
        vy: -(0.12 + Math.random() * 0.28),
        sway: 0.2 + Math.random() * 0.5,
        phase: Math.random() * Math.PI * 2,
        a: 0.10 + Math.random() * 0.22
      });
    }
  }

  function resize() {
    W = window.innerWidth; H = window.innerHeight;
    genui.width = W * DPR; genui.height = H * DPR;
    genui.style.width = W + 'px'; genui.style.height = H + 'px';
    ctx.setTransform(DPR, 0, 0, DPR, 0, 0);
    readPalette();
    build();
  }

  function frame(t) {
    if (!running) return;
    t = t || performance.now();
    var dt = lastT ? Math.min(50, t - lastT) : 16;
    lastT = t;
    if (isLight() !== wasLight) readPalette();
    ctx.clearRect(0, 0, W, H);

    for (var i = 0; i < parts.length; i++) {
      var p = parts[i];
      p.y += p.vy * dt * 0.06;                         // slow rise
      p.x += Math.sin(t * 0.0004 + p.phase) * p.sway * 0.3;

      // Gentle repel from both the real pointer and the independent boot orbit.
      var inputs = [realPointer, bootPointer];
      for (var pi = 0; pi < inputs.length; pi++) {
        var input = inputs[pi];
        if (!input.active) continue;
        var dx = p.x - input.x, dy = p.y - input.y;
        var dist = Math.sqrt(dx * dx + dy * dy);
        if (dist < REPEL && dist > 0.01) {
          var f = (1 - dist / REPEL) * 1.2;
          p.x += (dx / dist) * f;
          p.y += (dy / dist) * f;
        }
      }

      // Wrap around when it floats off the top / sides.
      if (p.y + p.r < 0) { p.y = H + p.r; p.x = Math.random() * W; }
      if (p.x < -p.r) p.x = W + p.r;
      else if (p.x > W + p.r) p.x = -p.r;

      var g = ctx.createRadialGradient(p.x, p.y, 0, p.x, p.y, p.r);
      g.addColorStop(0, 'rgba(' + orbRgb + ',' + p.a + ')');
      g.addColorStop(0.6, 'rgba(' + orbRgb + ',' + (p.a * 0.35) + ')');
      g.addColorStop(1, 'rgba(' + orbRgb + ',0)');
      ctx.fillStyle = g;
      ctx.beginPath();
      ctx.arc(p.x, p.y, p.r, 0, Math.PI * 2);
      ctx.fill();
    }

    if (document.hidden) rafId = setTimeout(function () { frame(performance.now()); }, 90);
    else rafId = requestAnimationFrame(frame);
  }

  function onPointerMove(e) { realPointer.x = e.clientX; realPointer.y = e.clientY; realPointer.active = true; }
  function onPointerLeave() { realPointer.active = false; }
  function onBootPointerMove(e) {
    var p = e.detail || {};
    if (!Number.isFinite(p.clientX) || !Number.isFinite(p.clientY)) return;
    bootPointer.x = p.clientX; bootPointer.y = p.clientY; bootPointer.active = true;
  }
  function onBootPointerEnd() { bootPointer.active = false; }
  // Touch/pen never fire a "leave" — when the finger lifts there's no further
  // pointermove, so without this the orbs would stay parted around the last
  // touch point forever. Clear the pointer off-screen on lift/cancel (touch &
  // pen only — a mouse click must NOT wipe the live cursor reaction); the orbs
  // then resume their normal drift with no snap.
  function onPointerEnd(e) { if (!e || e.pointerType !== 'mouse') onPointerLeave(); }
  function onResize() { DPR = Math.min(window.devicePixelRatio || 1, 2); resize(); }

  function start(cv) {
    if (running) return;
    genui = cv; ctx = genui.getContext('2d');
    if (reduced) { resize(); return; }
    running = true;
    window.addEventListener('pointermove', onPointerMove);
    document.addEventListener('mouseleave', onPointerLeave);
    window.addEventListener('pointerup', onPointerEnd);
    window.addEventListener('pointercancel', onPointerEnd);
    window.addEventListener('wa-boot-pointermove', onBootPointerMove);
    window.addEventListener('wa-boot-pointerend', onBootPointerEnd);
    window.addEventListener('resize', onResize);
    resize();
    frame(performance.now());
  }

  function stop() {
    running = false;
    if (rafId) { cancelAnimationFrame(rafId); clearTimeout(rafId); rafId = 0; }
    window.removeEventListener('pointermove', onPointerMove);
    document.removeEventListener('mouseleave', onPointerLeave);
    window.removeEventListener('pointerup', onPointerEnd);
    window.removeEventListener('pointercancel', onPointerEnd);
    window.removeEventListener('wa-boot-pointermove', onBootPointerMove);
    window.removeEventListener('wa-boot-pointerend', onBootPointerEnd);
    window.removeEventListener('resize', onResize);
    parts = [];
  }

  // Re-read the palette live when the Appearance panel edits the theme colours
  // (engine calls this on 'wa-appearance-changed'); the running loop repaints.
  function refresh() { if (running) readPalette(); }

  window.WA_BG.register(RID, { start: start, stop: stop, refresh: refresh });
})();

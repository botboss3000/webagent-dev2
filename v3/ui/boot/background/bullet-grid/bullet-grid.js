/* ============================================================
   Background plugin: Bullet grid  (mouse-reactive dot grid)
   ------------------------------------------------------------
   An evenly-spaced grid of small dots ("bullets"). Dots near the cursor
   are pushed away and brighten/grow, then spring back to their home
   position — a soft rippling reaction to mouse movement. It settles to a
   static grid when the pointer is still, avoiding an idle animation loop.

   Colours come from the --bgrid-* tokens in bullet-grid.css (this
   folder), derived from the app palette, so it suits dark and light.
   Self-contained: delete /v3-ui/boot/background/bullet-grid/ and it's gone.
   ============================================================ */
(function () {
  var RID = 'bullet-grid';
  var reduced = window.matchMedia && window.matchMedia('(prefers-reduced-motion: reduce)').matches;

  (function ensureCss() {
    if (document.querySelector('link[data-bg="' + RID + '"]')) return;
    var l = document.createElement('link');
    l.rel = 'stylesheet'; l.href = '/v3-ui/boot/background/bullet-grid/bullet-grid.css';
    l.setAttribute('data-bg', RID);
    // start() reads the palette synchronously on register — BEFORE this async
    // sheet (which defines the --bgrid-* tokens) has applied. Re-read once it
    // lands so the loop swaps the pre-CSS JS fallback (peach in light) for the
    // real themed tokens instead of staying stuck on the fallback all session.
    l.onload = function () { if (running) readPalette(); };
    document.head.appendChild(l);
  })();

  // Tunables
  var RADIUS = 110;        // cursor influence radius (px)
  var PUSH = 9;            // max displacement away from cursor (px) — kept small so it reads as a gentle nudge, not a bounce
  var EASE = 0.10;         // how fast a dot eases toward its target (and back home); low = smooth, no overshoot
  var DOT_R = 1.3;         // base dot radius
  var DOT_GROW = 2.0;      // extra radius right at the cursor

  var genui, ctx;
  var DPR = Math.min(window.devicePixelRatio || 1, 2);
  var W = 0, H = 0, spacing = 34;
  var dots = [];
  var realPointer = { x: -9999, y: -9999, active: false };
  var bootPointer = { x: -9999, y: -9999, active: false };
  var running = false, rafId = 0, lastT = 0;
  var settling = false;
  var POINTER_IDLE_MS = 250;
  var realPointerAt = -Infinity, bootPointerAt = -Infinity;
  var dotRgb = '150,165,215', activeRgb = '125,207,255';
  var wasLight = null;

  function isLight() { return document.body && document.body.classList.contains('light-mode'); }

  function readPalette() {
    var cs = window.getComputedStyle(document.body);
    function v(n, f) { var x = (cs.getPropertyValue(n) || '').trim(); return x || f; }
    var fbDot = isLight() ? '150,110,80' : '150,165,215';
    var fbAct = isLight() ? '255,140,66' : '125,207,255';
    dotRgb = v('--bgrid-dot-rgb', fbDot);
    activeRgb = v('--bgrid-active-rgb', fbAct);
    var sp = parseFloat(v('--bgrid-spacing', '34'));
    if (sp > 8) spacing = sp;
    wasLight = isLight();
  }

  function build() {
    dots = [];
    // Inset half a cell so the grid is centred and edges aren't clipped.
    var cols = Math.ceil(W / spacing) + 1;
    var rows = Math.ceil(H / spacing) + 1;
    var offX = (W - (cols - 1) * spacing) / 2;
    var offY = (H - (rows - 1) * spacing) / 2;
    for (var r = 0; r < rows; r++) {
      for (var c = 0; c < cols; c++) {
        var hx = offX + c * spacing, hy = offY + r * spacing;
        dots.push({ hx: hx, hy: hy, x: hx, y: hy, vx: 0, vy: 0, phase: (c + r) * 0.6 });
      }
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
    lastT = t;
    if (isLight() !== wasLight) readPalette();
    ctx.clearRect(0, 0, W, H);

    if (realPointer.active && t - realPointerAt > POINTER_IDLE_MS) realPointer.active = false;
    if (bootPointer.active && t - bootPointerAt > POINTER_IDLE_MS) bootPointer.active = false;

    settling = false;
    for (var i = 0; i < dots.length; i++) {
      var d = dots[i];
      var homeX = d.hx;
      var homeY = d.hy;

      // Repel from the cursor — compute a static displaced TARGET, then
      // ease toward it. No velocity/spring means no overshoot or wobble:
      // the dot drifts a little away from the cursor and eases straight
      // back home once the cursor leaves. Influence is measured from the
      // dot's HOME, not its live position, so a nudged dot can't feed back
      // into itself and amplify.
      var influence = 0;
      var targetX = homeX, targetY = homeY;
      var offX = 0, offY = 0;
      var inputs = [realPointer, bootPointer];
      for (var pi = 0; pi < inputs.length; pi++) {
        var input = inputs[pi];
        if (!input.active) continue;
        var dx = homeX - input.x, dy = homeY - input.y;
        var dist = Math.sqrt(dx * dx + dy * dy);
        if (dist < RADIUS && dist > 0.01) {
          // Each source has its own falloff. Their displacement vectors add,
          // while brightness uses the strongest nearby input.
          var sourceInfluence = 1 - dist / RADIUS;
          sourceInfluence = sourceInfluence * sourceInfluence;
          influence = Math.max(influence, sourceInfluence);
          var off = sourceInfluence * PUSH;
          offX += (dx / dist) * off;
          offY += (dy / dist) * off;
        }
      }
      var offLen = Math.sqrt(offX * offX + offY * offY);
      var offCap = PUSH * 1.5;
      if (offLen > offCap) { offX *= offCap / offLen; offY *= offCap / offLen; }
      targetX += offX;
      targetY += offY;
      // First-order lag toward the target (and back home).
      if (Math.abs(targetX - d.x) > 0.05 || Math.abs(targetY - d.y) > 0.05) settling = true;
      d.x += (targetX - d.x) * EASE;
      d.y += (targetY - d.y) * EASE;

      // Draw — brighter / larger near the cursor.
      var rr = DOT_R + influence * DOT_GROW;
      if (influence > 0.02) {
        var a = 0.5 + influence * 0.5;
        ctx.fillStyle = 'rgba(' + activeRgb + ',' + a + ')';
      } else {
        ctx.fillStyle = 'rgba(' + dotRgb + ',0.32)';
      }
      ctx.beginPath();
      ctx.arc(d.x, d.y, rr, 0, Math.PI * 2);
      ctx.fill();
    }

    if (!document.hidden && (realPointer.active || bootPointer.active || settling)) rafId = requestAnimationFrame(frame);
    else rafId = 0;
  }

  function requestFrame() { if (running && !document.hidden && !rafId) rafId = requestAnimationFrame(frame); }
  function onPointerMove(e) { realPointer.x = e.clientX; realPointer.y = e.clientY; realPointer.active = true; realPointerAt = performance.now(); requestFrame(); }
  function onPointerLeave() { realPointer.active = false; requestFrame(); }
  function onBootPointerMove(e) {
    var p = e.detail || {};
    if (!Number.isFinite(p.clientX) || !Number.isFinite(p.clientY)) return;
    bootPointer.x = p.clientX; bootPointer.y = p.clientY; bootPointer.active = true; bootPointerAt = performance.now(); requestFrame();
  }
  function onBootPointerEnd() { bootPointer.active = false; requestFrame(); }
  // Touch/pen never fire a "leave" — when the finger lifts there's no further
  // pointermove, so without this the grid would stay frozen in its pushed state
  // at the last touch point. Clear the pointer off-screen on lift/cancel (touch
  // & pen only — a mouse click must NOT wipe the live cursor reaction); the dots
  // then ease smoothly back home through the normal first-order lag.
  function onPointerEnd(e) { if (!e || e.pointerType !== 'mouse') onPointerLeave(); }
  function onResize() { DPR = Math.min(window.devicePixelRatio || 1, 2); resize(); requestFrame(); }
  function onVisibilityChange() { if (!document.hidden) requestFrame(); }

  function start(cv) {
    if (running) return;
    genui = cv; ctx = genui.getContext('2d');
    if (reduced) { resize(); return; }  // genui hidden under reduced-motion
    running = true;
    window.addEventListener('pointermove', onPointerMove);
    document.addEventListener('mouseleave', onPointerLeave);
    window.addEventListener('pointerup', onPointerEnd);
    window.addEventListener('pointercancel', onPointerEnd);
    window.addEventListener('wa-boot-pointermove', onBootPointerMove);
    window.addEventListener('wa-boot-pointerend', onBootPointerEnd);
    window.addEventListener('resize', onResize);
    document.addEventListener('visibilitychange', onVisibilityChange);
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
    document.removeEventListener('visibilitychange', onVisibilityChange);
    dots = [];
  }

  // Re-read the palette live when the Appearance panel edits the theme colours
  // (engine calls this on 'wa-appearance-changed'). --bgrid-active-rgb tracks the
  // accent (var(--brand-rgb)), so the cursor dot recolours instantly.
  function refresh() { if (running) readPalette(); }

  window.WA_BG.register(RID, { start: start, stop: stop, refresh: refresh });
})();

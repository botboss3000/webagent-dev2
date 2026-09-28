/* ============================================================
   Background plugin: Aurora  (slow flowing gradient mesh)
   ------------------------------------------------------------
   A few large, soft, blurred colour blobs that drift along gentle sine
   paths — a calm animated gradient mesh with no per-pixel mouse
   reaction. Blobs glow additively in dark mode and tint softly in light
   mode. Colours come from the --aurora-* tokens in aurora.css.
   ============================================================ */
(function () {
  var RID = 'aurora';
  var reduced = window.matchMedia && window.matchMedia('(prefers-reduced-motion: reduce)').matches;

  (function ensureCss() {
    if (document.querySelector('link[data-bg="' + RID + '"]')) return;
    var l = document.createElement('link');
    l.rel = 'stylesheet'; l.href = 'ui/background/aurora/aurora.css';
    l.setAttribute('data-bg', RID);
    // start() reads the palette synchronously on register — BEFORE this async
    // sheet (which defines the --aurora-* tokens) has applied. Re-read once it
    // lands so the loop swaps the pre-CSS JS fallback (peach in light) for the
    // real themed tokens instead of staying stuck on the fallback all session.
    l.onload = function () { if (running) readPalette(); };
    document.head.appendChild(l);
  })();

  var genui, ctx;
  var DPR = Math.min(window.devicePixelRatio || 1, 2);
  var W = 0, H = 0;
  var blobs = [];
  var running = false, rafId = 0;
  var colors = ['90,110,200', '150,110,210', '70,90,170'];
  var wasLight = null;

  function isLight() { return document.body && document.body.classList.contains('light-mode'); }

  function readPalette() {
    var cs = window.getComputedStyle(document.body);
    function v(n, f) { var x = (cs.getPropertyValue(n) || '').trim(); return x || f; }
    if (isLight()) {
      colors = [
        v('--aurora-1-rgb', '255,190,130'),
        v('--aurora-2-rgb', '255,150,90'),
        v('--aurora-3-rgb', '255,210,170')
      ];
    } else {
      colors = [
        v('--aurora-1-rgb', '90,110,200'),
        v('--aurora-2-rgb', '150,110,210'),
        v('--aurora-3-rgb', '70,90,170')
      ];
    }
    wasLight = isLight();
  }

  function build() {
    blobs = [];
    var n = 5;
    for (var i = 0; i < n; i++) {
      blobs.push({
        bx: Math.random(), by: Math.random(),          // base position (0..1)
        ax: 0.10 + Math.random() * 0.14,               // drift amplitude
        ay: 0.10 + Math.random() * 0.14,
        sx: 0.00006 + Math.random() * 0.00010,         // drift speed
        sy: 0.00006 + Math.random() * 0.00010,
        px: Math.random() * Math.PI * 2,
        py: Math.random() * Math.PI * 2,
        rad: 0.34 + Math.random() * 0.22,              // radius as frac of max(W,H)
        ci: i % 3
      });
    }
  }

  function resize() {
    W = window.innerWidth; H = window.innerHeight;
    genui.width = W * DPR; genui.height = H * DPR;
    genui.style.width = W + 'px'; genui.style.height = H + 'px';
    ctx.setTransform(DPR, 0, 0, DPR, 0, 0);
    readPalette();
    if (!blobs.length) build();
  }

  function frame(t) {
    if (!running) return;
    t = t || performance.now();
    if (isLight() !== wasLight) readPalette();
    ctx.clearRect(0, 0, W, H);
    var light = isLight();
    var prevComp = ctx.globalCompositeOperation;
    ctx.globalCompositeOperation = light ? 'multiply' : 'lighter';
    var maxD = Math.max(W, H);

    for (var i = 0; i < blobs.length; i++) {
      var b = blobs[i];
      var cx = (b.bx + Math.sin(t * b.sx + b.px) * b.ax) * W;
      var cy = (b.by + Math.cos(t * b.sy + b.py) * b.ay) * H;
      var R = b.rad * maxD;
      var col = colors[b.ci];
      var g = ctx.createRadialGradient(cx, cy, 0, cx, cy, R);
      g.addColorStop(0, 'rgba(' + col + ',' + (light ? 0.16 : 0.22) + ')');
      g.addColorStop(0.5, 'rgba(' + col + ',' + (light ? 0.07 : 0.09) + ')');
      g.addColorStop(1, 'rgba(' + col + ',0)');
      ctx.fillStyle = g;
      ctx.beginPath();
      ctx.arc(cx, cy, R, 0, Math.PI * 2);
      ctx.fill();
    }
    ctx.globalCompositeOperation = prevComp;

    if (document.hidden) rafId = setTimeout(function () { frame(performance.now()); }, 90);
    else rafId = requestAnimationFrame(frame);
  }

  function onResize() { DPR = Math.min(window.devicePixelRatio || 1, 2); resize(); }

  function start(cv) {
    if (running) return;
    genui = cv; ctx = genui.getContext('2d');
    if (reduced) { resize(); return; }
    running = true;
    window.addEventListener('resize', onResize);
    resize();
    frame(performance.now());
  }

  function stop() {
    running = false;
    if (rafId) { cancelAnimationFrame(rafId); clearTimeout(rafId); rafId = 0; }
    window.removeEventListener('resize', onResize);
    blobs = [];
  }

  // Re-read the palette live when the Appearance panel edits the theme colours
  // (engine calls this on 'wa-appearance-changed'); the running loop repaints.
  function refresh() { if (running) readPalette(); }

  window.WA_BG.register(RID, { start: start, stop: stop, refresh: refresh });
})();

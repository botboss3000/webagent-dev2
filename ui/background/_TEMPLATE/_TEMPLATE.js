/* ============================================================
   Background plugin SKELETON — copy this folder to add a background
   ------------------------------------------------------------
   1. Copy ui/background/_TEMPLATE/ to ui/background/<your-id>/
   2. Rename the files to <your-id>.js / <your-id>.json / <your-id>.css
   3. Set RID below to "<your-id>" (must match the folder + json id)
   4. Fill in start()/stop(); add your tokens to <your-id>.css
   That's it — the background auto-appears in the admin Appearance
   selector (App Settings) via GET /admin/settings/backgrounds. No core
   edits, no registration. Delete the folder to remove it everywhere.

   Contract:
     • window.WA_BG.register(RID, { start, stop, refresh })
     • start(genui) — the engine hands you the shared <genui>. Own your
       sizing (devicePixelRatio), listeners and requestAnimationFrame.
     • stop()        — cancel the loop, REMOVE every listener you added,
       leave the genui clean. Called on theme flip / background swap.
     • refresh()     — OPTIONAL. The engine calls this when the admin edits
       the theme colours live in the Appearance panel. Re-read your CSS
       tokens so the running loop repaints in the new palette (no resize /
       restart). Omit it if your look isn't palette-derived.
     • Read colours from CSS tokens (getComputedStyle) with JS fallbacks
       so there's no flash before <your-id>.css loads.
     • Bail when prefers-reduced-motion is set (the genui is hidden by
       CSS there anyway).
     • If you react to the pointer: touch/pen never fire mouseleave, so on
       pointerup/pointercancel (guard pointerType !== 'mouse') reset the
       pointer to its rest state or the effect stays stuck at the last touch
       point. Let your easing carry it back smoothly.
   ============================================================ */
(function () {
  var RID = '_TEMPLATE';  // ← change to your folder/id
  var reduced = window.matchMedia && window.matchMedia('(prefers-reduced-motion: reduce)').matches;

  (function ensureCss() {
    if (document.querySelector('link[data-bg="' + RID + '"]')) return;
    var l = document.createElement('link');
    l.rel = 'stylesheet'; l.href = 'ui/background/' + RID + '/' + RID + '.css';
    l.setAttribute('data-bg', RID);
    // start() reads the palette synchronously on register — BEFORE this async
    // sheet (your --<id>-* tokens) has applied, so the first read uses the JS
    // fallbacks. Re-read once the sheet lands so the loop picks up the real
    // themed tokens instead of staying stuck on the fallbacks all session.
    l.onload = function () { if (running) readPalette(); };
    document.head.appendChild(l);
  })();

  var genui, ctx;
  var DPR = Math.min(window.devicePixelRatio || 1, 2);
  var W = 0, H = 0;
  var running = false, rafId = 0;

  function readPalette() {
    // var cs = window.getComputedStyle(document.body);
    // read your --your-id-* tokens here (with JS fallbacks)
  }

  function resize() {
    W = window.innerWidth; H = window.innerHeight;
    genui.width = W * DPR; genui.height = H * DPR;
    genui.style.width = W + 'px'; genui.style.height = H + 'px';
    ctx.setTransform(DPR, 0, 0, DPR, 0, 0);
    readPalette();
    // (re)build your scene here
  }

  function frame() {
    if (!running) return;
    ctx.clearRect(0, 0, W, H);
    // —— draw your background here ——
    rafId = requestAnimationFrame(frame);
  }

  function onResize() { DPR = Math.min(window.devicePixelRatio || 1, 2); resize(); }

  function start(cv) {
    if (running) return;
    genui = cv; ctx = genui.getContext('2d');
    if (reduced) { resize(); return; }
    running = true;
    window.addEventListener('resize', onResize);
    resize();
    frame();
  }

  function stop() {
    running = false;
    if (rafId) { cancelAnimationFrame(rafId); rafId = 0; }
    window.removeEventListener('resize', onResize);
  }

  // OPTIONAL: re-read the palette live on an Appearance-panel colour edit.
  function refresh() { if (running) readPalette(); }

  window.WA_BG.register(RID, { start: start, stop: stop, refresh: refresh });
})();

/* ============================================================
   Cursor glow + per-element spotlight tracking
   ------------------------------------------------------------
   These are UI-polish effects that follow the pointer, INDEPENDENT of
   which animated background is running (they were previously bundled
   into stargaze.js). They stay on for every background — including the
   "None" background — which is why they live in shared/ rather than in
   a background plugin folder.

     • #cursor-glow  — a soft full-viewport radial glow under the cursor.
     • .spot         — cards / bubbles / popups get an inner spotlight
                       that follows the pointer (CSS in index.css reads
                       the --mx/--my variables set here).

   Disabled under prefers-reduced-motion (index.css also hides
   #cursor-glow there), matching the prior behaviour.
   ============================================================ */
(function () {
  if (window.matchMedia && window.matchMedia('(prefers-reduced-motion: reduce)').matches) return;

  /* —— Full-viewport cursor glow —— */
  var glowEl = document.getElementById('cursor-glow');
  var glowOn = false;
  window.addEventListener('pointermove', function (e) {
    if (glowEl) {
      glowEl.style.setProperty('--gx', e.clientX + 'px');
      glowEl.style.setProperty('--gy', e.clientY + 'px');
      if (!glowOn) { glowEl.classList.add('is-on'); glowOn = true; }
    }
  });
  document.addEventListener('mouseleave', function () {
    if (glowEl && glowOn) { glowEl.classList.remove('is-on'); glowOn = false; }
  });
  // The boot orbit is a second independent input. Keeping its coordinates in
  // separate CSS variables lets the glow render both sources simultaneously
  // instead of flickering between real and synthetic pointermove events.
  window.addEventListener('wa-boot-pointermove', function (e) {
    var p = e.detail || {};
    if (!glowEl || !Number.isFinite(p.clientX) || !Number.isFinite(p.clientY)) return;
    glowEl.style.setProperty('--boot-gx', p.clientX + 'px');
    glowEl.style.setProperty('--boot-gy', p.clientY + 'px');
    glowEl.classList.add('boot-is-on');
  });
  window.addEventListener('wa-boot-pointerend', function () {
    if (glowEl) glowEl.classList.remove('boot-is-on');
  });

  /* —— Spotlight tracking for cards / bubbles / dropdowns / panels —— */
  var SPOT_SELECTORS = [
    '#main-panel',
    '#chat-resize-handle',
    '.ac-stickynav .ac-category-summary',
    '.chat-bubble',
    '.tab-select-popup',
    '.tab-option',
    '.tab-select-pill',
    '.gh-restricted-card',
    '.files-restricted-card',
    '.ac-card',
    '.agent-card',
    '.tutorial-card',
    '.card'
  ];
  var SPOT_SELECTOR_STR = SPOT_SELECTORS.join(', ');

  function decorate(root) {
    var nodes = root && root.querySelectorAll ? root.querySelectorAll(SPOT_SELECTOR_STR) : [];
    for (var i = 0; i < nodes.length; i++) nodes[i].classList.add('spot');
    if (root && root.matches && root.matches(SPOT_SELECTOR_STR)) root.classList.add('spot');
  }
  decorate(document);

  // Watch for dynamically added elements (new chat bubbles, popups, etc.)
  var mo = new MutationObserver(function (mutations) {
    for (var m = 0; m < mutations.length; m++) {
      var added = mutations[m].addedNodes;
      for (var n = 0; n < added.length; n++) {
        var node = added[n];
        if (node.nodeType === 1) decorate(node);
      }
    }
  });
  mo.observe(document.body, { childList: true, subtree: true });

  // Single delegated pointermove to update --mx/--my on the nearest .spot
  document.addEventListener('pointermove', function (e) {
    var el = e.target && e.target.closest ? e.target.closest('.spot') : null;
    if (!el) return;
    var r = el.getBoundingClientRect();
    var mx = ((e.clientX - r.left) / r.width) * 100;
    var my = ((e.clientY - r.top) / r.height) * 100;
    el.style.setProperty('--mx', mx + '%');
    el.style.setProperty('--my', my + '%');
    if (!el.classList.contains('spot-active')) el.classList.add('spot-active');
  }, true);

  // Clear .spot-active when pointer leaves a tracked element
  document.addEventListener('pointerleave', function (e) {
    if (e.target && e.target.classList && e.target.classList.contains('spot')) {
      e.target.classList.remove('spot-active');
    }
  }, true);
  // Also clear on pointerout (more reliable for some elements)
  document.addEventListener('pointerout', function (e) {
    var from = e.target;
    var to = e.relatedTarget;
    if (from && from.classList && from.classList.contains('spot')) {
      if (!to || !from.contains(to)) from.classList.remove('spot-active');
    }
  }, true);
})();

/* V3 boot-only glow: no persistent pointer or card spotlight effects. */
(function () {
  'use strict';
  var glow = document.getElementById('cursor-glow');
  function move(e) {
    var p = e.detail || {};
    if (!glow || !Number.isFinite(p.clientX) || !Number.isFinite(p.clientY)) return;
    glow.style.setProperty('--boot-gx', p.clientX + 'px');
    glow.style.setProperty('--boot-gy', p.clientY + 'px');
    glow.classList.add('boot-is-on');
  }
  function finish() {
    if (glow) glow.remove();
    glow = null;
    observer.disconnect();
    window.removeEventListener('wa-boot-pointermove', move);
    window.removeEventListener('wa-boot-pointerend', finish);
  }
  function bootActive() {
    return document.body.classList.contains('is-booting') || !!document.getElementById('boot-curtain');
  }
  var observer = new MutationObserver(function () { if (!bootActive()) finish(); });
  observer.observe(document.body, {childList:true, attributes:true, attributeFilter:['class']});
  window.addEventListener('wa-boot-pointerend', finish);
  if (!bootActive()) { finish(); return; }
  if (!window.matchMedia('(prefers-reduced-motion: reduce)').matches) {
    window.addEventListener('wa-boot-pointermove', move);
  }
})();

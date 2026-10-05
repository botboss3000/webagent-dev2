/* V3 boot-only adapter for the synchronized v2 Bullet Grid renderer.
   Never select a persistent app background, even from an old cached choice. */
(function () {
  'use strict';
  var host = document.getElementById('stargaze-bg');
  if (!host) return;
  var ended = false;
  var activeApi = null;
  var surface = document.createElement('canvas');
  surface.dataset.waBgSurface = 'true';
  surface.setAttribute('aria-hidden', 'true');
  surface.style.cssText = 'position:absolute;inset:0;display:block;pointer-events:none;';
  host.appendChild(surface);
  function bootActive() {
    return document.body.classList.contains('is-booting') || !!document.getElementById('boot-curtain');
  }
  function finish() {
    if (ended) return;
    ended = true;
    if (activeApi) activeApi.stop();
    activeApi = null;
    host.remove();
    observer.disconnect();
    window.removeEventListener('wa-boot-pointerend', finish);
  }
  var observer = new MutationObserver(function () { if (!bootActive()) finish(); });
  observer.observe(document.body, {childList:true, attributes:true, attributeFilter:['class']});
  window.addEventListener('wa-boot-pointerend', finish);
  window.WA_BG.active = function () { return activeApi ? 'bullet-grid' : 'none'; };
  if (!bootActive()) { finish(); return; }
  var script = document.createElement('script');
  script.src = '/v3-ui/boot/background/bullet-grid/bullet-grid.js';
  script.onerror = finish;
  window.WA_BG.whenReady('bullet-grid', function (api) {
    if (ended || !bootActive()) return;
    activeApi = api;
    api.start(surface);
  });
  document.head.appendChild(script);
})();

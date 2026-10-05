"use strict";
  // Pinch-zoom guard. The viewport meta already sets user-scalable=no, but iOS
  // Safari ignores that, so we also block the non-standard gesture events it
  // fires before zooming, plus any multi-touch move as a backstop. Single-finger
  // panning/scrolling is untouched.
  (function () {
    function blockZoom(e) { e.preventDefault(); }
    document.addEventListener('gesturestart', blockZoom);
    document.addEventListener('gesturechange', blockZoom);
    document.addEventListener('gestureend', blockZoom);
    document.addEventListener('touchmove', function (e) {
      if (e.touches && e.touches.length > 1) e.preventDefault();
    }, { passive: false });
  })();

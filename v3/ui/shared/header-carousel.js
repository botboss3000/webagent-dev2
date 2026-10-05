"use strict";
export function initHeaderCarousel() {
const tabBar=document.getElementById("main-tabs"), tabWrap=document.getElementById("main-tabs-wrap"), chevLeft=document.getElementById("main-tabs-chev-left"), chevRight=document.getElementById("main-tabs-chev-right");
  function updateChevrons() {
    if (!chevLeft || !chevRight) return;
    var overflow = tabBar.scrollWidth - tabBar.clientWidth > 1;
    tabWrap.classList.toggle('has-overflow', overflow);
    chevLeft.classList.toggle('visible', overflow && tabBar.scrollLeft > 1);
    chevRight.classList.toggle('visible', overflow && tabBar.scrollLeft < tabBar.scrollWidth - tabBar.clientWidth - 1);
  }

  function syncActive(tabId) {
    tabBar.querySelectorAll('.main-tab').forEach(function (b) {
      b.classList.toggle('active', b.dataset.value === tabId);
    });
    var activeBtn = tabBar.querySelector('.main-tab.active');
    if (activeBtn) activeBtn.scrollIntoView({ behavior: 'smooth', inline: 'center', block: 'nearest' });
    requestAnimationFrame(updateChevrons);
    setTimeout(updateChevrons, 350); // catch end of smooth scroll
  }

  // Chevron carousel (matches app-config tabs pattern)
  if (chevLeft && chevRight) {
    var scrollStep = function () { return Math.max(80, Math.floor(tabBar.clientWidth * 0.6)); };
    chevLeft.addEventListener('click', function () { tabBar.scrollBy({ left: -scrollStep(), behavior: 'smooth' }); });
    chevRight.addEventListener('click', function () { tabBar.scrollBy({ left: scrollStep(), behavior: 'smooth' }); });
    tabBar.addEventListener('scroll', updateChevrons, { passive: true });

    requestAnimationFrame(function () {
      updateChevrons();
      var active = tabBar.querySelector('.main-tab.active');
      if (active) active.scrollIntoView({ inline: 'center', block: 'nearest' });
    });
    if (typeof ResizeObserver !== 'undefined') {
      var roPending = false;
      var ro = new ResizeObserver(function () {
        if (roPending) return;
        roPending = true;
        requestAnimationFrame(function () { roPending = false; updateChevrons(); });
      });
      ro.observe(tabBar);
      ro.observe(tabWrap);
    }
    window.addEventListener('resize', updateChevrons);
  }

  // ── Rubber-band overscroll for the tab carousel ──
  // Matches the footer handle's spring curve. Intercepts wheel + touch at
  // scroll boundaries and applies a diminishing translateX that snaps back
  // on release. CSS overscroll-behavior-x: none prevents native bounce from
  // competing on macOS trackpad.
  (function () {
    var _rbX = 0, _rbTimer = null;
    var MAX_PULL = 60, RESIST = 0.35;
    var SPRING = 'cubic-bezier(0.25, 0.46, 0.45, 0.94)';
    var _touchLastX = 0, _touchActive = false;
    var _snapping = false;
    var _snapTimeout = null;

    function _snap() {
      if (Math.abs(_rbX) < 0.5) return;
      _snapping = true;
      tabBar.style.transition = 'transform 0.45s ' + SPRING;
      _rbX = 0;
      tabBar.style.transform = '';
      clearTimeout(_rbTimer);
      _rbTimer = setTimeout(function () {
        tabBar.style.transition = '';
      }, 500);
      // Windows precision trackpad momentum can last 2+ s — keep ALL
      // wheel events suppressed for long enough that the tail is gone
      // before the rubber band re-arms. Otherwise residual momentum
      // triggers a second, weaker band after the first snap ends.
      clearTimeout(_snapTimeout);
      _snapTimeout = setTimeout(function () { _snapping = false; }, 2500);
    }

    function _pull(dx) {
      if (_snapping) return false;
      clearTimeout(_rbTimer);
      tabBar.style.transition = 'none';
      var maxS = tabBar.scrollWidth - tabBar.clientWidth;
      var atL = tabBar.scrollLeft <= 0 && dx < 0;
      var atR = tabBar.scrollLeft >= maxS - 0.5 && dx > 0;
      if (!atL && !atR) {
        if (Math.abs(_rbX) > 0.5) _snap();
        return false;
      }
      _rbX -= dx * RESIST;
      _rbX = Math.max(-MAX_PULL, Math.min(MAX_PULL, _rbX));
      tabBar.style.transform = 'translateX(' + _rbX + 'px)';
      _rbTimer = setTimeout(_snap, 120);
      return true;
    }

    // Wheel / trackpad — uses the dominant axis so a mouse wheel (which
    // produces deltaY, not deltaX) also scrolls the tab strip horizontally.
    // Boundary rubber band still fires; mid-strip, manual scrollLeft keeps
    // the page from scrolling vertically.
    tabBar.addEventListener('wheel', function (e) {
      // Pick the axis with the larger magnitude — trackpad → deltaX,
      // mouse wheel → deltaY.
      var dx = Math.abs(e.deltaX) > Math.abs(e.deltaY) ? e.deltaX : e.deltaY;
      if (!dx) return;
      var maxS = tabBar.scrollWidth - tabBar.clientWidth;
      if (_snapping) {
        if ((tabBar.scrollLeft <= 0 && dx < 0) || (tabBar.scrollLeft >= maxS - 0.5 && dx > 0)) {
          e.preventDefault();
        }
        return;
      }
      if (_pull(dx)) { e.preventDefault(); return; }
      // Not at a boundary — scroll the strip ourselves so deltaY from a
      // physical wheel doesn't scroll the page instead.
      tabBar.scrollLeft += dx;
      e.preventDefault();
    }, { passive: false });

    // Touch — track inter-frame delta past the boundary
    tabBar.addEventListener('touchstart', function (e) {
      var t = e.touches[0];
      if (!t) return;
      _touchLastX = t.clientX;
      _touchActive = true;
    }, { passive: true });

    tabBar.addEventListener('touchmove', function (e) {
      if (!_touchActive) return;
      var t = e.touches[0];
      if (!t) return;
      var dx = _touchLastX - t.clientX;  // positive = swipe left
      _touchLastX = t.clientX;
      var maxS = tabBar.scrollWidth - tabBar.clientWidth;
      var atL = tabBar.scrollLeft <= 0 && dx < 0;
      var atR = tabBar.scrollLeft >= maxS - 0.5 && dx > 0;
      if (atL || atR) {
        clearTimeout(_rbTimer);
        tabBar.style.transition = 'none';
        _rbX -= dx * RESIST;
        _rbX = Math.max(-MAX_PULL, Math.min(MAX_PULL, _rbX));
        tabBar.style.transform = 'translateX(' + _rbX + 'px)';
        _rbTimer = setTimeout(_snap, 120);
        e.preventDefault();  // suppress native overscroll
      } else if (Math.abs(_rbX) > 0.5) {
        _snap();
      }
    }, { passive: false });

    function _touchEnd() {
      _touchActive = false;
      if (Math.abs(_rbX) > 0.5) _snap();
    }
    tabBar.addEventListener('touchend', _touchEnd, { passive: true });
    tabBar.addEventListener('touchcancel', _touchEnd, { passive: true });
  })();


let last="";
function refresh(){const active=tabBar.querySelector(".main-tab.active");const id=active?.dataset.value;if(id && id!==last){last=id;syncActive(id);}else updateChevrons();}
new MutationObserver(refresh).observe(tabBar,{subtree:true,childList:true,attributes:true,attributeFilter:["class"]});
window.__refreshTabStrip=refresh; refresh();
}

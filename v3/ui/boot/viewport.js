"use strict";
  // Track the visible viewport height so the app shrinks when the mobile
  // on-screen keyboard appears. dvh handles browser chrome but on Android
  // Chrome does not shrink for keyboards, so we drive --app-height from
  // window.visualViewport.height (falls back to innerHeight).
  //
  // We ALSO flag the mobile soft keyboard being open by toggling `body.kb-open`:
  // the keyboard is open when the visual viewport is meaningfully shorter than
  // the (constant) layout viewport AND an editable field holds focus. Some pages
  // hide bottom-docked clutter while it's up so the focused field isn't covered —
  // e.g. the App Config chat pill + sticky section headings (see app3.css
  // `body.kb-open` rules). Class is mobile-gated here, so desktop never sets it.
  (function () {
    var root = document.documentElement;
    var vv = window.visualViewport;
    function apply() {
      var h = (vv ? vv.height : window.innerHeight);
      root.style.setProperty('--app-height', h + 'px');
    }
    function applyKb() {
      if (!document.body) return;
      var isMobile = window.innerWidth <= 800;       // mirrors __isMobileChatLayout / the CSS breakpoint
      var vvh = (vv ? vv.height : window.innerHeight);
      var ae = document.activeElement;
      var editable = !!ae && (ae.tagName === 'INPUT' || ae.tagName === 'TEXTAREA' || ae.isContentEditable);
      // Keyboards are ~250px+; a 120px floor clears browser-chrome jitter.
      var open = isMobile && editable && (window.innerHeight - vvh) > 120;
      document.body.classList.toggle('kb-open', open);
      // hide-header-on-keyboard threshold check. When the toggle is ON and
      // threshold=0 we always show the hidden state; with a positive threshold
      // we only hide when the remaining viewport height is below it.
      if (document.body.classList.contains('hide-header-kb')) {
        var raw = document.body.dataset.hhkThreshold;
        var thresh = parseInt(raw, 10);
        if (isNaN(thresh) || thresh < 0) thresh = 200;
        var below = thresh === 0 ? open : (open && vvh < thresh);
        document.body.classList.toggle('hhk-active', below);
      } else {
        document.body.classList.remove('hhk-active');
      }
    }
    function applyAll() { apply(); applyKb(); }
    window.__appHeightApply = apply;
    applyAll();
    if (vv) {
      vv.addEventListener('resize', applyAll);
      vv.addEventListener('scroll', applyAll);
    }
    window.addEventListener('resize', applyAll);
    window.addEventListener('orientationchange', function () {
      setTimeout(applyAll, 120);
    });
    // rAF scroll-lock: fights iOS Safari auto-scroll during keyboard open.
    // On focus, forces scrollTo(0,0) every frame for 400ms — the keyboard
    // animation duration — so iOS can't overshoot the pill.
    var scrollLockRaf = null;
    function scrollLock() { window.scrollTo(0, 0); }
    function rAFScrollLock() {
      var start = Date.now();
      scrollLock();
      if (scrollLockRaf) cancelAnimationFrame(scrollLockRaf);
      function loop() {
        scrollLock();
        if (Date.now() - start < 400) {
          scrollLockRaf = requestAnimationFrame(loop);
        }
      }
      scrollLockRaf = requestAnimationFrame(loop);
    }
    function needsMobileKeyboardLock(target) {
      if (window.innerWidth > 800 || !target) return false;
      return target.tagName === 'INPUT'
        || target.tagName === 'TEXTAREA'
        || target.isContentEditable;
    }
    document.addEventListener('focusin', function (event) {
      apply();       // update --app-height NOW so iOS Safari's auto-scroll
      applyKb();     // doesn't overshoot before the rAF loop kicks in
      // This lock exists only for a mobile soft keyboard. Running it for every
      // desktop button focus starts a 400 ms scroll loop between mousedown and
      // click, which makes controls across the app appear to need two clicks.
      if (needsMobileKeyboardLock(event.target)) rAFScrollLock();
    });
    document.addEventListener('focusout', function () { setTimeout(applyKb, 0); });
  })();

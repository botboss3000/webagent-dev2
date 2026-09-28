/* ============================================================================
   Splash effects engine — shared scroll/hover choreography
   ----------------------------------------------------------------------------
   The premium motion for the welcome landing page (ui/splash/splash-page),
   extracted so the standalone landing bootstrap (splash-landing.js) runs the
   exact same effects. Operates ONLY on the passed-in root + its inner
   [data-splash-scroll] scroller (never window scroll), so it stays self-contained
   and reusable. Lenis smooth-scroll is loaded lazily and degrades gracefully.

   initSplashEffects(root, scrollEl, base, { reduceMotion }) → { destroy() }
     root      — the #splash-root element holding the markup
     scrollEl  — the [data-splash-scroll] scroll container inside it
     base      — the plugin asset base (e.g. "/ui/splash/splash-page/")
   ========================================================================== */

export function initSplashEffects(root, scrollEl, base, opts) {
  opts = opts || {};
  const reduceMotion = !!opts.reduceMotion;
  let lenis = null;
  let rafId = 0;

  if (!root || !scrollEl) return { destroy() {} };

  // Wrap all scroll children in a single content node (Lenis needs a wrapper +
  // a content element). Done before Lenis init.
  let content = scrollEl.querySelector('.splash-content');
  if (!content) {
    content = document.createElement('div');
    content.className = 'splash-content';
    while (scrollEl.firstChild) content.appendChild(scrollEl.firstChild);
    scrollEl.appendChild(content);
  }

  _wireImageFallbacks(root);
  _wirePointerEffects(root, reduceMotion);

  (async () => {
    if (!reduceMotion) {
      const Lenis = await _loadLenis(base);
      if (Lenis && root.isConnected) {
        try {
          lenis = new Lenis({
            wrapper: scrollEl,
            content,
            duration: 1.15,
            easing: (t) => Math.min(1, 1.001 - Math.pow(2, -10 * t)),
            smoothWheel: true,
            touchMultiplier: 1.4,
          });
        } catch (e) { console.warn('[splash] Lenis init failed', e); lenis = null; }
      }
    }

    // Intercept in-page anchor jumps (e.g. "Take the tour").
    root.querySelectorAll('[data-splash-scrollto]').forEach((a) => {
      a.addEventListener('click', (e) => {
        e.preventDefault();
        const sel = a.getAttribute('href');
        const target = sel && scrollEl.querySelector(sel);
        if (!target) return;
        if (lenis) lenis.scrollTo(target, { offset: -10 });
        else target.scrollIntoView({ behavior: reduceMotion ? 'auto' : 'smooth' });
      });
    });

    // Cache effect targets.
    const topbar = root.querySelector('[data-splash-topbar]');
    const frames = Array.from(root.querySelectorAll('.sf-frame[data-tilt]'));
    const pinSection = root.querySelector('[data-splash-pin]');
    const pinShots = Array.from(root.querySelectorAll('[data-splash-pin-stage] .sp-pin-shot'));
    const pinDots = Array.from(root.querySelectorAll('[data-splash-pin-dots] i'));

    // Reveal-on-scroll via IntersectionObserver (works with or without Lenis).
    const io = new IntersectionObserver((entries) => {
      for (const en of entries) {
        if (en.isIntersecting) { en.target.classList.add('is-in'); io.unobserve(en.target); }
      }
    }, { root: scrollEl, threshold: 0.18 });
    root.querySelectorAll('[data-reveal]').forEach((el) => io.observe(el));

    // Per-frame loop: drive Lenis, sticky-topbar state, parallax, pinned cross-fade.
    let lastPin = -1;
    const vh = () => scrollEl.clientHeight;
    function frame(time) {
      if (!root.isConnected) return;
      if (lenis) lenis.raf(time);

      const st = scrollEl.scrollTop;
      if (topbar) topbar.classList.toggle('is-stuck', st > 12);

      if (!reduceMotion) {
        // Subtle parallax: shift each frame based on its distance from viewport center.
        const mid = st + vh() / 2;
        for (const f of frames) {
          const c = f.offsetTop + f.offsetHeight / 2 + offsetWithin(f, content);
          const d = (mid - c);
          const py = Math.max(-26, Math.min(26, -d * 0.03));
          f.style.setProperty('--py', py.toFixed(1) + 'px');
        }
      }

      // Pinned showcase progress → active screenshot.
      if (pinSection && pinShots.length) {
        const top = pinSection.offsetTop;
        const track = pinSection.offsetHeight - vh();
        let prog = track > 0 ? (st - top) / track : 0;
        prog = Math.max(0, Math.min(0.999, prog));
        const idx = Math.floor(prog * pinShots.length);
        if (idx !== lastPin) {
          lastPin = idx;
          pinShots.forEach((s, i) => s.classList.toggle('is-active', i === idx));
          pinDots.forEach((d, i) => d.classList.toggle('is-active', i === idx));
        }
      }

      rafId = requestAnimationFrame(frame);
    }
    rafId = requestAnimationFrame(frame);
  })();

  return {
    destroy() {
      if (rafId) { cancelAnimationFrame(rafId); rafId = 0; }
      if (lenis) { try { lenis.destroy(); } catch (_) {} lenis = null; }
    },
  };
}

// offsetTop is relative to offsetParent; the content wrapper may not be the
// offsetParent for nested frames, so normalise to the content's coordinate space.
function offsetWithin(el, ancestor) {
  let y = 0, node = el.offsetParent;
  while (node && node !== ancestor && ancestor.contains(node)) {
    y += node.offsetTop;
    node = node.offsetParent;
  }
  return y;
}

// Load the vendored Lenis UMD bundle (sets window.Lenis). Resolves to the
// constructor, or null if it can't load (page still works, just no smoothing).
function _loadLenis(base) {
  return new Promise((resolve) => {
    if (window.Lenis) return resolve(window.Lenis);
    const s = document.createElement('script');
    s.src = base + 'js/lenis.min.js';
    s.onload = () => resolve(window.Lenis || null);
    s.onerror = () => resolve(null);
    document.head.appendChild(s);
  });
}

function _wirePointerEffects(root, reduceMotion) {
  if (reduceMotion) return;

  // 3D tilt on screenshot frames.
  root.querySelectorAll('.sf-frame[data-tilt]').forEach((f) => {
    f.addEventListener('pointermove', (e) => {
      const r = f.getBoundingClientRect();
      const px = (e.clientX - r.left) / r.width - 0.5;
      const py = (e.clientY - r.top) / r.height - 0.5;
      f.style.setProperty('--ry', (px * 9).toFixed(2) + 'deg');
      f.style.setProperty('--rx', (-py * 7).toFixed(2) + 'deg');
    });
    f.addEventListener('pointerleave', () => {
      f.style.setProperty('--ry', '0deg');
      f.style.setProperty('--rx', '0deg');
    });
  });

  // Magnetic buttons.
  root.querySelectorAll('.sp-btn').forEach((b) => {
    b.addEventListener('pointermove', (e) => {
      const r = b.getBoundingClientRect();
      const x = (e.clientX - r.left - r.width / 2) * 0.3;
      const y = (e.clientY - r.top - r.height / 2) * 0.4;
      b.style.setProperty('--mxp', x.toFixed(1) + 'px');
      b.style.setProperty('--myp', y.toFixed(1) + 'px');
    });
    b.addEventListener('pointerleave', () => {
      b.style.setProperty('--mxp', '0px');
      b.style.setProperty('--myp', '0px');
    });
  });

  // Cursor spotlight on cards.
  root.querySelectorAll('[data-spot]').forEach((c) => {
    c.addEventListener('pointermove', (e) => {
      const r = c.getBoundingClientRect();
      c.style.setProperty('--mx', ((e.clientX - r.left) / r.width * 100).toFixed(1) + '%');
      c.style.setProperty('--my', ((e.clientY - r.top) / r.height * 100).toFixed(1) + '%');
    });
  });
}

function _wireImageFallbacks(root) {
  root.querySelectorAll('.sf-frame img').forEach((img) => {
    const mark = () => { const fr = img.closest('.sf-frame'); if (fr) fr.classList.add('is-empty'); };
    img.addEventListener('error', mark);
    if (img.complete && img.naturalWidth === 0) mark();
  });
}

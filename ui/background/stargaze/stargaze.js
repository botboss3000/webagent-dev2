/* ============================================================
   Background plugin: Stargaze  (starfield + nebula + shooting stars)
   ------------------------------------------------------------
   The app's original animated background, now a drop-in plugin. Colours
   come entirely from the --sg-* tokens in stargaze.css (this folder), so
   deleting ui/background/stargaze/ removes the look AND its tokens with
   no edit anywhere else. The engine (ui/background/_engine/manager.js)
   calls start(genui) when Stargaze is the chosen background for the
   active theme, and stop() when it is swapped out.
   ============================================================ */
(function () {
  var RID = 'stargaze';
  var reduced = window.matchMedia && window.matchMedia('(prefers-reduced-motion: reduce)').matches;

  // Ensure this plugin's stylesheet (its --sg-* tokens) is present.
  (function ensureCss() {
    var href = 'ui/background/stargaze/stargaze.css';
    if (document.querySelector('link[data-bg="' + RID + '"]')) return;
    var l = document.createElement('link');
    l.rel = 'stylesheet'; l.href = href; l.setAttribute('data-bg', RID);
    // start() reads the palette synchronously on register — BEFORE this async
    // sheet (which defines the --sg-* tokens) has applied. Re-read once it lands
    // so the loop swaps the pre-CSS JS fallback (peach in light) for the real
    // themed tokens instead of staying stuck on the fallback all session.
    l.onload = function () { if (running) readPalette(); };
    document.head.appendChild(l);
  })();

  var genui, ctx;
  var DPR = Math.min(window.devicePixelRatio || 1, 2);
  var W = 0, H = 0;
  var stars = [], shootingStars = [], dust = [];
  var nextShootAt = 0;
  var pointerX = 0.5, pointerY = 0.5, targetX = 0.5, targetY = 0.5;
  var realPointer = { x: 0.5, y: 0.5, active: false, lastX: 0, lastY: 0 };
  var bootPointer = { x: 0.5, y: 0.5, active: false, lastX: 0, lastY: 0 };
  var scrollY = 0, lastT = 0;
  var running = false, rafId = 0;
  // Decorative full-screen canvas work must never compete with chat rendering.
  // 20fps is visually smooth for slow stars while cutting gradient/raster work
  // by roughly two thirds versus an unrestricted display-rate RAF loop.
  var FRAME_DELAY_MS = 50;

  function scheduleFrame() {
    if (!running) return;
    if (document.hidden) {
      rafId = setTimeout(function () { frame(performance.now()); }, 500);
      return;
    }
    rafId = setTimeout(function () {
      if (running) rafId = requestAnimationFrame(frame);
    }, FRAME_DELAY_MS);
  }

  function isLight() { return document.body && document.body.classList.contains('light-mode'); }

  var PAL_FALLBACK = {
    dark:  { glow1:'60,70,130',    glow1b:'40,30,80',   glow2:'125,90,200', glow2b:'80,60,160', edge:'8,8,16',      starHue:220, starSat:'80%', ink:'210,225,255' },
    light: { glow1:'255,200,150', glow1b:'220,160,110', glow2:'200,120,70', glow2b:'180,100,60', edge:'245,220,195', starHue:22,  starSat:'55%', ink:'100,50,22' }
  };
  var pal = null, palLight = null;
  function readPalette() {
    var light = isLight();
    var fb = light ? PAL_FALLBACK.light : PAL_FALLBACK.dark;
    var cs = window.getComputedStyle(document.body);
    function v(name, fallback) { var x = (cs.getPropertyValue(name) || '').trim(); return x || fallback; }
    pal = {
      glow1:   v('--sg-glow-1-rgb',  fb.glow1),
      glow1b:  v('--sg-glow-1b-rgb', fb.glow1b),
      glow2:   v('--sg-glow-2-rgb',  fb.glow2),
      glow2b:  v('--sg-glow-2b-rgb', fb.glow2b),
      edge:    v('--sg-edge-rgb',    fb.edge),
      starHue: parseFloat(v('--sg-star-hue', fb.starHue)) || fb.starHue,
      starSat: v('--sg-star-sat',    fb.starSat),
      ink:     v('--sg-ink-rgb',     fb.ink)
    };
    palLight = light;
  }

  function resize() {
    W = window.innerWidth; H = window.innerHeight;
    genui.width = W * DPR; genui.height = H * DPR;
    genui.style.width = W + 'px'; genui.style.height = H + 'px';
    ctx.setTransform(DPR, 0, 0, DPR, 0, 0);
    readPalette();
    initStars();
  }

  function initStars() {
    stars = [];
    var count = Math.round((W * H) / 8000);
    for (var i = 0; i < count; i++) {
      var layer = Math.random() < 0.55 ? 0 : (Math.random() < 0.85 ? 1 : 2);
      stars.push({
        x: Math.random(), y: Math.random(),
        r: layer === 0 ? Math.random() * 0.7 + 0.3
           : layer === 1 ? Math.random() * 0.9 + 0.5
           : Math.random() * 1.4 + 0.8,
        layer: layer,
        tw: Math.random() * Math.PI * 2,
        twSpeed: 0.4 + Math.random() * 0.8,
        drift: 0.01 + Math.random() * 0.04,
        hueOffset: Math.random() < 0.85 ? 0 : (Math.random() < 0.5 ? 30 : -18)
      });
    }
  }

  function drawStars(t) {
    var light = isLight();
    var px = pointerX - 0.5, py = pointerY - 0.5;

    var g = ctx.createRadialGradient(W * 0.5, H * 0.3, 45, W * 0.5, H * 0.3, Math.max(W, H) * 0.9);
    g.addColorStop(0, 'rgba(' + pal.glow1 + ',0.18)');
    g.addColorStop(0.48, 'rgba(' + pal.glow1b + ',0.08)');
    g.addColorStop(1, 'rgba(' + pal.edge + ',0)');
    ctx.fillStyle = g;
    ctx.fillRect(0, 0, W, H);

    var g2 = ctx.createRadialGradient(
      W * (0.82 + px * 0.05), H * (0.78 + py * 0.05), 30,
      W * (0.82 + px * 0.05), H * (0.78 + py * 0.05), Math.max(W, H) * 0.6
    );
    g2.addColorStop(0, 'rgba(' + pal.glow2 + ',0.16)');
    g2.addColorStop(0.5, 'rgba(' + pal.glow2b + ',0.05)');
    g2.addColorStop(1, 'rgba(' + pal.edge + ',0)');
    ctx.fillStyle = g2;
    ctx.fillRect(0, 0, W, H);

    for (var i = 0; i < stars.length; i++) {
      var s = stars[i];
      var parallax = (s.layer + 1) * 10;
      var x = (s.x * W) - px * parallax;
      var y = (s.y * H) - py * parallax - (scrollY * 0.04 * (s.layer + 1));
      var tw = (Math.sin(t * 0.001 * s.twSpeed + s.tw) + 1) * 0.5;
      var r = s.r * (0.9 + tw * 0.25);
      var hue = pal.starHue + s.hueOffset, sat = pal.starSat;
      if (light) {
        var alpha = 0.45 + tw * 0.45, lightness = 18 + tw * 8;
        if (s.layer >= 1) {
          var halo = ctx.createRadialGradient(x, y, 0, x, y, r * 6);
          halo.addColorStop(0, 'hsla(' + hue + ',' + sat + ',' + lightness + '%,' + (alpha * 0.35) + ')');
          halo.addColorStop(1, 'hsla(' + hue + ',' + sat + ',' + lightness + '%,0)');
          ctx.fillStyle = halo;
          ctx.beginPath(); ctx.arc(x, y, r * 6, 0, Math.PI * 2); ctx.fill();
        }
        ctx.fillStyle = 'hsla(' + hue + ',' + sat + ',' + lightness + '%,' + alpha + ')';
        ctx.beginPath(); ctx.arc(x, y, r, 0, Math.PI * 2); ctx.fill();
      } else {
        var alphaD = 0.35 + tw * 0.55;
        if (s.layer >= 1) {
          var haloD = ctx.createRadialGradient(x, y, 0, x, y, r * 6);
          haloD.addColorStop(0, 'hsla(' + hue + ',' + sat + ',85%,' + (alphaD * 0.5) + ')');
          haloD.addColorStop(1, 'hsla(' + hue + ',' + sat + ',85%,0)');
          ctx.fillStyle = haloD;
          ctx.beginPath(); ctx.arc(x, y, r * 6, 0, Math.PI * 2); ctx.fill();
        }
        ctx.fillStyle = 'hsla(' + hue + ',' + sat + ',' + (85 + tw * 10) + '%,' + alphaD + ')';
        ctx.beginPath(); ctx.arc(x, y, r, 0, Math.PI * 2); ctx.fill();
      }
      s.x += s.drift * 0.0006;
      if (s.x > 1.05) s.x = -0.05;
    }
  }

  function drawConstellationAt(inputX, inputY) {
    var cx = inputX * W, cy = inputY * H;
    var candidates = [];
    for (var i = 0; i < stars.length; i++) {
      var s = stars[i];
      if (s.layer < 1) continue;
      var sx = s.x * W - (pointerX - 0.5) * (s.layer + 1) * 10;
      var sy = s.y * H - (pointerY - 0.5) * (s.layer + 1) * 10 - scrollY * 0.04 * (s.layer + 1);
      var d = Math.hypot(sx - cx, sy - cy);
      if (d < 220) candidates.push({ x: sx, y: sy, d: d });
    }
    candidates.sort(function (a, b) { return a.d - b.d; });
    var pick = candidates.slice(0, 5);
    if (pick.length < 2) return;
    ctx.lineWidth = 0.6; ctx.lineCap = 'round';
    for (var j = 0; j < pick.length - 1; j++) {
      var a = pick[j], b = pick[j + 1];
      var dist = Math.hypot(a.x - b.x, a.y - b.y);
      if (dist > 200) continue;
      var alpha = (1 - dist / 200) * 0.45 * (1 - a.d / 220);
      ctx.strokeStyle = 'rgba(' + pal.ink + ',' + alpha + ')';
      ctx.beginPath(); ctx.moveTo(a.x, a.y); ctx.lineTo(b.x, b.y); ctx.stroke();
    }
  }

  function spawnShooting(t) {
    var fromLeft = Math.random() < 0.5;
    var startX = fromLeft ? -50 + Math.random() * 100 : W + 50 - Math.random() * 100;
    var startY = Math.random() * H * 0.5;
    var speed = 0.8 + Math.random() * 0.6;
    var angle = (0.18 + Math.random() * 0.12) * Math.PI;
    shootingStars.push({
      x: startX, y: startY,
      vx: Math.cos(angle) * speed * (fromLeft ? 1 : -1),
      vy: Math.sin(angle) * speed + 0.3,
      maxLife: 900 + Math.random() * 500, born: t, trail: []
    });
  }

  function drawShootingStars(t) {
    if (t > nextShootAt) { spawnShooting(t); nextShootAt = t + 12000 + Math.random() * 20000; }
    for (var i = shootingStars.length - 1; i >= 0; i--) {
      var s = shootingStars[i];
      var age = t - s.born;
      if (age > s.maxLife) { shootingStars.splice(i, 1); continue; }
      s.x += s.vx * 16; s.y += s.vy * 16;
      s.trail.unshift({ x: s.x, y: s.y });
      if (s.trail.length > 22) s.trail.length = 22;
      var fade = age < 150 ? age / 150 : (age > s.maxLife - 250 ? (s.maxLife - age) / 250 : 1);
      for (var j = 0; j < s.trail.length - 1; j++) {
        var p1 = s.trail[j], p2 = s.trail[j + 1];
        var segAlpha = (1 - j / s.trail.length) * 0.9 * fade;
        ctx.strokeStyle = 'rgba(' + pal.ink + ',' + segAlpha + ')';
        ctx.lineWidth = (1 - j / s.trail.length) * 2 + 0.3; ctx.lineCap = 'round';
        ctx.beginPath(); ctx.moveTo(p1.x, p1.y); ctx.lineTo(p2.x, p2.y); ctx.stroke();
      }
      var head = ctx.createRadialGradient(s.x, s.y, 0, s.x, s.y, 8);
      head.addColorStop(0, 'rgba(' + pal.ink + ',' + (0.92 * fade) + ')');
      head.addColorStop(1, 'rgba(' + pal.ink + ',0)');
      ctx.fillStyle = head;
      ctx.beginPath(); ctx.arc(s.x, s.y, 8, 0, Math.PI * 2); ctx.fill();
    }
  }

  function drawDust(dtMs) {
    for (var i = dust.length - 1; i >= 0; i--) {
      var p = dust[i];
      p.life += dtMs;
      if (p.life > p.maxLife) { dust.splice(i, 1); continue; }
      p.x += p.vx * dtMs * 0.06; p.y += p.vy * dtMs * 0.06;
      var a = (1 - p.life / p.maxLife);
      ctx.fillStyle = 'rgba(' + pal.ink + ',' + (a * (p.isLight ? 0.55 : 0.75)) + ')';
      ctx.beginPath(); ctx.arc(p.x, p.y, p.r * a, 0, Math.PI * 2); ctx.fill();
    }
  }

  function frame(t) {
    if (!running) return;
    // A hidden tab needs no canvas paints. Keep only a cheap wake-up so the
    // animation resumes without a separate visibility listener.
    if (document.hidden) { scheduleFrame(); return; }
    t = t || performance.now();
    var dtMs = lastT ? Math.min(50, t - lastT) : 16;
    lastT = t;
    pointerX += (targetX - pointerX) * 0.06;
    pointerY += (targetY - pointerY) * 0.06;
    ctx.clearRect(0, 0, W, H);
    var light = isLight();
    if (pal === null || light !== palLight) readPalette();
    drawStars(t);
    // Draw a separate local constellation for each active input. Parallax uses
    // their stable midpoint, while the artifacts themselves remain visibly
    // responsive at both the real mouse and boot-orbit positions.
    if (realPointer.active) drawConstellationAt(realPointer.x, realPointer.y);
    if (bootPointer.active) drawConstellationAt(bootPointer.x, bootPointer.y);
    if (!realPointer.active && !bootPointer.active) drawConstellationAt(pointerX, pointerY);
    drawShootingStars(t);
    drawDust(dtMs);
    scheduleFrame();
  }

  // —— Listeners (tracked so stop() can remove them) ——
  function syncTarget() {
    if (realPointer.active && bootPointer.active) {
      targetX = (realPointer.x + bootPointer.x) * 0.5;
      targetY = (realPointer.y + bootPointer.y) * 0.5;
    } else if (realPointer.active) {
      targetX = realPointer.x; targetY = realPointer.y;
    } else if (bootPointer.active) {
      targetX = bootPointer.x; targetY = bootPointer.y;
    } else {
      targetX = 0.5; targetY = 0.5;
    }
  }

  function updateInput(input, clientX, clientY) {
    input.x = clientX / window.innerWidth;
    input.y = clientY / window.innerHeight;
    input.active = true;
    var dx = clientX - input.lastX, dy = clientY - input.lastY;
    var speed = Math.hypot(dx, dy);
    if (speed > 4 && Math.random() < Math.min(0.5, speed / 60)) {
      var light = isLight();
      for (var k = 0; k < 2; k++) {
        dust.push({
          x: clientX + (Math.random() - 0.5) * 6,
          y: clientY + (Math.random() - 0.5) * 6,
          vx: (Math.random() - 0.5) * 0.3,
          vy: (Math.random() - 0.5) * 0.3 - 0.15,
          r: 0.4 + Math.random() * 1.2,
          life: 0, maxLife: 600 + Math.random() * 400, isLight: light
        });
      }
    }
    input.lastX = clientX; input.lastY = clientY;
    syncTarget();
  }

  function onPointerMove(e) { updateInput(realPointer, e.clientX, e.clientY); }
  function onPointerLeave() { realPointer.active = false; syncTarget(); }
  function onBootPointerMove(e) {
    var p = e.detail || {};
    if (!Number.isFinite(p.clientX) || !Number.isFinite(p.clientY)) return;
    updateInput(bootPointer, p.clientX, p.clientY);
  }
  function onBootPointerEnd() { bootPointer.active = false; syncTarget(); }
  // Touch/pen never fire a "leave" — when the finger lifts there's no further
  // pointermove, so without this the nebula glow + parallax would stay biased
  // toward the last touch point forever. Recentre the target on lift/cancel
  // (touch & pen only — a mouse click must NOT wipe the live cursor reaction);
  // the frame loop eases pointerX/Y back to centre smoothly.
  function onPointerEnd(e) { if (!e || e.pointerType !== 'mouse') onPointerLeave(); }
  function onScroll() { scrollY = window.scrollY || 0; }
  function onResize() { DPR = Math.min(window.devicePixelRatio || 1, 2); resize(); }

  function start(cv) {
    if (running) return;
    genui = cv; ctx = genui.getContext('2d');
    if (reduced) {  // genui is display:none under reduced-motion; nothing to draw
      resize();     // size once so a one-off static frame would be correct if shown
      return;
    }
    running = true;
    window.addEventListener('pointermove', onPointerMove);
    document.addEventListener('mouseleave', onPointerLeave);
    window.addEventListener('pointerup', onPointerEnd);
    window.addEventListener('pointercancel', onPointerEnd);
    window.addEventListener('wa-boot-pointermove', onBootPointerMove);
    window.addEventListener('wa-boot-pointerend', onBootPointerEnd);
    window.addEventListener('scroll', onScroll, { passive: true });
    window.addEventListener('resize', onResize);
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
    window.removeEventListener('scroll', onScroll);
    window.removeEventListener('resize', onResize);
    stars = []; shootingStars = []; dust = [];
  }

  // Re-read the palette live when the Appearance panel edits the theme colours
  // (engine calls this on 'wa-appearance-changed'). The running frame loop then
  // repaints with the new --sg-* values — no restart, no resize needed.
  function refresh() { if (running) readPalette(); }

  window.WA_BG.register(RID, { start: start, stop: stop, refresh: refresh });
})();

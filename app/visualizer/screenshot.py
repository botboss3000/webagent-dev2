"""
screenshot_genui — give the design agent *eyes* on a genui it built.

An agent writes genui HTML, but that HTML only renders client-side (in the
user's browser, inside a shadow root). The agent never sees the visual result,
so it cannot catch a layout that came out wrong — not full-width, bad spacing,
illegible in light mode. This module renders the saved genui **headlessly, the
same way the app mounts it**, then hands back:

  • a real PNG screenshot, saved as a session attachment so the USER sees it in
    chat immediately and the AGENT can read it on demand via ``process_image``;
  • plain-text **signals** (width-fill %, console errors, blank-render hint) so
    the agent gets the most important verdicts even without a vision call.

It is a sibling of ``render_visual`` / ``get_genui`` in the **Visualizer**
ability and is auto-discovered — no core file is touched. Because first-class
genui run agent-authored page code with the user's own app trust, this tool
follows the same gate as the live genui render (app/auth `genui_first_class` →
``user_may_access_page``): the Gen UI page's VISIBILITY setting, enforced
server-side — registration-required by default ("auth"), "all" includes
anonymous, "off" is admins-only; admins and 'open' single-user mode always pass.
A caller the setting excludes gets a disabled error and never launches a browser.

The heavy Playwright import is lazy (inside the handler) so merely scanning the
ability stays cheap.
"""
from __future__ import annotations

import json
import logging
import re
from pathlib import Path
from typing import Dict, List, Optional

logger = logging.getLogger(__name__)

# Desktop viewport for the render — a realistic wide screen, the size at which a
# "not full-width" bug is most visible. Mobile is a noted follow-up.
_VIEWPORT_W = 1440
_VIEWPORT_H = 900

# Below this fraction of the viewport, content reads as unintentionally narrow.
_NARROW_THRESHOLD = 0.75

# Map Playwright console message types onto the genui log's levels (matches the
# live-browser capture in genui.js: log/info/warn/error/debug).
_PW_LEVEL = {"log": "log", "info": "info", "debug": "debug", "error": "error",
             "warning": "warn", "warn": "warn"}

# The exact base style the live app injects into every genui shadow root BEFORE
# the agent's own CSS (genui.js `_GENUI_BASE_STYLE`). Kept in sync so the
# headless render matches what the user sees. (grep: SISTER-SYNC: GENUI-BASE-STYLE)
_GENUI_BASE_STYLE = (
    ":host{display:block;width:100%;min-height:100%;background:transparent;"
    "font-family:'Inter',-apple-system,BlinkMacSystemFont,'Segoe UI',Roboto,sans-serif;"
    "color:#c0caf5;-webkit-font-smoothing:antialiased;}"
    "*,*::before,*::after{box-sizing:border-box;}"
    ".wa-genui-body{min-height:100%;}"  # private wrapper class — matches genui.js (WA-GENUI-BODY)
)


def _repo_root() -> Path:
    # app/visualizer/screenshot.py -> parents[2] is the repo root.
    return Path(__file__).resolve().parents[2]


def _design_tokens_css() -> str:
    """The app's global design-system stylesheet, inlined so every ``var(--token)``
    a genui consumes (colours, borders, shadows, fonts) resolves exactly as it
    does in the live app — and so the dark/light flip works. Without it the tokens
    are empty and the shot is unstyled garbage."""
    css_path = _repo_root() / "ui" / "shared" / "css" / "design-system.css"
    try:
        return css_path.read_text(encoding="utf-8")
    except Exception as e:  # pragma: no cover - defensive
        logger.warning("screenshot_genui: could not read design-system.css: %s", e)
        return ""


def _shell_html(theme: str, design_css: str) -> str:
    """The blank page: app design tokens inlined + the theme class on <body>. The
    genui itself is grafted in afterwards via page.evaluate(_BOOTSTRAP_JS, ...) so
    the genui markup never passes through the HTML parser of this document — a
    genui containing its own `</script>` would otherwise break an inline script."""
    body_class = "light-mode" if theme == "light" else ""
    return (
        "<!doctype html><html><head><meta charset=\"utf-8\">"
        "<style>" + design_css + "</style>"
        "<style>html,body{margin:0;padding:0;min-height:100vh;background:var(--bg-0,#16161e);}"
        "#genui-shot-host{width:100%;}</style>"
        "</head><body class=\"" + body_class + "\"></body></html>"
    )


# Reconstructs the real genui runtime, mirroring genui.js `mountGenui`: a
# host div with an OPEN shadow root, the base style first, the genui's own
# <style>/<link> lifted in, the body markup wrapped in `<div class="wa-genui-body">`,
# then the genui's <script>s re-created so they execute and mount via any of the
# three live forms: `WebagentGenui.register(fn)`, a top-level `mount(root, api)`,
# or inline use of `WebagentGenui.root`/`.api`. A stub `api` (the real verbs as
# no-ops) lets a contract-compliant genui wire up. Receives {raw, base, theme} as ONE argument
# so the genui HTML arrives as data, never as parsed markup.
_BOOTSTRAP_JS = """
(arg) => {
  const { raw, base, theme, data } = arg;
  window.__genuiErrors = window.__genuiErrors || [];
  // Bake the genui's data bag in just like the live /html endpoint does, so a
  // page that reads api.getData() renders its real content in the screenshot.
  window.__GENUI_DATA = (data && typeof data === 'object') ? data : {};
  const host = document.createElement('div');
  host.id = 'genui-shot-host';
  host.className = theme;
  host.style.cssText = 'display:block;width:100%;min-height:100%;';
  document.body.appendChild(host);
  const shadow = host.attachShadow({ mode: 'open' });
  window.__genuiShadow = shadow;
  const baseEl = document.createElement('style'); baseEl.textContent = base; shadow.appendChild(baseEl);
  let doc = null;
  try { doc = new DOMParser().parseFromString(raw, 'text/html'); } catch (e) { doc = null; }
  const STUB = {
    theme: theme, getTheme: () => theme, onTheme: () => {}, onStatus: () => {},
    chat: () => {}, send: () => {}, action: () => {}, refresh: () => {},
    getData: () => window.__GENUI_DATA || {},
    storeCredential: () => Promise.resolve(false),
  };
  // Mirror the live engine's three mount forms (genui.js): register(), a
  // top-level drop-in mount(root, api), or inline use of WebagentGenui.root/.api.
  let __mounted = false;
  const __runMount = (fn) => {
    if (typeof fn !== 'function') return;
    __mounted = true;
    try { fn(shadow, STUB); }
    catch (e) { window.__genuiErrors.push('mount: ' + (e && e.message || e)); }
  };
  window.WebagentGenui = {
    root: shadow, api: STUB,
    getData: () => window.__GENUI_DATA || {},
    register(fn) { __runMount(fn); },
    onCleanup() {},
  };
  window.mount = undefined;
  if (!doc) return;
  doc.querySelectorAll('style').forEach((st) => { const s = document.createElement('style'); s.textContent = st.textContent; shadow.appendChild(s); });
  doc.querySelectorAll('link[rel="stylesheet"]').forEach((ln) => { const l = document.createElement('link'); l.rel = 'stylesheet'; if (ln.getAttribute('href')) l.href = ln.getAttribute('href'); shadow.appendChild(l); });
  const root = document.createElement('div'); root.className = 'wa-genui-body';
  root.innerHTML = doc.body ? doc.body.innerHTML : raw;
  root.querySelectorAll('script').forEach((s) => s.remove());
  shadow.appendChild(root);
  doc.querySelectorAll('script').forEach((os) => {
    const s = document.createElement('script');
    for (const a of os.attributes) s.setAttribute(a.name, a.value);
    s.textContent = os.textContent;
    shadow.appendChild(s);
  });
  // Drop-in fallback: a page with a top-level mount(root, api) that never registered.
  if (!__mounted && typeof window.mount === 'function') __runMount(window.mount);
};
"""


# Runs in the page after settle: measures how much of the viewport width the
# rendered content actually occupies (the precise "won't fill the width" signal),
# plus content height and child count for a blank-render hint.
_MEASURE_JS = """
() => {
  const out = { hostWidth: 0, contentSpan: 0, fillPct: 0, contentHeight: 0, blockCount: 0 };
  const shadow = window.__genuiShadow;
  if (!shadow) return out;
  const host = document.getElementById('genui-shot-host');
  const hb = host ? host.getBoundingClientRect() : { left: 0, right: 0, width: 0 };
  out.hostWidth = Math.round(hb.width);
  const rootEl = shadow.querySelector('.wa-genui-body');
  if (!rootEl) return out;
  let minL = Infinity, maxR = -Infinity, maxB = -Infinity, n = 0;
  const walk = (el, depth) => {
    for (const c of el.children) {
      const r = c.getBoundingClientRect();
      if (r.width > 40 && r.height > 8) {
        minL = Math.min(minL, r.left); maxR = Math.max(maxR, r.right); maxB = Math.max(maxB, r.bottom); n++;
      }
      if (depth < 2) walk(c, depth + 1);
    }
  };
  walk(rootEl, 0);
  if (n > 0) {
    out.contentSpan = Math.round(maxR - minL);
    out.contentHeight = Math.round(maxB - hb.top);
    out.fillPct = out.hostWidth ? Math.round((out.contentSpan / out.hostWidth) * 100) : 0;
    out.blockCount = n;
  }
  return out;
};
"""


# Clicks an element INSIDE the genui shadow root (where the agent's markup lives) so
# the agent can verify an interaction — a name opening a profile panel, a calendar day
# expanding, a tab switching. Returns true if the selector matched something. Runs in
# the shadow, never `document`, mirroring the real mount scope.
_CLICK_JS = """
(sel) => {
  const sh = window.__genuiShadow;
  if (!sh) return false;
  let el = null;
  try { el = sh.querySelector(sel); } catch (e) { return false; }
  if (!el) return false;
  el.scrollIntoView && el.scrollIntoView({ block: 'center' });
  el.click();
  return true;
};
"""


# A coarse "what is visible right now" signature of the genui, used to tell whether a
# click actually CHANGED anything (opened a panel, expanded a row, swapped a tab). A click
# can match an element and fire with no effect — a mis-wired handler, the wrong element
# toggled, an `.open` class that never reveals — and the geometric signals + a flaky vision
# model won't catch that. Comparing this signature before vs after the clicks does: count of
# on-screen elements (a reveal makes clipped children measurable) + visible text length (a
# tab/content swap changes it). Equal before and after a click that LANDED ⇒ likely broken.
_CHANGE_SIG_JS = """
() => {
  const sh = window.__genuiShadow;
  if (!sh) return null;
  const root = sh.querySelector('.wa-genui-body') || sh;
  let vis = 0;
  const els = root.querySelectorAll('*');
  for (const el of els) {
    const r = el.getBoundingClientRect();
    if (r.width > 0 && r.height > 0) vis++;
  }
  const text = (root.textContent || '').replace(/\\s+/g, '').length;
  return [vis, text];
};
"""


def _normalize_clicks(click) -> List[str]:
    """Accept a single selector string OR a list of selectors (a click sequence).
    A string is treated as ONE selector (not comma-split) so CSS group selectors
    like ``.a, .b`` survive intact."""
    if not click:
        return []
    if isinstance(click, str):
        s = click.strip()
        return [s] if s else []
    if isinstance(click, (list, tuple)):
        return [str(c).strip() for c in click if str(c).strip()]
    return []


async def _render_one(playwright, genui_html: str, theme: str, design_css: str,
                      clicks: Optional[List[str]] = None,
                      genui_data: Optional[Dict] = None) -> Dict:
    """Render one theme, returning {png, signals, errors, clicked, missed}.

    If ``clicks`` is given, each selector is clicked inside the genui (in order, with
    a short settle between) AFTER the initial mount but BEFORE the measure/shot — so the
    screenshot, signals and vision review all reflect the POST-click state (the opened
    panel / popover / swapped content), which is how the agent verifies an interaction."""
    browser = await playwright.chromium.launch(
        headless=True,
        args=[
            "--no-sandbox",
            "--disable-setuid-sandbox",
            "--disable-dev-shm-usage",
            "--disable-gpu",
            # Synthetic webcam/mic so getUserMedia-using genui mount instead of
            # prompting or throwing.
            "--use-fake-ui-for-media-stream",
            "--use-fake-device-for-media-stream",
        ],
    )
    errors: List[str] = []
    # Every console message at every level (not just errors), so this headless render
    # can feed the same per-genui console.log the live browser writes — letting the
    # agent read a build's output via get_genui_logs right after rendering, with no
    # live user session. `errors` stays the errors-only list the width/blank notes use.
    console_entries: List[Dict] = []
    try:
        context = await browser.new_context(
            viewport={"width": _VIEWPORT_W, "height": _VIEWPORT_H},
            device_scale_factor=2,  # crisp, retina-ish capture
        )
        page = await context.new_page()

        def _on_console(m):
            lvl = _PW_LEVEL.get(m.type, "log")
            console_entries.append({"level": lvl, "text": m.text})
            if lvl == "error":
                errors.append(m.text)

        def _on_pageerror(e):
            console_entries.append({"level": "error", "text": str(e)})
            errors.append(str(e))

        page.on("console", _on_console)
        page.on("pageerror", _on_pageerror)

        await page.set_content(_shell_html(theme, design_css), wait_until="load")
        host_theme = "light" if theme == "light" else "dark"
        # Graft the genui in as DATA (one argument), never as parsed markup, so a
        # genui carrying its own </script> can't break the injection.
        await page.evaluate(_BOOTSTRAP_JS, {
            "raw": genui_html,
            "base": _GENUI_BASE_STYLE,
            "theme": host_theme,
            "data": genui_data if isinstance(genui_data, dict) else {},
        })
        # Let CDN assets, fonts and any rAF-driven first paint settle.
        try:
            await page.wait_for_load_state("networkidle", timeout=4000)
        except Exception:
            pass
        await page.wait_for_timeout(700)

        # Optional: drive interactions before measuring, so the shot captures the
        # opened/changed state (click a name → profile panel, a day → detail, a tab).
        clicked: List[str] = []
        missed: List[str] = []
        # Snapshot what's visible BEFORE any click, so we can tell whether a click that
        # landed actually changed anything (opened a panel) or fired with no effect.
        sig_before = None
        if clicks:
            try:
                sig_before = await page.evaluate(_CHANGE_SIG_JS)
            except Exception:
                sig_before = None
        for sel in (clicks or []):
            try:
                ok = await page.evaluate(_CLICK_JS, sel)
            except Exception:
                ok = False
            if ok:
                clicked.append(sel)
                await page.wait_for_timeout(450)  # let the open/transition settle
            else:
                missed.append(sel)
            # A click may have triggered a new script error — keep capturing.
            try:
                more = await page.evaluate("() => (window.__genuiErrors || [])")
                for e in (more or []):
                    if str(e) not in errors:
                        errors.append(str(e))
            except Exception:
                pass

        # Did the click(s) actually change the visible genui? Compare the signature.
        # None => couldn't measure (don't claim either way). True/False only when we have
        # both a landed click and a usable before/after reading.
        click_changed: Optional[bool] = None
        if clicked:
            try:
                sig_after = await page.evaluate(_CHANGE_SIG_JS)
            except Exception:
                sig_after = None
            if sig_before is not None and sig_after is not None:
                click_changed = (sig_after != sig_before)

        signals = await page.evaluate(_MEASURE_JS)
        # Any errors the genui script pushed onto window.__genuiErrors.
        _seen_console = {ce.get("text") for ce in console_entries}
        try:
            mount_errors = await page.evaluate("() => (window.__genuiErrors || [])")
            for e in (mount_errors or []):
                errors.append(str(e))
                if str(e) not in _seen_console:  # also record on the per-genui log
                    console_entries.append({"level": "error", "text": str(e)})
                    _seen_console.add(str(e))
        except Exception:
            pass

        host = page.locator("#genui-shot-host")
        try:
            png = await host.screenshot(type="png")
        except Exception:
            png = await page.screenshot(type="png", full_page=True)
        return {"png": png, "signals": signals or {}, "errors": errors,
                "console_entries": console_entries,
                "click_changed": click_changed,
                "clicked": clicked, "missed": missed}
    finally:
        try:
            await browser.close()
        except Exception:
            pass


def _interpret(signals: Dict, errors: List[str], png_len: int) -> List[str]:
    """Turn raw measurements into the plain-text warnings the agent reads."""
    notes: List[str] = []
    fill = signals.get("fillPct") or 0
    host_w = signals.get("hostWidth") or _VIEWPORT_W
    if fill and fill < int(_NARROW_THRESHOLD * 100):
        notes.append(
            "WIDTH: content fills only {}% of the {}px-wide genui — that is a DEFECT, not a "
            "style choice. Do NOT rationalize it as a 'balanced' or 'intentional 2-column' "
            "layout: a dashboard with a large empty side gutter is wrong and must be fixed before "
            "you call this done. Usual causes: fixed-width columns or a max-width cap instead of "
            "fr/flex that stretch; a wrapper sized to its content (inline-block / fit-content); or "
            "your top grid/flex not set to width:100%. Make the top layout fill the width (e.g. "
            "grid-template-columns using fr, width:100%) with ~20px page padding, then "
            "re-screenshot — this note must be gone.".format(fill, host_w)
        )
    elif fill:
        notes.append("WIDTH: content fills {}% of the genui width — good.".format(fill))
    if (signals.get("blockCount") or 0) == 0 or png_len < 2000:
        notes.append(
            "BLANK: almost nothing rendered (the shot is near-empty). The genui "
            "may have a script error or its markup never mounted — check the errors."
        )
    if errors:
        uniq = []
        for e in errors:
            if e not in uniq:
                uniq.append(e)
        joined = " | ".join(uniq[:6])
        note = ("CONSOLE ERRORS ({}): {} — this is a DEFECT that blocks 'done', not a "
                "warning to ignore. A script that throws aborts the rest of itself, so the "
                "page can LOOK fine while its clock, buttons, webcam and other interactions "
                "are dead.".format(len(uniq), joined))
        # The signature shadow-scope mistake — name it so the fix is obvious.
        low = joined.lower()
        if "null" in low or "undefined" in low:
            note += (" A 'Cannot ... properties of null/undefined' here is almost always the "
                     "shadow-scope bug: you used document.getElementById / document.querySelector "
                     "(which find NOTHING inside the genui's shadow root) or ran setup at script "
                     "top-level. Query through the `root` handed to WebagentGenui.register(root, api) "
                     "and do ALL setup inside that callback, then re-screenshot until this is gone.")
        notes.append(note)
    return notes


def _palette_lint(genui_html: str) -> Optional[str]:
    """Flag hardcoded hex colours that AREN'T var() fallbacks — they freeze the genui
    against a theme re-skin. This is the #1 follow-up palette mistake after a self-defined
    :host block: hardcoding status / level / avatar colours (green/amber/red/purple) with
    literal hex instead of the app's status + accent tokens. Source-level, theme-independent
    — computed once and attached to every shot's notes so it rides the same verify loop as
    WIDTH / CONSOLE ERRORS that the agent already fixes against."""
    if not genui_html:
        return None
    # Remove var(--x, #hex) fallbacks first — a literal hex IS allowed there.
    stripped = re.sub(r"var\([^()]*\)", "", genui_html)
    distinct: List[str] = []
    for h in re.findall(r"#[0-9a-fA-F]{6}\b", stripped):
        hl = h.lower()
        if hl not in distinct:
            distinct.append(hl)
    if len(distinct) < 3:
        # One or two stray literals (a single brand gradient, a scrim) aren't worth a flag.
        return None
    sample = ", ".join(distinct[:5])
    return (
        "PALETTE: {} distinct hardcoded hex colour(s) in your CSS that are NOT var() "
        "fallbacks (e.g. {}) — these are frozen and will NOT follow a theme re-skin or the "
        "App-Settings palette override, so the genui drifts from the app on any recolour. "
        "This is a DEFECT to fix, not a style choice: drive status/level colours from the app "
        "tokens var(--success) / var(--warning) / var(--danger), brand & categorical accents "
        "from var(--accent) (and --accent-soft/-mid), surfaces from var(--bg-elev), text from "
        "var(--fg-1/2/3). A literal hex may appear ONLY as a var() fallback "
        "(color:var(--fg-1,#c0caf5)). Re-render with these tokenised — this note must be "
        "gone.".format(len(distinct), sample)
    )


# What we ask the vision model to judge — the things the geometric signals CAN'T
# see: legibility/contrast in this theme, overlap/clipping/overflow, empty or
# broken-looking panels, uneven spacing, anything that just looks wrong.
_VISION_SYS = (
    "You are a meticulous UI reviewer. Look at the screenshot of an app dashboard/page "
    "and report only concrete, visible problems — be specific about WHERE. Judge: is text "
    "legible against its background in this theme (contrast)? Any overlap, clipping, cut-off "
    "text, or horizontal overflow? Any panel that looks empty, broken, or unstyled? Is spacing "
    "even and is the layout balanced (no large dead zones)? If it looks clean and production-ready, "
    "say so in one line. Do not invent problems that aren't visible."
)


async def _resolve_vision_model(user_id: str):
    """The configured Image-in model (same one process_image uses), or None."""
    try:
        from app import model_catalog
        await model_catalog.ensure_fresh()
    except Exception:
        pass
    try:
        from app.admin.settings import load_llm_capabilities_for_user, pick_vision_model
        caps = await load_llm_capabilities_for_user(user_id)
        return pick_vision_model(caps)
    except Exception as e:  # pragma: no cover - defensive
        logger.warning("screenshot_genui: could not resolve vision model: %s", e)
        return None


# Prefix that marks a `review` value as "the review did NOT happen" (vs. an actual
# critique). The agent is told (skill) to treat this as a non-pass, and it makes the
# failure visible in the tool result instead of a silent `null`.
_REVIEW_UNAVAILABLE = "unavailable"


def _unavailable(reason: str) -> str:
    return "{}: {}".format(_REVIEW_UNAVAILABLE, reason)


async def _vision_review(db, vision, att_id: str, theme: str,
                         user_id: str = "", session_id: str = "") -> str:
    """Delegate the just-rendered screenshot to the vision model and return its
    written critique — the SAME mechanism the image_vision ability uses, folded in
    so the agent gets the verdict in this one tool call (no separate process_image).
    (grep: SISTER-SYNC: MODEL-WORKER-DELEGATE)

    NEVER returns None: on any failure it returns an ``unavailable: <reason>`` string
    AND logs a warning, so the miss is visible in the result and recorded in
    diagnostics. ``ask_model`` itself swallows call failures and returns None, so
    without this the review would silently come back empty — the real error (e.g.
    the provider being out of credits) is captured via ``error_sink`` and folded
    into the reason so the AGENT knows the actual cause, and the user-facing
    402/credit alert is persisted so the human sees it too."""
    try:
        from app.agent.provider_completion import ask_model
        att = await db.get_attachment(att_id)
        if not att:
            logger.warning("screenshot_genui: vision review skipped — attachment %s not found", att_id)
            return _unavailable("screenshot attachment could not be re-read")
        question = (
            "This dashboard/page is rendered in {} mode. An overlay panel, popover, or "
            "expanded/swapped section MAY be open (it was just clicked) — if so, judge THAT "
            "too: is it readable, well-positioned, not clipped or overflowing, does it sit "
            "cleanly over the page? List concrete visual problems (layout, contrast/legibility, "
            "overlap, clipping, empty or broken panels, uneven spacing). If it's clean, say so "
            "briefly.".format(theme)
        )
        err_sink: List[str] = []
        out = await ask_model(vision, _VISION_SYS, question, attachments=[att],
                              max_tokens=600, error_sink=err_sink)
        if not out or not out.strip():
            # ask_model returns None on incomplete config OR a failed/empty call —
            # it does not raise, so log here or the failure is invisible. error_sink
            # carries the real provider error (e.g. "no remaining credits").
            model_name = (vision or {}).get("model", "?")
            err_text = (err_sink[-1] if err_sink else "").strip()
            logger.warning(
                "screenshot_genui: vision review empty (model=%s, theme=%s) — %s",
                model_name, theme,
                err_text or "Image-in model returned no text (check App Config -> Models)")
            reason = err_text or ("the Image-in model returned no text — check it is configured "
                                  "and reachable in App Config -> Models")
            # Provider account out of credits? Persist the user-facing alert in this
            # session so the human sees WHY vision is down (not just the agent).
            if err_text:
                try:
                    from app.util.alerts import is_provider_credit_error, persist_402_alert
                    if is_provider_credit_error(err_text):
                        await persist_402_alert(err_text, user_id, session_id,
                                                model_name, vision.get("provider", ""))
                except Exception:
                    pass
            return _unavailable(reason)
        return out.strip()
    except Exception as e:  # pragma: no cover - defensive
        logger.warning("screenshot_genui: vision review failed: %s", e)
        return _unavailable("vision review errored: {}".format(e))


async def screenshot_genui(
    slug: str,
    theme: str = "dark",
    click=None,
    user_id: str = "default",
    session_id: str = "",
) -> str:
    """Render a saved genui headlessly and return a screenshot + signals.

    Args:
        slug:    Which saved genui to render (same store get_genui reads).
        theme:   "dark" | "light" | "both".
        click:   Optional CSS selector (or list of selectors, clicked in order) to
                 trigger INSIDE the genui before the shot — so the screenshot,
                 signals and vision review capture the OPENED state (a profile panel,
                 popover, expanded day, switched tab). Use it to verify an interaction
                 actually works, not just that the page looks right at rest.
        user_id / session_id: injected by the ability; the shot is saved as an
                 attachment on this session so process_image can read it.
    """
    from app.auth.identity import user_may_access_page

    # Same gate as the live genui render (app/auth/__init__.py `genui_first_class`
    # → caller_may_access_page): a genui runs agent-authored code with the user's
    # own app trust, so access follows the Gen UI page's VISIBILITY setting,
    # enforced server-side — registration-required by default, "all" includes
    # anonymous, "off" is admins-only. Admins and 'open' single-user/local mode
    # always pass. The session's user_id is injected by the ability.
    if not await user_may_access_page(user_id, "main", "genui"):
        return json.dumps({
            "status": "error",
            "code": "disabled_on_hosted",
            "message": ("Gen UI isn't enabled for this account — first-class genui "
                        "run agent-authored code, so they follow the Gen UI page's "
                        "visibility (registration required by default)."),
        })

    theme = (theme or "dark").strip().lower()
    if theme not in ("dark", "light", "both"):
        theme = "dark"
    themes = ["dark", "light"] if theme == "both" else [theme]

    clicks = _normalize_clicks(click)

    from app.visualizer.genui import get_genui_html, get_genui_data
    genui_html = await get_genui_html(user_id=user_id, slug=slug)
    if not genui_html:
        return json.dumps({"status": "error", "message": "Gen UI '{}' not found or empty.".format(slug)})
    # The page may keep its source split (styles.css + app.js / app.*.js parts
    # inlined into the served document by the API route). Mirror that inlining
    # here so the headless render + palette lint see the same document a
    # browser gets — otherwise a split page would render unstyled/script-less.
    from app.api.genui import _inline_genui_assets
    genui_html = _inline_genui_assets(user_id=user_id, slug=slug, html=genui_html)
    # The page's content lives in its data bag; bake it into the headless render
    # so the screenshot shows real data, not empty slots.
    genui_data = await get_genui_data(user_id=user_id, slug=slug)

    try:
        from playwright.async_api import async_playwright  # lazy: heavy import
    except ImportError:
        return json.dumps({
            "status": "error",
            "message": ("Playwright is not installed on this host, so the genui "
                        "cannot be rendered. Install it (the browser_control ability uses it too)."),
        })

    design_css = _design_tokens_css()

    # Source-level palette check (same for every theme) — computed once, attached below.
    palette_note = _palette_lint(genui_html)

    from app.db.attachments.file_store import store_file
    from app.db import get_db
    db = get_db()

    # Resolve the vision model once (reused for every theme). None => no Image-in
    # model configured; we still return the geometric signals + screenshot.
    vision = await _resolve_vision_model(user_id)

    shots: List[Dict] = []
    # Console output captured across every theme this call renders — written to the
    # genui's own console.log at the end so get_genui_logs reflects this headless
    # render (not only a live user session).
    headless_logs: List[Dict] = []
    try:
        async with async_playwright() as pw:
            for th in themes:
                try:
                    result = await _render_one(pw, genui_html, th, design_css, clicks=clicks,
                                               genui_data=genui_data)
                except Exception as e:
                    shots.append({"theme": th, "error": "render failed: {}".format(e)})
                    continue

                png = result["png"]
                signals = result["signals"]
                errors = result["errors"]
                # Collect this theme's console output for the per-genui log. Tag the
                # theme when both are rendered so the agent can tell them apart.
                for ce in (result.get("console_entries") or []):
                    txt = ce.get("text", "")
                    if len(themes) > 1:
                        txt = "[{}] {}".format(th, txt)
                    headless_logs.append({"level": ce.get("level", "log"),
                                          "text": txt, "source": "headless"})
                notes = _interpret(signals, errors, len(png or b""))
                if palette_note:
                    notes.append(palette_note)
                # Report how the click-verify went so a wrong selector is obvious.
                did = result.get("clicked") or []
                missed = result.get("missed") or []
                if clicks:
                    if did:
                        notes.append("CLICKED before shot: {} — this shot shows the resulting "
                                     "state. Confirm the panel/popover/content actually opened.".format(", ".join(did)))
                    # Click landed but the genui looks identical before/after → the
                    # interaction probably isn't wired right (the geometric/vision checks
                    # can't catch a click that fires with no effect; this can).
                    if result.get("click_changed") is False:
                        notes.append(
                            "CLICK NO EFFECT: {} matched and was clicked, but NOTHING on the genui "
                            "changed (same visible elements + text before and after) — so the shot is "
                            "effectively the resting state. This is a DEFECT for a click meant to OPEN "
                            "a panel / expand a row / switch a tab: the handler isn't firing or toggles "
                            "the wrong element. Common causes: the click listener is on the wrong node "
                            "(delegate from the row container and read e.target.closest('[data-id]')), the "
                            "`.open`/`max-height` class is added to an element that has no collapsed→open "
                            "CSS, or you rebuild the list on click without carrying the open state. Fix the "
                            "wiring and re-screenshot until the click visibly opens something. (If this "
                            "click was a toggle/filter that swaps equal-sized content, you can ignore "
                            "it.)".format(", ".join(did) or "the selector"))
                    if missed:
                        notes.append("CLICK MISS: no element matched {} (nothing to click). Check the "
                                     "selector/id matches your markup; the shot is the UNchanged "
                                     "state.".format(", ".join(missed)))

                name = "genui-{}-{}.png".format(slug, th)
                stored = await store_file(
                    user_id=user_id, session_id=session_id,
                    file_bytes=png, filename=name, mime_type="image/png",
                )
                att_id = None
                try:
                    att_id = await db.insert_attachment(
                        user_id=user_id,
                        session_id=session_id,
                        original_name=name,
                        mime_type="image/png",
                        size_bytes=len(png or b""),
                        storage_path=stored.get("storage_path"),
                        metadata={"kind": "genui_screenshot", "slug": slug, "theme": th, "signals": signals},
                        storage_provider=stored.get("storage_provider", "local"),
                    )
                except Exception as e:
                    logger.warning("screenshot_genui: insert_attachment failed: %s", e)

                # Fold in the vision verdict (same path as image_vision) so the agent
                # gets an actual "looked at it" critique in this single call. When a
                # vision model is configured the review is NEVER a silent null — on any
                # miss it comes back as an "unavailable: <reason>" string (and is logged).
                review = None
                if vision:
                    if att_id:
                        review = await _vision_review(db, vision, att_id, th,
                                                      user_id=user_id, session_id=session_id)
                    else:
                        review = _unavailable("screenshot was not saved as an attachment")

                shots.append({
                    "theme": th,
                    "image_url": stored.get("public_url"),
                    "attachment_id": att_id,
                    "size_bytes": len(png or b""),
                    "signals": signals,
                    "notes": notes,
                    "review": review,
                    "clicked": result.get("clicked") or [],
                    "missed": result.get("missed") or [],
                })
    except Exception as e:
        logger.exception("screenshot_genui failed")
        return json.dumps({"status": "error", "message": "Rendering failed: {}".format(e)})

    # Feed this headless render's console output into the genui's own log so the agent
    # can read it via get_genui_logs right after a build (no live browser needed).
    # replace_source='headless' refreshes only the headless entries each call — live
    # user-session entries are kept, and a clean render clears stale headless errors.
    try:
        from app.visualizer.genui import append_genui_logs
        append_genui_logs(user_id, slug, headless_logs, replace_source="headless")
    except Exception as e:
        logger.warning("screenshot_genui: could not write per-genui logs: %s", e)

    # The screenshot is already the latest image in this chat (the user sees it) AND
    # each shot carries a `review` — the vision model's written critique of the actual
    # pixels, produced the same way the image_vision ability works. So the agent gets
    # the verdict in THIS call; no separate process_image is needed.
    if vision:
        guidance = (
            "Each shot's `review` is a vision model's critique of the real rendered pixels "
            "(plus the geometric `notes`/`signals`). Read both, fix anything flagged, and "
            "re-screenshot. The picture is already shown to the user as your proof. For a "
            "tighter follow-up question you can still call process_image on the same image."
        )
    else:
        guidance = (
            "No Image-in (vision) model is configured, so there is no automatic `review` — "
            "rely on the geometric `notes`/`signals` (width-fill, console errors, blank hint), "
            "or ask an admin to enable a vision model in App Config → Models. The screenshot is "
            "already shown to the user as your proof."
        )
    return json.dumps({
        "status": "ok",
        "slug": slug,
        "shots": shots,
        "guidance": guidance,
    })

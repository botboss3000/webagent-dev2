---
id: ui_theming
title: UI theming and surfaces
when_to_use: Use when building or changing WebAgent UI appearance — the design-system palette, borders, the Appearance panel, per-user themes, edge fades, rounded scroll corners, and mobile keyboard behaviour.
default_mode: visible
minimum_access: registered
---

# UI theming and surfaces

## Theming — one palette drives the whole app (dark AND light)

Every new UI feature in `ui/` (and any markup in `index.html`) **must be tested and visually correct in both dark mode and light mode** before it is considered done. The app toggles between themes via `body.light-mode`; all theme colours live as CSS variables in **`ui/shared/css/design-system.css`**.

### The palette is the single source of truth — re-theming is a one-file edit

`design-system.css` is structured in **layers**, and the whole app (every panel, button, bubble, scrollbar, **and** the animated starfield background) is wired to it:

1. **PALETTE block (the edit-here zone)** — at the top of **both** the `:root` (dark) and `body.light-mode` (light) blocks. Holds the raw colours: brand hue, purple, status hues, neutral surfaces, and the ambient page-glow colour. To switch the whole scheme (e.g. peach → green), you change **only** the raw values in these two blocks — nowhere else. (Animated-background tokens live with each background plugin under `ui/background/<id>/` but reference this palette — see below.)
2. **Each hue has an `-rgb` triple** (e.g. `--brand-rgb: 125, 207, 255`). Build any opacity from it with `rgba(var(--brand-rgb), 0.2)` — that way a hue is authored **once** per theme and every tint derives from it.
3. **Derived/semantic tokens (wiring — don't edit to re-theme)** — `--accent`, `--accent-soft`, `--user-bubble-bg`, the `--shadow-*` set, etc. are all defined as `var(--brand)` / `rgba(var(--brand-rgb), a)` formulas. They are declared in **both** theme blocks (CSS resolves `var()` per-element, so a token referencing the palette must be re-declared under `body.light-mode` for it to pick up the light palette — this is intentional duplication of the *formula*, not the colour). Alphas may differ between the two blocks on purpose (a tint needs more opacity over a bright surface).

So: dark and light are **independent palettes** (the factory default is **Ocean** in light mode and **Amber** in dark mode; the **Mono** preset is the neutral grey/black-and-white scheme; other selectable presets re-theme to Forest, Grape, Rose, etc.) — change either block's raw values to re-skin that theme.

### The animated background reads the palette too (a plugin system)

The full-screen animated background is **pluggable** — see `ui/background/README.md`. Each background is a self-contained drop-in folder `ui/background/<id>/` (`<id>.json` descriptor + `<id>.js` renderer + `<id>.css` tokens); the engine in `ui/background/_engine/` owns the single `<genui>` and loads whichever background the admin chose for the active theme. The admin picks one **per theme** (App Settings → **Appearance**), saved to app settings and delivered to every visitor via the public `GET /api/v1/auth/ui-config`. The built-in **None** = plain themed background.

**Tokens live with the plugin, derived from the palette.** A background is **not** painted with hard-coded colours — its `<id>.js` reads its own `--*` tokens from the computed style of `body` (with JS fallbacks only for the pre-stylesheet frame) and re-reads on theme flip. Those tokens are defined in the plugin's own `<id>.css` (e.g. Stargaze's `--sg-*` are in `ui/background/stargaze/stargaze.css`, **not** `design-system.css`), but they **reference the central palette** (`var(--brand-rgb)`, etc.) where possible so a scheme swap still flows. This keeps each background self-contained: delete its folder and the renderer, its CSS and its tokens all go together, and it disappears from the Appearance selector (a removed background falls back to Stargaze).

The cursor glow and card spotlights are **not** part of a background — they live in `ui/shared/js/cursor-effects.js` and run for every background, including None.

**Rules:**

1. **Never hard-code a brand/scheme colour** (hex or rgb) in CSS, inline styles, HTML attributes, or JS string templates. Reference the token. CSS variables resolve inside inline styles too, so JS can use `el.style.background = 'rgba(var(--brand-rgb),0.12)'` or `style="color:var(--brand)"`.
   - Backgrounds: `var(--bg-0)`, `var(--bg-1)`, `var(--bg-2)`, `var(--bg-elev)`, `var(--bg-elev-2)`, `var(--bg-tint)`
   - Borders: `var(--border)`, `var(--border-soft)`, `var(--border-strong)` (colour) — and **write the width as `var(--border-width)`, never a literal `1px`** (see "Global border control" below)
   - Text: `var(--fg-1)`, `var(--fg-2)`, `var(--fg-3)`, `var(--fg-4)`
   - Brand / semantic: `var(--accent)` (= `var(--brand)`), `var(--accent-soft/-mid/-line)`, `var(--purple)`, `var(--success)`, `var(--warning)`, `var(--danger)`; for custom opacities use `rgba(var(--brand-rgb) / --purple-rgb / --success-rgb / ..., a)`.
2. **Never write `var(--foo, #darkhex)` fallbacks.** A literal dark hex as a fallback silently breaks light mode. If the variable doesn't exist, add it to the palette.
3. **A new theme-sensitive colour goes in the PALETTE block of BOTH theme blocks** (with an `-rgb` triple if it'll ever need an alpha) — never sprinkled through feature code.
4. **Verify before pushing:** open the feature, toggle `body.light-mode`, confirm every panel/text/border is legible in both. A quick whole-app smoke-test: temporarily change `--brand`/`--brand-rgb` in one block and confirm the change cascades everywhere (including the background glow), then revert.

### Global border control (one knob for colour · thickness · on-off)

Every **neutral container / table / divider border** in the app is written as
`var(--border-width) solid var(--border)` (or `-soft` / `-strong` for the colour). That makes the whole app's structural border treatment controllable from **one place** — and there are now **three** equivalent edit surfaces, in order of friendliness:

- **App Settings → Appearance** (the UI; admin-only) — the border knob now lives inside the **combined theme panel** (see "Appearance panel" below): border thickness (Off / Thin / Medium / Thick) and the app/code fonts sit in a shared card, and the per-theme border **colour** is one of the palette swatches under each theme. Every control previews live in the admin's own window via `window.WA_APPEARANCE.setOverrides` and **auto-saves** to app settings with the shared green tick. Markup in `ui/main-panel/app/settings/app-settings/app-settings.html`; logic (`_initAppearance` / `_loadAppearance` / `_buildThemeCard`) in the sibling `.js`.
- **`data/config/app-settings.json`** (the live override the panel writes, also hand-editable) — `border_color_dark`, `border_color_light`, `border_width`, `font_sans`, `font_mono` (plus the full palette keys — see "Appearance panel") are served app-wide through the public `/api/v1/auth/ui-config` endpoint and injected at boot as CSS-variable overrides by **`ui/shared/js/appearance.js`** (mirrors the background manager). Edit the JSON, reload the page — no server restart, no CSS edit — and every visitor follows. **Exception:** adding a *brand-new* key the `AppSettings` model didn't have requires a server restart (no Python hot-reload); editing existing keys is restart-free. A **blank** value falls back to the design-system.css default for that token. (Server side: `app.admin.settings.get_appearance_config` + the `AppSettings` model.)
- **Per-theme injection (scope matters — derived tokens depend on it):** `appearance.js` emits its palette overrides as **two theme blocks** — `:root{…}` (dark) and `body.light-mode{…}` (light) — plus a separate `:root{…}` block for the theme-independent knobs (fonts, radius/shadow/ui scale). The **dark** block must land on **`:root`** because dark is the root/default theme and design-system.css declares the dark **derived** tokens (`--accent`, `--bg-1`, `--border-soft`, the brand-glow shadows, …) at `:root`, computing them from the base swatches at the **same scope**. The **light** block stays on `body.light-mode` (its derived tokens are declared there too) — and design-system.css always re-declares the **full** light palette at `body.light-mode` (higher specificity than `:root`), so a dark-only override can never leak into light mode. `--border-width` is written into **both** theme blocks so "Off"/thickness wins in either mode. Keep this shape: dark/default → `:root`, light → `body.light-mode`.
- **`design-system.css` PALETTE block** — the CSS defaults (`--border` / `-soft` / `-strong`, `--border-width`, `--font-sans` / `--font-mono`) that all of the above fall back to when an override is blank.

Either surface does the same three jobs:

- **Recolour** → `--border` / `--border-soft` / `--border-strong` in CSS, or `border_color_dark` / `border_color_light` in the JSON (both per-theme; the JSON drives only the main `--border`, soft/strong stay their CSS depth variants).
- **Thicken / thin** → `--border-width` in CSS, or `border_width` in the JSON (default `1px`).
- **Turn ALL borders off** → `--border-width: 0` (CSS) or `"border_width": "0"` (JSON).

Rules when adding UI:

1. **A neutral border's width is `var(--border-width)`, not a literal px.** A hard-coded `1px` opts that border out of the thickness/off control — don't do it for structural borders.
2. **Accent/semantic borders are deliberately NOT wired to this** — focus rings, `:hover`/`.active` highlights, error/drag (`dashed`) outlines, status borders, and **loading-spinner rings** keep their own literal width and accent colour, so they survive a "borders off" and stay meaningful. Leave those as-is.
3. **Documented colour-only exceptions:** the floating-glass surfaces re-skinned in `index.css` (`#main-panel`, the `#chat-panel .chat-pill` family, chat bubbles, `#chat-header` divider) keep a translucent `color-mix(... currentColor ...)` border *colour* (it auto-adapts to the theme) but **do** read `var(--border-width)`, so they still thicken and turn off with the global knob — they just don't follow the `--border` colour. The admin file-tree's `--files-border` / `--files-border-strong` tokens are redefined to `var(--border)` / `var(--border-strong)`, so the file tree follows the global colour too. The terminal overlay (self-contained dark surface) and the agent-loop visualizer keep their own borders, same as the other palette exceptions above.

### Known self-contained palette exceptions (NOT driven by the global scheme)

A few surfaces deliberately own their colours and will **not** follow a palette re-theme. If you do a full re-skin, edit these too:

- **The agent-loop visualizer** — `ui/main-panel/agents/agent-loop/loop-visual.css` (+ `js/loop.js`, `js/loop-diagram.js`) has its own complete dark **and** light palette, including many bespoke neutral shades with no design-system equivalent and a distinct light-mode blue/green scheme. Left as a self-contained island.
- **The terminal ANSI palette** — `ui/shared/js/terminal.js` defines conventional xterm ANSI colours; these are terminal-standard, not app brand.
- **Animated backgrounds** — each `ui/background/<id>/<id>.css` owns its colour tokens. They reference the palette where possible (so a brand swap mostly flows), but bespoke values (e.g. Stargaze's nebula triples) live in the plugin folder, not `design-system.css`. For a full re-skin, edit each background's CSS too.

### The Appearance panel — one combined theme editor (palette · background · fonts)

App Settings → **Appearance** is **one** group. It is a **two-row expandable table** built on the shared ability-table component (`.ac-list` → two `.ac-row`s, **Light** and **Dark**, each with an `.ac-ability-row` head + an `.ac-ability-body` that opens like an ability row), plus a shared, theme-independent card below. The group's **header bar** (the sticky `.ac-category-summary` with the palette icon + "Design") also carries a compact **live theme switch** on its right edge (`.ac-theme-switch` — Light · Dark · System): the SAME per-browser toggle as the user-menu dropdown (it reuses `setTheme`/`highlightThemeOption` exported from `ui/shared/js/user-panel.js`, so both stay in sync; wired by `_initThemeSwitch` in `app-settings.js`). It is `position:absolute` so it never grows the header's height. This flips *which* theme you're viewing — distinct from the Light/Dark rows below, which edit each theme's colours.

- **Each theme row** expands to reveal: a strip of **one-click preset chips** (`_PRESETS` — Mono (neutral greyscale) / Amber / Ocean / Forest / Grape / Rose; the factory default = Ocean light + Amber dark), the **animated-background select**, and the **full palette as colour swatches** (accent, secondary, the three status hues, the surface shades, text, muted text, ambient glow, border, **plus User bubble + Agent bubble + Chat pill**). The swatch **order + which CSS token each drives is the single source of truth in `ui/shared/js/appearance.js`** (`window.WA_APPEARANCE.tokens`); the panel only attaches a friendly label + icon + flags per token (`_TOKEN_META` in `app-settings.js`). The rows expand via the shared `_wireBootRow` (clicks on a control inside the body never toggle the row).
  - **Presets re-theme the WHOLE UI, not just the accent.** Each `_PRESETS` entry carries its own **neutral wash** (`surface_bg`/`surface_panel`/`surface_tint`/`text`/`text_muted`/`border_color`) per theme — a subtle hue-tinted set — so Forest/Grape/Ocean stop looking peachy.
  - **Clicking a theme is a COMPLETE RESET of that mode** (`_applyPreset`). It rewrites *every* palette token: the theme's explicit value where it has one, else **blank** for `follow` tokens (bubbles → back to tracking the theme) and the **design default** for everything else — wiping any per-swatch colour, opacity, or bubble override. Both the swatch and its opacity slider are re-synced.
  - **"Add theme" + custom themes.** The `+ Add theme` button (`_addCustomTheme`) snapshots the CURRENT palette of **both** modes (`_snapshotTheme` — every token incl. opacity `#rrggbbaa` and bubble state) into a named entry appended to `_customThemes`, which renders as an extra chip (with a delete `×`) beside the built-ins. Custom themes persist as a **JSON array string** in the `custom_themes` `AppSettings` field (admin-only; NOT in `ui-config` — visitors only need the resolved palette). They apply / reset exactly like built-in presets.
  - **`_TOKEN_META` flags:** `alpha:true` adds an **opacity slider** beside the colour swatch (on the surfaces + border + both bubbles + the chat pill); the colour + slider combine into a `#rrggbbaa` value. `follow:true` (the two bubbles **and the chat pill**) means the token defaults to **blank** (= follow the theme-derived default) and is skipped by presets; set its swatch to pin it. A bubble/pill at 0 opacity = text with no background. **Bubble/pill-bg defaults** resolve at the **target element** in `index.css`, not as global `:root` tokens — all three (user bubble, agent bubble, chat pill) share the identical `color-mix(--bg-elev 82%/85%)` default, so they match out of the box and each can be pinned independently. (Why it must resolve at the element, not `:root`, is in `docs/claude/ui-theming-reference.md`.)
- **The shared card** holds border thickness + the two fonts (these are not per-theme).
- **Every control auto-saves** per theme with the shared `.agents-autosave-check` green tick (same mechanic as the ability tables / model table — `_markCheckSaving` / `_flashCheck`), and previews live via `WA_APPEARANCE.setOverrides` (palette/border/font) or `WA_BG.setChoice` (background). The presets, swatches and background selects are **built in JS** by `_buildThemeCard` into each row's `.ac-ability-body`, so the HTML stays small (just the two row shells).

**Other knobs that ride the appearance pipeline** (same delivery route: `appearance.js` DEFAULTS → `_DEFAULT_APPEARANCE` + `AppSettings` + `UiConfigResponse` → injected at boot):

- **Theme-independent style knobs, JSON-only** — `radius_scale`, `shadow_strength`, `ui_scale`, `reduce_motion`, `cursor_glow`.
- **Media-cache keys** — `media_cache_enabled` / `media_cache_budget_mb` emit no CSS; they call `WA_MEDIA_CACHE.configure(...)`. Their admin UI is the Memory Cache (RAM) row in Data Settings.
- **Chat-composer geometry** — ten `chat_pill_*` keys for the tall composer (`CHAT-PILL-VARS` block in `app1.css`).
- **Chat panel layout** — `chat_position` / `chat_default_visible`, the two rows in the Design group.

Full key list, defaults, exact wiring and the mechanism notes for each: **`docs/claude/ui-theming-reference.md`**.

**How a swatch reaches the screen:** each palette token is stored per theme as `data/config/app-settings.json` keys `<base>_dark` / `<base>_light` (the border's are the historical `border_color_dark/_light`), defaulted server-side in `app.admin.settings._DEFAULT_APPEARANCE` (mirror of the two `design-system.css` palette blocks) and the matching `AppSettings` fields, served via `/api/v1/auth/ui-config`, and injected at boot by `appearance.js`. For any hue with translucent tints, `appearance.js` **derives the `-rgb` triple from the chosen hex** and writes both, so changing one swatch moves the solid colour *and* every `rgba(var(--x-rgb), a)` tint. Tokens cover `--brand`(+`-strong`/`-rgb`), `--purple`(+…), `--success`/`--warning`/`--danger`(+`-rgb`), `--bg-0`/`--bg-elev`/`--bg-tint`, `--fg-1`/`--fg-3`, `--ambient-2-rgb`, `--border`, the bubbles `--user-bubble-bg`/`--agent-bubble-bg`, and the chat composer pill `--chat-pill-bg`.

**Opacity (surfaces + bubbles).** A stored value may be `#rrggbb` **or** `#rrggbbaa` — the trailing `aa` is opacity. `appearance.js`'s `parseColor` reads either; `pushToken` emits the token as `rgba(r, g, b, a)` when `a < 1` (else a solid hex), while the `-rgb` triple stays alpha-free so existing `rgba(var(--x-rgb), a)` tints are unaffected. The panel stores/loads the alpha with the colour via `_combineColor` / `_splitColor`. Because surfaces feed the `color-mix` neutral ramp, a translucent surface yields a translucent ramp — by design (e.g. a see-through panel shows the animated background through every derived step).

**The graduated neutrals follow the swatches too — by derivation, not by a separate swatch.** `--bg-1`, `--bg-2`, `--bg-elev-2`, `--bg-tint-2`, `--border-soft`, `--border-strong`, `--fg-2` and `--fg-4` are **not** their own editable values; in `design-system.css` (both theme blocks) each is a `color-mix(in oklab, …)` of a swatch anchor (`--bg-0`/`--bg-elev`/`--bg-tint`/`--border`/`--fg-1`/`--fg-3`), on the rule *"an elevated surface moves a little toward the text colour"* (true in both themes). Because the mix references the swatch `var()`, editing a swatch flows through the **whole neutral ramp live**, not just the three raw surface anchors. So there are only **6 raw neutral knobs** (Background = `--bg-0`, Panel = `--bg-elev`, Tint = `--bg-tint`, Border = `--border`, Text = `--fg-1`, Muted = `--fg-3`) and every other neutral step is a function of them. **When you add a new neutral step, derive it the same way** — never hard-code a fixed neutral hex, or it will be the one surface that ignores the swatches. (This is why feature CSS must reference `var(--bg-*)`/`var(--fg-*)`/`var(--border*)` and never a raw neutral hex in *either* theme — a hard-coded light **or** dark neutral silently opts that surface out of its swatch.)

**Adding a palette token to the editor** = add it in **three mirrored places**: the `TOKENS` map in `appearance.js` (drives injection + the swatch order), `_DEFAULT_APPEARANCE` + the `AppSettings` field in `app/admin/settings.py` (persistence + the public endpoint), and a label/icon in the shared `TOKEN_META` in **`ui/shared/js/appearance-editor.js`** (NOT in `app-settings.js` — that file now imports `TOKEN_META`/`APP_DEFAULTS`/`PRESETS`/`FOLLOW_ALPHA` + the colour helpers from `appearance-editor.js` so the admin and per-user editors share one source). No HTML edit (swatches are built from the token list). A brand-new key needs a **server restart** to register on the `AppSettings` model.

### Per-user themes (each user's own appearance, layered on the global one)

The Appearance panel carries one admin switch — **"Allow per-user themes"** (`allow_user_appearance`, off by default; the toggle row sits right under the Light/Dark rows). When **on**, every signed-in user gets a **"My appearance"** editor on their **Manage Account** page (`ui/shared/js/account.js`) — the *same* Light/Dark theme table (presets, background, the full swatch palette, fonts, border) plus a **"Reset my theme"** button. It is built by `ui/shared/js/appearance-me.js`, which imports the shared definitions from `appearance-editor.js` and mirrors the admin `_buildThemeCard` structure (change one → mirror the other).

- **Storage = the user's profile.** A sparse JSON patch (only the tokens that user changed) is kept in `user_profiles.appearance` (migration 031 / the in-code "Migration 031" block in `app/db/local.py`; DB helpers `get_user_appearance` / `merge_user_appearance` / `set_user_appearance`). Self-service via **`/api/v1/auth/me/appearance`** (GET reads `{allow, overrides}`; PUT merges a patch — a blank value clears that token; DELETE resets all).
- **Application = one seam, no `appearance.js` change.** `GET /api/v1/auth/ui-config` already resolves the caller; when the flag is on and the caller is a signed-in user it layers their saved overrides on top of the global appearance (and re-validates their background) before responding, so the boot-time applier just paints an already-personalised theme. **Fall back to global:** any token the user hasn't set keeps the global value. When the flag is off the merge is skipped and the editor is hidden, so saved personal themes are simply ignored (not lost). The per-user editor reads the installed-background catalog from the public **`/api/v1/auth/backgrounds`** (the non-admin sibling of `/admin/settings/backgrounds`).

## Edge fades over a dynamic/themed background — mask, never a colour

When a scrolling strip (a horizontal tab carousel, a content list, etc.) needs its edges to **fade out** so the content doesn't cut off abruptly, **fade the content with a CSS alpha mask — never paint a coloured gradient to fake it.**

**Why this is a hard rule here:** several surfaces in this app sit over a background that is **not a static colour** — most notably the **live stargaze genui** (`ui/background/stargaze/stargaze.js`), which continuously redraws a warm peach (light) / nebula (dark) glow that shifts with theme and pointer position. The page base colour (`#faf5ee` light / `#07070c` dark, set in `index.css`) is *not* what you actually see there. So any edge fade implemented as `linear-gradient(..., <some colour>, transparent)` will be a guessed static colour that can never match the moving background — it shows up as a lighter/whiter rectangle. A **mask** paints no colour at all (alpha only): it fades the *content* to transparent and lets the real background — glow and all — show straight through, matching automatically in both themes and even while the genui animates.

**The pattern (two ingredients):**

1. **Transparent buttons/affordances** (e.g. carousel chevrons) positioned over the edge — `background: none`, glyph colour from a design-system var so it tracks the theme.
2. **A `-webkit-mask-image` / `mask-image` linear-gradient on the *scroller itself*** that goes `transparent → #000` over the fade width at the edge, applied **only on the side(s) that can still scroll**, toggled by the scroll-state classes the carousel JS already maintains.

**Canonical implementations to copy:**

| Location | Selector |
|----------|----------|
| Agent card carousel (origin of the technique) | `.agent-card-tabs` / `.agent-card-tabs-chev` in `ui/main-panel/agents/agents.css` |
| Main header tab strip | `#main-tabs` "Masked edge-fade carousel" block in `ui/shared/css/design-system.css` |

Both blocks carry a full explanatory comment. The `:has()` per-side selectors used for the header need a modern browser (Chrome 105+, which the app targets); if you ever need older support, toggle the mask via a JS class on the scroller instead.

## Pinned rounded scroll corners — mask, reveal the background

When a **scroll viewport** should show **rounded corners that stay put while the content scrolls** (they don't scroll away), round it with an alpha **mask** — not `border-radius` alone, and not by shrinking the viewport. This is the same "paint no colour, reveal what's behind" idea as the edge fade above, and it solves three problems at once:

- **The rounding stays pinned to the viewport.** A mask paints the scroller's *fixed* box, so the rounded corners sit still while rows scroll underneath.
- **The scrollbar stays at the far edge and the whole width still scrolls.** We do **not** clip the viewport narrower — the scroller stays full-width; we only knock the four corners transparent.
- **The corners are theme-correct.** The mask reveals the panel's **own** background (gradient, glow, the animated genui) through the corners, so they match in light/dark automatically — never a guessed corner colour.

**The reusable class:** `.rounded-scroll-corners` in `ui/shared/css/design-system.css` (right after the edge-fade carousel block). Add it to any `overflow:auto` scroller and its four corners round, pinned. Two custom-property knobs:

| Knob | Meaning | Default |
|------|---------|---------|
| `--rsc-r` | corner radius | `--r-lg` |
| `--rsc-side` | per-side inset to a **centred column** (keeps that side strip opaque, so a scrollbar / centring gutter stays visible) | `0` (full-width rounding) |

It builds an opaque rounded rectangle as a `mask-composite: add` **union** (two side strips + a horizontal bar + a vertical bar + four inset corner discs) and knocks out only the four outer-corner slivers → normal **outward** (convex) rounding. A `0` radius makes the whole mask a full-opaque no-op, which is how a shared scroller can carry the class but stay un-rounded until switched on.

**Reference implementation — the centred-column variant:** App Settings (`#app-config-content`, gated to its tab in `ui/shared/css/app3.css`). That scroller is shared by all 10 App-Config tabs, so the class is left **inert** by default (`--rsc-r: 0`) and switched on only while one of the standard centred-column sections is active — **App Settings, Agent Settings, and Data Settings** (each an `ac-stickynav` section with 750px-capped `.ac-list` tables) — where it's fed a **computed** `--rsc-side` so the rounding hugs the 750px column (not the full panel) and tracks the width down to the scrollbar-gutter floor. (App/Agent Settings have a page-assistant pill below the scroller; Data Settings has none, so its rounded bottom corners pin to the panel floor instead of above a pill.) The width is resolved with **container-query units** (`100cqw`) off a non-scrolling wrapper (`#app-config-scroll-wrap`, `container-type: inline-size`) that hosts the column math — the wrapper exists only to be that query container. Copy that wrapper-plus-`cqw` pattern only when you need the rounding inset to a centred column; for plain full-width rounded corners just add the class and (optionally) set `--rsc-r`.

## Mobile soft keyboard — `body.kb-open` hides bottom clutter off a focused field

On a touch device, opening the on-screen keyboard shrinks the **visual** viewport (the layout viewport / `window.innerHeight` stays put). The viewport tracker in `index.html` (the same IIFE that drives `--app-height`) uses that gap to flag the keyboard: when, on a **mobile-width** viewport, the visual viewport is meaningfully shorter than the layout viewport **and** an editable field holds focus, it toggles **`body.kb-open`**. The class is mobile-gated there, so desktop never sets it.

Use it to get **bottom-docked clutter out from under a focused field** while the keyboard is up. Today the App Config pages do this (`body.kb-open` rules in `ui/shared/css/app3.css`): the page-assistant **chat pills** (`#ac-pa-bar` / `#ac-as-pa-bar`) hide, and the **sticky section headings** (`.ac-stickynav .ac-category-summary`) drop from `position: sticky` to `static` so they stop stacking at the top/bottom edges and crowding the field (e.g. the Models table's "Search models…" input) — restored automatically on keyboard close. Any other page with a fixed/floating element that would sit over a focused input can react to the same class; keep the actual hiding scoped to that page's own selectors.


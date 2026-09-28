# UI theming — appearance-pipeline and panel reference

Reference companion to the `ui_theming` skill (Codebase Admin ability). The skill carries
the **rules** — palette discipline, the mask techniques, the panel's public shape. This file
carries the **implementation catalogue**: every key family that rides the appearance pipeline,
and the mechanism notes behind a few non-obvious defaults. Read it when adding a token/knob,
wiring a new appearance key, or debugging why a themed value resolves where it does. Verify
the paths and function names against the code if they look stale.

## Why palette overrides must land at `:root` (dark) and `body.light-mode` (light)

If the dark override sat on `body:not(.light-mode)` instead (as it once did), those `:root`-scoped derived tokens would keep computing from the *un-overridden* base defaults — so the whole derived ramp (accent alias, neutral surface/border steps, status tints, brand glow) would stay **frozen on the stock cyan/blue palette in dark mode** even after a theme change.

## Bubble / chat-pill background defaults — why they resolve at the element

**Bubble/pill-bg gotcha:** the theme-following default is derived at the **target element** in `index.css` (`--chat-bubble-bg: var(--user-bubble-bg, color-mix(in oklab, var(--bg-elev) …)))`, and `#chat-panel .chat-pill { background: var(--chat-pill-bg, color-mix(in oklab, var(--bg-elev) …)) }`), *not* as a global `--user-bubble-bg`/`--agent-bubble-bg`/`--chat-pill-bg` token at `:root`. Those override tokens are left **unset** unless a swatch pins them. Reason: the Appearance override of `--bg-elev` lands on `<body>`, so a `color-mix(var(--bg-elev))` evaluated at `:root` would bake in the pre-theme panel colour — the cause of the "peachy bubbles in every theme" bug. Resolving it at the element (inside `<body>`) picks up the live themed panel. **All three (user bubble, agent bubble, chat pill) share the identical `color-mix(--bg-elev 82%/85%)` default**, so they match out of the box and can each be pinned independently. The chat-pill default + override is scoped to `#chat-panel .chat-pill` only (resting + focus + drag) — the other floating-glass pills (ability-search / genui / browser) keep their lighter shared glass skin.

## Keys that ride the appearance pipeline

Each family is delivered the same way: `appearance.js` DEFAULTS → `_DEFAULT_APPEARANCE`
+ the `AppSettings` field in `app/admin/settings.py` → served on `/api/v1/auth/ui-config` →
applied at boot.

### JSON-only style knobs (theme-independent)

**JSON-only style knobs (no UI control — edit `data/config/app-settings.json`).** Five *theme-independent* knobs are deliberately **not** in the panel; they're applied app-wide by `appearance.js` exactly like the palette, but you change them only by editing the JSON (or POSTing to `/admin/settings/app`): `radius_scale` (corner roundness × — design-system radii are `calc(<px> * var(--radius-scale))`), `shadow_strength` (drop-shadow depth × — shadow alphas are `calc(<a> * var(--shadow-strength))`; the accent `--shadow-glow` is left unscaled), `ui_scale` (whole-UI `zoom` on `<body>` via `--ui-scale`), `reduce_motion` (`"on"` → `appearance.js` injects a global animation/transition-collapsing rule), and `cursor_glow` (`"off"` → injects `#cursor-glow{display:none}`). Each defaults to a no-op (`"1"` / `"off"` / `"on"`). To add another such knob: a CSS hook in `design-system.css` (a `calc()` on a `--var`, or a `zoom`), a default in `appearance.js` DEFAULTS + the apply logic, and the field in `_DEFAULT_APPEARANCE` + `AppSettings` + `UiConfigResponse`.

### Media-cache keys (no CSS — they configure the JS cache)

**Not every appearance-pipeline key is a theme/CSS knob — nor JSON-only.** The RAM media-cache keys **`media_cache_enabled`** (`"on"`/`"off"`) and **`media_cache_budget_mb`** ride this exact pipeline (DEFAULTS in `appearance.js` → `_DEFAULT_APPEARANCE` + `AppSettings` + `UiConfigResponse` → injected at boot), but instead of emitting CSS they call `window.WA_MEDIA_CACHE.configure(...)` to size the in-browser attachment/thumbnail cache (`ui/shared/js/media-cache.js`). They are the first such keys with their **own admin UI outside the Appearance panel** — the **Memory Cache (RAM)** row in App Config → **Data Settings** (`ui/main-panel/app/settings/data-settings/media-cache-settings.js`), which saves via `POST /admin/settings/app` and previews live through `WA_APPEARANCE.setOverrides`. So a control that writes an appearance-config key can live wherever it fits functionally; the delivery pipeline is the same. The cache is a **volatile speed layer** (LRU-evicted under a byte budget, never a place of record) sitting in front of whichever attachment store actually holds the bytes.

### Chat-composer geometry knobs (`chat_pill_*`)

**Chat-composer geometry knobs (also JSON-only).** The whole look of the **tall chat composer** (`#chat-input-row`) is exposed the same way — ten `chat_pill_*` keys, each a plain CSS length, injected by `appearance.js` as the matching `--chat-pill-*` variable and consumed by the `CHAT-PILL-VARS` block in `app1.css` (each `var()` carries a fallback equal to the JSON default, so the look holds before the override lands). The knobs: `chat_pill_max_width` (width cap; the composer is centred under it), `chat_pill_radius` (corner roundness), `chat_pill_padding` (outer padding shorthand), `chat_pill_min_height` / `chat_pill_max_height` (resting + grow-cap text-area height), `chat_pill_font_size` (input + placeholder), and the four button knobs `chat_pill_attach_size` / `chat_pill_attach_icon` (attach `+` box + glyph) and `chat_pill_button_size` / `chat_pill_button_icon` (mic / send box + glyph). They drive **only** the tall composer — the compact 1-line bars (search/builder/genui/browser) keep their fixed sizing via the more-specific `.chat-pill-1line *` rules. Wiring is table-driven (`CHAT_PILL_KNOBS` in `appearance.js`), so adding one is a row there + a `var()` in `app1.css` + the field in `_DEFAULT_APPEARANCE` + `AppSettings` + `UiConfigResponse`.

### Chat panel layout (`chat_position`, `chat_default_visible`)

**Chat panel layout (UI-exposed, in the Design group).** Two app-wide keys are exposed as standard config rows at the bottom of the App Settings **Design** table (`#ac-design-list`, alongside the Startup & Boot rows) and ride the same `appearance.js` → `/api/v1/auth/ui-config` pipeline: `chat_position` (`"right"` default | `"left"`) and `chat_default_visible` (`"visible"` default | `"hidden"`). `chat_position: "left"` makes `appearance.js` inject `@media (min-width:801px){#stage{flex-direction:row-reverse}}`, flipping the desktop split so the chat sits left of the main panel; `chatResize.js` reads the live geometry (chat-vs-handle position) to keep drag-to-resize working on either side, so no setting is read there. `chat_default_visible` only **seeds the first-visit desktop default** — `index.html`'s pre-paint block + `window.__chatVisibilityDefault()` read it off the cached `wa_appearance` blob; once a visitor toggles the chat their own per-browser `chatPanelVisible` wins. Both selects are wired in `app-settings.js` `_wireChatLayout` (save + green tick via `_saveAppearance`, live preview via `_previewAppearance`) and loaded in `_loadAppearance`; fields live in `_DEFAULT_APPEARANCE` + `AppSettings` + `UiConfigResponse` + `appearance.js` DEFAULTS.

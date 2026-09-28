# Welcome landing page (`ui/splash/splash-page/`)

A premium, animated **public landing page** shown at the **front door** (`/`).
A new visitor (or a search crawler / link scraper) sees this welcome page first;
clicking **Enter app** takes them into the workspace. It introduces WebAgent's
features with a hero, a scrolling feature tour, a pinned showcase, a card grid,
and a final call-to-action.

> **Why it's a real page, not an overlay.** Earlier this was a JavaScript overlay
> drawn *on top of* the app after boot. Search engines and chat/social link
> unfurlers don't run that JavaScript, so they saw nothing — and it had no address
> of its own. It is now **server-rendered at `/`** by
> `app/main.py` → `_render_landing_page`, which wraps this folder's `splash-page.html`
> in the `#splash-root` container the CSS targets and ships the full copy in the
> initial HTML. That makes it crawlable and gives every shared `/` link a proper
> preview card (the SEO/Open Graph tags are built by `_seo_head_block`).

## The app lives at `/app`

With the landing on, the front door (`/`) is the welcome page and the app has a
stable home at **`/app`** (and `/index.html`), which always bypass the landing.
The landing's **Enter app** button and the installed **PWA** (`manifest.json`
`start_url`) both point at `/app`, so a committed/installed user goes straight to
the workspace.

## Two controls gate it

- **App-wide master switch** (admin): App Settings → Startup & Boot → **Welcome
  landing page**. Persisted server-side as `splash_enabled` in
  `data/config/app-settings.json`, served via `/api/v1/auth/ui-config`. When
  **off**, `/` serves the app directly (no landing) and the catalog omits this
  plugin entirely (so `window.WA_SPLASH` is absent and the account row hides).
- **Per-device skip** (each visitor): the **`wa_seen_splash` cookie**, read
  **server-side** by the `/` front door so a returning visitor skips straight to
  the app. It's written when the visitor enters:
  - **"Don't show this again"** ticked → a **persistent** cookie (1 year) + a
    `localStorage["webagent.splashSeen.v1"]` mirror (parity with the account
    toggle) → never shown again on this device.
  - not ticked → a **session** cookie → the landing returns in a fresh browser
    session.
  The **Show welcome screen** toggle in Manage Account → Preferences drives the
  same flag via the `window.WA_SPLASH` API this plugin exposes.

## It is a drop-in plugin — delete this folder to remove the feature

Everything the landing needs lives here. It plugs in through two seams:

- **The front-door renderer** (`_render_landing_page`) reads `splash-page.html`
  from this folder at request time and returns `None` if it's missing — so the
  `/` route cleanly falls back to the app shell when the folder is gone.
- **The page-catalog** still discovers this folder (`page.json`) and the shell's
  boot hook (`ui/shared/js/partial-loader.js` → `bootSplashPlugins`) imports
  `js/splash-page.js` and calls its `start`. That now exists for ONE reason:
  exposing `window.WA_SPLASH` so the account toggle works. `startSplash` is a
  no-op (no overlay to mount).

Delete this folder and: the catalog has no entry, `_render_landing_page` returns
`None` (front door → app shell), `window.WA_SPLASH` is undefined (account row
hides) — no tab, no error, no trace.

The admin **App Settings → Welcome landing page** toggle is a built-in row, so it
still appears in a build that stripped this folder. It detects the absence by
probing `splash-page.html` (the same file the server renders) and, when it's
missing, **sits off** and shows an inline note; trying to switch it on flashes a
warning ("Add a welcome page to ui/splash/splash-page first") instead of saving —
so there's never a dead switch with nothing behind it.

## Files

| File | Purpose |
|------|---------|
| `page.json` | Descriptor (`entry`, `start`, `css`, `html`) — keeps `WA_SPLASH` loaded in the app shell. |
| `splash-page.html` | The landing markup — hero, feature sections, pinned showcase, card grid, final CTA. Read by `_render_landing_page` (server) and shipped inside `#splash-root`. |
| `splash-page.css` | All styling, scoped under `#splash-root`. Colours come **only** from design-system tokens, so it's correct in dark **and** light mode and follows any palette re-skin. |
| `js/splash-page.js` | App-shell hook: cookie+localStorage-backed `window.WA_SPLASH` + a no-op `startSplash`. |
| `js/splash-landing.js` | Standalone bootstrap for the server-rendered page: runs the effects and wires **Enter app** (set cookie → go to `/app`). |
| `js/splash-effects.js` | Shared effects engine (reveal-on-scroll, sticky topbar, parallax, pinned cross-fade, tilt/magnetic/spotlight, Lenis). |
| `js/lenis.min.js` | Vendored [Lenis](https://github.com/darkroomengineering/lenis) smooth-scroll lib (UMD, `window.Lenis`). Offline-safe; removed with this folder. |
| `img/*.webp` | Feature screenshots. |

## Behaviour

- **Visible without JS.** The markup ships inside `#splash-root.is-ready`, so the
  content shows (and the inner `[data-splash-scroll]` scrolls) even before
  JavaScript runs — which is what crawlers read. The effects (`splash-effects.js`)
  enhance on top; the **Enter app** buttons are wired by `splash-landing.js`.
- **Effects:** Lenis inertia scrolling (scoped to the page's own scroller),
  reveal-on-scroll, parallax on the screenshot frames, 3D tilt / magnetic buttons
  / cursor spotlight on hover, and a pinned cross-fade showcase. All disabled
  under `prefers-reduced-motion` (everything shows instantly).

## Replacing the screenshots

Drop new images into `img/` using the same filenames (`chat.webp`, `genui.webp`,
`browser.webp`, `wiki.webp`, `automations.webp`, `agents.webp`, `abilities.webp`).
Frames are a fixed aspect ratio, so a replacement is a straight file swap. If an
image is missing the frame degrades gracefully to a gradient placeholder.

## To re-test the first-visit experience

The front door checks the `wa_seen_splash` cookie. Clear it (and the mirror) in
the browser console, then load `/`:

```
document.cookie = 'wa_seen_splash=; path=/; max-age=0';
localStorage.removeItem('webagent.splashSeen.v1');
```

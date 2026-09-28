# `ui/background/` — pluggable animated backgrounds

The full-screen animated background behind the whole app. Each background
is a **self-contained drop-in folder**. The app admin chooses which one
runs, **separately for dark and light mode**, from the Appearance group in
**App Settings**. Built-in choice **None** = plain themed background (the
cursor glow and card spotlights still run).

```
ui/background/
  _engine/            core controller — NOT a background, never deleted
      registry.js         window.WA_BG.register(id, {start, stop})
      manager.js          owns the <genui>; loads/swaps the chosen background
  _TEMPLATE/          copy this to make a new background
  stargaze/           starfield (default for DARK)
  bullet-grid/        mouse-reactive dot grid (default for LIGHT)
  aurora/             slow gradient mesh
  particles/          floating bokeh orbs
```

## Add a background (no core edits)

1. Copy `_TEMPLATE/` to `ui/background/<your-id>/`.
2. Rename the three files to `<your-id>.js` / `.json` / `.css` and set the
   `id` in both the JS (`RID`) and the JSON to `<your-id>` (= folder name).
3. Implement `start(genui)` / `stop()` and put your colour tokens in the
   CSS (derive them from the palette in `ui/shared/css/design-system.css`).

It now auto-appears in the Appearance selector — discovered by
`GET /admin/settings/backgrounds` (which scans these folders server-side).
**Delete the folder** and the background, its tokens and its selector entry
all disappear; if it was the selected one the app falls back to Stargaze.

## Descriptor (`<id>.json`)

| field | meaning |
|-------|---------|
| `id` | stable id = folder name (= `RID` in the JS) |
| `name` | label shown in the selector |
| `description` | one line shown under the label |
| `icon` | Lucide icon name |
| `status` | `stable` \| `beta` \| `experimental` |
| `order` | sort position in the selector (lower first) |
| `themes` | hint: which themes it suits (all are theme-aware) |

## Renderer contract (`<id>.js`)

- Calls `window.WA_BG.register(RID, { start, stop, refresh })` on load.
- `start(genui)` — the engine hands over the shared genui; the renderer
  owns its sizing (`devicePixelRatio`), its listeners and its
  `requestAnimationFrame` loop, and reads colours from its CSS tokens.
- `stop()` — cancels the loop, **removes every listener it added**, clears
  the genui. Called on a theme flip or an admin change.
- `refresh()` — **optional**. The engine calls it when the admin edits the
  theme colours live in the Appearance panel (a `wa-appearance-changed`
  event). Re-read your CSS tokens so the running loop repaints in the new
  palette — no resize or restart. Omit it if the look isn't palette-derived
  (e.g. Stargaze's nebula is self-contained, but it still re-reads so a future
  palette-linked token would track too).
- Bails when `prefers-reduced-motion` is set (the genui is `display:none`
  there via `ui/shared/css/index.css`).

The cursor glow and card spotlights are **not** part of a background — they
live in `ui/shared/js/cursor-effects.js` and run for every background,
including None.

See [the ui_theming skill](../../plugins/abilities/Administrator/codebase_admin/skills/ui_theming.skill.md) for the
full design notes.

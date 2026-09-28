# Dashboard card plugins — drop-in contract

Every card in the Admin Dashboard is a **self-contained folder** under
`ui/main-panel/app/dashboard/cards/<id>/` — the same drop-in philosophy as
the `ui/admin-tools/<view>/` page folders. The shell scans this tree
(`GET /admin/dashboard/cards`), so **adding/removing a card = adding/removing a
folder. No shell edits.**

```
cards/
├── README.md               ← you are here
├── _lib/                   ← SHARED helpers (never edit per-card)
│   ├── card-lib.js         ← frontend formatters + stat/gauge/bars/list/sparkline
│   └── server-lib.py       ← backend time/query helpers (import as `dashboard_server_lib`)
├── _template/              ← copy this folder to start a new card
└── <id>/                   ← one folder per card
    ├── card.json           ← descriptor (id MUST equal the folder name)
    ├── card.js             ← ESM render module (required)
    ├── server.py           ← OPTIONAL backend: contributes a snapshot section
    └── card.css            ← OPTIONAL card styles (auto-injected when present)
```

## ⛔ DO NOT hardcode — the three rules

1. **DO NOT add data cards to `CARD_TYPES` in `dashboard.js`.** That registry holds
   ONLY the shell framework cards (`metric_chart` hero, `custom`, `add_card` tile).
   A new data card = a new folder here.
2. **DO NOT add section builders to `_build_snapshot()` in `dashboard/server.py`.**
   Plugin sections come from `cards/<id>/server.py` → `build_section(ctx)`, run by
   the shell in card.json `order`.
3. **DO NOT edit `instances/page.json` to mount a card's backend or CSS.** The
   catalog scan mounts backends and `dashboard.js` auto-injects `card.css`
   (idempotent, keyed by href). Drop the folder; done.

## card.json fields

| field | meaning |
|---|---|
| `id` | **must equal the folder name** — the shell maps type → folder |
| `label` | picker/title text |
| `icon` | Lucide icon name |
| `w`, `h` | default grid size (12-col grid, 88px rows) |
| `order` | picker sort position (ascending) |
| `live` | `true` → fills from the fast DB-free `/metrics?live=1` poll |
| `sections` | top-level snapshot sections this card reads (drives its spinner) |
| `chart` | optional timeseries kind (`"db"`/`"llm"`) — shell polls `/metrics/timeseries`, passes points via `ctx.ts` |
| `section` | snapshot key this card's `server.py` contributes (omit for pure-frontend cards) |
| `server` | backend filename (default `"server.py"`) |
| `aiHint` | optional one-liner for the card's AI assistant (the ✦ star) |

## card.js contract

```js
export default { render(snapshot, ctx) }   // → HTML string; re-called on every poll
```

- `snapshot` — the merged dashboard snapshot; only the sections listed in
  `card.json` `sections` are guaranteed present when `render` runs.
- `ctx` — `{ ts, chart, window }`: `ts` = timeseries points (chart cards),
  `chart` = hero-chart cache, `window` = active seconds.
- Import helpers from `../_lib/card-lib.js` — **do not redefine** formatters or
  the stat/gauge/bars/list/sparkline renderers.
- CSP-safe: no inline styles/handlers — use classes + `data-*` attributes
  (`data-bar-pct`, `data-leg-bg`, `data-tip-bg` are converted to CSS vars by the shell).

## server.py contract (optional backend)

```python
async def build_section(ctx) -> <section value>   # merged into the snapshot under card.json `section`
```

- `ctx` keys: `uid`, `window_s`, `rows` (the shell's ONE shared usage_events
  fetch — never re-scan), `run_rows`, `db_health`, `storage`, `project_root`,
  and `snapshot` — which **grows as earlier-`order` sections land**, so a card
  can read a sibling's section (e.g. `health_board` reads `devices`/`storage`).
- Import helpers from `dashboard_server_lib` (registered by the shell from
  `cards/_lib/server-lib.py`) — `to_epoch`, `raw_rows`, `iso_since`, `sql_ts`, …
- Best-effort: catch exceptions and return safe defaults; a broken backend is
  logged and skipped, never fails the dashboard.

## Adding / removing a card

1. `cp -r _template cards/<your-id>` (or write the folder by hand).
2. Fill `card.json` (`id` = folder name), write `render()` in `card.js`,
   add `build_section` in `server.py` only if it needs a new section.
3. Check: `node --check cards/<id>/card.js`; `python -m py_compile` the server.py.
4. **Restart the server** — the backend catalog is cached per process.
5. Verify: `GET /admin/dashboard/cards` (admin) lists it; it appears in the
   dashboard's **＋ Add a card** picker (catalog-driven, sorted by `order`).

To remove: delete the folder (and drop its entries from saved layouts).

## Order + sizing JSON (the layout)

Per-admin layout: `data/config/dashboard-layouts.json` (gitignored).
Default seed: `app/defaults/dashboard.json`. Each entry:
`{ "id": "c-xxx", "type": "<card-id>", "x": 0, "y": 0, "w": 6, "h": 4 }`
— `x,y` = grid position (reading order), `w,h` = size in 12 columns × 88px rows.
Saved from the UI by dragging/resizing (edit mode) → `PUT /admin/dashboard/layout`;
`Save as default` writes the seed; `Reset` restores it.

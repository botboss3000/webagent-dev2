# GenUI State Persistence — Standard

Every genui page should restore the user's last state when reopened: which
record was open, which sub-item was expanded, and where the page was scrolled.
This document is the standard every page follows. The canonical snippet to paste
is `ui/main-panel/genui/genui-state.standard.js`; the reference implementation is
`data/user_data/admin/genui/home/index.html`.

## Two layers — what the host already does vs. what the page must do

Genui pages run inside the app, mounted in a shadow root under `#genui-host`
(`ui/main-panel/genui/js/genui.js`). The host persists the **generic** state of
every page automatically, keyed per user+slug, no page code needed:

- page scroll (`#genui-host` scrollTop/scrollLeft) — saved on scroll/click/change
  and restored after remount on a retry ladder;
- inner panel scrolls of any element;
- `<details>` open state, checkbox/radio checked state, `<select>` value,
  `aria-expanded` attributes (structural state — element paths are re-resolved
  after remount);
- the last page slug (refresh reopens the same genui).

So **pages never re-implement scroll or toggle persistence.** What the host
cannot know is the page's **semantic** state — which project/card/tab is open,
which QA item is expanded, the active search query. Only the page knows that, and
that is what this standard covers.

## Convention

- **Key:** `genui:<slug>:state` — one JSON blob per page, in `localStorage`.
- **Shape:** a flat object of the page's semantic fields
  (`{ activeId, openItem, itemQ, scroll, ... }`). `null`/`undefined` fields are
  dropped on save (so closing something removes it from the blob).
- **Scroll fields** (`scroll`, `scrollLeft`) are optional — the host already
  persists them; include them if the page should be self-sufficient (e.g. opened
  outside the app host).

## Wiring checklist (for a coding agent creating a new genui)

1. **Include the snippet.** Paste `genui-state.standard.js` verbatim into the
   page's `<script>` (it defines the global `GenUIState` helper).
2. **Set the slug.** `var SLUG = '<page-slug>';`
3. **Define the page's state shape.** Keep a `STATE` object; add
   `openItem`/`activeId`/etc. as the page needs.
4. **`savePageState()`** — gathers the semantic state and calls
   `GenUIState.save(SLUG, {...})`. Call it on **every** state change
   (open/close/select/switch/search), not just on page unload — state must
   survive refreshes mid-session, not only full closes.
5. **Restore before first render.** In `boot()`, before any render call:
   `var saved = loadPageState()` and apply `saved.activeId` etc. into `STATE`.
   (The host mounts genui while the app may be `display:none`, so the page's
   own restore must happen in its boot path, not in a `DOMContentLoaded` that
   already fired.)
6. **Re-apply the open item after render.** Anything that expands a
   previously-open element (e.g. `restoreOpenItem()`) must run **after** the
   render that creates that element.
7. **Restore scroll after content exists.** `GenUIState.restoreScroll(saved.scroll,
   saved.scrollLeft)` — it retries at 80/200/400/800ms, so one call at the end of
   boot is enough. The host restores scroll too; this is belt-and-braces.
8. **Keep scroll fresh (optional).** Listen for scroll with capture on
   `document` and debounce `savePageState()` (~250ms) so `scroll` is up to date.

## Gotchas

- **Never persist secrets.** Text/password/email inputs are deliberately skipped
  by the host; a page must not store user-entered credential fields in its blob.
- **Restore order matters.** Semantic state → render → open-item expansion →
  scroll. Restoring scroll before the content that gives the page its height
  makes the restore silently no-op (the retry ladder exists for this reason).
- **Don't fight the host.** Do not duplicate generic persistence (details,
  checkboxes, plain scroll) in the page blob — it double-writes the same values.
  The page blob is only for what the host can't see.
- **Key collision safety.** Always include the slug in the key; never use a
  bare global name (two genui pages in one browser share `localStorage`).
- **Migration.** When upgrading a page that used ad-hoc keys (e.g. the home
  page's old `pd-active-project`), migrate once into the standard blob and
  remove the old keys.

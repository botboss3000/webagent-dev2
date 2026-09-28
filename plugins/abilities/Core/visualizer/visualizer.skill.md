# Visualizer — build live Gen UI

Use this ability for a real visual surface in the **Gen UI** workspace: a dashboard, form, report, chart, landing page, or interactive tool. A genui is a complete HTML document mounted inside the app, not browser automation and not a chat-only mockup.

## Choose the right flow

- **Existing genui:** call `get_genui(slug)` before editing. Use `edit_genui` for a targeted change; return a full valid document only when replacing the page deliberately.
- **New or redesigned genui:** follow the execution mode. In Plan, give a concise purpose/layout/data/interactions plan and do not render. In Ask, obtain confirmation for a new build or redesign. In Auto, build the requested base now.
- **Data-only change:** use `get_genui_data` / `set_genui_data`; call `refresh_genui` when the visible page needs a reload.
- **Advanced mechanic:** call `get_genui_reference` for exactly one needed topic. `list` shows the available `contract`, `style`, `data`, `verification`, `credentials`, `embedding`, and `chat` references. Do not load unrelated recipes.

## Non-negotiable build rules

1. Render a complete, working page—not a wireframe, a plan, or a partial fragment.
2. Follow the Gen UI mount contract. All page DOM work is rooted in the supplied root; do not invent global document/window bridges or message APIs. Load the `contract` reference before using a custom lifecycle, media, or chat integration.
3. Consume inherited app design tokens. Do not define a local palette, hardcode status colours, or use emoji as UI icons. Use a single inline line-SVG icon style. Load `style` for the token and accessibility checklist.
4. Keep visible/editable records in the data bag, not hardcoded HTML. Load `data` before creating an interactive, editable, or data-heavy surface.
5. Do not expose secrets. A credential form may only use the documented vault API; load `credentials` before adding one.
6. Build responsive, keyboard-usable UI with clear hierarchy and empty/loading/populated states. Add interaction only when it serves the requested job; do not add a menu of unrequested features in one render.

## Tool sequence

1. `list_genui` when the target is unknown; otherwise `get_genui` for an edit.
2. For an in-chat plan/status/choice card, use the core `present_plan` or `present_chat_component` tools — always available to every agent, not part of this ability.
3. `render_visual` for the initial page; use `edit_genui`, `rename_genui`, or `delete_genui` only for their named lifecycle action.
4. Use `set_genui_data` for records and `get_genui_logs` to diagnose a running page.
5. Use `screenshot_genui(slug, theme="both")` after every build or material edit. Pass a representative `click` selector for each promised interaction.

A build is complete only after screenshots show both themes are legible, interactions reach their promised state, and logs contain no relevant error. State what you verified; do not claim a feature works because it was written.

## Scope boundaries

- Use the Browser ability—not raw iframes or imagined APIs—for authenticated external-site work.
- Use `check_credential` before presenting connection state; it reveals configuration state and allowed fields, never secret values.
- Use `request_credential` only when the user needs to connect an ability. Never place credentials in page data, HTML, logs, or chat text.
- Preserve an existing page's data shape unless the requested change explicitly migrates it.

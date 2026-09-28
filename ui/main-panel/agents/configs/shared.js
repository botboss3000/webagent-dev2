'use strict';

/** Shared lifecycle for one dedicated Agent Config page.
 * Keeping the canonical content node intact preserves its existing listeners,
 * save overlays, and complex embedded renderers while it is displayed alone.
 *
 * This is BEHAVIOUR ONLY. Page metadata (id, title, icon, description, order)
 * is owned by pages/catalog.json — the single source of truth. Emitting
 * id/title here would let the registry's lifecycle merge clobber the catalog
 * entry, so it deliberately does not. */
export function createAgentConfigPage() {
  return Object.freeze({
    mount(host, content) {
      host.replaceChildren(content);
      content.hidden = false;
    },
    restore(anchor, content) {
      anchor.appendChild(content);
      content.hidden = true;
    },
  });
}

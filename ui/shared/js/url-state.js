'use strict';

// URL state — shared owner-aware helpers for bookmarkable app state.
export function routeParam(name) {
  try { return new URL(location.href).searchParams.get(name) || ''; } catch (_) { return ''; }
}

export function replaceRouteParams(values) {
  try {
    const url = new URL(location.href);
    if (Object.prototype.hasOwnProperty.call(values || {}, 'agent') && values.agent) url.searchParams.set('tab', 'agents');
    for (const [name, value] of Object.entries(values || {})) {
      if (value == null || value === '') url.searchParams.delete(name);
      else url.searchParams.set(name, String(value));
    }
    const ordered = new URL(url.href);
    ordered.search = '';
    const priority = ['tab', 'agent', 'agentView', 'session'];
    for (const name of priority) if (url.searchParams.has(name)) ordered.searchParams.set(name, url.searchParams.get(name));
    for (const [name, value] of url.searchParams) if (!priority.includes(name)) ordered.searchParams.append(name, value);
    url.search = ordered.search;
    history.replaceState(history.state, '', url);
  } catch (_) {}
}

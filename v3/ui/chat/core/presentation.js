'use strict';
// Adapted shared reference icon refresh; no v2 auth imports.
export function _refreshLucideIcons(host) {
  if (window.lucide && typeof window.lucide.createIcons === 'function') {
    try {
      const scope = host || document;
      const nodes = Array.from(scope.querySelectorAll('[data-lucide]:not(.lucide)'));
      if (!nodes.length) return;
      window.lucide.createIcons({ nodes });
      // The vendored lucide copies data-lucide onto the svgs it renders (and
      // renders them document-wide regardless of `nodes`). Remove the marker so
      // those svgs never match a future [data-lucide] scan again.
      scope.querySelectorAll('svg[data-lucide].lucide').forEach(function (svg) {
        svg.removeAttribute('data-lucide');
      });
    } catch (_) {}
  }
}


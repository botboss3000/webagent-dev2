import React from 'react';
import { createRoot } from 'react-dom/client';

// The native composer owns all user-visible activity surfaces. The island
// remains mounted for its lazy lifecycle bridge, but must not mirror progress.
function ComposerIsland() {
  return null;
}
export function mountComposerIsland(host, bridge) {
  const root = createRoot(host);
  root.render(<ComposerIsland bridge={bridge} />);
  document.body.dataset.composerReact = 'ready';
  return { destroy: () => root.unmount() };
}

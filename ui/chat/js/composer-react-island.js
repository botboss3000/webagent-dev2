'use strict';

// Composer React bridge - starts after the native idle shell is interactive.
import { composerActions, getComposerSnapshot, initComposerStore, subscribeComposerState } from './composer-store.js';
let attempted = false;
export function mountReactComposerIsland() {
  if (attempted) return;
  attempted = true;
  initComposerStore();
  const load = () => import('/ui/vendor/react-composer/react-composer.js?v=1').then(({ mountComposerIsland }) => {
    const host = document.getElementById('chat-composer-react-island');
    if (host && typeof mountComposerIsland === 'function') mountComposerIsland(host, { subscribe: subscribeComposerState, getSnapshot: getComposerSnapshot, actions: composerActions });
  }).catch(() => { document.body.dataset.composerReact = 'fallback'; });
  if ('requestIdleCallback' in window) requestIdleCallback(load, { timeout: 1800 }); else setTimeout(load, 300);
}

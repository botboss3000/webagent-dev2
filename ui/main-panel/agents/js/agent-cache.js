'use strict';

// Tenant-scoped IndexedDB cache for the Agents surface. Live APIs remain the
// authority; these rows provide useful first paint while network/database work
// is in flight and are purged automatically on logout or tenant changes.
import { kvCache } from '../../../chat/js/storage/kv-cache.js';
import { getBrowserStorageContext } from '../../../shared/js/browser-storage-policy.js';

const PREFIX = 'agents:v2:';

export function readAgentCache(name) {
  return kvCache.get(PREFIX + name);
}

export function writeAgentCache(name, value, ttlMs) {
  return kvCache.set(PREFIX + name, value, { ttlMs });
}

export function deleteAgentCache(name) {
  return kvCache.del(PREFIX + name);
}

export function ensureAgentCacheHydrated() {
  const scope = () => getBrowserStorageContext().owner_scope;
  if (scope()) return kvCache.ensureHydrated();
  if (typeof window === 'undefined') return kvCache.ensureHydrated();

  // Agents can start before storage-adapter receives the tenant cache scope.
  // Calling kvCache.hydrate() in that window completes as an empty/no-scope
  // hydration; without this hand-off the later IndexedDB rows are never offered
  // to the page and the sessions API wins every warm load. Wait for the scoped
  // hydration event, with a short fallback for disabled/memory-only policies.
  return new Promise((resolve) => {
    let settled = false;
    const finish = () => {
      if (settled) return;
      settled = true;
      window.removeEventListener('webagent-kv-cache-hydrated', onHydrated);
      resolve();
    };
    const onHydrated = (event) => {
      if (event?.detail?.owner_scope) finish();
    };
    window.addEventListener('webagent-kv-cache-hydrated', onHydrated);
    // Close the check/listen race if storage configured between the first
    // scope() call and addEventListener().
    if (scope()) kvCache.ensureHydrated().then(finish, finish);
    // Storage routing is intentionally off the shell's critical path and can
    // finish several seconds after a large module graph. This wait never blocks
    // the live fetch; it only keeps the parallel cache-paint branch alive long
    // enough to receive the real tenant scope.
    setTimeout(finish, 15000);
  });
}

export function invalidateAgentListCaches() {
  return Promise.all([
    'list:main', 'list:system', 'list:bin', 'list:clones',
  ].map(deleteAgentCache));
}

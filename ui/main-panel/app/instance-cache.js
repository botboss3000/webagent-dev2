'use strict';

// Tenant-scoped, IndexedDB-backed stale-while-revalidate cache for the
// Instances surface.  The live endpoints remain authoritative; these rows only
// give the grid, dashboard and settings chrome useful data while those requests
// are in flight. kvCache automatically obeys the browser-storage policy and is
// purged on logout/tenant changes.
import { kvCache } from '../../chat/js/storage/kv-cache.js';

const PREFIX = 'instances:v2:';

export function readInstanceCache(name) {
  return kvCache.get(PREFIX + name);
}

export function writeInstanceCache(name, value, ttlMs) {
  return kvCache.set(PREFIX + name, value, { ttlMs });
}

export function deleteInstanceCache(name) {
  return kvCache.del(PREFIX + name);
}

export function ensureInstanceCacheHydrated() {
  return kvCache.ensureHydrated();
}


'use strict';

/**
 * media-cache.js — RAM (browser-memory) speed layer for attachments & Gen UI.
 *
 * A bounded, backend-agnostic LRU that holds recently-used file bytes as Blobs
 * plus a minted object URL (and downscaled image thumbnails), so re-rendering
 * the same image/audio/preview — chat scroll-back, reopening the viewer,
 * revisiting a Gen UI page — is instant and skips a refetch, no matter WHERE the
 * bytes actually live (server disk, app database, cloud bucket, or the device's
 * own IndexedDB in ui/shared/js/attachments-idb.js).
 *
 * This is a CACHE, never a place of record. It is:
 *   - volatile — everything is dropped on reload / clear() (logout, session reset);
 *   - bounded — a byte budget (default 128 MB) it can never exceed;
 *   - self-evicting — least-recently-used entries are dropped, and their object
 *     URLs revoked, the moment a put() would push it over budget (so no leaks).
 *
 * Callers fetch-on-miss and put() the bytes; getUrl()/fetchUrl() serve hits.
 *
 * Admin control: the RAM-cache On/Off + budget knob (Data Settings, delivered via
 * /api/v1/auth/ui-config) calls configure(); when disabled, fetchUrl() passes the
 * source URL straight through and put() is a no-op, so the app behaves exactly as
 * it did before this layer existed.
 *
 * Breadcrumbs: consumed by ui/shared/js/attachments.js (thumbnails + viewer) and
 * the Gen UI preview path; boot config applied by ui/shared/js/appearance.js.
 */

import {authHeaders} from './left-login.js';

// ── Tunables (overridable via configure()) ──────────────────────────────────

const DEFAULT_BUDGET_BYTES = 128 * 1024 * 1024; // 128 MB
const THUMB_MAX_EDGE = 320;                      // px — longest side of a generated thumbnail

let _enabled = true;
let _budgetBytes = DEFAULT_BUDGET_BYTES;

// ── Store ───────────────────────────────────────────────────────────────────
// One Map keyed by a caller-chosen id (attachment_id / genui slug + variant).
// Map preserves insertion order, so it doubles as the LRU list: a hit re-inserts
// the entry at the tail (most-recent), eviction takes from the head (oldest).
// Each entry: { blob, url, bytes }. `url` is an object URL minted from `blob`.

const _store = new Map();
let _totalBytes = 0;

function _revoke(url) {
  if (url) { try { URL.revokeObjectURL(url); } catch { /* already gone */ } }
}

function _drop(key) {
  const e = _store.get(key);
  if (!e) return;
  _store.delete(key);
  _totalBytes -= e.bytes;
  _revoke(e.url);
}

// Evict least-recently-used entries until we're within budget (leaving room for
// `incoming` bytes about to be added). A single entry larger than the whole
// budget is simply not cached (handled by the caller checking the return).
function _evictToFit(incoming) {
  if (incoming > _budgetBytes) return false;
  for (const key of _store.keys()) {
    if (_totalBytes + incoming <= _budgetBytes) break;
    _drop(key);
  }
  return _totalBytes + incoming <= _budgetBytes;
}

function _touch(key) {
  // Move to the tail (most-recently-used) by re-inserting.
  const e = _store.get(key);
  if (!e) return null;
  _store.delete(key);
  _store.set(key, e);
  return e;
}

// ── Public API ──────────────────────────────────────────────────────────────

/** Apply admin/boot config. Missing fields are left unchanged.
 *  Shrinking the budget evicts immediately; disabling clears everything. */
export function configure({ enabled, budgetBytes } = {}) {
  if (typeof enabled === 'boolean') _enabled = enabled;
  if (Number.isFinite(budgetBytes) && budgetBytes >= 0) _budgetBytes = budgetBytes;
  if (!_enabled) { clear(); return; }
  _evictToFit(0); // honour a reduced budget right away
}

export function isEnabled() { return _enabled; }

/** True if a live entry exists for this key (does not affect LRU order). */
export function has(key) { return _enabled && _store.has(key); }

/** Object URL for a cached entry, or null on miss. Bumps LRU recency. */
export function getUrl(key) {
  if (!_enabled) return null;
  const e = _touch(key);
  return e ? e.url : null;
}

/** Blob for a cached entry, or null on miss. Bumps LRU recency. */
export function getBlob(key) {
  if (!_enabled) return null;
  const e = _touch(key);
  return e ? e.blob : null;
}

/** Cache a Blob under key and return a stable object URL for it (or a
 *  freshly-minted, un-cached URL if the cache is off or the blob won't fit). */
export function put(key, blob) {
  if (!blob) return '';
  if (!_enabled) return URL.createObjectURL(blob); // caller owns revocation
  if (_store.has(key)) _drop(key);                 // replace → revoke old URL first
  const bytes = blob.size || 0;
  if (!_evictToFit(bytes)) return URL.createObjectURL(blob); // too big to cache
  const url = URL.createObjectURL(blob);
  _store.set(key, { blob, url, bytes });
  _totalBytes += bytes;
  return url;
}

function _fetchSource(srcUrl) {
  // Workspace content grants still require the caller's identity. Keep bearer
  // headers off arbitrary media URLs, including external and blob sources.
  return fetch(srcUrl, srcUrl.startsWith('/api/v1/content/')
    ? {headers:authHeaders()} : undefined);
}

/** The common path: return the cached object URL for `key`, else fetch `srcUrl`,
 *  cache the bytes, and return an object URL. On any fetch failure (or when the
 *  cache is disabled) resolves to `srcUrl` unchanged, so rendering still works.
 *  @returns {Promise<string>} */
export async function fetchUrl(key, srcUrl) {
  if (!srcUrl) return '';
  const hit = getUrl(key);
  if (hit) return hit;
  try {
    const res = await _fetchSource(srcUrl);
    if (!res.ok) return '';
    const blob = await res.blob();
    return put(key, blob) || '';
  } catch {
    return '';
  }
}

/** Downscaled image thumbnail (longest edge THUMB_MAX_EDGE), cached under
 *  `${key}::thumb`. Falls back to the full-size URL when generation isn't
 *  possible (non-image, no canvas, decode error, or cache disabled). */
export async function thumbUrl(key, srcUrl, mimeType) {
  if (!srcUrl) return '';
  if (!(mimeType || '').startsWith('image/')) return srcUrl;
  const tkey = key + '::thumb';
  const hit = getUrl(tkey);
  if (hit) return hit;
  try {
    // Prefer the already-cached full blob; else fetch it once.
    let blob = getBlob(key);
    let fullUrl = getUrl(key);
    if (!blob) {
      const res = await _fetchSource(srcUrl);
      if (!res.ok) return srcUrl;
      blob = await res.blob();
      fullUrl = put(key, blob); // also serve small images without a second request
    }
    const thumb = await _downscale(blob);
    if (!thumb) return fullUrl || srcUrl;
    return put(tkey, thumb) || srcUrl;
  } catch {
    return srcUrl;
  }
}

async function _downscale(blob) {
  // createImageBitmap is the fast path; guard for browsers/types without it.
  if (typeof createImageBitmap !== 'function' || typeof document === 'undefined') return null;
  let bmp;
  try { bmp = await createImageBitmap(blob); } catch { return null; }
  const { width, height } = bmp;
  const longest = Math.max(width, height);
  if (longest <= THUMB_MAX_EDGE) { bmp.close && bmp.close(); return null; } // already small
  const scale = THUMB_MAX_EDGE / longest;
  const w = Math.round(width * scale);
  const h = Math.round(height * scale);
  const canvas = document.createElement('canvas');
  canvas.width = w; canvas.height = h;
  const ctx = canvas.getContext('2d');
  if (!ctx) { bmp.close && bmp.close(); return null; }
  ctx.drawImage(bmp, 0, 0, w, h);
  bmp.close && bmp.close();
  return await new Promise((resolve) => canvas.toBlob(resolve, 'image/webp', 0.82));
}

/** Forget one key (and its generated thumbnail), revoking any object URLs. */
export function remove(key) {
  _drop(key);
  _drop(key + '::thumb');
}

/** Drop everything and revoke every object URL. Call on logout / session reset. */
export function clear() {
  for (const e of _store.values()) _revoke(e.url);
  _store.clear();
  _totalBytes = 0;
}

/** Snapshot for diagnostics / the admin control's live read-out. */
export function stats() {
  return {
    enabled: _enabled,
    entries: _store.size,
    bytes: _totalBytes,
    budgetBytes: _budgetBytes,
  };
}

// Expose on window so non-module callers (and the admin control's live preview)
// can reach it without an import, mirroring window.WA_APPEARANCE / WA_BG.
try { window.WA_MEDIA_CACHE = { configure, clear, stats, isEnabled }; } catch { /* no window */ }

// Self-configure from the app-wide config appearance.js publishes at boot. This
// module may load before OR after appearance.js's first apply(), so we read the
// global it stashes here; appearance.js also calls configure() directly when it
// finds WA_MEDIA_CACHE already present. Either order converges on the same config.
try { if (window.__waMediaCacheCfg) configure(window.__waMediaCacheCfg); } catch { /* no window */ }

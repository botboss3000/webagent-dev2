'use strict';

import { getActive, removeAccount } from './accounts.js';

const CACHE_SCOPE_KEY = 'webagent_device_cache_scopes';
const DB_NAME_PREFIX = 'webagent_session_db';
const ATTACHMENT_DB_NAME_PREFIX = 'webagent_attachments';
const LEGACY_ATTACHMENT_DB = 'webagent-attachments';

function _decodeTokenContext(token) {
  try {
    const encoded = String(token || '').split('.')[1];
    if (!encoded) return null;
    const padded = encoded.replace(/-/g, '+').replace(/_/g, '/')
      .padEnd(Math.ceil(encoded.length / 4) * 4, '=');
    const payload = JSON.parse(atob(padded));
    const userId = String(payload.user_id || payload.sub || '');
    const revision = Number(payload.rev);
    if (!userId || !Number.isSafeInteger(revision) || revision < 0) return null;

    const active = getActive();
    const localUserId = String(
      active?.user_id || localStorage.getItem('auth_user_id') || '',
    );
    if (localUserId && localUserId !== userId) return null;
    return { userId, revision, deviceId: String(payload.device_id || '') };
  } catch (_) {
    return null;
  }
}

async function _scopeForToken(token) {
  const context = _decodeTokenContext(token);
  if (!context || !globalThis.crypto?.subtle) return '';
  const input = new TextEncoder().encode(
    `webagent-browser-cache:${context.userId}:${context.revision}`,
  );
  const bytes = new Uint8Array(await crypto.subtle.digest('SHA-256', input));
  const scope = Array.from(bytes, (byte) => byte.toString(16).padStart(2, '0'))
    .join('')
    .slice(0, 24);

  // Keep the last resolved scope tied to its token context. This gives a
  // reload/revocation handshake a durable reference without trusting a bare
  // localStorage database name.
  try {
    const raw = JSON.parse(localStorage.getItem(CACHE_SCOPE_KEY) || '{}');
    const scopes = raw && typeof raw === 'object' ? raw : {};
    scopes[`${context.userId}:${context.revision}`] = scope;
    localStorage.setItem(CACHE_SCOPE_KEY, JSON.stringify(scopes));
  } catch (_) {}
  return scope;
}

async function _purgeTenantDatabase(token) {
  try {
    const [{ purgeBrowserData }, { syncEngine }] = await Promise.all([
      import('./browser-lifecycle.js'),
      import('../../chat/js/storage/sync.js'),
    ]);
    syncEngine.stopAutoSync();
    const scope = await _scopeForToken(token);
    if (!scope) {
      return {
        complete: false,
        expected_databases: [],
        completed_databases: [],
        failures: [{ target: 'owner_scope', error: 'Could not derive tenant scope' }],
      };
    }
    if (typeof indexedDB.databases === 'function') {
      const names = new Set(
        (await indexedDB.databases()).map(database => database?.name).filter(Boolean),
      );
      const hasTenantData = names.has(`${DB_NAME_PREFIX}_${scope}`)
        || names.has(`${ATTACHMENT_DB_NAME_PREFIX}_${scope}`)
        || names.has(LEGACY_ATTACHMENT_DB);
      if (!hasTenantData) {
        const result = {
          // No account-owned database exists, so there is nothing left to purge.
          complete: true,
          expected_databases: [],
          completed_databases: [],
          failures: [],
        };
        // Still tell in-memory caches to drop any rows they may hold — the
        // purge decision is final even when there was nothing to delete.
        window.dispatchEvent(new CustomEvent('webagent-browser-storage-purge', {
          detail: { reason: 'device-revocation', ...result },
        }));
        return result;
      }
    }
    return purgeBrowserData(scope);
  } catch (error) {
    console.warn('[DevicePurge] Browser storage purge failed:', error);
    return {
      complete: false,
      expected_databases: [],
      completed_databases: [],
      failures: [{ target: 'lifecycle', error: String(error?.message || error) }],
    };
  }
}

async function _ack(token, purge) {
  if (!token) return false;
  try {
    const response = await fetch('/api/v1/auth/device/purge-ack', {
      method: 'POST',
      headers: {
        Authorization: `Bearer ${token}`,
        'Content-Type': 'application/json',
      },
      body: JSON.stringify({
        policy_epoch: purge.context?.policy_epoch || 0,
        schema_version: purge.context?.schema_version || 0,
        owner_scope: purge.context?.owner_scope || '',
        expected_databases: purge.expected_databases,
        completed_databases: purge.completed_databases,
      }),
    });
    return response.ok;
  } catch (_) {
    return false;
  }
}

export async function purgeAndAcknowledge(
  token,
  { reload = false, forgetAccount = true } = {},
) {
  const purge = await _purgeTenantDatabase(token);
  const acknowledged = purge.complete ? await _ack(token, purge) : false;
  if (purge.complete && acknowledged && forgetAccount) {
    const active = getActive();
    if (active) removeAccount(active.user_id);
    localStorage.removeItem('auth_token');
    localStorage.removeItem('remember_token');
    localStorage.removeItem('anonUserId');
    localStorage.removeItem('terminalUserId');
  }
  if (reload && purge.complete && acknowledged) window.location.reload();
  // purgeBrowserData dispatches the purge event on every path (success and
  // failure), so no caller-specific dispatch is needed here.
  return purge.complete && acknowledged;
}

export async function checkDevicePurge({ reload = true } = {}) {
  const active = getActive();
  const token = active?.access_token || localStorage.getItem('auth_token') || '';
  if (!token) return false;
  try {
    const response = await fetch('/api/v1/auth/device/purge-status', {
      method: 'POST',
      headers: { Authorization: `Bearer ${token}` },
      cache: 'no-store',
    });
    if (!response.ok) return false;
    const state = await response.json();
    if (!state.purge_required) return false;
    return purgeAndAcknowledge(token, { reload });
  } catch (_) {
    return false;
  }
}

export async function handleRemoteDeviceRevocation(event) {
  const active = getActive();
  const token = active?.access_token || localStorage.getItem('auth_token') || '';
  const context = _decodeTokenContext(token);
  const targets = Array.isArray(event?.device_ids) ? event.device_ids.map(String) : [];
  if (!context?.deviceId || !targets.includes(context.deviceId)) return false;
  return checkDevicePurge({ reload: true });
}

let _pollTimer = null;

export function startDevicePurgePolling() {
  checkDevicePurge();
  if (_pollTimer !== null) return;
  _pollTimer = window.setInterval(() => checkDevicePurge(), 15000);
}

import { getActive } from './accounts.js';
import { mountModelTable } from './model-table.js?v=15';
import { reconcileSavedModelRows } from './model-roster-reconcile.js';

function authHeaders() {
  const active = getActive();
  return active?.access_token ? { Authorization: `Bearer ${active.access_token}` } : {};
}

function scopedRequest(path, options = {}) {
  const joiner = path.includes('?') ? '&' : '?';
  const url = `${path}${joiner}scope=user`;
  return fetch(url, { ...options, headers: { ...authHeaders(), ...(options.headers || {}) } });
}

const ENTRY_FIELDS = [
  'entry_id', 'provider', 'auth_mode', 'base_url', 'api_key', 'model', 'display_name', 'enabled',
  'text_capable', 'image_capable', 'use_for_image', 'image_out_capable',
  'use_for_image_out', 'voice_capable', 'use_for_voice', 'use_for_system',
  'high_effort_capable', 'credential_scope',
];

export async function mountMyModels(slot) {
  if (!slot) return;
  let capabilities;
  try {
    const response = await fetch('/api/v1/entitlements/me', { headers: authHeaders() });
    capabilities = response.ok ? await response.json() : null;
  } catch (_) {}
  if (!(capabilities?.models?.allow_byo)) {
    slot.remove();
    return;
  }

  slot.innerHTML = `
    <h3 class="account-section-title">My model account</h3>
    <p class="account-pref-desc">Connect your own ChatGPT subscription and choose the models used by your personal BYO roster. These credentials are private to your account.</p>
    <div id="account-model-host"></div>
  `;
  const host = slot.querySelector('#account-model-host');
  const adapter = {
    loadConfig: async () => {
      const response = await fetch('/api/v1/entitlements/me/byo-models', { headers: authHeaders() });
      if (!response.ok) throw new Error(`HTTP ${response.status}`);
      const data = await response.json();
      return {
        provider: 'openai_subscription', auth_mode: 'chatgpt',
        base_url: 'codex://chatgpt', api_key: '', model: '', providerConfigs: {},
        roster: data.entries || [], inherited: [],
      };
    },
    saveRoster: async ({ providers }) => {
      const entries = providers.map((provider) => Object.fromEntries(
        ENTRY_FIELDS.filter((key) => provider[key] !== undefined).map((key) => [key, provider[key]])
      ));
      const standard = entries.find((entry) => entry.enabled !== false && entry.text_capable !== false);
      const response = await fetch('/api/v1/entitlements/me/byo-models', {
        method: 'PUT',
        headers: { 'Content-Type': 'application/json', ...authHeaders() },
        body: JSON.stringify({ entries, default_entry_id: standard?.entry_id || '' }),
      });
      const data = await response.json().catch(() => ({}));
      if (!response.ok) {
        throw new Error(data?.detail?.message || data?.detail || `HTTP ${response.status}`);
      }
      reconcileSavedModelRows(providers, data.entries || []);
      return data;
    },
    loadPresets: async () => ({
      openai_subscription: {
        name: 'OpenAI Subscription', base_url: 'codex://chatgpt', auth_mode: 'chatgpt',
      },
    }),
    subscriptionRequest: scopedRequest,
  };

  mountModelTable(host, {
    adapter,
    subscriptionScope: 'user',
    fetchFn: (url, options = {}) => fetch(url, {
      ...options, headers: { ...authHeaders(), ...(options.headers || {}) },
    }),
  });
}

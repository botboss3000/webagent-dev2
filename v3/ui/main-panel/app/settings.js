'use strict';

export function mountAppSettings(host) {
  host.innerHTML = '<h2>App Settings</h2><section class="members-policy"><h3 class="members-policy-title">Troubleshooting</h3><div class="ac-list"><div class="ac-ability-row"><span class="ac-ability-label"><span class="ac-ability-name">Encrypt admin account records</span><span class="ac-ability-desc">Turn off to save new or rewritten records for the fixed admin account as readable, authenticated JSON for troubleshooting. Existing records are not converted. Other accounts, app and agent data, and credentials remain encrypted.</span></span><button class="conn-toggle-wrap ac-ability-toggle-wrap app-settings-switch" type="button" role="switch" aria-checked="false" aria-label="Encrypt admin account records"><span class="conn-toggle-track" aria-hidden="true"></span></button></div></div><p class="app-settings-note" role="status"></p></section>';
  const toggle = host.querySelector('[role=switch]');
  const note = host.querySelector('[role=status]');
  let digest = '';
  let enabled = true;
  let generation = 0;
  async function request(method='GET', body) {
    const meResponse = await fetch('/api/v3/auth/me', {credentials:'same-origin',cache:'no-store'});
    if (!meResponse.ok) throw Error('Sign in required');
    const me = await meResponse.json();
    if (!me.is_admin) throw Error('Administrator access required');
    const response = await fetch('/api/v3/admin/app-settings/encryption', {
      method, credentials:'same-origin',cache:'no-store',
      headers:{'Content-Type':'application/json',...(method==='GET'?{}:{'x-csrf-token':me.csrf_token})},
      ...(body===undefined?{}:{body:JSON.stringify(body)})
    });
    const data = await response.json().catch(()=>({}));
    if (!response.ok) throw Error(data.error||data.message||'Request failed ('+response.status+')');
    return data;
  }
  toggle.disabled = true;
  note.textContent = 'Loading encryption setting…';
  request().then(data=>{
    if (!host.isConnected) return;
    enabled = data.enabled; digest = data.digest;
    toggle.setAttribute('aria-checked', String(enabled)); toggle.disabled = false;
    note.textContent = enabled ? 'Admin account records are encrypted.' : 'Troubleshooting mode is on. New admin account records may contain readable private data.';
  }).catch(error=>{if(host.isConnected)note.textContent=error.message;});
  toggle.onclick = async () => {
    const thisGeneration = ++generation;
    const next = !enabled;
    toggle.disabled = true;
    toggle.setAttribute('aria-busy', 'true');
    note.textContent = 'Saving…';
    try {
      const data = await request('PUT',{enabled:next,expected_digest:digest});
      if (!host.isConnected || thisGeneration!==generation) return;
      if (data.enabled !== next || typeof data.digest !== 'string' || !/^[a-f0-9]{64}$/i.test(data.digest)) throw Error('Save was not confirmed. Please try again.');
      enabled = data.enabled; digest = data.digest;
      toggle.setAttribute('aria-checked', String(enabled));
      note.textContent = enabled ? 'Encryption enabled for subsequent admin account writes.' : 'Troubleshooting mode is on. Subsequent admin account writes are readable, authenticated JSON.';
    } catch(error) {
      if (!host.isConnected || thisGeneration!==generation) return;
      note.textContent = error.message;
    } finally {
      if (host.isConnected && thisGeneration===generation) {
        toggle.removeAttribute('aria-busy');
        toggle.disabled = false;
      }
    }
  };
}

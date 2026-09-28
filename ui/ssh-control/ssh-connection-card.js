'use strict';

// Secure two-stage SSH connection card. Credentials are held only in this live
// browser DOM and POSTed directly to the ability router after the user confirms
// the server's host-key fingerprint. They never pass through an agent tool call.

import { apiPath } from '../shared/js/config.js';
import { authHeaders } from '../shared/js/left-login.js';
import { app } from '../shared/js/state.js';

const _open = new Map();
let _styles = false;

function _injectStyles() {
  if (_styles) return;
  _styles = true;
  const style = document.createElement('style');
  style.id = 'sshcc-styles';
  style.textContent = `
.sshcc-overlay{position:fixed;inset:0;z-index:5000;display:flex;align-items:center;justify-content:center;
  padding:18px;background:rgba(var(--bg-0-rgb),.66);backdrop-filter:blur(3px)}
.sshcc-card{width:min(620px,100%);max-height:calc(100vh - 36px);overflow:auto;background:var(--bg-elev);
  color:var(--fg-1);border:var(--border-width) solid var(--border);border-radius:calc(16px * var(--radius-scale));
  box-shadow:var(--shadow-modal);padding:20px}
.sshcc-head{display:flex;gap:10px;align-items:center;margin-bottom:5px}.sshcc-icon{display:flex;width:34px;height:34px;
  align-items:center;justify-content:center;border-radius:10px;background:var(--accent-soft);color:var(--accent)}
.sshcc-title{font-size:16px;font-weight:650}.sshcc-sub{font-size:12px;color:var(--fg-3);line-height:1.45;margin:6px 0 16px}
.sshcc-grid{display:grid;grid-template-columns:1fr 120px;gap:12px}.sshcc-field{margin-bottom:12px}.sshcc-wide{grid-column:1/-1}
.sshcc-label{display:block;font-size:12px;color:var(--fg-2);margin-bottom:5px}.sshcc-input{box-sizing:border-box;width:100%;
  padding:9px 11px;background:var(--bg-1);color:var(--fg-1);border:var(--border-width) solid var(--border);
  border-radius:9px;outline:none;font:inherit;font-size:13px}.sshcc-input:focus{border-color:var(--accent)}
textarea.sshcc-input{min-height:115px;resize:vertical;font-family:var(--font-mono,monospace);font-size:12px}
.sshcc-auth-key{display:none}.sshcc-card[data-auth="private_key"] .sshcc-auth-key{display:block}
.sshcc-card[data-auth="private_key"] .sshcc-auth-password{display:none}
.sshcc-trust{display:none;margin:4px 0 14px;padding:12px;border:var(--border-width) solid var(--warning,#e0af68);
  border-radius:10px;background:color-mix(in srgb,var(--warning,#e0af68) 10%,transparent)}
.sshcc-trust.show{display:block}.sshcc-fp{font-family:var(--font-mono,monospace);font-size:12px;overflow-wrap:anywhere;
  color:var(--fg-1);margin-top:6px}.sshcc-warn{font-size:12px;color:var(--fg-2);line-height:1.45;margin-top:7px}
.sshcc-msg{min-height:16px;font-size:12px;margin-top:8px}.sshcc-msg.err{color:var(--danger)}.sshcc-msg.ok{color:var(--success)}
.sshcc-actions{display:flex;justify-content:flex-end;gap:8px;margin-top:14px}.sshcc-btn{font-size:13px;font-weight:550;
  padding:8px 14px;border-radius:9px;border:var(--border-width) solid var(--border);background:transparent;color:var(--fg-2);cursor:pointer}
.sshcc-btn.primary{background:var(--accent);border-color:var(--accent);color:var(--bg-0)}.sshcc-btn[disabled]{opacity:.55;cursor:default}
@media(max-width:580px){.sshcc-grid{grid-template-columns:1fr}.sshcc-wide{grid-column:auto}}
`;
  document.head.appendChild(style);
}

function _icons(root) {
  try { window.lucide?.createIcons?.({ root }); } catch (_) {}
}

function _value(card, name) {
  return card.querySelector(`[name="${name}"]`)?.value || '';
}

function _wipeSecrets(card) {
  for (const name of ['password', 'private_key', 'key_passphrase', 'sudo_password']) {
    const input = card.querySelector(`[name="${name}"]`);
    if (input) input.value = '';
  }
}

async function _resumeOriginatingChat(payload, connectionName) {
  const originSessionId = String(payload.session_id || '');
  if (originSessionId && originSessionId !== String(app.currentSessionId || '')) return false;
  if (typeof app.sendChatMessage !== 'function') return false;
  const safeName = String(connectionName || 'SSH device').slice(0, 80);
  await app.sendChatMessage(
    `The SSH connection form was completed successfully. "${safeName}" is verified and ready. Continue with the requested SSH task.`,
  );
  return true;
}

export function renderSshConnectionCard(payload) {
  if (!payload?.agent_id) return;
  _injectStyles();
  const dedupe = payload.connection_id || `${payload.agent_id}:${payload.name || 'new'}`;
  const live = _open.get(dedupe);
  if (live?.isConnected) { live.querySelector('.sshcc-input')?.focus(); return; }

  const existing = payload.connection || {};
  const overlay = document.createElement('div');
  overlay.className = 'sshcc-overlay';
  overlay.innerHTML = `
    <div class="sshcc-card" data-auth="${existing.auth_method || 'password'}" role="dialog" aria-modal="true" aria-label="Secure SSH connection">
      <div class="sshcc-head"><span class="sshcc-icon"><i data-lucide="shield-check"></i></span>
        <span class="sshcc-title">${payload.connection_id ? 'Update SSH connection' : 'Add SSH connection'}</span></div>
      <div class="sshcc-sub"></div>
      <div class="sshcc-grid">
        <label class="sshcc-field sshcc-wide"><span class="sshcc-label">Device name</span><input class="sshcc-input" name="name" maxlength="80"></label>
        <label class="sshcc-field"><span class="sshcc-label">Host or IP address</span><input class="sshcc-input" name="host" autocomplete="off" autocapitalize="off" spellcheck="false"></label>
        <label class="sshcc-field"><span class="sshcc-label">Port</span><input class="sshcc-input" name="port" type="number" min="1" max="65535" value="22"></label>
        <label class="sshcc-field"><span class="sshcc-label">Username</span><input class="sshcc-input" name="username" autocomplete="off" autocapitalize="off" spellcheck="false"></label>
        <label class="sshcc-field"><span class="sshcc-label">Authentication</span><select class="sshcc-input" name="auth_method"><option value="password">Password</option><option value="private_key">Private key</option></select></label>
        <label class="sshcc-field sshcc-wide sshcc-auth-password"><span class="sshcc-label">SSH password${payload.connection_id ? ' (blank keeps saved value)' : ''}</span><input class="sshcc-input" name="password" type="password" autocomplete="new-password"></label>
        <label class="sshcc-field sshcc-wide sshcc-auth-key"><span class="sshcc-label">Private key${payload.connection_id ? ' (blank keeps saved value)' : ''}</span><textarea class="sshcc-input" name="private_key" autocomplete="off" spellcheck="false"></textarea></label>
        <label class="sshcc-field sshcc-auth-key"><span class="sshcc-label">Key passphrase (optional)</span><input class="sshcc-input" name="key_passphrase" type="password" autocomplete="new-password"></label>
        <label class="sshcc-field"><span class="sshcc-label">Sudo password (optional)</span><input class="sshcc-input" name="sudo_password" type="password" autocomplete="new-password"></label>
        ${existing.has_sudo_password ? '<label class="sshcc-field sshcc-wide"><input name="clear_sudo_password" type="checkbox"> Remove the currently saved sudo password</label>' : ''}
      </div>
      <div class="sshcc-trust"><strong>Verify this host key</strong><div class="sshcc-fp"></div>
        <div class="sshcc-warn">Confirm this fingerprint belongs to the intended device. WebAgent will pin it and refuse future connections if it changes.</div></div>
      <div class="sshcc-msg"></div>
      <div class="sshcc-actions"><button type="button" class="sshcc-btn sshcc-cancel">Cancel</button>
        <button type="button" class="sshcc-btn primary sshcc-probe">Check fingerprint</button>
        <button type="button" class="sshcc-btn primary sshcc-save" hidden>Trust &amp; save</button></div>
    </div>`;

  const card = overlay.querySelector('.sshcc-card');
  card.querySelector('.sshcc-sub').textContent = payload.message
    || 'Use this secure panel to inspect the host-key fingerprint and finish saving. Credentials go straight to the encrypted vault and are never shown to the agent.';
  const fields = {
    name: payload.name || existing.name || 'SSH device',
    host: existing.host || payload.host || payload.name || '',
    port: existing.port || 22, username: existing.username || '',
    auth_method: existing.auth_method || 'password',
  };
  for (const [name, value] of Object.entries(fields)) card.querySelector(`[name="${name}"]`).value = value;
  card.dataset.auth = fields.auth_method;

  const msg = card.querySelector('.sshcc-msg');
  const trust = card.querySelector('.sshcc-trust');
  const fp = card.querySelector('.sshcc-fp');
  const probeBtn = card.querySelector('.sshcc-probe');
  const saveBtn = card.querySelector('.sshcc-save');
  const cancelBtn = card.querySelector('.sshcc-cancel');
  let trustedFingerprint = '';

  const close = () => {
    _wipeSecrets(card);
    document.removeEventListener('keydown', onKey);
    overlay.remove();
    _open.delete(dedupe);
  };
  const onKey = (event) => { if (event.key === 'Escape') close(); };
  const resetTrust = () => {
    trustedFingerprint = '';
    trust.classList.remove('show');
    saveBtn.hidden = true;
    probeBtn.hidden = false;
  };
  const setBusy = (busy) => {
    for (const button of [probeBtn, saveBtn, cancelBtn]) button.disabled = busy;
  };
  const error = (text) => { msg.className = 'sshcc-msg err'; msg.textContent = text; };

  card.querySelector('[name="auth_method"]').addEventListener('change', (event) => {
    card.dataset.auth = event.target.value;
  });
  for (const name of ['host', 'port']) card.querySelector(`[name="${name}"]`).addEventListener('input', resetTrust);
  cancelBtn.addEventListener('click', close);
  overlay.addEventListener('mousedown', (event) => { if (event.target === overlay) close(); });
  document.addEventListener('keydown', onKey);

  probeBtn.addEventListener('click', async () => {
    const host = _value(card, 'host').trim();
    const port = Number(_value(card, 'port') || 22);
    if (!host || !Number.isInteger(port) || port < 1 || port > 65535) {
      error('Enter a valid host and port first.'); return;
    }
    setBusy(true); msg.className = 'sshcc-msg'; msg.textContent = 'Contacting the SSH server…';
    try {
      const response = await fetch(apiPath('/api/v1/ssh-control/probe'), {
        method: 'POST', headers: { 'Content-Type': 'application/json', ...authHeaders() },
        body: JSON.stringify({ agent_id: payload.agent_id, host, port }),
      });
      const body = await response.json().catch(() => ({}));
      if (!response.ok) throw new Error(body.detail || `HTTP ${response.status}`);
      trustedFingerprint = body.fingerprint;
      fp.textContent = `${body.key_type || 'SSH key'}  ${body.fingerprint}`;
      trust.classList.add('show');
      probeBtn.hidden = true; saveBtn.hidden = false;
      msg.className = 'sshcc-msg'; msg.textContent = '';
    } catch (err) { error(`Could not inspect the host: ${err?.message || err}`); }
    finally { setBusy(false); }
  });

  saveBtn.addEventListener('click', async () => {
    const body = {
      agent_id: payload.agent_id, connection_id: payload.connection_id || null,
      name: _value(card, 'name').trim(), host: _value(card, 'host').trim(),
      port: Number(_value(card, 'port') || 22), username: _value(card, 'username').trim(),
      auth_method: _value(card, 'auth_method'), password: _value(card, 'password'),
      private_key: _value(card, 'private_key'), key_passphrase: _value(card, 'key_passphrase'),
      sudo_password: _value(card, 'sudo_password'), expected_fingerprint: trustedFingerprint,
      clear_sudo_password: Boolean(card.querySelector('[name="clear_sudo_password"]')?.checked),
    };
    if (!body.name || !body.host || !body.username || !body.expected_fingerprint) {
      error('Complete the name, host, username, and fingerprint check first.'); return;
    }
    if (!payload.connection_id && body.auth_method === 'password' && !body.password) {
      error('Enter the SSH password.'); return;
    }
    if (!payload.connection_id && body.auth_method === 'private_key' && !body.private_key.trim()) {
      error('Paste the SSH private key.'); return;
    }
    setBusy(true); msg.className = 'sshcc-msg'; msg.textContent = 'Verifying fingerprint and login…';
    try {
      const response = await fetch(apiPath('/api/v1/ssh-control/connections'), {
        method: 'POST', headers: { 'Content-Type': 'application/json', ...authHeaders() },
        body: JSON.stringify(body),
      });
      const data = await response.json().catch(() => ({}));
      if (!response.ok) throw new Error(data.detail || `HTTP ${response.status}`);
      _wipeSecrets(card);
      card.querySelector('.sshcc-grid').remove(); trust.remove();
      probeBtn.remove(); saveBtn.remove(); cancelBtn.textContent = 'Done'; cancelBtn.disabled = false;
      msg.className = 'sshcc-msg ok';
      const connectionName = data.connection?.name || body.name;
      let resumed = false;
      try { resumed = await _resumeOriginatingChat(payload, connectionName); } catch (_) {}
      msg.textContent = resumed
        ? `${connectionName} is verified and saved. The agent has been notified and will continue automatically.`
        : `${connectionName} is verified and saved. Return to chat and tell the agent to continue.`;
    } catch (err) { error(`Could not save the connection: ${err?.message || err}`); setBusy(false); }
  });

  document.body.appendChild(overlay);
  _open.set(dedupe, overlay);
  _icons(overlay);
  card.querySelector('[name="host"]')?.focus();
}

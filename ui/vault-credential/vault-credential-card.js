'use strict';

// ── Vault credential card ───────────────────────────────────────────────────
// The secure entry card the agent's `request_credential` tool surfaces (Visualizer
// ability). When that tool result streams in, chat-activity.js calls
// renderVaultCredentialCard(payload) and this pops a themed modal asking the user
// for the secret.
//
// THE GUARANTEE: the value the user types here goes STRAIGHT to the encrypted vault
// (POST /api/v1/genui/vault/keys/{key_id}) — it never returns to the agent, the
// chat transcript, or any log. The agent only ever holds the key id; the dashboard
// it builds uses the secret by id via the server-side proxy (api.callWithKey).
//
// Self-contained: injects its own <style> once (design-system tokens only, so it's
// correct in dark + light), owns its overlay lifecycle, and re-focuses an existing
// card rather than stacking duplicates for the same key id.
//
// KEEP: the only writer of the secret is this card's POST — do not route the value
// through any agent-facing or transcript-facing path.

import { apiPath } from '../shared/js/config.js';

const _open = new Map();   // key_id → overlay element (dedupe live cards)
let _stylesInjected = false;

function _injectStyles() {
  if (_stylesInjected) return;
  _stylesInjected = true;
  const css = `
.vcc-overlay{position:fixed;inset:0;z-index:5000;display:flex;align-items:center;justify-content:center;
  background:rgba(var(--bg-0-rgb),0.62);backdrop-filter:blur(3px);-webkit-backdrop-filter:blur(3px);
  animation:vcc-fade .14s ease both;}
@keyframes vcc-fade{from{opacity:0}to{opacity:1}}
.vcc-card{width:min(420px,calc(100vw - 32px));background:var(--bg-elev);
  border:var(--border-width) solid var(--border);border-radius:calc(16px * var(--radius-scale));
  box-shadow:var(--shadow-modal);padding:20px 20px 18px;color:var(--fg-1);
  animation:vcc-rise .16s ease both;}
@keyframes vcc-rise{from{opacity:0;transform:translateY(8px) scale(.98)}to{opacity:1;transform:none}}
.vcc-head{display:flex;align-items:center;gap:10px;margin-bottom:4px;}
.vcc-badge{flex:0 0 auto;display:flex;align-items:center;justify-content:center;width:34px;height:34px;
  border-radius:calc(10px * var(--radius-scale));background:var(--accent-soft);color:var(--accent);}
.vcc-badge svg{width:18px;height:18px;}
.vcc-title{font-size:15px;font-weight:600;line-height:1.2;}
.vcc-sub{font-size:12px;color:var(--fg-3);margin:6px 0 14px;line-height:1.4;}
.vcc-sub .vcc-svc{color:var(--fg-2);}
.vcc-field{margin-bottom:12px;}
.vcc-label{display:block;font-size:12px;color:var(--fg-2);margin-bottom:5px;}
.vcc-input{width:100%;box-sizing:border-box;padding:9px 11px;font-size:13px;
  background:var(--bg-1);color:var(--fg-1);border:var(--border-width) solid var(--border);
  border-radius:calc(9px * var(--radius-scale));outline:none;transition:border-color .12s ease;}
.vcc-input:focus{border-color:var(--accent);}
.vcc-actions{display:flex;gap:8px;justify-content:flex-end;margin-top:16px;}
.vcc-btn{font-size:13px;font-weight:500;padding:8px 15px;border-radius:calc(9px * var(--radius-scale));
  border:var(--border-width) solid var(--border);background:transparent;color:var(--fg-2);cursor:pointer;
  transition:background .12s ease,border-color .12s ease,opacity .12s ease;}
.vcc-btn:hover{background:var(--bg-1);}
.vcc-btn.primary{background:var(--accent);border-color:var(--accent);color:var(--bg-0);}
.vcc-btn.primary:hover{background:var(--accent-hover);}
.vcc-btn[disabled]{opacity:.55;cursor:default;}
.vcc-msg{font-size:12px;margin-top:10px;min-height:14px;line-height:1.4;}
.vcc-msg.err{color:var(--danger);}
.vcc-msg.ok{color:var(--success);}
.vcc-done{display:flex;align-items:center;gap:8px;color:var(--success);font-size:14px;font-weight:600;padding:6px 0;}
.vcc-done svg{width:18px;height:18px;}
`;
  const style = document.createElement('style');
  style.id = 'vcc-styles';
  style.textContent = css;
  document.head.appendChild(style);
}

function _hostOf(url) {
  try { return new URL(url).host; } catch (_) { return ''; }
}

function _lucide(el) {
  try { if (window.lucide && window.lucide.createIcons) window.lucide.createIcons({ root: el }); } catch (_) {}
}

/**
 * Pop the secure entry card for a reserved vault key.
 * @param {{key_id:string,name:string,fields:Array,service?:string,filled?:boolean}} payload
 */
export function renderVaultCredentialCard(payload) {
  if (!payload || !payload.key_id) return;
  _injectStyles();

  // Don't stack duplicates — re-focus the live card for this key instead.
  const existing = _open.get(payload.key_id);
  if (existing && existing.isConnected) {
    const inp = existing.querySelector('.vcc-input');
    if (inp) inp.focus();
    return;
  }

  const fields = Array.isArray(payload.fields) && payload.fields.length
    ? payload.fields
    : [{ key: 'secret', label: payload.name || 'Secret', secret: true }];

  const overlay = document.createElement('div');
  overlay.className = 'vcc-overlay';

  const host = _hostOf(payload.service || '');
  const fieldsHtml = fields.map((f, i) => {
    const isSecret = f.secret !== false;
    const safeLabel = String(f.label || f.key || 'Value');
    return `<div class="vcc-field">
      <label class="vcc-label" for="vcc-f-${i}">${safeLabel}</label>
      <input class="vcc-input" id="vcc-f-${i}" data-key="${String(f.key)}"
        type="${isSecret ? 'password' : 'text'}" autocomplete="off" autocapitalize="off"
        autocorrect="off" spellcheck="false" />
    </div>`;
  }).join('');

  overlay.innerHTML = `
    <div class="vcc-card" role="dialog" aria-modal="true" aria-label="Enter credential">
      <div class="vcc-head">
        <span class="vcc-badge"><i data-lucide="shield-check"></i></span>
        <span class="vcc-title"></span>
      </div>
      <div class="vcc-sub">Saved straight to your vault — WebAgent never sees this value.${
        host ? ` Used only with <span class="vcc-svc">${host}</span>.` : ''
      }</div>
      <div class="vcc-body">${fieldsHtml}</div>
      <div class="vcc-msg"></div>
      <div class="vcc-actions">
        <button type="button" class="vcc-btn vcc-cancel">Cancel</button>
        <button type="button" class="vcc-btn primary vcc-save">Save to vault</button>
      </div>
    </div>`;

  // Set the title via textContent (never innerHTML) so an agent-chosen name can't inject markup.
  overlay.querySelector('.vcc-title').textContent = payload.name || 'Add a credential';

  const card = overlay.querySelector('.vcc-card');
  const msg = overlay.querySelector('.vcc-msg');
  const saveBtn = overlay.querySelector('.vcc-save');
  const cancelBtn = overlay.querySelector('.vcc-cancel');

  const close = () => {
    document.removeEventListener('keydown', onKey);
    overlay.remove();
    _open.delete(payload.key_id);
  };
  const onKey = (e) => { if (e.key === 'Escape') close(); };

  overlay.addEventListener('mousedown', (e) => { if (e.target === overlay) close(); });
  cancelBtn.addEventListener('click', close);
  document.addEventListener('keydown', onKey);

  async function save() {
    const values = {};
    overlay.querySelectorAll('.vcc-input').forEach((inp) => {
      values[inp.getAttribute('data-key')] = inp.value;
    });
    const anyFilled = Object.values(values).some((v) => String(v || '').trim());
    if (!anyFilled) {
      msg.className = 'vcc-msg err';
      msg.textContent = 'Enter a value first.';
      return;
    }
    saveBtn.disabled = true; cancelBtn.disabled = true;
    msg.className = 'vcc-msg';
    msg.textContent = 'Saving…';
    try {
      const resp = await fetch(apiPath('/api/v1/genui/vault/keys/' + encodeURIComponent(payload.key_id)), {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ values }),
      });
      if (!resp.ok) {
        let detail = '';
        try { detail = (await resp.json()).detail || ''; } catch (_) {}
        throw new Error(detail || ('HTTP ' + resp.status));
      }
      // Success — wipe the inputs from the DOM immediately, then show confirmation.
      card.querySelector('.vcc-body').innerHTML = '';
      msg.remove();
      card.querySelector('.vcc-actions').remove();
      const done = document.createElement('div');
      done.className = 'vcc-done';
      done.innerHTML = '<i data-lucide="check-circle-2"></i><span></span>';
      done.querySelector('span').textContent = 'Saved to vault ✓';
      card.appendChild(done);
      _lucide(done);
      setTimeout(close, 1300);
    } catch (err) {
      saveBtn.disabled = false; cancelBtn.disabled = false;
      msg.className = 'vcc-msg err';
      msg.textContent = 'Could not save: ' + (err && err.message || err);
    }
  }

  saveBtn.addEventListener('click', save);
  overlay.addEventListener('keydown', (e) => {
    if (e.key === 'Enter' && (e.target.classList && e.target.classList.contains('vcc-input'))) {
      e.preventDefault(); save();
    }
  });

  document.body.appendChild(overlay);
  _open.set(payload.key_id, overlay);
  _lucide(overlay);
  const first = overlay.querySelector('.vcc-input');
  if (first) first.focus();
}

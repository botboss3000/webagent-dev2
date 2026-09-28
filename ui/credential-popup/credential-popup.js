'use strict';

// ── Shared credential popover ───────────────────────────────────────────────
// A small floating panel for entering and saving cloud/provider credentials —
// the app-wide REUSABLE credential form. First consumer: the Instances ↑ HTTPS
// button (ui/main-panel/app/instances.js → _deviceConnectPopup).
//
// Defaults to a POPOVER: a card anchored near the trigger element that floats
// OVER the content — no full-screen dim, no blur, the app stays visible and
// interactive behind it. `placement: 'center'` opts into a dimmed centered
// modal instead (for flows that want to force focus).
//
// Self-contained: injects its own <style> once (design-system tokens only →
// correct in dark AND light), owns its overlay lifecycle (Escape / outside
// click / single-popover dedupe), and knows nothing about the caller's page —
// it is driven entirely by the options object:
//
//   openCredentialPopup({
//     title, hint,
//     anchor,                            // element to anchor the popover to (required for popover)
//     placement: 'popover' | 'center',   // default 'popover'
//     providers: [ { id, display_name, icon, has_key, project,
//                    connect_fields:    [{key,label,placeholder,required,value}],
//                    credential_fields: [{key,label,secret,textarea,optional,placeholder}] } ],
//     initialProviderId, mode: 'summary' | 'form',
//     summaryOnly,                       // saved rows only; hides form/footer
//     // Which fields to show — filter by key, or fully custom:
//     includeFields: ['service_account_json'],        // only these keys
//     excludeFields: ['github_token', 'admin_password'],      // skip these keys
//     fieldFilter(field, kind),                       // kind: 'connect'|'credential'|'form'
//     fieldTips: { '<key>': 'text' | {html, wide} },  // per-field "?" info bubbles
//     saveLabel, endpoint, extraBody,
//     save(values, providerId, popup),          // custom persistence (else default POST)
//     onSaved(popup, info),                     // after a successful save
//     onUseSaved(providerId, popup),            // a saved-credentials row chosen
//     onCancel(),                               // dismissed (Escape/outside/Cancel)
//     fields: [{key,label,secret,textarea,placeholder,required,value,tip}],  // OR a bare custom form
//   })
//
// Returns a handle { el, close(), showNote(text, kind) }. Callers use close()
// to dismiss after a success, or showNote() to keep the popover open with a
// warn/ok message (e.g. "connected, but no VM matched yet").
//
// Field filtering: each consumer shows ONLY the fields its flow needs.
//   • HTTPS / device linking → includeFields ['service_account_json'] — the
//     Google key JSON carries the project id, extracted server-side on save.
//   • Git control → includeFields ['github_token'] (or no filter), as needed.
// Field help: any field with a tip renders the SAME circled "?" info badge the
// App Settings → Deploy target section uses (ui/shared/js/field-tip.js →
// tipBadge) — click it for a floating help bubble.
//
// KEEP: this is the canonical shared credential form — new pages that need a
// credential entry popover import openCredentialPopup from here instead of
// building their own.
//
// Sibling: ui/vault-credential/vault-credential-card.js (the agent-triggered
// vault secret card — a different, security-critical flow; do not merge).

import { apiPath } from '../shared/js/config.js';
import { _esc, _escAttr } from '../shared/js/dom-utils.js';
import { tipBadge } from '../shared/js/field-tip.js';

let _current = null;        // the one open popover (single-popover dedupe)
let _stylesInjected = false;

function _injectStyles() {
  if (_stylesInjected) return;
  _stylesInjected = true;
  const css = `
/* Popover placement — transparent click-catcher (no dim, no blur): the app stays
   fully visible behind the floating card. */
.crp-overlay{position:fixed;inset:0;z-index:5000;background:transparent;}
/* Centered modal placement — dimmed + blurred backdrop. */
.crp-overlay.dim{display:flex;align-items:center;justify-content:center;
  background:rgba(var(--bg-0-rgb),0.62);backdrop-filter:blur(3px);-webkit-backdrop-filter:blur(3px);
  animation:crp-fade .14s ease both;}
@keyframes crp-fade{from{opacity:0}to{opacity:1}}
.crp-card{width:min(400px,calc(100vw - 24px));max-height:min(74vh,600px);overflow:auto;
  background:var(--bg-elev);border:var(--border-width) solid var(--border);
  border-radius:calc(14px * var(--radius-scale));box-shadow:var(--shadow-modal);
  padding:16px 18px 14px;color:var(--fg-1);animation:crp-rise .15s ease both;}
.crp-overlay:not(.dim) .crp-card{position:fixed;}
@keyframes crp-rise{from{opacity:0;transform:translateY(6px) scale(.985)}to{opacity:1;transform:none}}
.crp-head{display:flex;align-items:center;gap:10px;margin-bottom:2px;}
.crp-badge{flex:0 0 auto;display:flex;align-items:center;justify-content:center;width:32px;height:32px;
  border-radius:calc(10px * var(--radius-scale));background:var(--accent-soft);color:var(--accent);}
.crp-badge svg{width:17px;height:17px;}
.crp-title{font-size:14.5px;font-weight:600;line-height:1.2;}
.crp-x{margin-left:auto;flex:0 0 auto;width:26px;height:26px;display:flex;align-items:center;justify-content:center;
  border:none;background:transparent;color:var(--fg-3);font-size:15px;line-height:1;cursor:pointer;
  border-radius:calc(8px * var(--radius-scale));transition:background .12s ease,color .12s ease;}
.crp-x:hover{background:var(--bg-1);color:var(--fg-1);}
.crp-sub{font-size:12px;color:var(--fg-3);margin:6px 0 12px;line-height:1.45;}
.crp-note{font-size:12px;line-height:1.45;padding:8px 10px;border-radius:calc(9px * var(--radius-scale));
  border:var(--border-width) solid var(--border);margin:12px 0 10px;color:var(--fg-2);}
.crp-note[hidden]{display:none;}
.crp-note.warn{color:var(--warning);border-color:var(--warning);background:rgba(var(--warning-rgb),0.08);}
.crp-note.ok{color:var(--success);border-color:var(--success);background:rgba(var(--success-rgb),0.08);}
.crp-field{margin-bottom:11px;}
.crp-label{display:block;font-size:12px;color:var(--fg-2);margin-bottom:5px;}
.crp-req{color:var(--danger);margin-left:2px;}
.crp-input{width:100%;box-sizing:border-box;padding:9px 11px;font-size:13px;font-family:inherit;
  background:var(--bg-1);color:var(--fg-1);border:var(--border-width) solid var(--border);
  border-radius:calc(9px * var(--radius-scale));outline:none;transition:border-color .12s ease;}
.crp-input:focus{border-color:var(--accent);}
textarea.crp-input{resize:vertical;min-height:84px;line-height:1.4;}
.crp-actions{display:flex;gap:8px;align-items:center;margin-top:14px;}
.crp-actions[hidden]{display:none;}
.crp-btn{font-size:13px;font-weight:500;padding:8px 15px;border-radius:calc(9px * var(--radius-scale));
  border:var(--border-width) solid var(--border);background:transparent;color:var(--fg-2);cursor:pointer;
  transition:background .12s ease,border-color .12s ease,opacity .12s ease;white-space:nowrap;}
.crp-btn:hover{background:var(--bg-1);}
.crp-btn.primary{background:var(--accent);border-color:var(--accent);color:var(--bg-0);}
.crp-btn.primary:hover{background:var(--accent-hover);}
.crp-btn[disabled]{opacity:.55;cursor:default;}
.crp-status{flex:1;font-size:12px;line-height:1.4;text-align:right;color:var(--fg-3);min-height:14px;}
.crp-status.err{color:var(--danger);}
.crp-status.ok{color:var(--success);}
.crp-saved-label{font-size:11px;font-weight:600;text-transform:uppercase;letter-spacing:.06em;color:var(--fg-3);margin:4px 0 6px;}
.crp-saved-row{display:flex;align-items:center;gap:10px;padding:9px 10px;margin-bottom:8px;
  border:var(--border-width) solid var(--border);border-radius:calc(11px * var(--radius-scale));background:var(--bg-1);}
.crp-saved-icon{flex:0 0 auto;display:flex;align-items:center;justify-content:center;width:30px;height:30px;
  border-radius:calc(9px * var(--radius-scale));background:var(--accent-soft);color:var(--accent);}
.crp-saved-icon svg{width:16px;height:16px;}
.crp-saved-name{font-size:13px;font-weight:600;color:var(--fg-1);min-width:0;overflow:hidden;text-overflow:ellipsis;white-space:nowrap;}
.crp-saved-project{font-size:12px;color:var(--fg-3);min-width:0;overflow:hidden;text-overflow:ellipsis;white-space:nowrap;margin-right:auto;}
.crp-saved-pill{flex:0 0 auto;font-size:11px;font-weight:600;padding:3px 8px;border-radius:999px;background:rgba(var(--success-rgb),0.12);color:var(--success);}
.crp-saved-actions{flex:0 0 auto;display:flex;gap:6px;}
.crp-new-row{display:flex;gap:8px;align-items:center;}
/* "Save new credentials" row — dashed, opens the same form (data-act="new-account"). */
.crp-add-row{display:flex;align-items:center;gap:10px;width:100%;box-sizing:border-box;margin-top:4px;
  padding:9px 10px;border:1px dashed var(--border-strong,var(--border));border-radius:calc(11px * var(--radius-scale));
  background:transparent;color:var(--fg-2);cursor:pointer;font-family:inherit;font-size:13px;text-align:left;
  transition:background .12s ease,border-color .12s ease,color .12s ease;}
.crp-add-row:hover{background:var(--bg-1);border-color:var(--accent);color:var(--fg-1);}
.crp-add-row .crp-add-icon{background:transparent;color:var(--fg-3);border:1px dashed var(--border-strong,var(--border));}
.crp-add-row:hover .crp-add-icon{color:var(--accent);border-color:var(--accent);}
`;
  const style = document.createElement('style');
  style.id = 'crp-styles';
  style.textContent = css;
  document.head.appendChild(style);
}

function _lucide(el) {
  try { if (window.lucide && window.lucide.createIcons) window.lucide.createIcons({ root: el }); } catch (_) {}
}

// ── Provider helpers (Instances /admin/instances/providers shape) ───────────
function _providerComplete(p) {
  return !!p.has_key && (p.connect_fields || []).filter(function(f) { return f.required; })
    .every(function(f) { return String(f.value || '').trim(); });
}
function _providerProject(p) {
  if (p.connect_fields && p.connect_fields.length && p.connect_fields[0].value) return p.connect_fields[0].value;
  return p.project || '';
}
function _defaultProviderId(provs) {
  const complete = provs.find(_providerComplete);
  if (complete) return complete.id;
  const partial = provs.find(function(p) {
    return p.has_key || (p.connect_fields || []).some(function(f) { return String(f.value || '').trim(); });
  });
  return (partial || provs[0] || {}).id || '';
}

// ── Field filtering ─────────────────────────────────────────────────────────
// Each consumer decides which fields its flow needs. Filters by key
// (includeFields / excludeFields) and/or a custom fieldFilter(field, kind).
function _filterFields(opts, fields, kind) {
  const incl = opts.includeFields ? opts.includeFields.map(String) : null;
  const excl = opts.excludeFields ? opts.excludeFields.map(String) : null;
  return fields.filter(function(f) {
    const k = String(f.key);
    if (incl && incl.indexOf(k) === -1) return false;
    if (excl && excl.indexOf(k) !== -1) return false;
    if (typeof opts.fieldFilter === 'function' && !opts.fieldFilter(f, kind)) return false;
    return true;
  });
}

// ── Field rendering ─────────────────────────────────────────────────────────
// One labelled input/select/textarea. `f` is a normalized field def:
// {key,label,placeholder,required,value,secret,textarea,keep,optional,type,options,tip}
function _fieldHtml(f, i) {
  const id = 'crp-f-' + i;
  const req = f.required ? '<span class="crp-req">*</span>' : '';
  const keepPh = (f.keep && !f.value) ? 'saved — leave blank to keep' : (f.placeholder || '');
  const ph = _escAttr(keepPh);
  const attrs = ' data-fk="' + _escAttr(f.key) + '" data-label="' + _escAttr(f.label || f.key) + '"'
    + (f.required ? ' data-required="1"' : '')
    + (f.secret ? ' data-secret="1"' : '')
    + (f.optional ? ' data-optional="1"' : '')
    + (f.dropzone ? ' data-dropzone="1"' : '');
  let control;
  if (f.type === 'select') {
    control = '<select class="crp-input" id="' + id + '"' + attrs + '>'
      + (f.options || []).map(function(o) {
        return '<option value="' + _escAttr(o) + '"' + (o === f.value ? ' selected' : '') + '>' + _esc(o) + '</option>';
      }).join('')
      + '</select>';
  } else if (f.textarea) {
    control = '<textarea class="crp-input" id="' + id + '"' + attrs + ' rows="4" placeholder="' + ph
      + '" autocomplete="off" autocapitalize="off" autocorrect="off" spellcheck="false"></textarea>';
  } else {
    const type = f.secret ? 'password' : 'text';
    const val = (!f.secret && f.value != null) ? ' value="' + _escAttr(f.value) + '"' : '';
    control = '<input class="crp-input" id="' + id + '"' + attrs + ' type="' + type + '"' + val
      + ' placeholder="' + ph + '" autocomplete="off" autocapitalize="off" autocorrect="off" spellcheck="false">';
  }
  return '<div class="crp-field" data-tip-key="' + _escAttr(f.key) + '"><label class="crp-label" for="' + id + '">'
    + _esc(f.label || f.key || 'Value') + req + '</label>' + control + '</div>';
}

// ── Drag-and-drop file fill ──────────────────────────────────────────────────
// Mirrors the deploy page's JSON dropzone (_wireDropzone): dropping the
// downloaded service-account .json onto the box reads it as text and fills the
// field as if pasted. The `.ac-dropzone` highlight styles are the shared ones
// from app3.css (same affordance as the Deploy target form).
function _wireDropzone(ta) {
  ta.classList.add('ac-dropzone');
  const stop = function(e) { e.preventDefault(); e.stopPropagation(); };
  ta.addEventListener('dragenter', function(e) { stop(e); ta.classList.add('is-dragover'); });
  ta.addEventListener('dragover', function(e) { stop(e); ta.classList.add('is-dragover'); });
  ta.addEventListener('dragleave', function(e) { stop(e); ta.classList.remove('is-dragover'); });
  ta.addEventListener('drop', function(e) {
    stop(e);
    ta.classList.remove('is-dragover');
    const file = e.dataTransfer && e.dataTransfer.files && e.dataTransfer.files[0];
    if (!file) return;
    const reader = new FileReader();
    reader.onload = function() {
      ta.value = (typeof reader.result === 'string') ? reader.result : '';
      ta.dispatchEvent(new Event('input', { bubbles: true }));
      try { ta.focus(); } catch (_) {}
    };
    reader.readAsText(file);
  });
}

// After the body renders, attach the dropzone to any field that asks for it
// (provider defs carry `dropzone: true` on the service-account JSON).
function _wireDropzones(handle) {
  const body = handle.el.querySelector('.crp-body');
  if (!body) return;
  body.querySelectorAll('.crp-input[data-dropzone]').forEach(function(ta) {
    if (ta.dataset.dropWired) return;
    ta.dataset.dropWired = '1';
    _wireDropzone(ta);
  });
}

// Normalize a provider/field `tip` (string or the Deploy form's rich
// {text, link, images} descriptor) into the shared tipBadge string shape.
function _normalizeTip(tip) {
  if (!tip) return '';
  if (typeof tip === 'string') return tip;
  if (typeof tip === 'object') return tip.text || tip.html || '';
  return '';
}

// Provider def → normalized, FILTERED fields (connect_fields as text/select,
// credential_fields as secret/textarea). Each field keeps its tip for the "?"
// info badge.
function _providerFields(p, opts) {
  const fields = [];
  _filterFields(opts, p.connect_fields || [], 'connect').forEach(function(f) {
    fields.push({
      key: f.key, label: f.label || 'Value', placeholder: f.placeholder || '',
      required: !!f.required, value: f.value != null ? f.value : '',
      type: f.type, options: f.options, secret: false,
      tip: _normalizeTip(f.tip),
    });
  });
  _filterFields(opts, p.credential_fields || [], 'credential').forEach(function(f) {
    fields.push({
      key: f.key, label: f.label || 'Key', placeholder: f.placeholder || '',
      required: !!f.required && !f.optional, value: '',
      secret: !!f.secret, textarea: !!f.textarea, keep: !!p.has_key, optional: !!f.optional,
      dropzone: !!f.dropzone,
      tip: _normalizeTip(f.tip),
    });
  });
  return fields;
}

// ── Mode builders ───────────────────────────────────────────────────────────
function _formInner(opts, prefillPid) {
  const provs = opts.providers || [];
  const bare = _filterFields(opts, opts.fields || [], 'form');
  let html = '';
  if (opts.hint) html += '<div class="crp-sub">' + _esc(opts.hint) + '</div>';
  if (bare.length) {
    html += '<div class="crp-fields">' + bare.map(_fieldHtml).join('') + '</div>';
    return html;
  }
  const showPid = prefillPid || _defaultProviderId(provs);
  const showP = provs.find(function(p) { return p.id === showPid; }) || provs[0] || null;
  const pid = showP ? showP.id : '';
  const optsHtml = provs.map(function(p) {
    return '<option value="' + _escAttr(p.id) + '"' + (p.id === pid ? ' selected' : '')
      + '>' + _esc(p.display_name || p.id) + '</option>';
  }).join('');
  html += '<div class="crp-field">'
    + '<label class="crp-label" for="crp-provider">Provider</label>'
    + '<select class="crp-input" id="crp-provider" data-crp-provider>' + optsHtml + '</select>'
    + '</div>'
    + '<div class="crp-fields">' + _providerFields(showP, opts).map(_fieldHtml).join('') + '</div>';
  return html;
}

function _summaryInner(opts) {
  const provs = opts.providers || [];
  const saved = provs.filter(_providerComplete);
  if (!saved.length) return null;   // nothing saved → caller falls through to the form
  let html = '';
  if (opts.hint) html += '<div class="crp-sub">' + _esc(opts.hint) + '</div>';
  html += '<div class="crp-saved-label">Saved cloud credentials</div>';
  saved.forEach(function(p) {
    const projVal = _providerProject(p);
    html += '<div class="crp-saved-row" data-provider="' + _escAttr(p.id) + '">'
      + '<span class="crp-saved-icon"><i data-lucide="' + _escAttr(p.icon || 'cloud') + '"></i></span>'
      + '<span class="crp-saved-name">' + _esc(p.display_name || p.id) + '</span>'
      + (projVal ? '<span class="crp-saved-project">' + _esc(projVal) + '</span>' : '')
      + '<span class="crp-saved-pill">Connected</span>'
      + '<span class="crp-saved-actions">'
      +   '<button type="button" class="crp-btn primary" data-act="use-saved" data-provider="' + _escAttr(p.id) + '">Use saved</button>'
      + '</span>'
      + '</div>';
  });
  // One explicit row to ADD new credentials — opens the same form (which carries
  // the provider picker), replacing the old "select a provider + Continue" row.
  if (!opts.summaryOnly) {
    html += '<button type="button" class="crp-add-row" data-act="new-account">'
      + '<span class="crp-saved-icon crp-add-icon"><i data-lucide="plus"></i></span>'
      + '<span class="crp-saved-name">Save new credentials</span>'
      + '</button>';
  }
  return html;
}

function _saveLabel(opts, provider) {
  const base = opts.saveLabel || 'Save';
  if (base && provider && provider.has_key && !(opts.fields && opts.fields.length)) return 'Reconnect';
  return base;
}

// ── Field "?" info badges ───────────────────────────────────────────────────
// After the body renders, attach the shared tipBadge (same circled "?" the
// App Settings → Deploy target section uses) to any field that has a tip —
// from its provider def, or overridden per-key via opts.fieldTips.
function _wireTips(opts, handle) {
  const body = handle.el.querySelector('.crp-body');
  if (!body) return;
  const sel = body.querySelector('[data-crp-provider]');
  const provider = sel
    ? (opts.providers || []).find(function(p) { return p.id === sel.value; }) || null
    : null;
  const fields = provider ? _providerFields(provider, opts) : _filterFields(opts, opts.fields || [], 'form');
  const tips = {};
  fields.forEach(function(f) { if (f.tip) tips[f.key] = f.tip; });
  if (opts.fieldTips) {
    Object.keys(opts.fieldTips).forEach(function(k) { if (opts.fieldTips[k]) tips[k] = opts.fieldTips[k]; });
  }
  body.querySelectorAll('.crp-field[data-tip-key]').forEach(function(fieldEl) {
    const k = fieldEl.getAttribute('data-tip-key');
    const tip = tips[k];
    if (!tip) return;
    const label = fieldEl.querySelector('.crp-label');
    if (!label || label.querySelector('.ac-field-tip')) return;
    // Longer setup-style help → a wider bubble.
    const badge = (typeof tip === 'string' && tip.length > 110)
      ? tipBadge({ html: _esc(tip).replace(/\n/g, '<br>'), wide: true })
      : tipBadge(tip);
    if (badge) label.appendChild(badge);
  });
}

// ── Save ────────────────────────────────────────────────────────────────────
async function _defaultSave(opts, pid, values) {
  const res = await fetch(apiPath(opts.endpoint || '/admin/instances/connect'), {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify(Object.assign({ provider: pid, values: values }, opts.extraBody || {})),
  });
  let body = null;
  try { body = await res.json(); } catch (_) {}
  if (!res.ok) throw new Error((body && body.detail) || ('HTTP ' + res.status));
  return { ok: true, body: body };
}

async function _save(opts, handle) {
  const card = handle.el;
  const status = card.querySelector('.crp-status');
  const saveBtn = card.querySelector('.crp-save');
  const cancelBtn = card.querySelector('.crp-cancel');
  const providerSel = card.querySelector('[data-crp-provider]');
  const pid = providerSel ? providerSel.value : ((opts.providers && opts.providers[0]) ? opts.providers[0].id : '');
  const provider = (opts.providers || []).find(function(p) { return p.id === pid; }) || null;

  // Collect + validate.
  const values = {};
  let missing = '';
  card.querySelectorAll('.crp-input[data-fk]').forEach(function(inp) {
    const k = inp.getAttribute('data-fk');
    if (inp.tagName === 'SELECT') { values[k] = inp.value; return; }
    const v = inp.value;
    if (inp.hasAttribute('data-required') && !String(v || '').trim() && !missing) {
      missing = inp.getAttribute('data-label') || k;
    }
    if (inp.hasAttribute('data-secret')) {
      if (String(v || '').trim()) values[k] = v;   // blank secret = keep the saved one
    } else {
      values[k] = v;
    }
  });
  if (missing) {
    status.className = 'crp-status err';
    status.textContent = missing + ' is required.';
    return;
  }
  // A provider with no saved key must receive one now (mirrors the inline
  // panel's rule: the first secret credential field is required when empty).
  if (provider && !provider.has_key && !(opts.fields && opts.fields.length)) {
    const sec = card.querySelector('.crp-input[data-secret]');
    if (sec && !String(sec.value || '').trim()) {
      status.className = 'crp-status err';
      status.textContent = 'Paste your ' + (sec.getAttribute('data-label') || 'key') + ' first.';
      return;
    }
  }

  saveBtn.disabled = true; cancelBtn.disabled = true;
  status.className = 'crp-status';
  status.textContent = 'Saving…';
  try {
    const result = opts.save
      ? await opts.save(values, pid, handle)
      : await _defaultSave(opts, pid, values);
    saveBtn.disabled = false; cancelBtn.disabled = false;
    status.className = 'crp-status ok';
    status.textContent = 'Saved ✓';
    if (typeof opts.onSaved === 'function') opts.onSaved(handle, { provider: pid, values: values, result: result });
  } catch (err) {
    saveBtn.disabled = false; cancelBtn.disabled = false;
    status.className = 'crp-status err';
    status.textContent = 'Could not save: ' + ((err && err.message) || err);
  }
}

// ── Popover positioning ─────────────────────────────────────────────────────
// Place the fixed card near the anchor (below it, right-aligned; pass
// align='after' to open below and RIGHT of the trigger; flips above when there
// isn't room; clamped to the viewport). Called on open and again on scroll/
// resize so the card tracks the trigger.
function _positionPopover(card, anchor, align) {
  if (!anchor || !anchor.getBoundingClientRect) return;
  const r = anchor.getBoundingClientRect();
  const margin = 8;
  card.style.left = 'auto';
  card.style.top = 'auto';
  const cw = card.offsetWidth || 400;
  const ch = card.offsetHeight || 300;
  let left = align === 'after' ? r.right + margin : r.right - cw;
  let top = r.bottom + margin;
  if (left < margin) left = margin;
  if (left + cw > window.innerWidth - margin) left = window.innerWidth - margin - cw;
  if (top + ch > window.innerHeight - margin) top = r.top - ch - margin;
  if (top < margin) top = margin;
  card.style.left = Math.round(left) + 'px';
  card.style.top = Math.round(top) + 'px';
}

// ── Popover lifecycle ───────────────────────────────────────────────────────
function _close(handle, cancelled) {
  document.removeEventListener('keydown', handle._onKey);
  window.removeEventListener('scroll', handle._onRepos, true);
  window.removeEventListener('resize', handle._onRepos);
  handle.el.remove();
  if (_current === handle) _current = null;
  if (cancelled && typeof handle._opts.onCancel === 'function') handle._opts.onCancel();
}

function _renderBody(opts, handle, mode, prefillPid) {
  const body = handle.el.querySelector('.crp-body');
  const actions = handle.el.querySelector('.crp-actions');
  if (mode === 'summary') {
    const s = _summaryInner(opts);
    if (s) {
      body.innerHTML = s;
      handle._mode = 'summary';
      if (actions) actions.hidden = !!opts.summaryOnly;
      _lucide(body);
      const act = body.querySelector('[data-act="use-saved"]');
      if (act) act.focus();
      return;
    }
    // No saved credentials — fall through to the form.
  }
  handle._mode = 'form';
  if (actions) actions.hidden = false;
  body.innerHTML = _formInner(opts, prefillPid);
  _lucide(body);
  _wireTips(opts, handle);
  _wireDropzones(handle);
  const saveBtn = handle.el.querySelector('.crp-save');
  const sel = body.querySelector('[data-crp-provider]');
  if (sel) {
    const p = (opts.providers || []).find(function(x) { return x.id === sel.value; }) || null;
    saveBtn.textContent = _saveLabel(opts, p);
  }
  const first = body.querySelector('.crp-input');
  if (first) first.focus();
}

export function openCredentialPopup(opts) {
  if (!opts || (!(opts.providers || []).length && !(opts.fields || []).length)) return null;
  _injectStyles();

  // Single popover at a time — supersede any open one (no cancel callback: a new
  // open replaces it deliberately).
  if (_current) _close(_current, false);

  const isDim = opts.placement === 'center';
  const overlay = document.createElement('div');
  overlay.className = 'crp-overlay' + (isDim ? ' dim' : '');

  const title = opts.title || 'Enter credentials';
  overlay.innerHTML = `
    <div class="crp-card" role="dialog" aria-modal="true" aria-label="${_escAttr(title)}">
      <div class="crp-head">
        <span class="crp-badge"><i data-lucide="shield-check"></i></span>
        <span class="crp-title"></span>
        <button type="button" class="crp-x" data-act="cancel" aria-label="Close">&times;</button>
      </div>
      <div class="crp-note" hidden></div>
      <div class="crp-body"></div>
      <div class="crp-actions">
        <span class="crp-status"></span>
        <button type="button" class="crp-btn crp-cancel" data-act="cancel">Cancel</button>
        <button type="button" class="crp-btn primary crp-save" data-act="save"></button>
      </div>
    </div>`;

  // Title via textContent so option-provided text can never inject markup.
  overlay.querySelector('.crp-title').textContent = title;
  overlay.querySelector('.crp-save').textContent = _saveLabel(opts, null);

  const handle = {
    el: overlay,
    _opts: opts,
    _mode: 'form',
    close: function() { _close(handle, false); },
    showNote: function(text, kind) {
      const note = overlay.querySelector('.crp-note');
      note.hidden = !text;
      note.textContent = text || '';
      note.className = 'crp-note' + (kind === 'ok' ? ' ok' : kind === 'warn' ? ' warn' : '');
    },
    _onKey: null,
    _onRepos: null,
  };

  const startMode = opts.mode === 'summary' ? 'summary' : 'form';
  _renderBody(opts, handle, startMode, opts.initialProviderId || '');

  // Delegate clicks (summary rows / form actions / ✕).
  overlay.addEventListener('click', function(e) {
    const act = e.target.closest('[data-act]');
    if (!act) return;
    const a = act.getAttribute('data-act');
    if (a === 'save') { _save(opts, handle); return; }
    if (a === 'cancel') { _close(handle, true); return; }
    if (a === 'use-saved') {
      const pid = act.getAttribute('data-provider');
      if (typeof opts.onUseSaved === 'function') opts.onUseSaved(pid, handle);
      return;
    }
    if (a === 'use-different') {
      _renderBody(opts, handle, 'form', act.getAttribute('data-provider') || '');
      return;
    }
    if (a === 'new-account') {
      _renderBody(opts, handle, 'form', '');
      return;
    }
  });

  // Provider change → re-render its fields + save label + tips.
  overlay.addEventListener('change', function(e) {
    if (!e.target.matches('[data-crp-provider]')) return;
    const sel = e.target;
    const p = (opts.providers || []).find(function(x) { return x.id === sel.value; }) || null;
    const fieldsWrap = overlay.querySelector('.crp-fields');
    if (fieldsWrap) fieldsWrap.innerHTML = _providerFields(p, opts).map(_fieldHtml).join('');
    _wireTips(opts, handle);
    _wireDropzones(handle);
    const saveBtn = overlay.querySelector('.crp-save');
    if (saveBtn) saveBtn.textContent = _saveLabel(opts, p);
    const first = overlay.querySelector('.crp-fields .crp-input');
    if (first) first.focus();
  });

  // Enter saves (inputs only — not textareas, where Enter is a newline).
  overlay.addEventListener('keydown', function(e) {
    if (e.key === 'Enter' && e.target.classList && e.target.classList.contains('crp-input')
      && e.target.tagName !== 'TEXTAREA') {
      e.preventDefault();
      _save(opts, handle);
    }
  });

  // Escape / outside click dismiss — always closes the panel.
  const onKey = function(e) {
    if (e.key === 'Escape') { e.preventDefault(); _close(handle, true); }
  };
  handle._onKey = onKey;
  document.addEventListener('keydown', onKey);
  overlay.addEventListener('mousedown', function(e) {
    if (e.target === overlay) _close(handle, true);
  });

  document.body.appendChild(overlay);
  _current = handle;
  _lucide(overlay);

  // Popover: pin the card near the trigger and keep it there on scroll/resize.
  if (!isDim) {
    const card = overlay.querySelector('.crp-card');
    const repos = function() { _positionPopover(card, opts.anchor, opts.popoverAlign); };
    handle._onRepos = repos;
    window.addEventListener('scroll', repos, true);
    window.addEventListener('resize', repos);
    repos();
  }

  return handle;
}

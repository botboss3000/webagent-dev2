'use strict';

// COLOR SCHEME → ui/shared/css/design-system.css (single source of truth).
// Use CSS variables in inline styles (var(--accent), rgba(var(--success-rgb),a)).
// Never write hex/rgb colour literals here.

/**
 * Per-agent Monetization tab.
 *
 * Rendered by the agents card (ui/main-panel/agents/js/view.js) when the
 * Monetization tab is selected. The agent admin sets how THIS agent charges its
 * users; the agent keeps everything it earns. Shared builders + the global
 * runtime live in ui/shared/js/billing.js.
 * REMOVE-WHEN: the per-agent Monetization tab is dropped from the agent card.
 */

import {
  _el, _api, _qs, _userId, _loadProcessors,
  _buildCostFields, _labelled, _inputStyle, _selectStyle, _cents, STRATEGIES,
} from '../../../shared/js/billing.js';

export async function renderAgentMonetization(container, agentId) {
  container.innerHTML = '';
  container.appendChild(_el('div', {
    style: { fontSize: '12px', color: 'var(--fg-3)', marginBottom: '12px', lineHeight: '1.5' },
  }, 'Configure how this agent charges its users. The agent keeps everything it earns.'));

  const loading = _el('div', {});
  loading.innerHTML = _billingSkeletonHtml();
  container.appendChild(loading);

  let cfg, processors = [];
  try {
    // Parallel — config + processor list are independent (was a sequential waterfall).
    [cfg, processors] = await Promise.all([
      _api(`/api/v1/billing/config?${_qs({ user_id: _userId(), agent_id: agentId || '' })}`),
      _loadProcessors(),
    ]);
  } catch (e) {
    loading.innerHTML = '';
    loading.style.color = 'var(--fg-3)';
    loading.textContent = `Failed to load billing config: ${e.message}`;
    return;
  }
  loading.remove();

  const agent = cfg.agent || {};
  const effective = cfg.effective || {};
  const allowedStrategies = (effective.allowed_strategies && effective.allowed_strategies.length)
    ? effective.allowed_strategies : STRATEGIES.map(s => s.value);
  const allowedProcessors = (effective.allowed_processors && effective.allowed_processors.length)
    ? effective.allowed_processors : processors.map(p => p.name);

  // ── Strategy ──
  const stratWrap = _el('div', { style: { marginBottom: '14px' } });
  stratWrap.appendChild(_el('label', { style: { display: 'block', fontSize: '12px', fontWeight: '600', marginBottom: '4px' } }, 'Monetization method'));
  const stratSel = _el('select', { style: _selectStyle() });
  const allowTrial = allowedStrategies.includes('trial');
  const allowCredits = allowedStrategies.includes('credits');
  const stratOptions = STRATEGIES.filter(s =>
    s.value === 'free'
    || (s.value === 'credits' && allowCredits)
    || (s.value === 'trial' && allowTrial)
    || (s.value === 'trial,credits' && allowTrial && allowCredits));
  for (const s of stratOptions) {
    const o = _el('option', { value: s.value }, s.label);
    if ((agent.strategy || 'free') === s.value) o.selected = true;
    stratSel.appendChild(o);
  }
  stratWrap.appendChild(stratSel);
  container.appendChild(stratWrap);

  // ── Accepted payment methods ──
  const procWrap = _el('div', { style: { marginBottom: '14px' } });
  procWrap.appendChild(_el('label', { style: { display: 'block', fontSize: '12px', fontWeight: '600', marginBottom: '4px' } }, 'Payment methods this agent accepts'));
  const procRow = _el('div', { style: { display: 'flex', flexWrap: 'wrap', gap: '8px' } });
  const procCheckboxes = {};
  const procPool = processors.filter(p => allowedProcessors.includes(p.name));
  if (procPool.length === 0) {
    procRow.appendChild(_el('div', { style: { color: 'var(--fg-3)', fontSize: '12px' } }, 'No payment processors are enabled yet.'));
  }
  for (const p of procPool) {
    const c = _el('input', { type: 'checkbox' });
    c.checked = (agent.allowed_processors || []).includes(p.name);
    procCheckboxes[p.name] = c;
    const dot = _el('span', {
      style: { display: 'inline-block', width: '8px', height: '8px', borderRadius: '50%',
                marginRight: '4px', background: p.configured ? 'var(--success)' : 'var(--fg-3)' },
    });
    procRow.appendChild(_el('label', {
      style: { display: 'inline-flex', alignItems: 'center', gap: '4px', fontSize: '13px',
                padding: '4px 8px', border: '1px solid var(--border)', borderRadius: '6px', cursor: 'pointer' },
    }, c, dot, p.display_name + (p.configured ? '' : ' (not configured)')));
  }
  procWrap.appendChild(procRow);
  container.appendChild(procWrap);

  // ── Cost-based pricing ──
  const rateWrap = _el('div', { style: { marginBottom: '14px' } });
  rateWrap.appendChild(_el('label', { style: { display: 'block', fontSize: '12px', fontWeight: '600', marginBottom: '4px' } }, 'Credit pricing'));
  const costFields = _buildCostFields({
    cost_multiplier: agent.cost_multiplier ?? effective.cost_multiplier ?? 1.0,
    min_charge_cents: agent.min_charge_cents ?? effective.min_charge_cents ?? 1,
    flat_image_cost_usd: agent.flat_image_cost_usd ?? effective.flat_image_cost_usd ?? 0.01,
  });
  const rateGrid = _el('div', {
    style: { display: 'grid', gridTemplateColumns: '1fr 1fr', gap: '12px',
              padding: '10px', background: 'var(--bg-2)', borderRadius: '8px' },
  });
  rateGrid.appendChild(costFields.el);
  rateWrap.appendChild(rateGrid);
  container.appendChild(rateWrap);

  // ── Trial credit grant ──
  const trial = agent.trial_config || {};
  const trialWrap = _el('div', { style: { marginBottom: '14px', padding: '10px', background: 'var(--bg-2)', borderRadius: '8px' } });
  trialWrap.appendChild(_el('label', { style: { display: 'block', fontSize: '12px', fontWeight: '600', marginBottom: '6px' } }, 'New-user trial'));
  const trialGrid = _el('div', { style: { display: 'grid', gridTemplateColumns: '1fr 1fr', gap: '8px' } });
  const trialDays = _el('input', { type: 'number', min: '0', value: String(trial.days || 0), style: _inputStyle() });
  const trialCredits = _el('input', { type: 'number', min: '0', step: '0.01',
                                       value: String(((trial.credit_cents || 0) / 100).toFixed(2)), style: _inputStyle() });
  trialGrid.appendChild(_labelled('Days', trialDays));
  trialGrid.appendChild(_labelled('Credit value ($)', trialCredits));
  trialWrap.appendChild(trialGrid);
  trialWrap.appendChild(_el('div', { style: { marginTop: '6px', fontSize: '11px', color: 'var(--fg-3)', lineHeight: '1.4' } },
    'Granted automatically on a user’s first chat. The grant spends exactly like purchased credits (cost × multiplier) and cannot be restarted automatically. 0 days = never expires.'));
  container.appendChild(trialWrap);

  // ── Free-access grants (this agent's own users) ──
  container.appendChild(await _renderAgentExemptions(agentId));

  // ── Save ──
  const saveBtn = _el('button', {
    class: 'billing-btn',
    style: { padding: '10px 16px', background: 'var(--accent)', color: 'white', /* contrast ink on accent fill */
              border: 'none', borderRadius: '6px', cursor: 'pointer', fontSize: '14px', marginTop: '8px' },
    onclick: async () => {
      saveBtn.disabled = true;
      saveBtn.textContent = 'Saving…';
      try {
        const allowed_processors = Object.entries(procCheckboxes).filter(([_, c]) => c.checked).map(([n]) => n);
        const cost = costFields.read();
        const body = {
          user_id: _userId(),
          strategy: stratSel.value,
          allowed_processors,
          cost_multiplier: cost.cost_multiplier,
          min_charge_cents: cost.min_charge_cents,
          flat_image_cost_usd: cost.flat_image_cost_usd,
          trial_config: {
            days: parseInt(trialDays.value, 10) || 0,
            credit_cents: _cents(trialCredits.value),
          },
        };
        await _api(`/api/v1/billing/config/agent/${encodeURIComponent(agentId)}`, {
          method: 'PUT', body: JSON.stringify(body),
        });
        saveBtn.textContent = 'Saved ✓';
        setTimeout(() => { saveBtn.textContent = 'Save'; saveBtn.disabled = false; }, 1500);
      } catch (e) {
        alert('Save failed: ' + e.message);
        saveBtn.textContent = 'Save';
        saveBtn.disabled = false;
      }
    },
  }, 'Save');
  container.appendChild(saveBtn);
}

// Loading skeleton — mirrors the strategy select, payment chips, rate-card grid,
// trial panel and subscription bar so the tab has shape while billing config loads.
// Shape helpers .sk-line/.sk-block + .sk-shimmer fill live in app3.css.
function _billingSkeletonHtml() {
  const lbl  = (w) => `<div class="sk-shimmer sk-line" style="width:${w};height:12px;margin-bottom:6px;"></div>`;
  const bar  = (h) => `<div class="sk-shimmer sk-block" style="width:100%;height:${h};"></div>`;
  const chip = (w) => `<div class="sk-shimmer sk-block" style="width:${w};height:30px;border-radius:6px;"></div>`;
  return `
    <div style="margin-bottom:14px;">${lbl('130px')}${bar('38px')}</div>
    <div style="margin-bottom:14px;">${lbl('220px')}
      <div style="display:flex;flex-wrap:wrap;gap:8px;">${chip('88px')}${chip('108px')}${chip('76px')}</div>
    </div>
    <div style="margin-bottom:14px;">${lbl('70px')}
      <div style="display:grid;grid-template-columns:1fr 1fr;gap:12px;padding:10px;background:var(--bg-2);border-radius:8px;">
        ${bar('88px')}${bar('88px')}
      </div>
    </div>
    <div style="margin-bottom:14px;padding:10px;background:var(--bg-2);border-radius:8px;">${lbl('80px')}
      <div style="display:grid;grid-template-columns:1fr 1fr 1fr;gap:8px;">${bar('44px')}${bar('44px')}${bar('44px')}</div>
    </div>
    <div style="margin-bottom:14px;">${lbl('200px')}${bar('38px')}</div>`;
}

async function _renderAgentExemptions(agentId) {
  const wrap = _el('div', { style: { marginBottom: '14px', padding: '10px', background: 'var(--bg-2)', borderRadius: '8px' } });
  wrap.appendChild(_el('div', { style: { fontSize: '12px', fontWeight: '600', marginBottom: '6px' } },
    'Free access (no-pay users for this agent)'));
  const listDiv = _el('div', { style: { marginBottom: '8px' } });
  wrap.appendChild(listDiv);

  async function reload() {
    listDiv.innerHTML = '';
    try {
      const r = await _api(`/api/v1/billing/exemptions?${_qs({ user_id: _userId(), agent_id: agentId || '' })}`);
      const rows = (r.exemptions || []).filter(ex => ex.kind === 'user_for_agent');
      if (rows.length === 0) {
        listDiv.appendChild(_el('div', { style: { color: 'var(--fg-3)', fontSize: '12px' } }, 'No free-access grants yet.'));
      }
      for (const ex of rows) {
        const row = _el('div', { style: { display: 'flex', alignItems: 'center', gap: '8px', padding: '4px 0', fontSize: '12px' } });
        row.appendChild(_el('span', {}, `User ${String(ex.user_id || '').slice(0, 12)} — free for this agent`));
        row.appendChild(_el('button', {
          style: { marginLeft: 'auto', padding: '2px 6px', fontSize: '11px', background: 'transparent',
                    color: 'var(--fg-3)', border: '1px solid var(--border)', borderRadius: '4px', cursor: 'pointer' },
          onclick: async () => {
            if (!confirm('Remove this grant?')) return;
            await _api(`/api/v1/billing/exemptions/${ex.id}?user_id=${encodeURIComponent(_userId())}`, { method: 'DELETE' });
            reload();
          },
        }, 'remove'));
        listDiv.appendChild(row);
      }
    } catch (e) {
      listDiv.appendChild(_el('div', { style: { color: 'var(--fg-3)', fontSize: '12px' } }, 'Could not load: ' + e.message));
    }
  }
  reload();

  const form = _el('div', { style: { display: 'flex', gap: '6px', marginTop: '8px', flexWrap: 'wrap' } });
  const idInput = _el('input', { placeholder: 'user_id to grant free access', style: { ..._inputStyle(), flex: '1 1 200px' } });
  form.appendChild(idInput);
  form.appendChild(_el('button', {
    style: { padding: '6px 10px', background: 'var(--accent)', color: 'white', /* contrast ink on accent fill */
              border: 'none', borderRadius: '4px', cursor: 'pointer', fontSize: '12px' },
    onclick: async () => {
      const id = (idInput.value || '').trim();
      if (!id) return;
      try {
        await _api('/api/v1/billing/exemptions', {
          method: 'POST',
          body: JSON.stringify({ user_id: _userId(), kind: 'user_for_agent', agent_id: agentId, target_user_id: id }),
        });
        idInput.value = '';
        reload();
      } catch (e) { alert('Failed to add: ' + e.message); }
    },
  }, 'Add'));
  wrap.appendChild(form);
  return wrap;
}

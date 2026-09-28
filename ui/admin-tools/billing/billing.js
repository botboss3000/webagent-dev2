'use strict';

// COLOR SCHEME → ui/shared/css/design-system.css (single source of truth).
// Use CSS variables in inline styles; never write hex/rgb colour literals here.

/**
 * Platform (marketplace) billing — OPTIONAL, drop-in Admin Tools view.
 *
 * This whole view is the PLATFORM tier and is stripped from a public /
 * agent-only edition. Discovered via ui/admin-tools/billing/page.json and driven
 * by the admin shell, which dynamically imports this module and calls the
 * exported startView / stopView when the view is shown / hidden. It sets the
 * platform-wide policy, the platform fee, payout (Connect) onboarding and
 * platform exemptions, and talks to the platform routes (/api/v1/billing/
 * config/platform, /connect/*) that only exist when this package is installed.
 *
 * Sister panel (agent tier, always present): ui/main-panel/agents/billing/agent-billing.js.
 * REMOVE-WHEN: the platform billing tier is excluded from the edition.
 */

import {
  _el, _api, _qs, _userId, _loadProcessors,
  _buildCostFields, _labelled, _inputStyle, _selectStyle, _cents, STRATEGIES,
  startBilling, stopBilling,
} from '../../shared/js/billing.js';

const PROCESSOR_SETUP = {
  stripe: {
    name: 'Stripe',
    summary: 'Card payments, subscriptions, and marketplace payouts through Stripe Connect.',
    docs: 'https://docs.stripe.com/keys',
    variables: [
      ['STRIPE_SECRET_KEY', 'Secret API key'],
      ['STRIPE_WEBHOOK_SECRET', 'Signing secret for the WebAgent webhook'],
    ],
    steps: [
      'Create or open a Stripe account, then copy a secret key from Developers → API keys.',
      'In Developers → Webhooks, add the WebAgent webhook URL shown below.',
      'Subscribe the webhook to checkout, payment, refund, and subscription events, then copy its signing secret.',
      'Add the environment variables below to the WebAgent deployment and restart the server.',
    ],
  },
  paypal: {
    name: 'PayPal',
    summary: 'PayPal checkout and subscriptions, with sandbox support for testing.',
    docs: 'https://developer.paypal.com/dashboard/applications/',
    variables: [
      ['PAYPAL_CLIENT_ID', 'REST app client ID'],
      ['PAYPAL_CLIENT_SECRET', 'REST app secret'],
      ['PAYPAL_WEBHOOK_ID', 'Webhook ID from the PayPal developer dashboard'],
      ['PAYPAL_ENV', 'sandbox or live'],
    ],
    steps: [
      'Create a REST app in the PayPal developer dashboard and copy its client ID and secret.',
      'Add the WebAgent webhook URL shown below to that app and copy the webhook ID.',
      'Enable payment capture, refund, checkout-order, and billing-subscription events.',
      'Add the environment variables below to the WebAgent deployment and restart the server.',
    ],
  },
  bitcoin: {
    name: 'Bitcoin',
    summary: 'Bitcoin and Lightning payments through a self-hosted or managed BTCPay Server.',
    docs: 'https://docs.btcpayserver.org/Development/GreenFieldExample-NodeJS/',
    variables: [
      ['BTCPAY_URL', 'BTCPay Server URL'],
      ['BTCPAY_STORE_ID', 'Store ID'],
      ['BTCPAY_API_KEY', 'Greenfield API key with create-invoice permission'],
      ['BTCPAY_WEBHOOK_SECRET', 'Secret assigned to the WebAgent webhook'],
    ],
    steps: [
      'Create a BTCPay Server store and connect its Bitcoin or Lightning wallet.',
      'Create a Greenfield API key that can create invoices for that store.',
      'In Store settings → Webhooks, add the WebAgent webhook URL shown below and keep the generated secret.',
      'Add the environment variables below to the WebAgent deployment and restart the server.',
    ],
  },
};

export function startView() {
  startBilling();
  _renderPlatformPanel();
}

export function stopView() {
  stopBilling();
}

async function _renderPlatformPanel() {
  const container = document.getElementById('billing-platform-panel');
  if (!container) return;
  container.innerHTML = '';

  // ── Header ──
  const header = _el('div', {
    style: { display: 'flex', alignItems: 'center', justifyContent: 'space-between', marginBottom: '16px' },
  });
  header.appendChild(_el('h2', { style: { margin: '0', fontSize: '16px', fontWeight: '600' } }, 'Monetization'));
  const saveBtn = _el('button', {
    class: 'billing-btn',
    style: { padding: '8px 16px', background: 'var(--accent)', color: 'white', /* contrast ink on accent fill */
              border: 'none', borderRadius: '6px', cursor: 'pointer', fontSize: '13px' },
    onclick: async () => {
      saveBtn.disabled = true;
      saveBtn.textContent = 'Saving…';
      try {
        const body = {
          user_id: _userId(),
          strategy: Object.entries(stratCheckboxes).filter(([_, c]) => c.checked).map(([n]) => n).join(',') || 'free',
          allowed_strategies: Object.entries(allowedStrategyCheckboxes).filter(([_, c]) => c.checked).map(([n]) => n),
          allowed_processors: Object.entries(procCheckboxes).filter(([_, c]) => c.checked).map(([n]) => n),
          cost_multiplier: costFields.read().cost_multiplier,
          min_charge_cents: costFields.read().min_charge_cents,
          flat_image_cost_usd: costFields.read().flat_image_cost_usd,
          platform_fee_pct: parseFloat(feePctInput.value) || 0,
          platform_fee_flat_cents: parseInt(feeFlatInput.value, 10) || 0,
          trial_config: {
            days: parseInt(trialDays.value, 10) || 0,
            credit_cents: _cents(trialCredits.value),
          },
        };
        await _api('/api/v1/billing/config/platform', { method: 'PUT', body: JSON.stringify(body) });
        saveBtn.textContent = 'Saved ✓';
        setTimeout(() => { saveBtn.textContent = 'Save'; saveBtn.disabled = false; }, 1500);
      } catch (e) {
        alert('Save failed: ' + e.message);
        saveBtn.textContent = 'Save';
        saveBtn.disabled = false;
      }
    },
  }, 'Save');
  header.appendChild(saveBtn);
  container.appendChild(header);

  container.appendChild(_el('div', {
    style: { fontSize: '12px', color: 'var(--fg-3)', marginBottom: '12px', lineHeight: '1.5' },
  }, 'Choose which monetization methods agent admins can use, set the platform fee, and pick which payment processors are available.'));

  const loading = _el('div', { style: { color: 'var(--fg-3)' } }, 'Loading…');
  container.appendChild(loading);

  let platform = {}, processors = [];
  try {
    const r = await _api(`/api/v1/billing/config/platform?${_qs({ user_id: _userId() })}`);
    platform = r.platform || {};
    processors = await _loadProcessors();
  } catch (e) {
    loading.textContent = `Failed to load platform billing config: ${e.message}`;
    return;
  }
  loading.remove();

  // ── Default monetization method ──
  const stratWrap = _el('div', { style: { marginBottom: '14px' } });
  stratWrap.appendChild(_el('label', { style: { display: 'block', fontSize: '12px', fontWeight: '600', marginBottom: '4px' } }, 'Default monetization method'));
  const stratBoxes = _el('div', { style: { display: 'flex', flexWrap: 'wrap', gap: '8px' } });
  const stratCheckboxes = {};
  const STRAT_ORDER = ['trial', 'credits'];
  const currentStrategy = (platform.strategy || 'free');
  const currentStrategies = currentStrategy.split(',').filter(Boolean);
  for (const sv of STRAT_ORDER) {
    const s = STRATEGIES.find(x => x.value === sv);
    if (!s) continue;
    const c = _el('input', { type: 'checkbox' });
    c.checked = currentStrategies.includes(sv);
    stratCheckboxes[s.value] = c;
    stratBoxes.appendChild(_el('label', {
      style: { display: 'inline-flex', alignItems: 'center', gap: '4px', fontSize: '13px',
                padding: '4px 8px', border: '1px solid var(--border)', borderRadius: '6px', cursor: 'pointer' },
    }, c, s.label));
  }
  stratWrap.appendChild(stratBoxes);
  stratWrap.appendChild(_el('div', { style: { fontSize: '11px', color: 'var(--fg-3)', marginTop: '4px' } },
    'Trial runs first (a credit grant), then credits apply. None checked = free.'));
  container.appendChild(stratWrap);

  // ── Methods agent admins can enable ──
  const allowedStratWrap = _el('div', { style: { marginBottom: '14px' } });
  allowedStratWrap.appendChild(_el('label', { style: { display: 'block', fontSize: '12px', fontWeight: '600', marginBottom: '4px' } }, 'Methods agent admins can enable'));
  const boxes = _el('div', { style: { display: 'flex', flexWrap: 'wrap', gap: '8px' } });
  const allowedStrategyCheckboxes = {};
  for (const s of STRATEGIES.filter(s => s.value !== 'free' && !s.value.includes(','))) {
    const c = _el('input', { type: 'checkbox' });
    c.checked = (platform.allowed_strategies || []).includes(s.value);
    allowedStrategyCheckboxes[s.value] = c;
    boxes.appendChild(_el('label', {
      style: { display: 'inline-flex', alignItems: 'center', gap: '4px', fontSize: '13px',
                padding: '4px 8px', border: '1px solid var(--border)', borderRadius: '6px', cursor: 'pointer' },
    }, c, s.label));
  }
  allowedStratWrap.appendChild(boxes);
  container.appendChild(allowedStratWrap);

  // ── Payment processors users can pay with ──
  const procWrap = _el('div', { style: { marginBottom: '18px' } });
  procWrap.appendChild(_el('label', {
    style: { display: 'block', fontSize: '12px', fontWeight: '600', marginBottom: '4px' },
  }, 'Payment services'));
  procWrap.appendChild(_el('div', {
    style: { color: 'var(--fg-3)', fontSize: '11px', marginBottom: '8px' },
  }, 'Choose a service to see its connection details and enable it for checkout.'));
  const procRow = _el('div', {
    role: 'tablist',
    'aria-label': 'Payment services',
    style: { display: 'flex', flexWrap: 'wrap', gap: '8px' },
  });
  const procDetails = _el('div', {
    style: {
      marginTop: '10px', padding: '12px', background: 'var(--bg-2)',
      border: '1px solid var(--border)', borderRadius: '8px',
    },
  });
  procDetails.appendChild(_el('div', {
    style: { color: 'var(--fg-3)', fontSize: '12px' },
  }, 'Select Stripe, PayPal, or Bitcoin to configure it.'));
  const procCheckboxes = {};
  const processorButtons = {};
  for (const p of processors.filter(p => PROCESSOR_SETUP[p.name])) {
    const setup = PROCESSOR_SETUP[p.name];
    const c = _el('input', { type: 'checkbox' });
    c.checked = (platform.allowed_processors || []).includes(p.name);
    procCheckboxes[p.name] = c;
    const dot = _el('span', {
      style: { display: 'inline-block', width: '8px', height: '8px', borderRadius: '50%',
                background: p.configured ? 'var(--success)' : 'var(--fg-3)' },
    });
    const button = _el('button', {
      type: 'button',
      role: 'tab',
      'aria-selected': 'false',
      style: { display: 'inline-flex', alignItems: 'center', gap: '6px', fontSize: '13px',
                padding: '7px 10px', background: 'var(--bg-1)', color: 'var(--fg-1)',
                border: '1px solid var(--border)', borderRadius: '6px', cursor: 'pointer' },
      onclick: () => {
        for (const b of Object.values(processorButtons)) {
          b.setAttribute('aria-selected', 'false');
          b.style.borderColor = 'var(--border)';
          b.style.background = 'var(--bg-1)';
        }
        button.setAttribute('aria-selected', 'true');
        button.style.borderColor = 'var(--accent)';
        button.style.background = 'var(--accent-soft)';
        _renderProcessorSetup(procDetails, p, setup, c);
      },
    }, dot, setup.name);
    processorButtons[p.name] = button;
    procRow.appendChild(button);
  }
  procWrap.appendChild(procRow);
  procWrap.appendChild(procDetails);
  container.appendChild(procWrap);

  // ── Cost-based pricing (defaults agent admins inherit) ──
  const rateWrap = _el('div', { style: { marginBottom: '14px' } });
  rateWrap.appendChild(_el('label', { style: { display: 'block', fontSize: '12px', fontWeight: '600', marginBottom: '4px' } }, 'Default credit pricing'));
  const costFields = _buildCostFields({
    cost_multiplier: platform.cost_multiplier,
    min_charge_cents: platform.min_charge_cents,
    flat_image_cost_usd: platform.flat_image_cost_usd,
  });
  const rateGrid = _el('div', {
    style: { display: 'grid', gridTemplateColumns: '1fr 1fr', gap: '12px',
              padding: '10px', background: 'var(--bg-2)', borderRadius: '8px' },
  });
  rateGrid.appendChild(costFields.el);
  rateWrap.appendChild(rateGrid);
  container.appendChild(rateWrap);

  // ── Platform fee ──
  const feeWrap = _el('div', { style: { marginBottom: '14px', display: 'flex', gap: '12px' } });
  const feePctInput = _el('input', { type: 'number', step: '0.1', min: '0', max: '100',
                                      value: String(platform.platform_fee_pct || 0), style: _inputStyle() });
  const feeFlatInput = _el('input', { type: 'number', step: '1', min: '0',
                                       value: String(platform.platform_fee_flat_cents || 0), style: _inputStyle() });
  feeWrap.appendChild(_labelled('Platform fee %', feePctInput));
  feeWrap.appendChild(_labelled('Platform flat fee (¢)', feeFlatInput));
  container.appendChild(feeWrap);

  // ── Trial defaults ──
  const trial = platform.trial_config || {};
  const trialWrap = _el('div', { style: { marginBottom: '14px', padding: '10px', background: 'var(--bg-2)', borderRadius: '8px' } });
  trialWrap.appendChild(_el('label', { style: { display: 'block', fontSize: '12px', fontWeight: '600', marginBottom: '6px' } }, 'Trial period'));
  const trialGrid = _el('div', { style: { display: 'grid', gridTemplateColumns: '1fr 1fr', gap: '8px' } });
  const trialDays = _el('input', { type: 'number', min: '0', value: String(trial.days || 0), style: _inputStyle() });
  const trialCredits = _el('input', { type: 'number', min: '0', step: '0.01',
                                       value: String(((trial.credit_cents || 0) / 100).toFixed(2)), style: _inputStyle() });
  trialGrid.appendChild(_labelled('Days', trialDays));
  trialGrid.appendChild(_labelled('Credit value ($)', trialCredits));
  trialWrap.appendChild(trialGrid);
  trialWrap.appendChild(_el('div', { style: { marginTop: '6px', fontSize: '11px', color: 'var(--fg-3)', lineHeight: '1.4' } },
    'Granted on a user’s first chat and spent exactly like purchased credits (cost × multiplier). 0 days = never expires.'));
  container.appendChild(trialWrap);

  // ── Exemptions (no-pay users / agents) ──
  container.appendChild(await _renderPlatformExemptions());
}

function _renderProcessorSetup(container, processor, setup, enabledCheckbox) {
  container.innerHTML = '';
  const webhookUrl = `${window.location.origin}/api/v1/billing/webhook/${processor.name}`;
  const titleRow = _el('div', {
    style: { display: 'flex', alignItems: 'center', gap: '8px', marginBottom: '4px' },
  });
  titleRow.appendChild(_el('strong', { style: { fontSize: '13px' } }, setup.name));
  titleRow.appendChild(_el('span', {
    style: {
      marginLeft: 'auto', padding: '2px 7px', borderRadius: '999px', fontSize: '10px',
      color: processor.configured ? 'var(--success)' : 'var(--fg-3)',
      border: `1px solid ${processor.configured ? 'var(--success)' : 'var(--border)'}`,
    },
  }, processor.configured ? 'Connected' : 'Not connected'));
  container.appendChild(titleRow);
  container.appendChild(_el('div', {
    style: { color: 'var(--fg-2)', fontSize: '12px', lineHeight: '1.45', marginBottom: '10px' },
  }, setup.summary));

  const enableLabel = _el('label', {
    style: {
      display: 'inline-flex', alignItems: 'center', gap: '6px', marginBottom: '12px',
      fontSize: '12px', cursor: 'pointer',
    },
  }, enabledCheckbox, 'Allow users to pay with this service');
  container.appendChild(enableLabel);

  container.appendChild(_el('div', {
    style: { fontSize: '11px', fontWeight: '600', marginBottom: '4px' },
  }, 'Webhook URL'));
  const webhookRow = _el('div', {
    style: { display: 'flex', gap: '6px', marginBottom: '10px' },
  });
  webhookRow.appendChild(_el('code', {
    style: {
      flex: '1', minWidth: '0', padding: '7px 8px', background: 'var(--bg-1)',
      border: '1px solid var(--border)', borderRadius: '4px', fontSize: '11px',
      overflowWrap: 'anywhere',
    },
  }, webhookUrl));
  webhookRow.appendChild(_copyButton(webhookUrl, 'Copy URL'));
  container.appendChild(webhookRow);

  container.appendChild(_el('div', {
    style: { fontSize: '11px', fontWeight: '600', marginBottom: '4px' },
  }, 'Deployment variables'));
  const variableGrid = _el('div', {
    style: {
      display: 'grid', gridTemplateColumns: 'minmax(150px, auto) 1fr',
      gap: '4px 10px', marginBottom: '8px', fontSize: '11px',
    },
  });
  for (const [name, description] of setup.variables) {
    variableGrid.appendChild(_el('code', { style: { color: 'var(--fg-1)' } }, name));
    variableGrid.appendChild(_el('span', { style: { color: 'var(--fg-3)' } }, description));
  }
  container.appendChild(variableGrid);
  const envTemplate = setup.variables
    .map(([name]) => `${name}=${name === 'PAYPAL_ENV' ? 'sandbox' : ''}`)
    .join('\n');
  container.appendChild(_copyButton(envTemplate, 'Copy environment template'));

  const steps = _el('ol', {
    style: {
      margin: '12px 0 8px', paddingLeft: '20px', color: 'var(--fg-2)',
      fontSize: '11px', lineHeight: '1.55',
    },
  });
  for (const step of setup.steps) steps.appendChild(_el('li', {}, step));
  container.appendChild(steps);
  container.appendChild(_el('a', {
    href: setup.docs,
    target: '_blank',
    rel: 'noopener noreferrer',
    style: { color: 'var(--accent)', fontSize: '11px' },
  }, `Open ${setup.name} setup documentation ↗`));
  if (!processor.configured) {
    container.appendChild(_el('div', {
      style: {
        marginTop: '10px', padding: '8px', borderLeft: '3px solid var(--accent)',
        background: 'var(--bg-1)', color: 'var(--fg-3)', fontSize: '11px', lineHeight: '1.45',
      },
    }, 'Credentials are read from the server environment, not stored in this browser. After adding them, restart WebAgent and this status will change to Connected.'));
  }
}

function _copyButton(value, label) {
  const button = _el('button', {
    type: 'button',
    style: {
      padding: '6px 9px', background: 'var(--bg-1)', color: 'var(--fg-2)',
      border: '1px solid var(--border)', borderRadius: '4px', cursor: 'pointer',
      fontSize: '11px',
    },
    onclick: async () => {
      try {
        await navigator.clipboard.writeText(value);
        button.textContent = 'Copied ✓';
        setTimeout(() => { button.textContent = label; }, 1200);
      } catch (_) {
        window.prompt('Copy this value:', value);
      }
    },
  }, label);
  return button;
}

async function _renderPlatformExemptions() {
  const wrap = _el('div', { style: { marginBottom: '14px', padding: '10px', background: 'var(--bg-2)', borderRadius: '8px' } });
  wrap.appendChild(_el('div', { style: { fontSize: '12px', fontWeight: '600', marginBottom: '6px' } }, 'Exemptions (no-pay users / agents)'));
  const listDiv = _el('div', { style: { marginBottom: '8px' } });
  wrap.appendChild(listDiv);

  async function reload() {
    listDiv.innerHTML = '';
    try {
      const r = await _api(`/api/v1/billing/exemptions?${_qs({ user_id: _userId() })}`);
      const rows = r.exemptions || [];
      if (rows.length === 0) {
        listDiv.appendChild(_el('div', { style: { color: 'var(--fg-3)', fontSize: '12px' } }, 'No exemptions yet.'));
      }
      for (const ex of rows) {
        const label = ex.kind === 'agent'
          ? `Agent ${String(ex.agent_id || '').slice(0, 8)} — entire agent is free`
          : ex.kind === 'user'
            ? `User ${String(ex.user_id || '').slice(0, 12)} — exempt globally`
            : `User ${String(ex.user_id || '').slice(0, 12)} for agent ${String(ex.agent_id || '').slice(0, 8)}`;
        const row = _el('div', { style: { display: 'flex', alignItems: 'center', gap: '8px', padding: '4px 0', fontSize: '12px' } });
        row.appendChild(_el('span', {}, label));
        row.appendChild(_el('button', {
          style: { marginLeft: 'auto', padding: '2px 6px', fontSize: '11px', background: 'transparent',
                    color: 'var(--fg-3)', border: '1px solid var(--border)', borderRadius: '4px', cursor: 'pointer' },
          onclick: async () => {
            if (!confirm('Remove this exemption?')) return;
            await _api(`/api/v1/billing/exemptions/${ex.id}?user_id=${encodeURIComponent(_userId())}`, { method: 'DELETE' });
            reload();
          },
        }, 'remove'));
        listDiv.appendChild(row);
      }
    } catch (e) {
      listDiv.appendChild(_el('div', { style: { color: 'var(--fg-3)', fontSize: '12px' } }, 'Could not load exemptions: ' + e.message));
    }
  }
  reload();

  const form = _el('div', { style: { display: 'flex', gap: '6px', marginTop: '8px', flexWrap: 'wrap' } });
  const kindSel = _el('select', { style: { ..._inputStyle(), width: 'auto', flex: '0 0 auto' } });
  kindSel.appendChild(_el('option', { value: 'user' }, 'Exempt a user globally'));
  kindSel.appendChild(_el('option', { value: 'agent' }, 'Exempt an entire agent'));
  form.appendChild(kindSel);
  const idInput = _el('input', { placeholder: 'user_id (or agent_id if kind=agent)', style: { ..._inputStyle(), flex: '1 1 200px' } });
  form.appendChild(idInput);
  form.appendChild(_el('button', {
    style: { padding: '6px 10px', background: 'var(--accent)', color: 'white', /* contrast ink on accent fill */
              border: 'none', borderRadius: '4px', cursor: 'pointer', fontSize: '12px' },
    onclick: async () => {
      const id = (idInput.value || '').trim();
      if (!id) return;
      const body = kindSel.value === 'agent'
        ? { user_id: _userId(), kind: 'agent', agent_id: id }
        : { user_id: _userId(), kind: 'user', target_user_id: id };
      try {
        await _api('/api/v1/billing/exemptions', { method: 'POST', body: JSON.stringify(body) });
        idInput.value = '';
        reload();
      } catch (e) { alert('Failed to add exemption: ' + e.message); }
    },
  }, 'Add'));
  wrap.appendChild(form);
  return wrap;
}

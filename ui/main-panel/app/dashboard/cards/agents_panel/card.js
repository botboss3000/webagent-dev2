'use strict';

/* agents_panel — Agents card plugin (quick-access list, busiest first).
   Rows carry data-act="open-agent" handled by the shell's grid click.
   Reads the `agents` snapshot section built by this card's server.py.
   REMOVE-WHEN: the Dashboard tab is dropped from the Instances page. */

import { _esc, _escAttr } from '../_lib/card-lib.js';
import { fmtInt, fmtNum, fmtCost, windowLabel } from '../_lib/card-lib.js';

export default {
  render(s) {
    const a = s.agents || {};
    const rows = a.list || [];
    if (!rows.length) return `<div class="dash-muted">No agents yet.</div>`;
    return `<div class="dash-opwrap"><div class="dash-oplist">`
      + rows.map(ag =>
        `<div class="dash-oprow dash-op-click" data-act="open-agent" data-id="${_escAttr(ag.id)}" title="Open a chat with this agent">`
        + `<span class="dash-oprow-ico"><i data-lucide="${_escAttr(ag.icon || 'bot')}" class="dash-ico-15"></i></span>`
        + `<span class="dash-oprow-main"><span class="dash-oprow-name">${_esc(ag.name)}</span>`
        + `<span class="dash-oprow-sub">${_esc(ag.model || 'inherited model')}</span></span>`
        + (ag.running ? `<span class="dash-pill tone-success">${fmtInt(ag.running)} live</span>` : '')
        + `<span class="dash-oprow-val">${fmtNum(ag.tokens)} tok<span class="dash-oprow-vsub">${fmtCost(ag.cost_usd)}</span></span>`
        + `</div>`).join('')
      + `</div><div class="dash-oplist-foot">${fmtInt(a.total)} agents · usage over ${windowLabel()}</div></div>`;
  },
};

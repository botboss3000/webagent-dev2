'use strict';

/* sessions_monitor — Sessions & Runs card plugin (running first, then recent).
   Rows carry data-act open-session / stop-run handled by the shell's grid click.
   Reads the `sessions` snapshot section built by this card's server.py.
   REMOVE-WHEN: the Dashboard tab is dropped from the Instances page. */

import { _esc, _escAttr } from '../_lib/card-lib.js';
import { fmtInt, fmtDur, fmtCost, agoLabel, windowLabel } from '../_lib/card-lib.js';

export default {
  render(s) {
    const se = s.sessions || {};
    const rows = se.list || [];
    if (!rows.length) return `<div class="dash-muted">No sessions yet.</div>`;
    return `<div class="dash-opwrap"><div class="dash-oplist">`
      + rows.map(r => {
        const dot = r.running
          ? `<span class="dash-dot ${r.stale ? 'stale' : 'run'}"></span>`
          : `<span class="dash-dot off"></span>`;
        const right = r.running
          ? `<span class="dash-oprow-val tone-success">${fmtDur(r.running_s || 0)}${r.stale ? ' · stalled?' : ' · live'}<span class="dash-oprow-vsub">${fmtCost(r.cost_usd)}</span></span>`
            + `<button type="button" class="dash-oprow-btn danger" data-act="stop-run" data-id="${_escAttr(r.id)}" title="Stop this run"><i data-lucide="square" class="dash-ico-12"></i></button>`
          : `<span class="dash-oprow-val">${agoLabel(r.updated_s)}<span class="dash-oprow-vsub">${fmtCost(r.cost_usd)}</span></span>`;
        return `<div class="dash-oprow dash-op-click" data-act="open-session" data-id="${_escAttr(r.id)}" title="Open this session in chat">`
          + dot
          + `<span class="dash-oprow-main"><span class="dash-oprow-name">${_esc(r.title)}</span>`
          + `<span class="dash-oprow-sub">${_esc(r.agent_id || '—')} · ${_esc(String(r.user_id || '').slice(0, 18))}</span></span>`
          + right + `</div>`;
      }).join('')
      + `</div><div class="dash-oplist-foot">${fmtInt(se.active)} running · cost over ${windowLabel()}</div></div>`;
  },
};

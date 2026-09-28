'use strict';

/* failures — Recent Failures card plugin (error/critical diagnostics).
   Reads the `failures` snapshot section built by this card's server.py.
   REMOVE-WHEN: the Dashboard tab is dropped from the Instances page. */

import { _esc } from '../_lib/card-lib.js';
import { fmtInt, windowLabel } from '../_lib/card-lib.js';

export default {
  render(s) {
    const f = s.failures || {};
    const head = `<div class="dash-fail-head"><span class="dash-stat-big tone-${(f.count ? 'danger' : 'success')}">${fmtInt(f.count)}</span><span class="dash-stat-sub">errors · ${windowLabel()}</span></div>`;
    const rows = (f.recent || []).slice(0, 6).map(r =>
      `<div class="dash-fail-row"><span class="dash-pill tone-${r.level === 'critical' ? 'danger' : 'warning'}">${_esc(r.category || r.level)}</span><span class="dash-fail-msg">${_esc(r.message || '')}</span></div>`
    ).join('');
    return head + (rows ? `<div class="dash-fail-list">${rows}</div>` : `<div class="dash-muted">No failures — all clear.</div>`);
  },
};

'use strict';

/* security — Security & Sign-ins card plugin (auth diagnostics).
   Reads the `security` snapshot section built by this card's server.py.
   REMOVE-WHEN: the Dashboard tab is dropped from the Instances page. */

import { _esc } from '../_lib/card-lib.js';
import { fmtInt, agoIso, windowLabel } from '../_lib/card-lib.js';

export default {
  render(s) {
    const sec = s.security || {};
    const head = `<div class="dash-ophead">`
      + `<span class="tone-success"><b>${fmtInt(sec.signins)}</b> sign-ins</span>`
      + `<span class="${sec.failed ? 'tone-danger' : ''}"><b>${fmtInt(sec.failed)}</b> failed / blocked</span>`
      + `</div>`;
    const rows = (sec.recent || []).map(r => {
      const bad = ['warning', 'error', 'critical'].includes(r.level);
      return `<div class="dash-oprow">`
        + `<span class="dash-dot ${bad ? 'hb-warn' : 'hb-ok'}"></span>`
        + `<span class="dash-oprow-main"><span class="dash-oprow-name dash-oprow-wrap">${_esc(r.message)}</span></span>`
        + `<span class="dash-oprow-val">${_esc(agoIso(r.ts))}</span>`
        + `</div>`;
    }).join('');
    return `<div class="dash-opwrap">${head}<div class="dash-oplist">${rows || `<div class="dash-muted">No sign-in activity in ${windowLabel()}.</div>`}</div></div>`;
  },
};

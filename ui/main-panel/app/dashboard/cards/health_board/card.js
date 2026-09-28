'use strict';

/* health_board — System Health card plugin (status-light board).
   Reads the `health` snapshot section built by this card's server.py.
   REMOVE-WHEN: the Dashboard tab is dropped from the Instances page. */

import { _esc, _escAttr } from '../_lib/card-lib.js';

export default {
  render(s) {
    const rows = s.health || [];
    if (!rows.length) return `<div class="dash-muted">No health checks reported.</div>`;
    return `<div class="dash-opwrap"><div class="dash-oplist">`
      + rows.map(h =>
        `<div class="dash-oprow">`
        + `<span class="dash-dot hb-${_escAttr(h.state || 'off')}"></span>`
        + `<span class="dash-oprow-main"><span class="dash-oprow-name">${_esc(h.label)}</span>`
        + (h.detail ? `<span class="dash-oprow-sub">${_esc(h.detail)}</span>` : '')
        + `</span>`
        + `<span class="dash-oprow-val">${_esc(h.value == null ? '—' : String(h.value))}</span>`
        + `</div>`).join('')
      + `</div></div>`;
  },
};

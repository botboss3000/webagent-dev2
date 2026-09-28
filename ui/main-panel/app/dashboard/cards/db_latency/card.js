'use strict';

/* db_latency — Database Latency card plugin (avg/p95/calls + connection bar).
   Reads the shell-built `live.db` gauges + `db_health` connection info.
   REMOVE-WHEN: the Dashboard tab is dropped from the Instances page. */

import { _esc } from '../_lib/card-lib.js';
import { stat, fmtMs, fmtNum, getPath, latTone } from '../_lib/card-lib.js';

export default {
  render(s) {
    const d = getPath(s, 'live.db') || {};
    const h = s.db_health || {};
    const prov = h.provider || 'sqlite';
    const host = h.host || 'localhost';
    const hybrid = h.hybrid ? ' · hybrid' : '';
    const tone = h.degraded ? 'danger' : (h.ok ? 'success' : 'warning');
    const statusLabel = h.degraded ? 'degraded'
      : h.remote ? 'connected' : 'local';
    return `<div class="dash-dblat">`
      + `<div class="dash-trio">`
      + `<div>${stat(fmtMs(d.avg_ms), 'avg', latTone(d.avg_ms))}</div>`
      + `<div>${stat(fmtMs(d.p95_ms), 'p95', latTone(d.p95_ms))}</div>`
      + `<div>${stat(fmtNum(d.rate_per_min), 'calls/min', 'brand')}</div></div>`
      + `<div class="dash-dblat-conn">`
      + `<span class="dash-pill tone-brand">${_esc(prov)}</span>`
      + `<span class="dash-dblat-host">${_esc(host)}</span>`
      + `<span class="dash-pill tone-${tone}">${_esc(statusLabel)}${hybrid}</span>`
      + `</div></div>`;
  },
};

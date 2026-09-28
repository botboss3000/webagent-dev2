'use strict';

/* devices — Devices card plugin (fleet presence, online/offline).
   Reads the `devices` snapshot section built by this card's server.py.
   REMOVE-WHEN: the Dashboard tab is dropped from the Instances page. */

import { _esc } from '../_lib/card-lib.js';
import { list, fmtDur } from '../_lib/card-lib.js';

export default {
  render(s) {
    const ds = s.devices || [];
    if (!ds.length) return `<div class="dash-muted">No devices reporting.</div>`;
    return list(ds.map(d => ({
      k: `<span class="dash-dot ${d.online ? 'on' : 'off'}"></span>${_esc(d.label)} <span class="dash-muted-inline">${_esc(d.platform)}</span>`,
      v: d.online ? 'online' : (d.last_seen_s != null ? fmtDur(d.last_seen_s) + ' ago' : 'offline'),
      tone: d.online ? 'success' : null,
    })));
  },
};

'use strict';

/* tokens_by_agent — Tokens by Agent card plugin (top agents by volume).
   Reads the shell-built `tokens.by_agent` breakdown.
   REMOVE-WHEN: the Dashboard tab is dropped from the Instances page. */

import { _esc } from '../_lib/card-lib.js';
import { bars, fmtNum, getPath, windowLabel } from '../_lib/card-lib.js';

export default {
  render(s) {
    const rows = (getPath(s, 'tokens.by_agent') || []).map(a => ({
      label: _esc(String(a.agent).slice(0, 16)), value: (a.in || 0) + (a.out || 0),
      display: fmtNum((a.in || 0) + (a.out || 0)), tone: 'purple',
    }));
    return rows.length ? bars(rows) : `<div class="dash-muted">No usage in ${windowLabel()}.</div>`;
  },
};

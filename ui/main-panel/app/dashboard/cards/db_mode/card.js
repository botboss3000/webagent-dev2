'use strict';

/* db_mode — Database mode card plugin (live backend + silent-degradation flag).
   Reads the shell-built `db_health` snapshot section.
   REMOVE-WHEN: the Dashboard tab is dropped from the Instances page. */

import { stat } from '../_lib/card-lib.js';

export default {
  render(s) {
    const h = s.db_health || {};
    const tone = h.degraded ? 'danger' : (h.ok ? 'success' : 'warning');
    const badge = h.degraded ? 'DEGRADED → local' : (h.remote ? 'remote · connected' : 'local');
    return stat((h.actual || '—').toUpperCase(), `<span class="dash-pill tone-${tone}">${badge}</span>`, tone);
  },
};

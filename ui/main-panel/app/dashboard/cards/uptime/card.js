'use strict';

/* uptime — Uptime card plugin (since last restart, live).
   REMOVE-WHEN: the Dashboard tab is dropped from the Instances page. */

import { stat, fmtDur, getPath } from '../_lib/card-lib.js';

export default {
  render(s) {
    return stat(fmtDur(getPath(s, 'live.uptime_s')), 'since last restart', 'success');
  },
};

'use strict';

/* cost — Estimated Spend card plugin.
   Reads the shell-built `tokens` snapshot section (usage_events aggregate).
   REMOVE-WHEN: the Dashboard tab is dropped from the Instances page. */

import { stat, fmtCost, fmtInt, getPath, windowLabel } from '../_lib/card-lib.js';

export default {
  render(s) {
    return stat(fmtCost(getPath(s, 'tokens.cost_usd')), `${fmtInt(getPath(s, 'tokens.calls'))} calls · ${windowLabel()}`, 'brand');
  },
};

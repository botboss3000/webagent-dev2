'use strict';

/* memory — Memory card plugin (resident set size, live).
   REMOVE-WHEN: the Dashboard tab is dropped from the Instances page. */

import { stat, fmtNum } from '../_lib/card-lib.js';

export default {
  render(s) {
    return stat(fmtNum(s.memory_mb) + ' <span class="dash-unit">MB</span>', 'resident set', 'purple');
  },
};

'use strict';

/* cpu — CPU gauge card plugin (process utilisation, live).
   REMOVE-WHEN: the Dashboard tab is dropped from the Instances page. */

import { gauge, getPath, cpuTone } from '../_lib/card-lib.js';

export default {
  render(s) {
    return gauge(getPath(s, 'live.cpu_percent'), 'process utilisation', cpuTone(getPath(s, 'live.cpu_percent')));
  },
};

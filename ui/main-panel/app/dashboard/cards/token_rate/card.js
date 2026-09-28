'use strict';

/* token_rate — Token Throughput card plugin (tokens/min, live).
   REMOVE-WHEN: the Dashboard tab is dropped from the Instances page. */

import { stat, fmtNum, getPath } from '../_lib/card-lib.js';

export default {
  render(s) {
    return stat(fmtNum(getPath(s, 'live.llm.tokens_per_min')), 'tokens / min', 'purple');
  },
};

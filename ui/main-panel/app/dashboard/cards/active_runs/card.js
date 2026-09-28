'use strict';

/* active_runs — Active Agent Runs card plugin.
   Reads the shell-built `active_runs` count + the `live` LLM rate.
   REMOVE-WHEN: the Dashboard tab is dropped from the Instances page. */

import { stat, fmtInt, fmtNum, getPath } from '../_lib/card-lib.js';

export default {
  render(s) {
    return stat(fmtInt(getPath(s, 'active_runs')), `${fmtNum(getPath(s, 'live.llm.rate_per_min'))} LLM calls/min`, 'success');
  },
};

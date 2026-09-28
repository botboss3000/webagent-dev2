'use strict';

/* loop_split — Where the loop spends time card plugin (LLM vs DB % bars).
   REMOVE-WHEN: the Dashboard tab is dropped from the Instances page. */

import { bars, getPath } from '../_lib/card-lib.js';

export default {
  render(s) {
    const sp = getPath(s, 'live.loop_split') || {};
    return bars([
      { label: 'LLM', value: sp.llm_pct, display: (sp.llm_pct || 0) + '%', tone: 'purple' },
      { label: 'Database', value: sp.db_pct, display: (sp.db_pct || 0) + '%', tone: 'brand' },
    ]);
  },
};

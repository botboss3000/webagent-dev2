'use strict';

/* context — Context Window card plugin (default model + caps).
   Reads the `context` snapshot section built by this card's server.py.
   REMOVE-WHEN: the Dashboard tab is dropped from the Instances page. */

import { _esc } from '../_lib/card-lib.js';
import { list, fmtNum } from '../_lib/card-lib.js';

export default {
  render(s) {
    const c = s.context || {};
    return list([
      { k: 'Model', v: _esc(c.model || '—') },
      { k: 'Max input', v: c.max_input ? fmtNum(c.max_input) + ' tok' : '—' },
      { k: 'Max output', v: c.max_output ? fmtNum(c.max_output) + ' tok' : '—' },
    ]);
  },
};

'use strict';

/* my_card — My Card plugin (copy of _template). See cards/README.md for the
   drop-in contract. REMOVE-WHEN: the Dashboard tab is dropped. */

import { stat, fmtNum, getPath } from '../_lib/card-lib.js';

export default {
  render(s, ctx) {
    // Only the sections listed in card.json `sections` are guaranteed present.
    return stat(fmtNum(getPath(s, 'live.cpu_percent')), 'example — read a section', 'brand');
  },
};

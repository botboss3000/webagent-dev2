'use strict';

/* tokens — Token Usage card plugin (tokens in / out over the window).
   Reads the shell-built `tokens` snapshot section.
   REMOVE-WHEN: the Dashboard tab is dropped from the Instances page. */

import { stat, fmtNum, getPath } from '../_lib/card-lib.js';

export default {
  render(s) {
    return `<div class="dash-duo"><div>${stat(fmtNum(getPath(s, 'tokens.in')), 'tokens in', 'purple')}</div>`
      + `<div>${stat(fmtNum(getPath(s, 'tokens.out')), 'tokens out', 'brand')}</div></div>`;
  },
};

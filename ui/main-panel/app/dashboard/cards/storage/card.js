'use strict';

/* storage — Storage card plugin (data dir file sizes + disk free).
   Reads the shell-built `storage` snapshot section.
   REMOVE-WHEN: the Dashboard tab is dropped from the Instances page. */

import { _esc } from '../_lib/card-lib.js';
import { list } from '../_lib/card-lib.js';

export default {
  render(s) {
    const st = s.storage || {};
    const rows = (st.files || []).map(f => ({ k: _esc(f.name), v: f.mb + ' MB' }));
    if (st.disk_free_gb != null) rows.push({ k: 'Disk free', v: st.disk_free_gb + ' / ' + st.disk_total_gb + ' GB', tone: 'success' });
    return list(rows);
  },
};

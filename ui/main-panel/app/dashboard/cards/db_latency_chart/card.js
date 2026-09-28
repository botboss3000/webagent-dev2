'use strict';

/* db_latency_chart — DB latency trend card plugin (sparkline).
   The SHELL refreshes this card on its chart cadence (card.json `chart: "db"`
   → GET /admin/dashboard/metrics/timeseries?kind=db) and hands the points to
   render() via ctx.ts.
   REMOVE-WHEN: the Dashboard tab is dropped from the Instances page. */

import { sparkline } from '../_lib/card-lib.js';

export default {
  render(s, ctx) {
    const pts = (ctx && ctx.ts && ctx.ts.points) || [];
    if (!pts.length) return `<div class="dash-muted">Collecting samples…</div>`;
    return sparkline(pts, 'brand');
  },
};

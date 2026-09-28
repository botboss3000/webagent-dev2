'use strict';

/* tool_usage — Tool & Model Usage card plugin (top tools + per-model).
   Reads the `tool_usage` snapshot section built by this card's server.py.
   REMOVE-WHEN: the Dashboard tab is dropped from the Instances page. */

import { _esc } from '../_lib/card-lib.js';
import { fmtInt, fmtNum, fmtMs, fmtCost, windowLabel } from '../_lib/card-lib.js';

export default {
  render(s) {
    const t = s.tool_usage || {};
    const tools = t.tools || [];
    const models = t.models || [];
    const maxCalls = Math.max(1, ...tools.map(x => x.calls || 0));
    const toolRows = tools.map(x =>
      `<div class="dash-tm-row">`
      + `<span class="dash-tm-name">${_esc(x.name)}</span>`
      + `<span class="dash-bar-track"><span class="tone-brand dash-bar-fill" data-bar-pct="${Math.round((x.calls || 0) / maxCalls * 100)}"></span></span>`
      + `<span class="dash-tm-val">${fmtInt(x.calls)}${x.failures ? ` <span class="tone-danger">· ${x.fail_pct}% fail</span>` : ''} · ${fmtMs(x.avg_ms)}</span>`
      + `</div>`).join('');
    const modelRows = models.map(m =>
      `<div class="dash-tm-row no-track">`
      + `<span class="dash-tm-name">${_esc(m.model)}</span>`
      + `<span class="dash-tm-val">${fmtInt(m.calls)} calls · ${fmtNum(m.tokens)} tok · ${fmtCost(m.cost_usd)}</span>`
      + `</div>`).join('');
    if (!tools.length && !models.length) return `<div class="dash-muted">No tool or model activity in ${windowLabel()}.</div>`;
    return `<div class="dash-opwrap"><div class="dash-oplist">`
      + (tools.length ? `<div class="dash-oplist-cap">Tools · ${fmtInt(t.tool_calls)} calls${t.tool_failures ? ` · <span class="tone-danger">${fmtInt(t.tool_failures)} failed</span>` : ''}</div>` + toolRows : '')
      + (models.length ? `<div class="dash-oplist-cap">Models</div>` + modelRows : '')
      + `</div><div class="dash-oplist-foot">${windowLabel()}</div></div>`;
  },
};

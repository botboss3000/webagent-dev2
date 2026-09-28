'use strict';

/* user_mgmt — User Management card plugin (counts + recent accounts).
   Approve/reject buttons carry data-act handled by the shell's grid click.
   Reads the `users` snapshot section built by this card's server.py.
   REMOVE-WHEN: the Dashboard tab is dropped from the Instances page. */

import { _esc, _escAttr } from '../_lib/card-lib.js';
import { fmtInt, agoLabel, windowLabel } from '../_lib/card-lib.js';

export default {
  render(s) {
    const u = s.users || {};
    const head = `<div class="dash-ophead">`
      + `<span><b>${fmtInt(u.total)}</b> users</span>`
      + `<span><b>${fmtInt(u.admins)}</b> admins</span>`
      + `<span class="${u.pending ? 'tone-warning' : ''}"><b>${fmtInt(u.pending)}</b> pending</span>`
      + (u.new_in_window ? `<span class="tone-success"><b>+${fmtInt(u.new_in_window)}</b> ${windowLabel()}</span>` : '')
      + `</div>`;
    const rows = (u.recent || []).map(r =>
      `<div class="dash-oprow">`
      + `<span class="dash-oprow-ico"><i data-lucide="${r.admin ? 'shield' : 'user'}" class="dash-ico-15"></i></span>`
      + `<span class="dash-oprow-main"><span class="dash-oprow-name">${_esc(r.name)}</span>`
      + `<span class="dash-oprow-sub">${_esc(r.username)}</span></span>`
      + (r.admin ? `<span class="dash-pill tone-brand">admin</span>` : '')
      + (!r.approved
        ? `<span class="dash-oprow-acts">`
          + `<button type="button" class="dash-oprow-btn ok" data-act="approve-user" data-id="${_escAttr(r.user_id)}" title="Approve this account"><i data-lucide="check" class="dash-ico-13"></i></button>`
          + `<button type="button" class="dash-oprow-btn danger" data-act="reject-user" data-id="${_escAttr(r.user_id)}" title="Reject &amp; delete this account"><i data-lucide="x" class="dash-ico-13"></i></button>`
          + `</span>`
        : `<span class="dash-oprow-val">${agoLabel(r.last_login_s)}</span>`)
      + `</div>`).join('');
    return `<div class="dash-opwrap">${head}<div class="dash-oplist">${rows || '<div class="dash-muted">No users yet.</div>'}</div></div>`;
  },
};

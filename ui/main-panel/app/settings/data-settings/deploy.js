'use strict';

/**
 * Deploy — the install-a-new-copy engine behind the Instances page's "New
 * instance" tile ("New Deployment" tab, ui/main-panel/app/new-deployment/)
 * and the Export/Import setup-bundle bars in Data Settings → Database → Data
 * Migration. (It NO LONGER backs a "Deployment card" — that Data Settings section
 * was removed; its hold-to-restart + port editor MOVED to the Instances page's
 * "This device" tile's Device fact, and its sibling local checkouts became
 * first-class grid TILES — both re-implemented natively in
 * ui/main-panel/app/instances.js (`_hubPortFactHtml` + `_localTiles` /
 * `_renderLocalOverview`). The list/restart
 * functions below — _loadInstances / _renderInstances / _instBar / _hubBody /
 * _siblingBody / _onInstClick / _instAction / _instRemove / _instSave / _hubSave /
 * _beginResetHold & the reset-hold helpers — are therefore ORPHANED: no
 * #ac-deploy-instances host exists on any page any more, so they never render/fire.
 * The register-a-checkout form functions (_registerAdd / _checkRegisterFolder /
 * _seedRegisterPort / _nextFreePort) STAY LIVE — the New Deployment tab's "This
 * device" target still uses them.
 * DEACTIVATED (orphaned): the instances-LIST + hub-RESTART machinery.
 * REMOVE-WHEN: a cleanup pass excises the orphaned list/restart functions above.)
 *
 * Live one-click deploy of this app onto a cloud target (Google VM today; AWS,
 * a plain Linux box and Docker are drop-in targets to follow). Talks to
 * /admin/deploy/* : lists the discovered targets + their forms, saves the
 * non-secret settings, saves the cloud key into the encrypted vault, tests the
 * connection, and streams the deploy / tear-down as NDJSON into a live log.
 *
 * The cloud key is used for one deploy then auto-discarded (the per-target
 * "Forget keys after deploy" setting, default on). Admin-only; the Deploy and
 * Tear-down actions are confirm-gated (they create / delete billable resources).
 *
 * Colours use design-system variables so the card is correct in dark + light.
 * Mirrors the structure of ui/shared/js/remote-access.js.
 */

import { apiPath } from '../../../../shared/js/config.js';
import { authHeaders, isAdmin } from '../../../../shared/js/left-login.js?v=253';
import { _refreshLucideIcons, _esc, _escAttr } from '../../../../shared/js/dom-utils.js';
import { copyText } from '../../../../shared/js/clipboard.js';
import { showQrPopup, closeQrPopup } from '../../../../shared/js/qr-popup.js';

let _catalog = null;        // last /catalog payload
let _busy = false;          // a deploy / tear-down stream is running

// Typed cloud-form values carried across a save / activate reload. A successful
// deploy auto-forgets the cloud key (default on), which would make _load() re-render
// the form empty and — for a staged target (Google VM) — collapse the key/VM fields
// + action buttons back to stage 1. We snapshot what the admin typed before the
// reload and re-apply it in _renderProvider, so the JSON key stays put and the
// staged fields stay revealed. Cleared on a real provider switch (see _onProviderChange).
let _preserve = null;       // {provider, cfg:{}, cred:{}} | null

// ── Manual install row state (one QR popover open at a time across all rows) ──
let _qrTimer = null;        // debounce timer for the (server-side) QR refresh
const _embedTimers = {};    // per-row debounce for the server-built command (embed mode)
let _qrHandle = null;       // { setPlate } from last showQrPopup call
let _qrAnchor = null;       // the QR button it was opened from
let _qrDesc = null;         // which manual-row descriptor the open QR belongs to

function _qs(id) { return document.getElementById(id); }
function _userId() { try { return localStorage.getItem('auth_user_id') || ''; } catch { return ''; } }
function _provider() { return (_qs('ac-deploy-provider') || {}).value || ''; }
function _current() { return (_catalog && (_catalog.providers || []).find(p => p.id === _provider())) || null; }

// ── Shared repo details ──────────────────────────────────────────────────────
// The repo choice lives ONCE in the Repo-details bar (#ac-deploy-repo-*) and is
// carried into EVERY target: the manual command boxes read it (via _manualInputs),
// and the cloud deploy has it injected into its config/creds (see _saveSettings).
// The token + admin password are never stored; only the URL + visibility persist
// (to the reserved "_repo" slot) so the bar pre-fills next time.
function _readSharedRepo() {
  return {
    github_url: (_qs('ac-deploy-repo-url')?.value || '').trim(),
    // The Public/Private choice is auto-detected (no manual picker). It follows the
    // last GitHub access-check verdict (_lastProbe): only a CONFIRMED-public repo is
    // public; everything else (private, needs-auth, unknown) counts as private so a
    // token is used to clone. A blank verdict = the default standard repo = public.
    visibility: (_lastProbe && _lastProbe !== 'public') ? 'private' : 'public',
    token: (_qs('ac-deploy-repo-token')?.value || '').trim(),
    branch: (_qs('ac-deploy-repo-branch')?.value || '').trim(),
    // Blank unless the "Set an admin password" box is ticked (and not locked): the
    // password field is revealed only then. When the shared database is carried the
    // box is disabled, so the login rides from THIS install — see _syncSharedDbLocks.
    admin_password: (() => {
      const chk = _qs('ac-deploy-pw-check'), fld = _qs('ac-deploy-repo-admin-pw');
      return (chk && chk.checked && !chk.disabled && fld) ? fld.value : '';
    })(),
    // Carry AI keys / vault / database onto the new install. There is no separate
    // bare/include switch anymore — including is simply "any config box ticked",
    // and the ticked boxes ARE the sections (empty list = a bare install).
    embed_config: _embedSections().length > 0,
    embed_sections: _embedSections(),
  };
}

// The ticked config sections (database / vault / llm) in the Configuration bar.
function _embedSections() {
  return Array.from(document.querySelectorAll('#ac-deploy-config-row [data-embed-sec]:checked'))
    .map(el => el.dataset.embedSec);
}

// Refresh everything that hangs off the Configuration boxes: the shared-DB locks,
// the password field's visibility, the note + the header mode line. The bare/include
// dropdown is gone — the boxes alone drive the mode — so this just re-derives state.
function _syncEmbedMode() {
  _syncSharedDbLocks();          // locks + reveals the password field (via _syncPwField)
  _updateEmbedNote();
  _updateRepoModeLine();
}

// The admin-password field is revealed only while its "Set an admin password" box is
// ticked and enabled (the box is disabled when the shared database carries the login).
function _syncPwField() {
  const chk = _qs('ac-deploy-pw-check');
  const wrap = _qs('ac-deploy-pw-wrap');
  if (wrap) wrap.hidden = !(chk && chk.checked && !chk.disabled);
}

// When the new install will share THIS app's database AND vault (both ticked), the
// admin login + AI key ride along automatically, so the two dependent boxes are
// locked because they're handled for you:
//   • Admin password — logins live in the shared database's user_accounts table, so a
//     box pointed at it already has this admin and reuses it; nothing to pre-set. The
//     checkbox is disabled + unticked (its field hidden) and a note explains why.
//   • AI model & key — already reachable via the shared database + vault, so the box
//     is greyed but kept TICKED: the backend still ships whatever the new box can't
//     resolve on its own, so un-including it here would risk a box with no model.
function _syncSharedDbLocks() {
  const dbCb = document.querySelector('#ac-deploy-config-row [data-embed-sec="database"]');
  const vaultCb = document.querySelector('#ac-deploy-config-row [data-embed-sec="vault"]');
  const shared = !!(dbCb && dbCb.checked && vaultCb && vaultCb.checked);

  // AI model & key box.
  const llmCb = document.querySelector('#ac-deploy-config-row [data-embed-sec="llm"]');
  if (llmCb) {
    llmCb.disabled = shared;
    if (shared) llmCb.checked = true;        // stays included; backend decides what to ship
    const row = llmCb.closest('.ac-boot-check');
    if (row) row.classList.toggle('ac-op07', shared);
  }
  const llmNote = _qs('ac-deploy-llm-lock-note');
  if (llmNote) llmNote.classList.toggle('ac-hidden', !shared);

  // Admin-password box + its field.
  const pwCb = _qs('ac-deploy-pw-check');
  if (pwCb) {
    pwCb.disabled = shared;
    if (shared) pwCb.checked = false;        // carried login — nothing to pre-set
    const row = pwCb.closest('.ac-boot-check');
    if (row) row.classList.toggle('ac-op07', shared);
  }
  const pwField = _qs('ac-deploy-repo-admin-pw');
  if (pwField && shared) pwField.value = '';
  _syncPwField();
  const pwNote = _qs('ac-deploy-admin-pw-note');
  if (pwNote) { pwNote.classList.toggle('ac-hidden', !shared); if (shared) _refreshLucideIcons(pwNote); }
}

// The Configuration row's second line is a live config-mode indicator, visible
// without expanding the row: "Bare config" (nothing carried) or "Includes this app’s
// config" (any config box ticked — set by the clone button or by hand). Via _syncEmbedMode.
function _updateRepoModeLine() {
  const el = _qs('ac-deploy-repo-mode');
  if (!el) return;
  el.textContent = _embedSections().length ? 'Includes this app’s config' : 'Bare config';
}

// The live note under the config boxes. Carrying configuration needs no admin
// password (the bundle is keyless — see app/deploy/config_embed); the note just
// confirms what will be carried onto the new install.
function _updateEmbedNote() {
  const note = _qs('ac-deploy-embed-note');
  if (!note) return;
  if (!_embedSections().length) { note.textContent = ''; return; }
  note.classList.remove('ac-fg-danger','ac-fg-success','ac-fg3');
  note.textContent = 'Ready — the ticked settings are packaged into the install command and set up on the new install automatically on first open.';
}
// "Clone current repo" — the icon button in the Repo-to-deploy row header is a
// TOGGLE (see _toggleCloneCurrentRepo, exposed as window.__deployToggleClone and
// driven single-click from new-deployment.js). ON: fill the bar from the repo the
// Git Control page is pointed at (GET /admin/deploy/current-repo) — its origin
// URL, current branch, and — when it's a private repo with a stored token — the
// token + Private visibility, plus tick this app's config to carry along. OFF: revert
// to the default production repo (bare). The token rides the (masked, never-persisted)
// token field exactly like a typed one. Feedback rides the header URL text
// (`_flashRepoHead`) since the header has no status line.
let _repoHeadTimer = null;
function _flashRepoHead(text, isErr) {
  const head = _qs('ac-deploy-repo-head');
  if (!head) return;
  if (_repoHeadTimer) { clearTimeout(_repoHeadTimer); _repoHeadTimer = null; }
  head.textContent = ': ' + text;
  head.classList.toggle('ac-fg-danger', isErr);
  // Restore the real repo URL after the transient message.
  _repoHeadTimer = setTimeout(() => { head.classList.remove('ac-fg-danger'); _updateRepoHead(); }, 3500);
}

// Tick/untick every config section box (database / vault / llm) that isn't locked.
function _setEmbedChecks(on) {
  document.querySelectorAll('#ac-deploy-config-row [data-embed-sec]').forEach(cb => {
    if (!cb.disabled) cb.checked = on;
  });
}

// The header button's two-state entry point. When the button is already ON (a clone
// is in effect) a click reverts to the production repo; otherwise it clones.
async function _toggleCloneCurrentRepo() {
  if (!isAdmin()) return;
  const btn = _qs('ac-deploy-repo-clone');
  if (btn && btn.classList.contains('ac-deploy-repo-clone-on')) { _revertToProductionRepo(); return; }
  await _cloneCurrentRepo();
}

// Revert the Repo + Configuration bars to the default production-repo state: clear the
// URL / branch / token (the placeholder then shows the standard repo), hide the token
// field, and untick everything carried (bare install). The mirror image of a clone.
function _revertToProductionRepo() {
  const url = _qs('ac-deploy-repo-url'); if (url) url.value = '';
  const branch = _qs('ac-deploy-repo-branch'); if (branch) branch.value = '';
  const token = _qs('ac-deploy-repo-token'); if (token) token.value = '';
  _showTokenField(false);
  _setEmbedChecks(false);
  const pwCb = _qs('ac-deploy-pw-check'); if (pwCb) pwCb.checked = false;
  const pwField = _qs('ac-deploy-repo-admin-pw'); if (pwField) pwField.value = '';
  _syncEmbedMode();
  _updateRepoHead();
  _probeRepo();                  // re-detect access for the (now default) repo
  const desc = MANUAL_ROWS.find(d => d.id === (_qs('ac-deploy-target')?.value || ''));
  if (desc) _manualRender(desc);
  _persistSharedRepo();
  const btn = _qs('ac-deploy-repo-clone'); if (btn) btn.classList.remove('ac-deploy-repo-clone-on');
  _flashRepoHead('using the production repository');
}

async function _cloneCurrentRepo() {
  if (!isAdmin()) return;
  const btn = _qs('ac-deploy-repo-clone');
  if (btn) { btn.disabled = true; btn.classList.add('ac-deploy-repo-clone-busy'); }
  _flashRepoHead('reading current repo…');
  try {
    const r = await _get('/current-repo');
    if (!r.ok || !r.github_url) { _flashRepoHead('no repository URL found for the current repo', true); return; }
    const url = _qs('ac-deploy-repo-url'); if (url) url.value = r.github_url;
    const branch = _qs('ac-deploy-repo-branch'); if (branch && r.branch) branch.value = r.branch;
    const token = _qs('ac-deploy-repo-token'); if (token) token.value = r.token || '';
    // A stored token came back → reveal the (now pre-filled) token field right away,
    // so the real key is visible while the probe runs; the probe then confirms it.
    if ((r.token || '').trim()) _showTokenField(true);
    _probeRepo();                // auto-detect public/private for the cloned URL (+token)
    _updateRepoHead();           // header now shows the cloned URL
    // Cloning THIS install means carrying its configuration onto the new one too —
    // tick the config boxes (the header's "Includes this app’s config" line updates).
    // No admin password is needed for the config — the bundle is keyless (config_embed).
    _setEmbedChecks(true);
    _syncEmbedMode();
    if (btn) btn.classList.add('ac-deploy-repo-clone-on');   // toggle is now ON
    // Re-render whichever manual command is showing so it picks up the new repo.
    const desc = MANUAL_ROWS.find(d => d.id === (_qs('ac-deploy-target')?.value || ''));
    if (desc) _manualRender(desc);
    _persistSharedRepo();        // keep the non-secret URL/visibility/branch (token isn't saved)
    // A private repo with no stored token needs one typed below — nudge via the head.
    if (r.visibility === 'private' && !r.has_token) _flashRepoHead(r.github_url.replace(/^https?:\/\//, '') + ' — private, add a token', true);
  } catch (e) { _flashRepoHead(e.message, true); }
  finally { if (btn) { btn.disabled = false; btn.classList.remove('ac-deploy-repo-clone-busy'); } }
}

// ── Repository access auto-detection ─────────────────────────────────────────
// The Public/Private choice is no longer a manual dropdown. The app quietly asks
// GitHub about the entered URL (POST /admin/deploy/probe-repo) and shows the result:
//   • public        → green shield; no token needed; token field hidden.
//   • unknown       → 404 with no token (private OR wrong address): amber note +
//                     the token field slides in.
//   • authenticated → the pasted token can read a private repo: green "access
//                     confirmed" under the token field.
//   • denied        → the token can't reach it: red note under the token field.
//   • error         → GitHub didn't answer (rate-limit/network): neutral note; the
//                     token field is revealed anyway so a private deploy isn't blocked.
// The "visibility" the backend still needs (whether to inject a token when cloning)
// is derived purely from a token being present — see _readSharedRepo.
let _probeTimer = null;          // debounce for the URL/token-driven probe
let _lastProbe = '';             // last probe verdict (public/authenticated/…) — drives visibility
let _savedKeyOnFile = false;     // a reusable GitHub key is stored in the vault
let _savedKeyMasked = '';        // masked hint for that saved key (never the key)

function _repoStatusEl() { return _qs('ac-deploy-repo-status'); }
function _tokenStatusEl() { return _qs('ac-deploy-repo-token-status'); }

// Paint one status line with a lucide icon + message in a colour. Blank msg hides it.
function _setRepoLine(el, icon, msg, color) {
  if (!el) return;
  if (!msg) { el.classList.add('ac-hidden'); el.innerHTML = ''; return; }
  el.classList.remove('ac-hidden');
  if (color === 'var(--danger)') el.classList.add('ac-fg-danger'); else el.classList.remove('ac-fg-danger');
  el.innerHTML = (icon ? '<i data-lucide="' + icon + '" class="ac-ico-15 ac-flex-none"></i>' : '')
    + '<span>' + _esc(msg) + '</span>';
  _refreshLucideIcons(el);
}

// Show/hide the access-token field. Never hides it while it holds a typed token
// (the admin may be mid-deploy of a private repo).
function _showTokenField(on) {
  const wrap = _qs('ac-deploy-repo-token-wrap');
  if (!wrap) return;
  const hasToken = (_qs('ac-deploy-repo-token')?.value || '').trim();
  wrap.classList.toggle('ac-hidden', !(on || hasToken));
}

// Apply a probe result (the whole response) to the status lines, the token field's
// visibility, and the saved-key controls. `r.used_saved` = the stored key did the
// check (field was blank); `r.has_saved` = a reusable key is on file.
function _applyProbeState(r) {
  r = r || {};
  const state = r.state || 'unknown';
  const usedSaved = !!r.used_saved;
  _savedKeyOnFile = !!r.has_saved;
  const typedTok = (_qs('ac-deploy-repo-token')?.value || '').trim();

  if (state === 'public') {
    _setRepoLine(_repoStatusEl(), 'shield-check', 'Public repository — no access token needed.', 'var(--success)');
    _setRepoLine(_tokenStatusEl(), '', '');
    _showTokenField(false);
  } else if (state === 'authenticated') {
    _showTokenField(true);
    if (usedSaved) {
      _setRepoLine(_repoStatusEl(), 'shield-check', 'Private repository — confirmed with your saved GitHub key.', 'var(--success)');
      _setRepoLine(_tokenStatusEl(), '', '');
    } else {
      _setRepoLine(_repoStatusEl(), 'lock', 'Private repository.', 'var(--fg-muted)');
      _setRepoLine(_tokenStatusEl(), 'shield-check', 'Access confirmed — this token can read the repository.', 'var(--success)');
    }
  } else if (state === 'denied') {
    _showTokenField(true);
    _setRepoLine(_repoStatusEl(), 'alert-triangle', 'Couldn’t confirm this repository is public — it may be private or the address may be wrong.', 'var(--warning, var(--danger))');
    _setRepoLine(_tokenStatusEl(), 'x', usedSaved
      ? 'Your saved GitHub key can’t access this repository — paste one that can.'
      : 'This token can’t access that repository.', 'var(--danger)');
  } else if (state === 'error') {
    _setRepoLine(_repoStatusEl(), 'help-circle', r.detail || 'Couldn’t check right now.', 'var(--fg-muted)');
    _showTokenField(true);
    if (!typedTok) _setRepoLine(_tokenStatusEl(), '', '');
  } else {
    // unknown: private OR wrong address, and no key reached it — amber, reveal field.
    _setRepoLine(_repoStatusEl(), 'alert-triangle', 'Couldn’t confirm this repository is public — it may be private or the address may be wrong. Add an access token to continue.', 'var(--warning, var(--danger))');
    _showTokenField(true);
    _setRepoLine(_tokenStatusEl(), '', '');
  }
  _renderSavedKeyControls(state, typedTok);
}

// Run the detection for the current URL (+ token if the field holds one, else the
// vault-stored key server-side). A blank URL clears everything (the default standard
// repo is public). Guards against a stale response arriving after the admin has
// changed the URL again.
async function _probeRepo() {
  const url = (_qs('ac-deploy-repo-url')?.value || '').trim();
  if (!url) {
    _lastProbe = '';
    _setRepoLine(_repoStatusEl(), '', '');
    _setRepoLine(_tokenStatusEl(), '', '');
    _showTokenField(false);
    _renderSavedKeyControls('', '');
    return;
  }
  if (!isAdmin()) return;
  const token = (_qs('ac-deploy-repo-token')?.value || '').trim();
  _setRepoLine(_repoStatusEl(), 'loader', 'Checking repository…', 'var(--fg-muted)');
  try {
    const r = await _post('/admin/deploy/probe-repo', { github_url: url, token });
    if ((_qs('ac-deploy-repo-url')?.value || '').trim() !== url) return;   // superseded
    _lastProbe = r.state || 'unknown';
    _applyProbeState(r);
  } catch {
    _lastProbe = 'error';
    _applyProbeState({ state: 'error', detail: 'Couldn’t check right now.' });
  }
  // The verdict just set the effective visibility (+ whether a saved key applies), so
  // rebuild the active manual command to match (public → no token; private → token).
  const desc = MANUAL_ROWS.find(d => d.id === (_qs('ac-deploy-target')?.value || ''));
  if (desc) _manualRender(desc);
}

// Paint the saved-key line (always visible when a key is on file, so it can be
// Removed anytime) + the "Save key for reuse" button (only when a freshly-typed key
// just proved it can read a private repo, so we only ever offer to store a WORKING
// key). The Remove link and Save button are wired once in _initSharedRepo.
function _renderSavedKeyControls(state, typedTok) {
  const savedLine = _qs('ac-deploy-repo-saved-key');
  const savedText = _qs('ac-deploy-repo-saved-key-text');
  if (savedLine) {
    savedLine.classList.toggle('ac-hidden', !_savedKeyOnFile);
    if (savedText) savedText.textContent = _savedKeyMasked
      ? ('Saved GitHub key on file (' + _savedKeyMasked + ') — reused for private clones')
      : 'Saved GitHub key on file — reused for private clones';
    _refreshLucideIcons(savedLine);
  }
  const actions = _qs('ac-deploy-repo-token-actions');
  const saveBtn = _qs('ac-deploy-repo-token-save');
  const canSave = state === 'authenticated' && !!typedTok;   // a working, freshly-typed key
  if (saveBtn) saveBtn.textContent = _savedKeyOnFile ? 'Replace saved key' : 'Save key for reuse';
  if (actions) actions.classList.toggle('ac-hidden', !canSave);
}

// Load whether a reusable GitHub key is stored (admin-only; never the key itself).
async function _loadSavedKeyStatus() {
  if (!isAdmin()) return;
  try {
    const r = await _get('/github-token');
    _savedKeyOnFile = !!r.configured;
    _savedKeyMasked = r.masked || '';
  } catch { _savedKeyOnFile = false; _savedKeyMasked = ''; }
  _renderSavedKeyControls(_lastProbe, (_qs('ac-deploy-repo-token')?.value || '').trim());
}

// Store the typed token in the vault as the reusable key, then clear the field and
// re-check (so the status flips to the saved-key path and the secret leaves the DOM).
async function _saveTokenKey() {
  const tok = (_qs('ac-deploy-repo-token')?.value || '').trim();
  if (!tok) return;
  const btn = _qs('ac-deploy-repo-token-save');
  if (btn) btn.disabled = true;
  try {
    const r = await _post('/admin/deploy/github-token', { token: tok });
    _savedKeyOnFile = !!r.configured;
    _savedKeyMasked = r.masked || '';
    const t = _qs('ac-deploy-repo-token'); if (t) t.value = '';
    await _probeRepo();
  } catch (e) { _setRepoLine(_tokenStatusEl(), 'x', e.message, 'var(--danger)'); }
  finally { if (btn) btn.disabled = false; }
}

// Forget the stored reusable key, then re-check (a private repo falls back to amber).
async function _removeTokenKey(e) {
  if (e) e.preventDefault();
  try {
    await _post('/admin/deploy/github-token/clear', {});
    _savedKeyOnFile = false; _savedKeyMasked = '';
    await _probeRepo();
  } catch (er) { _setRepoLine(_tokenStatusEl(), 'x', er.message, 'var(--danger)'); }
}

// Debounced entry point used by the URL / token input handlers.
function _probeRepoSoon() {
  if (_probeTimer) clearTimeout(_probeTimer);
  _probeTimer = setTimeout(_probeRepo, 550);
}
// Echo the chosen repo on the collapsed Repo-details bar, so "Repo details: <repo>"
// is visible without expanding it. Blank → the standard WebAgent repo; the scheme
// is trimmed for brevity.
function _updateRepoHead() {
  const head = _qs('ac-deploy-repo-head');
  if (!head) return;
  const url = (_qs('ac-deploy-repo-url')?.value || '').trim() || _MC_DEFAULT_REPO;
  head.textContent = ': ' + url.replace(/^https?:\/\//, '');
}

// Cloud config/cred keys now OWNED by the shared Repo-details bar — skipped when
// rendering a cloud target's own forms, then injected on save/deploy (repo_url +
// branch + visibility into config; github_token + admin_password into the vault).
const SHARED_CLOUD_KEYS = new Set(['repo_url', 'branch', 'visibility', 'github_token', 'admin_password', 'github_url']);

// The five deploy-target panels revealed by the #ac-deploy-target dropdown. The
// three manual values (termux/windows/macos) match MANUAL_ROWS ids so a manual
// selection re-renders its command box.
const TARGET_PANELS = [
  { target: 'cloud', panel: 'ac-deploy-cloud-panel' },
  { target: 'local', panel: 'ac-deploy-local-panel' },
  { target: 'termux', panel: 'ac-deploy-linux-panel' },
  { target: 'windows', panel: 'ac-deploy-win-panel' },
  { target: 'macos', panel: 'ac-deploy-mac-panel' },
];
function _syncTargetPanel() {
  // The picker now lives in the row BODY, and the header chevron/title toggle owns the
  // row's open/close (new-deployment.js _wireRows). So this only switches which target
  // panel is shown: empty value hides them all; a real target reveals its own panel.
  const t = _qs('ac-deploy-target')?.value || '';
  TARGET_PANELS.forEach(p => { const el = _qs(p.panel); if (el) el.hidden = (p.target !== t); });
  const desc = MANUAL_ROWS.find(d => d.id === t);   // re-render the newly-shown command
  if (desc) _manualRender(desc);
}

function _setStatus(msg, kind) {
  const el = _qs('ac-deploy-status');
  if (!el) return;
  el.textContent = msg || '';
  el.classList.remove('ac-fg-success','ac-fg-danger'); if (kind === 'ok') el.classList.add('ac-fg-success'); else if (kind === 'err') el.classList.add('ac-fg-danger');
}

async function _get(path) {
  const sep = path.includes('?') ? '&' : '?';
  const res = await fetch(apiPath('/admin/deploy' + path + sep + 'requesting_user_id=' + encodeURIComponent(_userId())), { headers: { ...authHeaders() } });
  let data = {};
  try { data = await res.json(); } catch {}
  if (!res.ok) throw new Error(data.detail || data.error || ('HTTP ' + res.status));
  return data;
}

async function _post(path, body) {
  const res = await fetch(apiPath(path), {
    method: 'POST',
    headers: { 'Content-Type': 'application/json', ...authHeaders() },
    body: JSON.stringify({ requesting_user_id: _userId(), ...body }),
  });
  let data = {};
  try { data = await res.json(); } catch {}
  if (!res.ok) throw new Error(data.detail || data.error || ('HTTP ' + res.status));
  return data;
}

// ── Load + render ──────────────────────────────────────────────────────────
async function _load() {
  if (!isAdmin()) return;
  try {
    const res = await fetch(apiPath('/admin/deploy/catalog?requesting_user_id=' + encodeURIComponent(_userId())), { headers: { ...authHeaders() } });
    if (!res.ok) return;          // 403 for non-admins — leave the card quiet
    _catalog = await res.json();
  } catch { return; }
  _renderAll();
  _loadInstances();               // the local-deployments list (hub + siblings)
}

function _renderAll() {
  if (!_catalog) return;
  const sel = _qs('ac-deploy-provider');
  const providers = _catalog.providers || [];
  // The cloud-deploy dropdown lists only CLOUD targets. "Manual" targets
  // (Linux/Termux, Windows, macOS) each get their own dedicated row below —
  // see _initManualRows / MANUAL_ROWS.
  const cloud = providers.filter(p => !p.manual);

  const badge = _qs('ac-deploy-badge');
  if (badge) badge.textContent = cloud.length
    ? (cloud.length + (cloud.length === 1 ? ' target' : ' targets')) : 'no targets';

  if (sel) {
    const want = (cloud.some(p => p.id === _catalog.active_provider)
      ? _catalog.active_provider : (cloud[0] && cloud[0].id)) || '';
    sel.innerHTML = cloud.map(p =>
      `<option value="${_esc(p.id)}"${p.available ? '' : ' disabled'}>` +
      `${_esc(p.display_name)}${p.available ? '' : ' — unavailable'}</option>`).join('');
    sel.value = want;
  }
  _prefillSharedRepo();
  _probeRepo();                   // auto-detect access for the pre-filled repo URL
  _renderProvider();
  _renderManualPrefill();
  _syncTargetPanel();
}

// Pre-fill the shared Repo-details bar from the reserved "_repo" slot (URL + branch
// only; the token + admin password are never persisted, and access is re-detected
// from the URL). Only fills a blank field, so it never clobbers mid-typing.
function _prefillSharedRepo() {
  const repo = (_catalog && _catalog.shared_repo) || {};
  const url = _qs('ac-deploy-repo-url');
  const branch = _qs('ac-deploy-repo-branch');
  if (url && !url.value && repo.github_url) url.value = repo.github_url;
  if (branch && !branch.value && repo.branch) branch.value = repo.branch;
  _updateRepoHead();
}

function _renderProvider() {
  _hideTip();                    // drop any help bubble from the old form
  const p = _current();
  const cfgHost = _qs('ac-deploy-config');
  const credHost = _qs('ac-deploy-creds');
  const destroyBtn = _qs('ac-deploy-destroy');
  if (!p) {
    if (cfgHost) cfgHost.innerHTML = '';
    if (credHost) credHost.innerHTML = '';
    return;
  }

  // Settings form (non-secret). The repo URL / visibility / branch are NOT shown
  // here — they come from the shared Repo-details bar (SHARED_CLOUD_KEYS), injected
  // on save/deploy. A `progressive` target (Google VM) renders ONLY its first
  // config field (the project) into the main host and every OTHER setting into the
  // "rest" host below the key, so the reveal runs project → key → rest.
  const restHost = _qs('ac-deploy-config-rest');
  const cfgFields = (p.config_fields || []).filter(f => !SHARED_CLOUD_KEYS.has(f.key));
  const gateKey = (p.progressive && cfgFields.length) ? cfgFields[0].key : null;
  if (cfgHost) cfgHost.innerHTML = '';
  if (restHost) restHost.innerHTML = '';
  cfgFields.forEach(f => {
    const host = (gateKey && f.key !== gateKey && restHost) ? restHost : cfgHost;
    if (host) host.appendChild(_buildField(f, (p.config || {})[f.key], 'cfg'));
  });
  // Cloud-key form (secret); a "set" secret shows a placeholder, never the value.
  // The GitHub token + admin password are skipped here — they ride the shared
  // Repo-details bar and are injected on save/deploy.
  if (credHost) {
    credHost.innerHTML = '';
    (p.credential_fields || []).forEach(f => {
      if (SHARED_CLOUD_KEYS.has(f.key)) return;
      const isSet = !!(p.credentials_set || {})[f.key];
      credHost.appendChild(_buildField(f, '', 'cred', isSet));
    });
  }
  // Any field can carry a project-ID-driven "link" (e.g. the Service-Accounts
  // page for the entered project). Refresh them — and re-evaluate the staged
  // reveal — whenever the project ID or the key changes.
  const pidInput = cfgHost && cfgHost.querySelector('[data-key="project_id"]');
  if (pidInput) {
    pidInput.addEventListener('input', () => { _syncDynLinks(); _applyCloudStages(); });
  }
  const saInput = credHost && credHost.querySelector('[data-key="service_account_json"]');
  if (saInput) saInput.addEventListener('input', _applyCloudStages);
  _restorePreserved();           // re-apply values typed before a save / activate reload
  _syncDynLinks();
  _applyCloudStages();           // set the initial stage visibility for this target
  _resetMorePanel();             // collapse the advanced-actions panel on (re)render
  _renderServers(p);            // the "Saved servers" picker (SSH target only)

  // Tear-down only when there's a recorded server
  const dep = p.deployment || {};
  if (destroyBtn) destroyBtn.classList.toggle('ac-hidden', !dep.server);
  // Session-only: show the address only if a deploy happened in THIS session for
  // this target, never rebuilt from the saved record — so reopening the page clears it.
  _renderDeployResult((_sessionDeploy && _sessionDeploy.provider === _provider()) ? _sessionDeploy.dep : null);
  _setStatus('');
  if (window.lucide) { try { lucide.createIcons(); } catch {} }
}

// ── Staged (progressive) reveal ──────────────────────────────────────────────
// For a `progressive` target the deploy form is shown one step at a time so it
// never overwhelms: (1) only the project field, (2) once a project ID is typed,
// the key field appears, (3) once a key is present (typed now or already saved),
// the rest of the settings + the action buttons appear. Non-progressive targets
// (SSH) render everything into the main host and show it all at once.
function _applyCloudStages() {
  const p = _current();
  const cfgHost = _qs('ac-deploy-config');
  const credHost = _qs('ac-deploy-creds');
  const restHost = _qs('ac-deploy-config-rest');
  const show = (id, on) => { const el = _qs(id); if (el) el.classList.toggle('ac-hidden', !on); };

  const prog = !!(p && p.progressive);
  if (!prog) {
    // Everything visible; the two section labels head their groups as before.
    show('ac-deploy-settings-label', true);
    show('ac-deploy-key-label', true);
    if (credHost) credHost.classList.remove('ac-hidden');
    if (restHost) restHost.classList.remove('ac-hidden');
    show('ac-deploy-actions', true);
    return;
  }

  const pid = (cfgHost && cfgHost.querySelector('[data-key="project_id"]')?.value || '').trim();
  const saVal = (credHost && credHost.querySelector('[data-key="service_account_json"]')?.value || '').trim();
  const keyDone = !!saVal || !!p.configured;      // typed now OR a key already saved
  const stage2 = !!pid;                            // reveal the key
  const stage3 = stage2 && keyDone;                // reveal the rest + buttons

  // The section labels are redundant in the staged flow — each field self-labels.
  show('ac-deploy-settings-label', false);
  show('ac-deploy-key-label', false);
  if (credHost) credHost.classList.toggle('ac-hidden', !stage2);
  if (restHost) restHost.classList.toggle('ac-hidden', !stage3);
  show('ac-deploy-actions', stage3);
}

// Snapshot the currently-typed config + cloud-key values so a save / activate
// reload doesn't wipe them (see _preserve). Keyed by provider so it never leaks
// across targets.
function _captureForm() {
  _preserve = { provider: _provider(), cfg: _gather('cfg'), cred: _gather('cred') };
}

// Re-apply a preserved snapshot into the freshly-rendered fields of the SAME
// provider. Only fills from a non-blank captured value, so it never clobbers a
// server-loaded default with an empty string. Called from _renderProvider before
// the staged-reveal check, so the restored key keeps the VM fields visible.
function _restorePreserved() {
  if (!_preserve || _preserve.provider !== _provider()) return;
  const put = (hosts, vals) => {
    Object.entries(vals || {}).forEach(([k, v]) => {
      if (v === '' || v == null || v === false) return;
      let inp = null;
      for (const h of hosts) { inp = document.querySelector(h + ' [data-key="' + k + '"]'); if (inp) break; }
      if (!inp) return;
      if (inp.dataset.type === 'checkbox') inp.checked = !!v;
      else inp.value = v;
    });
  };
  put(['#ac-deploy-config', '#ac-deploy-config-rest'], _preserve.cfg);
  put(['#ac-deploy-creds'], _preserve.cred);
}

// Session-only live-address banner. Shown right after a successful deploy in THIS
// browser session (see `_sessionDeploy`) and it survives the in-session _load()
// re-render — but it is NOT rebuilt from the saved deployment record on a fresh
// page load, so reopening the page clears it. Blank → hidden. (Tear-down still keys
// off the saved `dep` record, not this banner.)
let _sessionDeploy = null;      // { provider, dep } from a deploy done this session
function _renderDeployResult(dep) {
  const el = _qs('ac-deploy-result');
  if (!el) return;
  const url = ((dep && dep.public_url) || (dep && dep.ip ? 'http://' + dep.ip : '')).trim();
  if (!url) { el.hidden = true; el.innerHTML = ''; return; }
  el.hidden = false;
  let html = 'Deployment successful: '
    + '<a href="' + _escAttr(url) + '" target="_blank" rel="noopener" class="ac-deploy-current-link">' + _esc(url) + '</a>';
  // When a domain is the live URL, the VM's raw IP is a separate fact — so the
  // admin can SSH straight to the box without hunting for it in the console.
  const vmIp = (dep && dep.ip ? String(dep.ip).trim() : '');
  if (vmIp && url.indexOf(vmIp) === -1) {
    html += ' &nbsp;<span class="ac-deploy-vm-ip">VM IP: <span class="mono">' + _esc(vmIp) + '</span>'
      + ' <button class="inst-url-btn" data-act="copy-url" data-url="' + _escAttr(vmIp) + '" title="Copy VM IP"><i data-lucide="copy"></i></button></span>';
  }
  // Google VM deploys also get a link straight to that project's Cloud Console
  // instances list, so the user doesn't have to hunt for it after activation.
  const project = (dep && dep.project ? String(dep.project).trim() : '');
  if (_provider() === 'google_vm' && project) {
    const consoleUrl = 'https://console.cloud.google.com/compute/instances?project=' + encodeURIComponent(project);
    html += ' &nbsp;<a href="' + _escAttr(consoleUrl) + '" target="_blank" rel="noopener" class="ac-deploy-current-link">Google VM Instances</a>';
  } else if (_provider() === 'google_cloud_run' && project) {
    const region = (dep && (dep.region || dep.zone) ? String(dep.region || dep.zone).trim() : '');
    const service = (dep && dep.server ? String(dep.server).trim() : '');
    const consoleUrl = 'https://console.cloud.google.com/run/detail/'
      + encodeURIComponent(region) + '/' + encodeURIComponent(service)
      + '/metrics?project=' + encodeURIComponent(project);
    html += ' &nbsp;<a href="' + _escAttr(consoleUrl) + '" target="_blank" rel="noopener" class="ac-deploy-current-link">Google Cloud Run</a>';
  }
  el.innerHTML = html;
  // Wire the VM-IP copy button (present only when a domain URL + IP are shown).
  const ipBtn = el.querySelector('button[data-act="copy-url"]');
  if (ipBtn) {
    ipBtn.addEventListener('click', function () {
      copyText(ipBtn.dataset.url || '').then(function () { _flashCopied(ipBtn); }).catch(function () {});
    });
  }
}

// Collapse the "More" advanced-actions panel (called on every (re)render so a
// panel left open on one target doesn't carry over to the next).
function _resetMorePanel() {
  const panel = _qs('ac-deploy-more-panel');
  const btn = _qs('ac-deploy-more-btn');
  if (panel) panel.hidden = true;
  if (btn) { btn.setAttribute('aria-expanded', 'false'); btn.textContent = 'More ▾'; }
}

// ── Saved servers (profile-aware targets, e.g. the SSH one) ──────────────────
// A dropdown of named servers whose address + login are kept (secrets encrypted
// in the vault). Picking one LOADS it into the form (the deploy runtime then acts
// on it); "Save server" stores the current form as a named server; "Delete"
// removes one. Every Google VM you create is auto-added server-side. The whole
// block is hidden for targets that aren't profile-aware (p.saved_servers false).
let _serversCache = [];         // last-rendered saved servers (for the live control sync)
function _renderServers(p) {
  const wrap = _qs('ac-deploy-servers-wrap');
  const sel = _qs('ac-deploy-servers');
  if (!wrap || !sel) return;
  if (!p || !p.saved_servers) { wrap.classList.add('ac-hidden'); _serversCache = []; return; }
  wrap.classList.remove('ac-hidden');

  const servers = p.servers || [];
  _serversCache = servers;
  const active = p.active_server || '';
  const opts = ['<option value="">＋ New server…</option>'];
  servers.forEach(s => {
    const tag = s.host ? ' — ' + s.host : '';
    const src = s.source === 'google_vm' ? ' (Google VM)' : '';
    opts.push('<option value="' + _escAttr(s.id) + '">' + _esc((s.label || s.host || 'Server') + tag + src) + '</option>');
  });
  sel.innerHTML = opts.join('');
  sel.value = active;

  _syncServerControls();         // name field + Delete button follow the dropdown selection
}

// Point the name field + the Delete button at whatever server is CURRENTLY chosen in
// the dropdown — driven off the live <select> value, not a server round-trip, so the
// Delete button appears the instant a real saved server is picked and hides on
// "＋ New server…". Called on render AND on every dropdown change (see _onServerSelect).
function _syncServerControls() {
  const sel = _qs('ac-deploy-servers');
  if (!sel) return;
  const id = sel.value || '';
  const cur = _serversCache.find(s => s.id === id);
  const labelInp = _qs('ac-deploy-server-label');
  if (labelInp) labelInp.value = cur ? (cur.label || '') : '';
  const delBtn = _qs('ac-deploy-server-delete');
  if (delBtn) delBtn.classList.toggle('ac-hidden', !id);
}

// Pick a saved server (or "New server…") → load it into the working form.
async function _onServerSelect() {
  if (!isAdmin()) return;
  _preserve = null;              // loading a saved server's own values — those win
  _resetDeleteBtn();             // clear any half-finished hold from the previous selection
  _syncServerControls();         // reveal/hide Delete immediately — before the round-trip
  const server_id = (_qs('ac-deploy-servers') || {}).value || '';
  _setStatus(server_id ? 'Loading server…' : 'Cleared — enter a new server below.');
  try {
    await _post('/admin/deploy/servers/select', { provider: _provider(), server_id });
    await _load();                 // re-render the form filled from the loaded server
    _setStatus('');
  } catch (e) { _setStatus(e.message, 'err'); }
}

// Save the current form as a server (updates the selected one, or creates a new
// one when "New server…" is selected). Secrets left blank keep the stored login.
async function _onServerSave() {
  if (!isAdmin()) return;
  const server_id = (_qs('ac-deploy-servers') || {}).value || '';
  const label = (_qs('ac-deploy-server-label')?.value || '').trim();
  const values = { ..._gather('cfg'), ..._gather('cred') };
  if (!String(values.host || '').trim()) { _setStatus('Enter the server address first.', 'err'); return; }
  _setStatus('Saving server…');
  try {
    await _post('/admin/deploy/servers/save', { provider: _provider(), server_id, label, values });
    await _load();
    _setStatus('Server saved.', 'ok');
  } catch (e) { _setStatus(e.message, 'err'); }
}

// Remove a saved server + its stored login (the machine itself is untouched).
// Fired by the press-and-hold gesture below — no confirm dialog, the hold is the
// confirmation. Re-checks a server is selected before hitting the endpoint.
async function _onServerDelete() {
  if (!isAdmin()) return;
  const server_id = (_qs('ac-deploy-servers') || {}).value || '';
  if (!server_id) return;
  _setStatus('Deleting…');
  try {
    await _post('/admin/deploy/servers/delete', { provider: _provider(), server_id });
    await _load();
    _setStatus('Deleted.', 'ok');
  } catch (e) {
    _setStatus(e.message, 'err');
  } finally {
    _resetDeleteBtn();           // clear any lingering holding/warning state after firing
  }
}

// ── Hold-to-delete (the saved-server Delete button) ──────────────────────────
// Press and HOLD Delete: a danger fill sweeps the button (shared .ac-dz-hold
// styling), the label switches to "Keep holding…" partway through, and holding the
// whole _DEL_HOLD_MS runs the delete. Release / pointer-cancel before the end
// aborts with no side effect — the deliberate hold replaces the old confirm dialog.
// The CSS fill duration is driven from --dz-hold (set in _init) so it stays in
// lock-step with _DEL_HOLD_MS.
const _DEL_HOLD_MS = 1500;       // total hold → fire the delete
const _DEL_ARM_MS = 380;         // hold this long → arm (intensify + swap the label)
const _DEL_REST_LABEL = 'Hold to delete';
let _delHold = null;             // {btn, armTimer, fireTimer} while a hold is live

function _delSetLabel(btn, text) {
  const el = btn && btn.querySelector('.ac-dz-hold-label');
  if (el) el.textContent = text;
}

// Restore the button to its resting look (label + no holding/warning tint).
function _resetDeleteBtn() {
  const btn = _qs('ac-deploy-server-delete');
  if (btn) { btn.classList.remove('holding', 'warning'); _delSetLabel(btn, _DEL_REST_LABEL); }
}

// Stop the active hold. `revert` restores the resting label (an abort); leave it
// false when the delete is firing (the finally in _onServerDelete tidies up).
function _delCancel(revert) {
  const h = _delHold; _delHold = null;
  if (!h) return;
  clearTimeout(h.armTimer); clearTimeout(h.fireTimer);
  window.removeEventListener('pointerup', _delRelease, true);
  window.removeEventListener('pointercancel', _delRelease, true);
  if (h.btn && revert) {
    h.btn.classList.remove('holding', 'warning');
    _delSetLabel(h.btn, _DEL_REST_LABEL);
  }
}
function _delRelease() { _delCancel(true); }

function _beginDeleteHold(btn) {
  if (_delHold || !isAdmin()) return;
  if (!((_qs('ac-deploy-servers') || {}).value || '')) return;   // nothing selected
  const armTimer = setTimeout(() => {
    btn.classList.add('warning');
    _delSetLabel(btn, 'Keep holding to delete…');
  }, _DEL_ARM_MS);
  const fireTimer = setTimeout(() => {
    _delCancel(false);           // clear timers/listeners, keep the button filled
    _onServerDelete();
  }, _DEL_HOLD_MS);
  _delHold = { btn, armTimer, fireTimer };
  // Kick the CSS fill on the next frame so the transition actually animates.
  requestAnimationFrame(() => { if (_delHold && _delHold.btn === btn) btn.classList.add('holding'); });
  window.addEventListener('pointerup', _delRelease, true);
  window.addEventListener('pointercancel', _delRelease, true);
}

function _onServerDeletePointerDown(e) {
  if (e.button != null && e.button !== 0) return;   // primary button / touch only
  e.preventDefault();
  _beginDeleteHold(e.currentTarget);
}

// ── Current deployment + other local deployments ─────────────────────────────
// The list at the top of the Deploy card: this app (the hub) plus any registered
// sibling WebAgent checkouts on this machine, each shown as an expandable bar with
// a live status dot and a "Port N · folder" line. The hub bar expands to a port
// editor (change + relaunch this app); a sibling bar to Open / Start / Stop / edit
// / Remove. Backed by /admin/deploy/instances/* (app/local_instances.py) — the same
// registry the Dashboard's instance header uses. The rows are BUILT here (count
// varies), so their expand + actions are wired by delegation, not _wireBootRow.
let _insts = [];                 // last-loaded instances (hub first)
let _instHubPort = 8080;         // this app's current port
let _instBusy = false;           // a start / stop / relaunch is running
const _instExpanded = new Set(); // ids whose bar is expanded (survives re-render)

async function _loadInstances() {
  const host = _qs('ac-deploy-instances');
  if (!host || !isAdmin()) return;
  try {
    const data = await _get('/instances');
    _insts = data.instances || [];
    _instHubPort = data.hub_port || 8080;
  } catch (e) {
    host.innerHTML = '<div class="ac-hint" class="ac-p10-14-fg-danger">' + _esc(e.message) + '</div>';
    return;
  }
  _renderInstances();
  _seedRegisterPort();
}

function _instDotClass(status) {
  return status === 'running' ? 'ac-deploy-dot-run'
    : status === 'busy' ? 'ac-deploy-dot-busy' : 'ac-deploy-dot-stop';
}
function _instStatusLabel(status) {
  return status === 'running' ? 'Running' : status === 'busy' ? 'Port busy' : 'Stopped';
}

function _renderInstances() {
  const host = _qs('ac-deploy-instances');
  if (!host) return;
  host.innerHTML = _insts.map(_instBar).join('') || '';
  _refreshLucideIcons(host);
}

// One instance = an expandable `.ac-row`. Collapsed: status dot, name + badges, a
// right-hand status word, and the "Port N · folder" line. Expanded body differs
// for the hub (port editor) vs a sibling (actions + inline edit).
function _instBar(i) {
  const expanded = _instExpanded.has(i.id);
  const badges = [];
  if (i.builtin) badges.push('<span class="ac-deploy-badge ac-deploy-badge-app">This app</span>');
  if (!i.builtin && i.is_checkout === false) badges.push('<span class="ac-deploy-badge ac-deploy-badge-warn">Folder missing</span>');
  else if (!i.builtin && i.port_aware === false) badges.push('<span class="ac-deploy-badge ac-deploy-badge-warn">Needs update</span>');

  const meta = '<span class="ac-deploy-meta-port">Port ' + _esc(String(i.port)) + '</span>'
    + '<span class="ac-deploy-meta-folder">' + _esc(i.folder) + '</span>';

  // Hold-to-restart control — the hub (this app) only. Press and HOLD it: after a
  // beat it arms to the hazard icon (mirroring the app's delete buttons), a fill
  // sweeps it, and at the end the server restarts. See _beginResetHold; the hold IS
  // the confirmation, so there's no extra dialog. A sibling can't restart itself
  // from here — use its Start / Stop actions instead.
  const resetBtn = i.builtin
    ? '<button class="ac-deploy-reset-btn" type="button" data-hub-reset title="Hold to restart this server">'
      + '<span class="ac-deploy-reset-fill"></span>'
      + '<span class="ac-deploy-reset-ico"><i data-lucide="power"></i></span>'
      + '</button>'
    : '';

  return '<div class="ac-row ac-deploy-inst' + (expanded ? ' expanded' : '') + '" data-inst="' + _escAttr(i.id) + '">'
    + '<div class="ac-ability-row">'
    +   '<span class="ac-ability-icon"><span class="ac-deploy-dot ' + _instDotClass(i.status) + '" title="' + _escAttr(_instStatusLabel(i.status)) + '"></span></span>'
    +   '<div class="ac-ability-label">'
    +     '<div class="ac-ability-name">' + _esc(i.label) + badges.join('')
    +       '<span class="ac-deploy-inst-status">' + _esc(_instStatusLabel(i.status)) + '</span>'
    +     '</div>'
    +     '<div class="ac-ability-desc">' + meta + '</div>'
    +   '</div>'
    +   resetBtn
    +   '<span class="ac-row-chevron"><svg width="13" height="13" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.5"><polyline points="9 18 15 12 9 6"/></svg></span>'
    + '</div>'
    + '<div class="ac-ability-body">' + (i.builtin ? _hubBody(i) : _siblingBody(i)) + '</div>'
    + '</div>';
}

// The hub's body: change this app's own port (persist + relaunch). The SETUP
// BUNDLE tools (export / import a config code) are NO LONGER nested here — they
// are two expandable rows nested in the DATABASE card's "Data Migration" row
// (see data-settings.html → #ac-deploy-export-row / #ac-deploy-import-row),
// still wired below in initDeploy by id. Engine: app/admin/bootstrap_bundle.py.
function _hubBody(i) {
  return '<label class="ac-label">Change this app’s port</label>'
    + '<input class="ac-input" type="number" min="1024" max="65535" data-hub-port value="' + _escAttr(String(i.port)) + '">'
    + '<div class="ac-hint" class="ac-fs11-mt4">Each deployment needs its own port. Changing this <strong>restarts WebAgent</strong> on the new port — this connection drops and you reopen it at the new address.</div>'
    + '<div class="ac-ra-actions" class="ac-mt10">'
    +   '<button class="ac-btn ac-deploy-go-btn" type="button" data-hub-save>Change &amp; relaunch</button>'
    +   '<span class="ac-hint" data-hub-status class="ac-ml6"></span>'
    + '</div>';
}

// A sibling's body: Open (running) / Start / Stop / Remove, plus inline edit of its
// name / folder / port.
function _siblingBody(i) {
  const acts = [];
  if (i.status === 'running') acts.push('<a class="ac-btn" href="' + _escAttr(i.url) + '" target="_blank" rel="noopener" data-open><i data-lucide="external-link"></i> Open</a>');
  if (i.status === 'running') acts.push('<button class="ac-btn" type="button" data-stop><i data-lucide="square"></i> Stop</button>');
  else acts.push('<button class="ac-btn ac-deploy-go-btn" type="button" data-start><i data-lucide="play"></i> Start</button>');
  acts.push('<button class="ac-btn ac-deploy-danger-btn" type="button" data-remove><i data-lucide="trash-2"></i> Remove</button>');

  let warn = '';
  if (i.is_checkout === false) warn = '<div class="ac-hint" class="ac-fg-danger-fs115">This folder is no longer a WebAgent checkout — fix the path below.</div>';
  else if (i.port_aware === false) warn = '<div class="ac-hint" class="ac-fg-warn-danger-fs115">This checkout is too old to run on a custom port — update it (git pull) before starting.</div>';

  return '<div class="ac-ra-actions" class="ac-mb8">' + acts.join('') + '</div>'
    + warn
    + '<label class="ac-label">Name</label>'
    + '<input class="ac-input" type="text" data-edit-label value="' + _escAttr(i.label) + '">'
    + '<label class="ac-label" class="ac-mt8">Repo folder</label>'
    + '<input class="ac-input" type="text" data-edit-folder value="' + _escAttr(i.folder) + '" spellcheck="false">'
    + '<label class="ac-label" class="ac-mt8">Port</label>'
    + '<input class="ac-input" type="number" min="1024" max="65535" data-edit-port value="' + _escAttr(String(i.port)) + '">'
    + '<div class="ac-ra-actions" class="ac-mt10">'
    +   '<button class="ac-btn" type="button" data-inst-save>Save changes</button>'
    +   '<span class="ac-hint" data-inst-status class="ac-ml6"></span>'
    + '</div>';
}

// Delegated clicks across the whole instances list: action buttons first, then a
// head-click toggles that bar (ignoring clicks on controls inside it).
function _onInstClick(e) {
  const row = e.target.closest('.ac-deploy-inst');
  if (!row) return;
  const id = row.dataset.inst;
  if (e.target.closest('[data-open]')) return;                          // native <a> handles it
  if (e.target.closest('[data-start]')) { _instAction('start', id); return; }
  if (e.target.closest('[data-stop]')) { _instAction('stop', id); return; }
  if (e.target.closest('[data-remove]')) { _instRemove(id); return; }
  if (e.target.closest('[data-inst-save]')) { _instSave(id, row); return; }
  if (e.target.closest('[data-hub-save]')) { _hubSave(row); return; }
  const head = e.target.closest('.ac-ability-row');
  if (head && head.parentElement === row) {
    if (e.target.closest('input, textarea, select, button, a, label')) return;
    row.classList.toggle('expanded');
    if (row.classList.contains('expanded')) _instExpanded.add(id); else _instExpanded.delete(id);
  }
}

// Start / Stop a sibling (streams NDJSON into the shared deploy log).
async function _instAction(action, id) {
  if (_instBusy) return;
  const inst = _insts.find(x => x.id === id);
  if (!inst) return;
  if (action === 'stop' && !window.confirm('Stop “' + inst.label + '”?\n\nIts server (port ' + inst.port + ') is shut down; anyone using it is disconnected.')) return;
  _instBusy = true;
  _logReset();
  _logLine((action === 'stop' ? 'Stopping ' : 'Starting ') + inst.label + '…');
  try {
    await _stream('/admin/deploy/instances/' + action, { id }, (result) => {
      _logLine(result.message || (result.ok ? 'Done.' : 'Failed.'), result.ok ? 'ok' : 'err');
    });
  } catch (e) { _logLine(e.message, 'err'); }
  finally { _instBusy = false; await _loadInstances(); }
}

async function _instRemove(id) {
  const inst = _insts.find(x => x.id === id);
  if (!inst) return;
  const running = inst.status === 'running';
  const msg = running
    ? 'Remove “' + inst.label + '” from the list?\n\nIt is still RUNNING — removing only forgets it here; it keeps running until you Stop it.'
    : 'Remove “' + inst.label + '” from the list?\n\nThe repo folder on disk is not touched.';
  if (!window.confirm(msg)) return;
  try { await _post('/admin/deploy/instances/remove', { id }); await _loadInstances(); }
  catch (e) { window.alert(e.message); }
}

async function _instSave(id, row) {
  const status = row.querySelector('[data-inst-status]');
  const label = row.querySelector('[data-edit-label]')?.value || '';
  const folder = (row.querySelector('[data-edit-folder]')?.value || '').trim();
  const port = parseInt(row.querySelector('[data-edit-port]')?.value || '', 10);
  if (status) { status.textContent = 'Saving…'; status.classList.remove('ac-fg-danger','ac-fg-success','ac-fg3'); }
  try {
    await _post('/admin/deploy/instances/update', { id, label, folder, port: port || 0 });
    await _loadInstances();
  } catch (e) { if (status) { status.textContent = e.message; status.classList.add('ac-fg-danger'); status.classList.remove('ac-fg-success','ac-fg3'); } }
}

// Change THIS app's port: persist it + relaunch. The server exits right after
// responding, so we replace the body with a "reopen here" link.
async function _hubSave(row) {
  const inp = row.querySelector('[data-hub-port]');
  const status = row.querySelector('[data-hub-status]');
  const port = parseInt(inp?.value || '', 10);
  const setStatus = (m, err) => { if (status) { status.textContent = m || ''; status.classList.toggle('ac-fg-danger', !!err); status.classList.remove(err ? 'ac-fg-success' : 'ac-fg-danger', 'ac-fg3'); } };
  if (!port) return setStatus('Enter a port.', true);
  if (port === _instHubPort) return setStatus('That’s already the current port.', true);
  if (!window.confirm('Change this app’s port to ' + port + ' and restart now?\n\n'
      + 'This connection will drop and WebAgent will reopen at http://localhost:' + port + '/')) return;
  setStatus('Saving & restarting…');
  try {
    const r = await _post('/admin/deploy/instances/set-hub-port', { port });
    const url = r.url || ('http://localhost:' + port + '/');
    const body = row.querySelector('.ac-ability-body');
    if (body) {
      body.innerHTML = '<div class="ac-hint" class="ac-lh16">'
        + 'WebAgent is restarting on port <strong>' + _esc(String(r.port || port)) + '</strong>. '
        + (r.auto_restart === false
            ? 'Start it again from your server window, then open: '
            : 'It’ll be back in a few seconds — reopen it here: ')
        + '<a href="' + _esc(url) + '" class="ac-deploy-current-link">' + _esc(url) + '</a></div>';
    }
  } catch (e) { setStatus(e.message, true); }
}

// ── Setup bundle: export / import (nested in the "Data Migration" row) ────────
// "Export config to link another account" (#ac-deploy-export-row) and "Import
// config to link this account" (#ac-deploy-import-row) are two expandable rows
// nested in the DATABASE card's "Data Migration" row, wired in initDeploy by id.
// `row` here is that bar element. All server-side (crypto in Python): browser
// crypto.subtle is
// unavailable on plain-http LAN addresses, and moving config between devices is
// the whole point.

async function _bootShare(row) {
  const status = row.querySelector('[data-boot-share-status]');
  const sections = Array.from(row.querySelectorAll('[data-boot-sec]:checked')).map(el => el.dataset.bootSec);
  const set = (m, err) => { if (status) { status.textContent = m || ''; status.classList.toggle('ac-fg-danger', !!err); } };
  if (!sections.length) return set('Pick at least one thing to include.', true);
  set('Creating…');
  let r;
  try { r = await _post('/admin/storage/bootstrap/export', { sections }); }
  catch (e) { return set(e.message, true); }
  if (!r.ok) return set(r.error || 'Could not create the code.', true);
  set('');
  const result = row.querySelector('[data-boot-share-result]');
  const codeEl = row.querySelector('[data-boot-code]');
  if (codeEl) codeEl.value = r.code;
  if (result) result.classList.remove('ac-hidden');
}

async function _bootCopy(row, btn) {
  const code = row.querySelector('[data-boot-code]')?.value || '';
  if (!code) return;
  try { await copyText(code); _flashCopied(btn); } catch {}
}

async function _bootPreview(row) {
  const status = row.querySelector('[data-boot-import-status]');
  const code = row.querySelector('[data-boot-import-code]')?.value?.trim() || '';
  const set = (m, err) => { if (status) { status.textContent = m || ''; status.classList.toggle('ac-fg-danger', !!err); } };
  set('', false);
  let result;
  if (!code) return set('Paste a setup code.', true);
  set('Decoding…');
  let r;
  try { r = await _post('/admin/storage/bootstrap/preview', { code }); }
  catch (e) { return set(e.message, true); }
  if (!r.ok) return set(r.error || 'Could not decode the code.', true);
  set('');
  const host = row.querySelector('[data-boot-preview-result]');
  if (!host) return;
  // Password verified → show what the code carries (read-only) and the Accept
  // button. Accept adopts every carried section (overwrite) so this install
  // points at the same setup as the source — that's what "link this account"
  // means; the confirm dialog in _bootApply is the safety net. The carried
  // section names ride a data-attr so _bootApply can build the choices map.
  const secs = (r.sections || []).map(s => s.section).filter(Boolean);
  host.dataset.sections = secs.join(',');
  const rows = (r.sections || []).map(s =>
    '<div class="ac-boot-preview-row">'
    + '<span>' + _esc(s.summary) + '</span>'
    + (s.present_now ? '<span class="ac-deploy-badge ac-deploy-badge-warn">already set</span>' : '')
    + '</div>').join('');
  host.innerHTML =
    '<div class="ac-info-banner" class="ac-deploy-warn-box">Accepting brings the settings below into this install, replacing any it already has.</div>'
    + rows
    + '<div class="ac-ra-actions" class="ac-mt12">'
    +   '<button class="ac-btn ac-deploy-go-btn" type="button" data-boot-apply><i data-lucide="check"></i> Accept</button>'
    +   '<span class="ac-hint" data-boot-apply-status class="ac-ml6"></span>'
    + '</div>';
  host.classList.remove('ac-hidden');
  _refreshLucideIcons(host);
}

async function _bootApply(row) {
  const status = row.querySelector('[data-boot-apply-status]');
  const code = row.querySelector('[data-boot-import-code]')?.value?.trim() || '';
  const set = (m, err) => { if (status) { status.textContent = m || ''; status.classList.toggle('ac-fg-danger', !!err); } };
  // Every carried section is adopted (overwrite) — see _bootPreview. The names
  // were stashed on the result host when the preview decoded the code.
  const host = row.querySelector('[data-boot-preview-result]');
  const choices = {};
  ((host && host.dataset.sections) || '').split(',').filter(Boolean).forEach(s => { choices[s] = 'overwrite'; });
  if (!window.confirm('Apply this setup to this install?\n\nThis replaces your current settings for everything the code carries.')) return;
  set('Applying…');
  let r;
  try { r = await _post('/admin/storage/bootstrap/apply', { code, choices }); }
  catch (e) { return set(e.message, true); }
  if (r.ok === false) return set(r.error || 'Apply failed.', true);
  const lines = Object.entries(r.results || {}).map(([k, v]) => k + ': ' + v).join(' · ');
  set(lines || 'Done.');
}

// Editing an export/import field AFTER a verify invalidates what was revealed —
// a generated code no longer matches the ticked sections/password, a decoded
// preview no longer matches the pasted code/password. Hide the stale result +
// its status so the admin re-verifies rather than copying/accepting something
// out of date. `row` is the bar; the pair of selectors is result + status.
function _bootInvalidate(row, resultSel, statusSel) {
  const res = row.querySelector(resultSel); if (res) res.classList.add('ac-hidden');
  const st = row.querySelector(statusSel); if (st) st.textContent = '';
}

// Wire the two static setup-bundle rows: their expand is handled by
// _wireBootRow in data-settings.js; here we bind the Generate / Copy / Preview /
// Accept actions (Accept is built into the preview result, so it's delegated)
// and hide a stale result whenever a field is edited after a Generate/Preview.
// Idempotent.
function _initBootRows() {
  const exp = _qs('ac-deploy-export-row');
  if (exp && !exp.dataset.bootWired) {
    exp.dataset.bootWired = '1';
    exp.addEventListener('click', (e) => {
      if (e.target.closest('[data-boot-share]')) { _bootShare(exp); return; }
      const cp = e.target.closest('[data-boot-copy]'); if (cp) { _bootCopy(exp, cp); return; }
    });
    const inval = () => _bootInvalidate(exp, '[data-boot-share-result]', '[data-boot-share-status]');
    exp.addEventListener('input', inval);
    exp.addEventListener('change', inval);   // checkbox ticks fire `change`
  }
  const imp = _qs('ac-deploy-import-row');
  if (imp && !imp.dataset.bootWired) {
    imp.dataset.bootWired = '1';
    imp.addEventListener('click', (e) => {
      if (e.target.closest('[data-boot-preview]')) { _bootPreview(imp); return; }
      if (e.target.closest('[data-boot-apply]')) { _bootApply(imp); return; }
    });
    imp.addEventListener('input', () => _bootInvalidate(imp, '[data-boot-preview-result]', '[data-boot-import-status]'));
  }
}

// ── Register a local checkout (the add form) ─────────────────────────────────
function _nextFreePort() {
  const used = new Set(_insts.map(i => i.port));
  used.add(_instHubPort);
  let p = 8081;
  while (used.has(p)) p++;
  return p;
}
function _seedRegisterPort() {
  const el = _qs('ac-inst-port');
  if (el && !el.value) el.value = String(_nextFreePort());
}

async function _checkRegisterFolder() {
  const tip = _qs('ac-inst-folder-tip');
  const folder = (_qs('ac-inst-folder')?.value || '').trim();
  if (!folder || !tip) return;
  tip.classList.remove('ac-fg-danger','ac-fg-success','ac-fg3');
  tip.textContent = 'Checking…';
  try {
    const res = await _post('/admin/deploy/instances/validate', { folder });
    if (!res.is_webagent) { tip.classList.add('ac-fg-danger'); tip.classList.remove('ac-fg-success','ac-fg3'); tip.textContent = res.message; return; }
    if (!res.port_aware) { tip.classList.add('ac-fg-danger'); tip.classList.remove('ac-fg-success','ac-fg3'); tip.textContent = res.message; return; }
    tip.classList.add('ac-fg-success'); tip.classList.remove('ac-fg-danger','ac-fg3');
    tip.textContent = 'Looks good — a WebAgent checkout ready to run on its own port.';
  } catch (e) { tip.classList.add('ac-fg-danger'); tip.classList.remove('ac-fg-success','ac-fg3'); tip.textContent = e.message; }
}

async function _registerAdd() {
  const status = _qs('ac-inst-add-status');
  const folder = (_qs('ac-inst-folder')?.value || '').trim();
  const label = _qs('ac-inst-label')?.value || '';
  const port = parseInt(_qs('ac-inst-port')?.value || '', 10);
  const setStatus = (m, kind) => { if (status) { status.textContent = m || ''; status.classList.remove('ac-fg-danger','ac-fg-success'); if (kind==='err') status.classList.add('ac-fg-danger'); else if (kind==='ok') status.classList.add('ac-fg-success'); } };
  if (!folder) return setStatus('Enter the repo folder first.', 'err');
  if (!port) return setStatus('Enter a port.', 'err');
  setStatus('Adding…');
  try {
    await _post('/admin/deploy/instances/add', { label, folder, port });
    if (_qs('ac-inst-folder')) _qs('ac-inst-folder').value = '';
    if (_qs('ac-inst-label')) _qs('ac-inst-label').value = '';
    if (_qs('ac-inst-port')) _qs('ac-inst-port').value = '';
    setStatus('Added.', 'ok');
    await _loadInstances();
    // The New-Deployment "This device" target lives on the Instances page now; nudge
    // it to refresh so the freshly-registered checkout appears as a tile immediately
    // (instead of waiting for its 20s poll). No-op anywhere else.
    try { window.__instancesReloadLocal && window.__instancesReloadLocal(); } catch {}
  } catch (e) { setStatus(e.message, 'err'); }
}

// ── Hold-to-restart (the hub row's reset button) ─────────────────────────────
// Press and HOLD the reset button: after _RESET_HAZARD_MS the icon flips to the
// hazard triangle + tints red (the same cue the app's delete buttons use), while
// a fill sweeps the button; hold the whole _RESET_HOLD_MS and the server restarts.
// Release / pointer-cancel before the end aborts with no side effect — the
// deliberate hold IS the confirmation, so there's no extra dialog. Restarting
// drops this connection; the relauncher brings the server back on the same port
// and we reload once it answers again. The fill duration in CSS
// (.ac-deploy-reset-btn.holding .ac-deploy-reset-fill) MUST match _RESET_HOLD_MS.
const _RESET_HAZARD_MS = 350;    // hold this long → arm (show the hazard icon)
const _RESET_HOLD_MS = 1200;     // total hold → fire the restart
let _resetHold = null;           // {btn, hazardTimer, fireTimer} while a hold is live

function _resetSetIcon(btn, name, spin) {
  const ico = btn.querySelector('.ac-deploy-reset-ico');
  if (!ico) return;
  ico.classList.toggle('session-status-running', !!spin);
  ico.innerHTML = '<i data-lucide="' + name + '"></i>';
  _refreshLucideIcons(ico);
}

// Stop the active hold. `revert` restores the resting reset icon (an abort);
// leave it false when the restart is firing (the button becomes a spinner).
function _resetCancel(revert) {
  const h = _resetHold; _resetHold = null;
  if (!h) return;
  clearTimeout(h.hazardTimer); clearTimeout(h.fireTimer);
  window.removeEventListener('pointerup', _resetRelease, true);
  window.removeEventListener('pointercancel', _resetRelease, true);
  if (h.btn && revert) {
    h.btn.classList.remove('holding', 'warning');
    h.btn.title = 'Hold to restart this server';
    _resetSetIcon(h.btn, 'power');
  }
}
function _resetRelease() { _resetCancel(true); }

function _beginResetHold(btn) {
  if (_resetHold || _instBusy) return;
  const hazardTimer = setTimeout(() => {
    btn.classList.add('warning');
    btn.title = 'Keep holding to restart…';
    _resetSetIcon(btn, 'alert-triangle');
  }, _RESET_HAZARD_MS);
  const fireTimer = setTimeout(() => {
    _resetCancel(false);           // clear timers/listeners, keep the button as-is
    _doServerReset(btn);
  }, _RESET_HOLD_MS);
  _resetHold = { btn, hazardTimer, fireTimer };
  // Kick the CSS fill on the next frame so the transition actually animates.
  requestAnimationFrame(() => { if (_resetHold && _resetHold.btn === btn) btn.classList.add('holding'); });
  window.addEventListener('pointerup', _resetRelease, true);
  window.addEventListener('pointercancel', _resetRelease, true);
}

// Fire the restart. On success the server exits a beat later; we spin the button
// and poll until it answers again, then reload. A 409 (host can't self-revive)
// leaves the server up — surface it and reset the button.
async function _doServerReset(btn) {
  _instBusy = true;
  btn.classList.remove('holding');
  _resetSetIcon(btn, 'loader-2', true);
  try {
    await _post('/admin/storage/server/restart', {});
    _resetShowRestarting(btn);
  } catch (e) {
    _instBusy = false;
    btn.classList.remove('warning');
    btn.title = 'Hold to restart this server';
    _resetSetIcon(btn, 'power');
    window.alert('Could not restart the server:\n\n' + e.message);
  }
}

// Reflect the restart in the hub row (busy dot + "Restarting…"), then poll the
// catalog endpoint; the first success after the process has cycled → reload.
function _resetShowRestarting(btn) {
  const row = btn.closest('.ac-deploy-inst');
  const dot = row && row.querySelector('.ac-deploy-dot');
  const statusEl = row && row.querySelector('.ac-deploy-inst-status');
  if (dot) dot.className = 'ac-deploy-dot ac-deploy-dot-busy';
  if (statusEl) statusEl.textContent = 'Restarting…';
  const ping = async () => {
    try {
      const res = await fetch(apiPath('/admin/deploy/catalog?requesting_user_id=' + encodeURIComponent(_userId())), { cache: 'no-store', headers: { ...authHeaders() } });
      if (res.ok) { window.location.reload(); return; }
    } catch { /* still down — keep waiting */ }
    setTimeout(ping, 1500);
  };
  // Wait past the relauncher's own exit delay before the first probe, so we don't
  // catch the old process still answering and reload into a server that's going down.
  setTimeout(ping, 3000);
}

// Delegated press-and-hold on the reset button (pointerdown here; release is
// handled on window inside _beginResetHold). stopPropagation keeps the click from
// toggling the row open.
function _onInstPointerDown(e) {
  const btn = e.target.closest('[data-hub-reset]');
  if (!btn) return;
  if (e.button != null && e.button !== 0) return;   // primary button / touch only
  e.preventDefault();
  e.stopPropagation();
  _beginResetHold(btn);
}

function _initInstances() {
  const host = _qs('ac-deploy-instances');
  if (host && !host.dataset.wired) {
    host.dataset.wired = '1';
    host.addEventListener('click', _onInstClick);
    host.addEventListener('pointerdown', _onInstPointerDown);
  }
  const folder = _qs('ac-inst-folder');
  if (folder && !folder.dataset.wired) { folder.dataset.wired = '1'; folder.addEventListener('blur', _checkRegisterFolder); }
  const add = _qs('ac-inst-add');
  if (add && !add.dataset.wired) { add.dataset.wired = '1'; add.addEventListener('click', _registerAdd); }
}

// Wire a "Copy" button to copy a code element's text. Re-run safe (the button
// carries a `wired` flag so re-renders don't stack listeners). Shared by the
// phone-install row's command box.
function _wireCopy(btn, code) {
  if (!btn || !code || btn.dataset.wired) return;
  btn.dataset.wired = '1';
  btn.addEventListener('click', async () => {
    const text = code.textContent || '';
    try {
      // Use the shared clipboard helper, NOT navigator.clipboard directly: on a
      // phone this panel is reached over http://<device-ip>:8080 (the address the
      // install steps tell you to open), a NON-secure context where
      // navigator.clipboard is undefined — copyText falls back to an execCommand
      // copy there so the button actually works on phones.
      await copyText(text);
      _flashCopied(btn);
    } catch {
      // Last resort: select the text so it can be copied by hand.
      const range = document.createRange();
      range.selectNodeContents(code);
      const sel = window.getSelection();
      sel.removeAllRanges();
      sel.addRange(range);
    }
  });
}

// Briefly flash a green check on an icon-only button, then restore its icon.
// We swap the inner Lucide <i>/<svg> rather than setting btn.textContent — the
// latter would delete the icon SVG, leaving the button blank after one copy.
function _flashCopied(btn) {
  if (btn.dataset.flashing) return;
  btn.dataset.flashing = '1';
  btn.innerHTML = '<i data-lucide="check"></i>';
  btn.classList.add('ac-fg-success'); btn.classList.remove('ac-fg-danger','ac-fg3');
  _refreshLucideIcons(btn);
  setTimeout(() => {
    btn.innerHTML = '<i data-lucide="copy"></i>';
    btn.classList.remove('ac-fg-success','ac-fg-danger','ac-fg3');
    _refreshLucideIcons(btn);
    delete btn.dataset.flashing;
  }, 1500);
}

// ── Manual install rows (Linux/Termux, Windows, macOS) ───────────────────────
// Each is its OWN dedicated row below the cloud-deploy row (NOT a cloud provider
// in the dropdown): GitHub URL + public/private (+ token for private) → the one
// command to paste into a terminal / PowerShell, shown LIVE, plus a QR of that
// command. Backed by POST /admin/deploy/command (no cloud account, nothing
// billable; the token is used to build the command and never stored). The
// non-secret URL + visibility persist so each row pre-fills.
//
// All three rows share ONE set of render / QR / copy / tips functions, driven by
// a per-platform descriptor (MANUAL_ROWS): only the element ids, the command
// builder, and the static steps/note text differ. Adding a platform = a drop-in
// app/deploy/providers/<id>.py + one descriptor here + one row in the HTML.

// ── Shared command-building primitives (mirror app/deploy/manual_common.py) ──
// The command is built RIGHT HERE in the browser so the box is NEVER empty and
// updates the instant a field changes — no server round-trip to depend on (an
// un-restarted / unreachable server must not leave the box blank). Each builder
// below is BYTE-IDENTICAL to its provider's `build_command`. The QR is still made
// server-side, on demand (see _fetchQr).
// Blank repository → install the STANDARD WebAgent repository, so the command is
// always ready to run as is (the admin only types a URL for their own fork).
const _MC_DEFAULT_REPO = 'https://github.com/botboss3000/webagent';
const _MC_PLACEHOLDER_TOKEN = 'YOUR_ACCESS_TOKEN';
const _MC_BRANCH = 'main';
// Default install folder per platform (where the repo is cloned + run from when
// the admin doesn't choose one). POSIX expands $HOME / Windows expands
// $env:USERPROFILE inside the double quotes the command wraps them in.
const _MC_DEFAULT_DIR_POSIX = '$HOME/webagent';
const _MC_DEFAULT_DIR_WINDOWS = '$env:USERPROFILE\\webagent';
const _MC_BAD_URL = "'\";\n\r\\ &|`$(){}<>";
const _MC_BAD_TOKEN = "'\";\n\r\\ &|`$(){}<>@/ ";
// Looser than _MC_BAD_URL: a real path legitimately has spaces, \\, :, $, % — only
// reject shell metacharacters. Mirror of manual_common.BAD_DIR.
const _MC_BAD_DIR = "\"'" + "`" + ";\n\r|&<>(){}*?";

function _mcHasBad(s, bad) { for (const c of bad) if (s.indexOf(c) >= 0) return true; return false; }
function _mcStripScheme(u) { return u.replace(/^https?:\/\//, ''); }

// Resolve the install folder from the row input (mirror manual_common.resolve_dir).
function _mcResolveDir(dir, def) {
  const d = (dir || '').trim();
  if (!d) return def;
  if (_mcHasBad(d, _MC_BAD_DIR)) return def;
  return d;
}

// Tidy the branch the admin typed (mirror manual_common._safe). Blank → main;
// anything with a shell-breaking character → also main, so it can't break the
// one-liner's quoting.
function _mcSafeBranch(branch) {
  const b = (branch || '').trim() || _MC_BRANCH;
  return _mcHasBad(b, _MC_BAD_URL) ? _MC_BRANCH : b;
}

// Resolve the clone target from the row inputs (mirror manual_common.resolve_clone).
function _mcResolve(inp) {
  const typed = (inp.github_url || '').trim();
  let defaultRepo = !typed, warning = '', repo;
  if (defaultRepo) { repo = _MC_DEFAULT_REPO; }
  else if (_mcHasBad(typed, _MC_BAD_URL)) {
    repo = _MC_DEFAULT_REPO; defaultRepo = true;
    warning = 'That repository address isn’t a valid URL — using the standard WebAgent repository instead.';
  } else { repo = typed; }
  const priv = (inp.visibility || 'public') === 'private';
  let cloneUrl = repo, placeholderToken = false;
  if (priv) {
    let tok = (inp.token || '').trim();
    if (tok && _mcHasBad(tok, _MC_BAD_TOKEN)) { warning = 'That token contains characters that aren’t valid in a GitHub token.'; tok = ''; }
    if (!tok) { tok = _MC_PLACEHOLDER_TOKEN; placeholderToken = true; }
    cloneUrl = 'https://' + tok + '@' + _mcStripScheme(repo);
  }
  return { repo, cloneUrl, defaultRepo, placeholderToken, warning };
}

// The optional pre-set admin password (mirror manual_common.resolve_admin). Typed
// → carried into the setup script (WA_ADMIN_PW → BOOTSTRAP_ADMIN_PASSWORD in .env).
// Blank → no prefix: the first visitor sets the password on the setup page. An
// unsafe password falls back to first-visitor with a warning rather than splicing
// something that would break the command's quoting.
const _MC_BAD_PW = "'\"" + "`" + "$\\\n\r";
function _mcResolveAdmin(pw) {
  const p = (pw || '').trim();
  if (!p) return { prewire: false, password: '', warning: '' };
  if (_mcHasBad(p, _MC_BAD_PW)) return { prewire: false, password: '',
    warning: 'That password contains characters that can’t be placed in the command safely — use letters, digits and simple punctuation, or leave it blank to let the first visitor set it instead.' };
  const warning = p.length < 6 ? 'The admin password should be at least 6 characters.' : '';
  return { prewire: true, password: p, warning };
}

// Build the shell prefix that carries the pre-set password into the setup script;
// empty when blank. Byte-mirror of the providers' `admin_prefix`.
// POSIX (Termux/macOS): a leading `WA_ADMIN_PW='val' ` env assignment.
function _mcAdminPrefixPosix(a) {
  return a.prewire ? ("WA_ADMIN_PW='" + a.password + "' ") : '';
}
// Windows: a `$env:WA_ADMIN_PW='val'; ` assignment the child powershell inherits.
function _mcAdminPrefixWin(a) {
  return a.prewire ? ("$env:WA_ADMIN_PW='" + a.password + "'; ") : '';
}

// Linux / Termux — mirror app/deploy/providers/termux.py build_command. ONE
// command for both: install git with whatever package manager is present; if the
// folder already holds a clone, re-point its origin at the chosen repo (a graceful
// update — the setup script then pulls), else clone fresh; then hand off to
// deploy/termux-setup.sh which detects Termux vs plain Linux.
function _buildTermux(inp) {
  const r = _mcResolve(inp);
  const a = _mcResolveAdmin(inp.admin_password);
  const branch = _mcSafeBranch(inp.branch);
  const directory = _mcResolveDir(inp.install_dir, _MC_DEFAULT_DIR_POSIX);
  const command = 'SUDO=; [ "$(id -u 2>/dev/null)" = 0 ] || SUDO=sudo; '
    + 'D="' + directory + '"; '
    + 'if command -v git >/dev/null 2>&1; then :; '
    + 'elif command -v pkg >/dev/null 2>&1; then pkg install -y git; '
    + 'elif command -v apt-get >/dev/null 2>&1; then $SUDO apt-get update && $SUDO apt-get install -y git; '
    + 'elif command -v dnf >/dev/null 2>&1; then $SUDO dnf install -y git; '
    + 'elif command -v pacman >/dev/null 2>&1; then $SUDO pacman -Sy --noconfirm git; fi; '
    + '{ if [ -d "$D/.git" ]; then git -C "$D" remote set-url origin ' + r.cloneUrl + '; '
    + 'else git clone --depth 1 --branch ' + branch + ' ' + r.cloneUrl + ' "$D"; fi; } && '
    + _mcAdminPrefixPosix(a) + 'bash "$D/deploy/termux-setup.sh"';
  return { command, directory, defaultRepo: r.defaultRepo, placeholderToken: r.placeholderToken,
    warning: [r.warning, a.warning].filter(Boolean).join(' '), prewire: a.prewire };
}

// Windows — mirror app/deploy/providers/windows.py build_command (a PowerShell
// one-liner: ensure git via winget; re-point an existing clone or clone fresh;
// run the ps1).
function _buildWindows(inp) {
  const r = _mcResolve(inp);
  const a = _mcResolveAdmin(inp.admin_password);
  const branch = _mcSafeBranch(inp.branch);
  const directory = _mcResolveDir(inp.install_dir, _MC_DEFAULT_DIR_WINDOWS);
  const command = "$ErrorActionPreference='Stop'; "
    + "$repo='" + r.cloneUrl + "'; $dir=\"" + directory + "\"; "
    + "if(-not(Get-Command git -EA SilentlyContinue)){Write-Host 'Installing Git...'; "
    + "try{winget install --id Git.Git -e --source winget --accept-package-agreements --accept-source-agreements --silent}catch{}; "
    + "$env:Path=[Environment]::GetEnvironmentVariable('Path','Machine')+';'+[Environment]::GetEnvironmentVariable('Path','User')}; "
    + "if(-not(Get-Command git -EA SilentlyContinue)){Write-Host 'Git is required. Install it from https://git-scm.com/download/win then run this again.'; return}; "
    + "if(Test-Path \"$dir\\.git\"){git -C \"$dir\" remote set-url origin $repo}else{git clone --depth 1 --branch " + branch + " $repo \"$dir\"}; "
    + _mcAdminPrefixWin(a)
    + "powershell -NoProfile -ExecutionPolicy Bypass -File \"$dir\\deploy\\windows-setup.ps1\"";
  return { command, directory, defaultRepo: r.defaultRepo, placeholderToken: r.placeholderToken,
    warning: [r.warning, a.warning].filter(Boolean).join(' '), prewire: a.prewire };
}

// macOS — mirror app/deploy/providers/macos.py build_command (a Terminal
// one-liner: ensure git/Command Line Tools; re-point an existing clone or clone
// fresh; run the sh).
function _buildMac(inp) {
  const r = _mcResolve(inp);
  const a = _mcResolveAdmin(inp.admin_password);
  const branch = _mcSafeBranch(inp.branch);
  const directory = _mcResolveDir(inp.install_dir, _MC_DEFAULT_DIR_POSIX);
  const command = 'set -e; D="' + directory + '"; '
    + 'if ! command -v git >/dev/null 2>&1; then '
    + "echo 'Installing the Command Line Tools (a dialog may appear)...'; "
    + 'xcode-select --install 2>/dev/null || true; '
    + "echo 'If a dialog appeared, finish it, then paste this command again.'; fi; "
    + '{ if [ -d "$D/.git" ]; then git -C "$D" remote set-url origin ' + r.cloneUrl + '; '
    + 'else git clone --depth 1 --branch ' + branch + ' ' + r.cloneUrl + ' "$D"; fi; } && '
    + _mcAdminPrefixPosix(a) + 'bash "$D/deploy/macos-setup.sh"';
  return { command, directory, defaultRepo: r.defaultRepo, placeholderToken: r.placeholderToken,
    warning: [r.warning, a.warning].filter(Boolean).join(' '), prewire: a.prewire };
}

const _TERMUX_STEPS = [
  'On a phone: install the free Termux app, then open it. On a Linux computer: open a terminal.',
  'Scan the QR code or paste the command, then press Enter.',
  'The first run takes a few minutes while it installs everything (on a phone it also sets up a small Ubuntu environment).',
  'When it finishes, open http://localhost:8080 on that device, or http://DEVICE-IP:8080 from another device on the same network (the script prints the address).',
];
const _TERMUX_NOTE = 'On a phone the command installs WebAgent inside a small Ubuntu environment (the reliable way to run the full app on Android); on a Linux computer it installs straight onto the system. Either way it keeps running in the background and restarts itself if it stops. On a Linux computer it also restarts automatically after a reboot; on a phone, install the free Termux:Boot add-on to start it on boot. On a phone it also installs the Server Manager — type webagent in Termux to inspect, restart or diagnose the install. To stop it later: on a phone paste “proot-distro login ubuntu -- pkill -f run.py”, on Linux paste “pkill -f run.py”.';

const _WIN_STEPS = [
  "Open PowerShell: click Start, type 'PowerShell', and open it.",
  'Paste the command and press Enter. (If Windows offers to install Git, allow it.)',
  'The first run takes a few minutes while it downloads Python and installs everything.',
  'When it finishes, open http://localhost:8080 on this PC, or http://THIS-PC-IP:8080 from another device on the same network.',
];
const _WIN_NOTE = 'WebAgent installs into a folder in your user profile and runs in the background as a Scheduled Task named “WebAgent” — it starts automatically when you log in and restarts itself if it stops. It also installs the Server Manager — open a new terminal and type webagent to inspect, restart or diagnose it. To stop it later: paste “Stop-ScheduledTask -TaskName WebAgent” into PowerShell; to stop it starting on login: “Unregister-ScheduledTask -TaskName WebAgent -Confirm:$false”.';

const _MAC_STEPS = [
  "Open Terminal: press Cmd+Space, type 'Terminal', and open it.",
  'Paste the command and press Enter. (The first time, macOS may ask to install the Command Line Tools — allow it, then paste the command again.)',
  'The first run takes a few minutes while it installs everything.',
  'When it finishes, open http://localhost:8080 on this Mac, or http://THIS-MAC-IP:8080 from another device on the same network.',
];
const _MAC_NOTE = 'WebAgent installs into a folder in your home directory and runs in the background via launchd — it starts automatically when you log in and restarts itself if it stops. It also installs the Server Manager — type webagent to inspect, restart or diagnose it. To stop it later: paste “launchctl unload ~/Library/LaunchAgents/com.webagent.server.plist”; to start it again: “launchctl load -w ~/Library/LaunchAgents/com.webagent.server.plist”.';

// ── Run-only commands (start the server when it's ALREADY installed) ──────────
// No repo URL / token, nothing to clone or rebuild. Each is BYTE-IDENTICAL to its
// provider's run command (termux/windows/macos.py) so the copy box matches what
// the backend would build. They detect however the server was installed and start
// it the matching way:
//   • Linux/Termux — Termux → start_server_termux.sh (proot keep-alive); a systemd
//     Linux box → systemctl start webagent; otherwise the nohup keep-alive loop.
//   • Windows — the “WebAgent” Scheduled Task if present, else the keep-alive ps1.
//   • macOS — (re)load the launchd agent, falling back to a kickstart if loaded.
// Termux/Windows reference the install folder (so they follow a custom location);
// macOS uses the launchd agent, which is folder-independent.
function _runTermux(dir) {
  return 'if [ -n "$TERMUX_VERSION" ] || [ -d /data/data/com.termux ]; then '
    + 'bash "' + dir + '/start_server_termux.sh"; '
    + 'elif command -v systemctl >/dev/null 2>&1 && systemctl list-unit-files webagent.service >/dev/null 2>&1; then '
    + 'sudo systemctl start webagent; '
    + 'else bash "' + dir + '/deploy/start_server_linux.sh" "' + dir + '"; fi';
}
function _runWindows(dir) {
  return 'if(Get-ScheduledTask -TaskName WebAgent -EA SilentlyContinue){Start-ScheduledTask -TaskName WebAgent}'
    + 'else{powershell -NoProfile -ExecutionPolicy Bypass -File "' + dir + '\\deploy\\start_server_windows.ps1"}';
}
function _runMac() {
  return 'launchctl load -w "$HOME/Library/LaunchAgents/com.webagent.server.plist" 2>/dev/null '
    + '|| launchctl kickstart -k "gui/$(id -u)/com.webagent.server"';
}

// One descriptor per manual platform: its panel + field element ids, command
// builder, and static steps/note. The repo URL / visibility / token / admin
// password are NO LONGER per-row — they live in the shared Repo-details bar
// (_readSharedRepo); only the install folder is per-platform. `row` is the target
// PANEL id (so _wireManualTips finds the install-location label; _syncTargetPanel
// maps a dropdown value to its command). The build functions + step constants
// above are referenced here, so this list must come AFTER them.
const MANUAL_ROWS = [
  { id: 'termux', row: 'ac-deploy-linux-panel',
    dir: 'ac-tx-dir', defaultDir: _MC_DEFAULT_DIR_POSIX,
    cmd: 'ac-tx-cmd', copy: 'ac-tx-copy', qrBtn: 'ac-tx-qr-btn', status: 'ac-tx-status',
    run: 'ac-tx-run', runCopy: 'ac-tx-run-copy', runBuild: _runTermux,
    steps: 'ac-tx-steps', note: 'ac-tx-note',
    build: _buildTermux, stepsText: _TERMUX_STEPS, noteText: _TERMUX_NOTE,
    qrLabel: 'Scan this in Termux on the phone' },
  { id: 'windows', row: 'ac-deploy-win-panel',
    dir: 'ac-win-dir', defaultDir: _MC_DEFAULT_DIR_WINDOWS,
    cmd: 'ac-win-cmd', copy: 'ac-win-copy', qrBtn: 'ac-win-qr-btn', status: 'ac-win-status',
    run: 'ac-win-run', runCopy: 'ac-win-run-copy', runBuild: _runWindows,
    steps: 'ac-win-steps', note: 'ac-win-note',
    build: _buildWindows, stepsText: _WIN_STEPS, noteText: _WIN_NOTE,
    qrLabel: 'Scan to copy the command to another device' },
  { id: 'macos', row: 'ac-deploy-mac-panel',
    dir: 'ac-mac-dir', defaultDir: _MC_DEFAULT_DIR_POSIX,
    cmd: 'ac-mac-cmd', copy: 'ac-mac-copy', qrBtn: 'ac-mac-qr-btn', status: 'ac-mac-status',
    run: 'ac-mac-run', runCopy: 'ac-mac-run-copy', runBuild: _runMac,
    steps: 'ac-mac-steps', note: 'ac-mac-note',
    build: _buildMac, stepsText: _MAC_STEPS, noteText: _MAC_NOTE,
    qrLabel: 'Scan to copy the command to another device' },
];

// Read one manual target's inputs: the SHARED repo details (URL/visibility/token/
// admin password) merged with this platform's own install folder.
function _manualInputs(desc) {
  const repo = _readSharedRepo();
  return {
    github_url: repo.github_url,
    visibility: repo.visibility,
    token: repo.token,
    branch: repo.branch,
    install_dir: (_qs(desc.dir)?.value || '').trim(),
    admin_password: repo.admin_password,
    // When "Include this app's configuration" is on the command is server-built
    // (see _manualRender) — these tell the server what to pack in.
    embed_config: repo.embed_config,
    embed_sections: repo.embed_sections,
  };
}

// Pre-fill every manual panel's install folder from its saved (non-secret) config
// (the repo URL/visibility now live in the shared bar, not per-panel), then paint
// each command.
function _renderManualPrefill() {
  MANUAL_ROWS.forEach(desc => {
    const t = (_catalog && (_catalog.providers || []).find(p => p.id === desc.id)) || null;
    const cfg = (t && t.config) || {};
    const dir = _qs(desc.dir);
    if (dir && !dir.value) dir.value = cfg.install_dir || '';
    if (dir && !dir.placeholder) dir.placeholder = desc.defaultDir;
    _refreshLucideIcons(_qs(desc.row));   // Copy / QR button icons
    _manualRender(desc);                  // show the command straight away
  });
}

// Build one row's command from its current inputs and paint it into the
// always-visible box (instant, no network). If this row's QR popover is open,
// refresh its code too.
function _manualRender(desc) {
  const inp = _manualInputs(desc);
  const code = _qs(desc.cmd);
  const runCode = _qs(desc.run);
  const status = _qs(desc.status);
  const qrBtn = _qs(desc.qrBtn);

  if (inp.embed_config) {
    // "Include this app's configuration": the command carries a locked bundle of
    // this install's AI keys / vault / database, and only the SERVER holds those
    // secrets — so the browser can't build it. Fetch it from the server instead
    // (debounced), hide the QR (a QR can't hold a kilobyte-scale bundle), and show
    // the returned command. A missing/short password comes back as a warning.
    if (qrBtn) qrBtn.classList.add('ac-hidden');
    if (_qrHandle && _qrDesc === desc) closeQrPopup();
    // Only fetch for the target the admin is actually looking at — the other two
    // manual rows also render on load, but building each one gathers + encrypts
    // secrets server-side, so don't fire three requests for two hidden panels.
    const activeTarget = (_qs('ac-deploy-target')?.value || '') === desc.id;
    if (!activeTarget) { if (code) code.textContent = ''; return; }
    if (status) { status.textContent = 'Preparing command with your configuration…'; status.classList.remove('ac-fg-danger','ac-fg-success','ac-fg3'); }
    clearTimeout(_embedTimers[desc.id]);
    _embedTimers[desc.id] = setTimeout(async () => {
      let r;
      try { r = await _post('/admin/deploy/command', { provider: desc.id, ...inp, persist: false }); }
      catch {
        if (status) { status.textContent = 'Couldn’t reach the server to build the command. If you just updated WebAgent, restart it and try again.'; status.classList.add('ac-fg-danger'); status.classList.remove('ac-fg-success','ac-fg3'); }
        return;
      }
      if (code) code.textContent = r.command || '';
      if (runCode && desc.runBuild) runCode.textContent = desc.runBuild(r.install_dir || '');
      if (status) {
        if (r.warning) { status.textContent = r.warning; status.classList.add('ac-fg-danger'); status.classList.remove('ac-fg-success','ac-fg3'); }
        else if (r.embedded) { status.textContent = 'This command carries your encrypted configuration — it’s long, so use Copy (a QR can’t hold it).'; status.classList.remove('ac-fg-danger','ac-fg-success','ac-fg3'); }
        else { status.textContent = ''; status.classList.remove('ac-fg-danger','ac-fg-success','ac-fg3'); }
      }
    }, 300);
  } else {
    // Bare command — built live in the browser (never empty, no server round-trip).
    if (qrBtn) qrBtn.classList.remove('ac-hidden');
    const r = desc.build(inp);
    if (code) code.textContent = r.command;
    // The run-only (already-installed) command follows the chosen install folder
    // (Termux/Windows); macOS ignores it. Paint it from the same resolved folder.
    if (runCode && desc.runBuild) runCode.textContent = desc.runBuild(r.directory);
    // A real warning in red; otherwise a gentle note about which repo/folder is in
    // use, or a nudge to finish a private repo's token.
    if (status) {
      if (r.warning) { status.textContent = r.warning; status.classList.add('ac-fg-danger'); status.classList.remove('ac-fg-success','ac-fg3'); }
      else if (r.placeholderToken) { status.textContent = 'Enter your access token above to finish the command.'; status.classList.remove('ac-fg-danger','ac-fg-success','ac-fg3'); }
      else if (r.defaultRepo) { status.textContent = 'Installing the standard WebAgent repository — enter an address above only to install your own fork.'; status.classList.remove('ac-fg-danger','ac-fg-success','ac-fg3'); }
      else { status.textContent = ''; status.classList.remove('ac-fg-danger','ac-fg-success','ac-fg3'); }
    }
  }

  // Steps + note never change — fill them once.
  const steps = _qs(desc.steps);
  if (steps && !steps.dataset.filled) {
    steps.innerHTML = desc.stepsText.map(s => '<li>' + _esc(s) + '</li>').join('');
    steps.dataset.filled = '1';
  }
  const note = _qs(desc.note);
  if (note && !note.dataset.filled) {
    note.hidden = false; note.textContent = desc.noteText; note.dataset.filled = '1';
  }

  if (_qrHandle && _qrDesc === desc) _fetchQr(desc);   // keep an open QR in sync
}

// Save one manual panel's install folder so it pre-fills next time (the repo URL/
// visibility live in the shared bar and persist via _persistSharedRepo; the token +
// admin password are never sent). Fire-and-forget; a missing endpoint = no pre-fill.
function _manualPersist(desc) {
  if (!isAdmin()) return;
  _post('/admin/deploy/config', { provider: desc.id, config: { install_dir: (_qs(desc.dir)?.value || '').trim() } }).catch(() => {});
}

// Save the shared non-secret repo details (URL + visibility only) to the reserved
// "_repo" slot so the Repo-details bar pre-fills next time. The token + admin
// password are never persisted.
function _persistSharedRepo() {
  if (!isAdmin()) return;
  const r = _readSharedRepo();
  _post('/admin/deploy/config', { provider: '_repo', config: { github_url: r.github_url, visibility: r.visibility, branch: r.branch } }).catch(() => {});
}

// ── QR popover (mirrors Remote Access → Same network) ──
// A small click-toggled card anchored to a row's QR button, showing that row's
// current command as a scannable code on a white plate (so it reads in either
// theme). Only one is open at a time (across all rows). The QR itself is generated
// server-side on demand; while the card is open, changing any field re-fetches it
// so it stays in sync with the live command.
// ── QR popover (shared: ui/shared/js/qr-popup.js) ──────────────────────
// The shared module handles the panel, close button, drag, dismiss, and reflow.
// We keep only the deploy-specific toggle + debounced server re-fetch.

function _closeQr() {
  closeQrPopup();
  _qrHandle = null;
  _qrAnchor = null;
  _qrDesc = null;
}

function _toggleQr(desc, anchor) {
  if (_qrHandle && _qrAnchor === anchor) { _closeQr(); return; }
  const result = showQrPopup({
    anchor,
    label: desc.qrLabel,
    loading: '<div class="ac-hint ac-deploy-empty">Generating…</div>',
    className: 'ac-tx-qr-pop',
  });
  _qrHandle = result;
  _qrAnchor = anchor;
  _qrDesc = desc;
  _fetchQr(desc);
}

// Ask the server for a QR of one row's current command (debounced). Degrades to a
// clear message if the server can't make one (e.g. not yet restarted).
function _fetchQr(desc) {
  clearTimeout(_qrTimer);
  _qrTimer = setTimeout(async () => {
    if (!_qrHandle) return;
    let r;
    try {
      r = await _post('/admin/deploy/command', { provider: desc.id, ..._manualInputs(desc), persist: false });
    } catch {
      _qrHandle.setPlate('<div class="ac-hint ac-deploy-empty">Couldn’t reach the server for the QR code. If you just updated WebAgent, restart it and try again.</div>');
      return;
    }
    if (!_qrHandle) return;
    if (r && r.qr_svg) {
      _qrHandle.setPlate(r.qr_svg);
      const plate = _qrHandle.panel.querySelector('.qr-pop-plate');
      if (plate) {
        const el = plate.querySelector('svg');
        if (el) el.classList.add('ac-deploy-img-full');
      }
    } else {
      _qrHandle.setPlate('<div class="ac-hint ac-deploy-empty">QR codes need the “qrcode” package installed on the server.</div>');
    }
  }, 120);
}

// Turn every `data-tip` label in the shared Repo-details bar + the manual panels'
// install-location labels into a circled "?" help badge — the same affordance the
// cloud fields get (via _buildField). Bespoke here because this markup is hand-
// written, not built from field descriptors. Idempotent — a `wired` flag stops
// re-runs from stacking.
function _wireManualTips() {
  const sel = ['#ac-deploy-repo-row .ac-label[data-tip]', '#ac-deploy-config-row .ac-label[data-tip]']
    .concat(MANUAL_ROWS.map(d => '#' + d.row + ' .ac-label[data-tip]')).join(', ');
  document.querySelectorAll(sel).forEach(lab => {
    if (lab.dataset.tipWired) return;
    lab.dataset.tipWired = '1';
    const badge = _tipBadge(lab.dataset.tip);
    if (badge) lab.appendChild(badge);
  });
}

// Wire the shared Repo-details bar: any edit re-renders the currently-selected
// manual command; the URL/visibility also persist (non-secret). Idempotent.
function _initSharedRepo() {
  const rerenderActive = () => {
    const desc = MANUAL_ROWS.find(d => d.id === (_qs('ac-deploy-target')?.value || ''));
    if (desc) _manualRender(desc);
  };
  const url = _qs('ac-deploy-repo-url');
  if (url && !url.dataset.wired) {
    url.dataset.wired = '1';
    url.addEventListener('input', () => { _updateRepoHead(); rerenderActive(); _probeRepoSoon(); });  // instant, client-side
    url.addEventListener('change', () => { rerenderActive(); _persistSharedRepo(); _probeRepo(); });
  }
  // The clone TOGGLE button is wired for single-click by new-deployment.js (it warms
  // deploy.js first, then calls window.__deployToggleClone, set in initDeploy). No
  // click listener here, so the two never double-fire.
  const branch = _qs('ac-deploy-repo-branch');
  if (branch && !branch.dataset.wired) {
    branch.dataset.wired = '1';
    branch.addEventListener('input', rerenderActive);
    branch.addEventListener('change', () => { rerenderActive(); _persistSharedRepo(); });
  }
  const token = _qs('ac-deploy-repo-token');
  if (token && !token.dataset.wired) {
    token.dataset.wired = '1';
    token.addEventListener('input', () => { rerenderActive(); _probeRepoSoon(); });  // never persisted; re-checks access
  }
  // Reusable GitHub key: Save (store the typed key) + Remove (forget the stored one).
  const saveKeyBtn = _qs('ac-deploy-repo-token-save');
  if (saveKeyBtn && !saveKeyBtn.dataset.wired) {
    saveKeyBtn.dataset.wired = '1';
    saveKeyBtn.addEventListener('click', _saveTokenKey);
  }
  const removeKeyLink = _qs('ac-deploy-repo-token-remove');
  if (removeKeyLink && !removeKeyLink.dataset.wired) {
    removeKeyLink.dataset.wired = '1';
    removeKeyLink.addEventListener('click', _removeTokenKey);
  }
  _loadSavedKeyStatus();          // reflect any already-stored reusable key on the bar
  const pw = _qs('ac-deploy-repo-admin-pw');
  if (pw && !pw.dataset.wired) {
    pw.dataset.wired = '1';
    pw.addEventListener('input', () => { _updateEmbedNote(); rerenderActive(); });   // never persisted
  }
  // The "Set an admin password" checkbox reveals/hides the password field.
  const pwCb = _qs('ac-deploy-pw-check');
  if (pwCb && !pwCb.dataset.wired) {
    pwCb.dataset.wired = '1';
    pwCb.addEventListener('change', () => { _syncPwField(); _updateEmbedNote(); rerenderActive(); });
  }
  // The config section boxes (database / vault / llm) drive the include mode. Ticking
  // both "Database (shared)" and "Secrets vault" re-locks the AI-key + admin-password
  // controls (they're carried/handled automatically) — see _syncSharedDbLocks.
  document.querySelectorAll('#ac-deploy-config-row [data-embed-sec]').forEach(cb => {
    if (cb.dataset.wired) return;
    cb.dataset.wired = '1';
    cb.addEventListener('change', () => { _syncEmbedMode(); rerenderActive(); });
  });
  _syncEmbedMode();              // set the initial locks + note + mode line
  _updateRepoHead();             // show the default repo on the bar before any load
}

function _initManualRows() {
  MANUAL_ROWS.forEach(desc => {
    const dir = _qs(desc.dir);
    if (dir && !dir.dataset.wired) {
      dir.dataset.wired = '1';
      dir.addEventListener('input', () => _manualRender(desc));
      dir.addEventListener('change', () => { _manualRender(desc); _manualPersist(desc); });
    }
    _wireCopy(_qs(desc.copy), _qs(desc.cmd));         // idempotent (guards on its own flag)
    _wireCopy(_qs(desc.runCopy), _qs(desc.run));      // the run-only command's Copy
    const qrBtn = _qs(desc.qrBtn);
    if (qrBtn && !qrBtn.dataset.wired) {
      qrBtn.dataset.wired = '1';
      qrBtn.addEventListener('click', () => _toggleQr(desc, qrBtn));
    }
  });
  _initSharedRepo();             // the shared Repo-details bar
  _wireManualTips();             // circled "?" help badges on the repo bar + panels
}

// ── Field-help popover ───────────────────────────────────────────────────────
// A small circled "?" beside a label. CLICK / TAP opens a real help bubble
// (hover also previews it on a mouse). The native `title=` tooltip was hover-
// only — it did nothing on click and nothing at all on touch. One shared
// floating bubble is appended to <body> so it can't be clipped by the panel's
// scroll box. The help text lives on the backend field descriptor (f.tip) so
// every cloud target ships its own field guidance.
let _tipPop = null;        // the shared floating bubble element
let _tipAnchor = null;     // badge it's currently pointing at
let _tipPinned = false;    // true when opened by click/tap (stays until dismissed)
let _tipHideTimer = null;  // delayed-hide handle so the pointer can cross into the bubble

// A tip can be a plain string OR a rich descriptor {text, images, link}. The rich
// bubble carries screenshots and a (possibly project-ID-driven) link, so we give
// the pointer a moment to travel from the "?" badge into the bubble before hiding
// it — otherwise its link/images would be unreachable.
function _scheduleHideTip() {
  clearTimeout(_tipHideTimer);
  _tipHideTimer = setTimeout(_hideTip, 160);
}

// ── Screenshot lightbox ──────────────────────────────────────────────────────
// One shared full-screen viewer (appended to <body>, above the tip bubble) that a
// help-bubble screenshot opens when clicked. Click anywhere on it — or press
// Escape — to close.
let _lightbox = null;
function _openLightbox(src) {
  if (!_lightbox) {
    _lightbox = document.createElement('div');
    _lightbox.className = 'ac-tip-lightbox';
    _lightbox.hidden = true;
    const img = document.createElement('img');
    img.alt = '';
    _lightbox.appendChild(img);
    _lightbox.addEventListener('click', _closeLightbox);
    document.addEventListener('keydown', (e) => { if (e.key === 'Escape') _closeLightbox(); });
    document.body.appendChild(_lightbox);
  }
  _lightbox.querySelector('img').src = src;
  _lightbox.hidden = false;
  requestAnimationFrame(() => _lightbox && _lightbox.classList.add('show'));
}
function _closeLightbox() {
  if (!_lightbox) return;
  _lightbox.classList.remove('show');
  _lightbox.hidden = true;
}

// Resolve a link descriptor {url, label, dynamic?}. A dynamic link substitutes
// {project_id} from the live project-ID field and returns null while it's blank —
// so the Service-Accounts links simply don't appear until a project is entered.
function _resolveDyn(desc) {
  if (!desc || !desc.url) return null;
  const label = desc.label || desc.url;
  if (!desc.dynamic) return { url: desc.url, label };
  const pid = _projectId();
  if (!pid) return null;
  return {
    url: desc.url.replace(/\{project_id\}/g, encodeURIComponent(pid)),
    label: label.replace(/\{project_id\}/g, pid),
  };
}

function _ensureTipPop() {
  if (_tipPop) return _tipPop;
  _tipPop = document.createElement('div');
  _tipPop.className = 'ac-tip-pop';
  _tipPop.setAttribute('role', 'tooltip');
  _tipPop.hidden = true;
  document.body.appendChild(_tipPop);
  // Dismiss a pinned bubble on outside click, Escape, scroll or resize. Clicks
  // INSIDE the bubble (its link) don't dismiss — so the link stays clickable.
  document.addEventListener('click', (e) => {
    if (!_tipPinned) return;
    if (e.target === _tipAnchor) return;
    if (_tipPop.contains(e.target)) return;
    _hideTip();
  }, true);
  document.addEventListener('keydown', (e) => { if (e.key === 'Escape') _hideTip(); });
  // Dismiss on page scroll, but NOT when the scroll happens INSIDE the bubble
  // itself (a tall image bubble scrolls internally) — capture:true would otherwise
  // catch that inner scroll and close it.
  window.addEventListener('scroll', (e) => {
    if (_tipPop.hidden) return;
    if (e.target === _tipPop || (_tipPop.contains && _tipPop.contains(e.target))) return;
    _hideTip();
  }, true);
  window.addEventListener('resize', () => { if (!_tipPop.hidden) _hideTip(); });
  // Keep a rich bubble alive while the pointer is over it (so its link/images are
  // reachable); a badge's mouseleave only schedules a delayed hide we cancel here.
  _tipPop.addEventListener('mouseenter', () => { clearTimeout(_tipHideTimer); });
  _tipPop.addEventListener('mouseleave', () => { if (!_tipPinned) _hideTip(); });
  return _tipPop;
}

function _hideTip() {
  clearTimeout(_tipHideTimer);
  if (_tipAnchor) _tipAnchor.classList.remove('is-open');
  _tipPinned = false;
  _tipAnchor = null;
  if (_tipPop) {
    _tipPop.hidden = true;
    _tipPop.classList.remove('show', 'is-rich');
    _tipPop.style.maxWidth = '';
    _tipPop.style.maxHeight = '';
  }
}

function _showTip(anchor, pinned) {
  clearTimeout(_tipHideTimer);
  const data = anchor._tipData || null;                 // rich payload, if any
  const text = data ? (data.text || '') : (anchor.dataset.tip || '');
  const link = data ? _resolveDyn(data.link) : null;    // null while dynamic + no ID
  const images = (data && data.images) || [];
  if (!text && !link && !images.length) return;
  const pop = _ensureTipPop();
  const rich = !!(link || images.length);
  pop.classList.toggle('is-rich', rich);
  if (rich) {
    pop.textContent = '';
    if (text) {
      const t = document.createElement('div');
      t.className = 'ac-tip-text';
      t.textContent = text;
      pop.appendChild(t);
    }
    if (link) {
      const a = document.createElement('a');
      a.className = 'ac-tip-link';
      a.href = link.url; a.target = '_blank'; a.rel = 'noopener noreferrer';
      a.textContent = link.label;
      pop.appendChild(a);
    }
    if (images.length) {
      const g = document.createElement('div');
      g.className = 'ac-tip-gallery';
      images.forEach((src) => {
        const im = document.createElement('img');
        im.src = src; im.alt = ''; im.loading = 'lazy';
        im.title = 'Click to enlarge';
        im.onerror = () => im.remove();      // hide a not-yet-added screenshot
        // Click a thumbnail → open it full-size in the shared lightbox. stopProp so
        // the outside-click dismiss doesn't also fire on this same click.
        im.addEventListener('click', (e) => { e.preventDefault(); e.stopPropagation(); _openLightbox(src); });
        g.appendChild(im);
      });
      pop.appendChild(g);
    }
    pop.style.maxWidth = '400px';
  } else {
    pop.textContent = text;
    pop.style.maxWidth = '';
  }
  pop.hidden = false;                       // make it measurable before placing
  _tipAnchor = anchor;
  _tipPinned = pinned || _tipPinned;
  // Light up the badge when pinned by a click, so the click visibly "lands"
  // even if a hover had already previewed the bubble.
  document.querySelectorAll('.ac-field-tip.is-open').forEach(el => { if (el !== anchor) el.classList.remove('is-open'); });
  anchor.classList.toggle('is-open', _tipPinned);
  const margin = 8, gap = 8;
  const r = anchor.getBoundingClientRect();
  // Place the bubble on whichever side of the badge has more vertical room, and
  // cap its height to that room so a tall image bubble scrolls inside itself
  // rather than running off the top or bottom of the screen.
  const spaceBelow = window.innerHeight - r.bottom - margin - gap;
  const spaceAbove = r.top - margin - gap;
  const placeAbove = spaceAbove > spaceBelow;
  pop.style.maxHeight = Math.max(120, Math.floor(placeAbove ? spaceAbove : spaceBelow)) + 'px';
  const pr = pop.getBoundingClientRect();   // measure AFTER capping the height
  let left = r.left + r.width / 2 - pr.width / 2;
  if (left < margin) left = margin;
  if (left + pr.width > window.innerWidth - margin) left = window.innerWidth - margin - pr.width;
  let top = placeAbove ? (r.top - gap - pr.height) : (r.bottom + gap);
  if (top < margin) top = margin;
  if (top + pr.height > window.innerHeight - margin) top = window.innerHeight - margin - pr.height;
  pop.style.left = Math.round(Math.max(margin, left)) + 'px';
  pop.style.top = Math.round(Math.max(margin, top)) + 'px';
  pop.classList.add('show');
}

function _toggleTip(b) {
  if (_tipPinned && _tipAnchor === b) _hideTip();
  else _showTip(b, true);
}

// Exported so the sibling App Access → Social sign-in panel (social-auth.js) can
// reuse the exact same "?" help badge + shared floating bubble, rather than
// duplicating the popover system. The returned badge closes over this module's
// single shared bubble, so all field tips across Data Settings share one popover.
export function _tipBadge(tip) {
  // `tip` is a plain string OR a rich descriptor {text, images, link}.
  const isObj = tip && typeof tip === 'object';
  const text = isObj ? (tip.text || '') : (tip || '');
  const hasExtra = isObj && (((tip.images || []).length) || tip.link);
  if (!text && !hasExtra) return null;
  const b = document.createElement('span');
  b.className = 'ac-field-tip';
  b.textContent = '?';
  b.dataset.tip = text;                     // plain-text fallback (hover + aria)
  if (isObj) b._tipData = tip;              // rich payload consumed by _showTip
  b.tabIndex = 0;
  b.setAttribute('role', 'button');
  b.setAttribute('aria-label', 'Help: ' + (text || 'more information'));
  // preventDefault+stopPropagation so a badge inside a checkbox <label> doesn't
  // toggle the checkbox, and so the outside-click dismiss doesn't fight us.
  b.addEventListener('click', (e) => { e.preventDefault(); e.stopPropagation(); _toggleTip(b); });
  b.addEventListener('keydown', (e) => {
    if (e.key === 'Enter' || e.key === ' ') { e.preventDefault(); _toggleTip(b); }
  });
  b.addEventListener('mouseenter', () => { if (!_tipPinned) _showTip(b, false); });
  b.addEventListener('mouseleave', () => { if (!_tipPinned) _scheduleHideTip(); });
  b.addEventListener('blur', () => { if (!_tipPinned) _hideTip(); });
  return b;
}

// ── Dynamic field links + drag-drop key file ────────────────────────────────
// The live value of the project-ID field (drives the Service-Accounts links).
function _projectId() {
  const el = document.querySelector('#ac-deploy-config [data-key="project_id"]');
  return (el && el.value ? el.value.trim() : '');
}

// A helper link shown UNDER a field (e.g. "Open the Google Cloud console"). A
// `dynamic` link is built from the project ID and hidden until one is entered.
function _fieldLink(desc) {
  const a = document.createElement('a');
  a.className = 'ac-field-link';
  a.target = '_blank';
  a.rel = 'noopener noreferrer';
  if (desc.dynamic) {
    a.dataset.dyn = '1';
    a.dataset.urlTpl = desc.url;
    a.dataset.labelTpl = desc.label || desc.url;
    _applyDynLink(a);
  } else {
    a.href = desc.url;
    a.textContent = desc.label || desc.url;
  }
  return a;
}
// Fill/hide one dynamic link from the current project ID.
function _applyDynLink(a) {
  const pid = _projectId();
  if (!pid) { a.classList.add('ac-hidden'); return; }
  a.href = a.dataset.urlTpl.replace(/\{project_id\}/g, encodeURIComponent(pid));
  a.textContent = a.dataset.labelTpl.replace(/\{project_id\}/g, pid);
  a.classList.remove('ac-hidden');
}
// Re-resolve every dynamic link after the project ID changes.
function _syncDynLinks() {
  document.querySelectorAll('.ac-field-link[data-dyn]').forEach(_applyDynLink);
}

// Let the admin drop the downloaded service-account .json file onto the box; we
// read it as text and drop it in as if pasted (then fire `input` so any listeners
// and the save-gather see it).
function _wireDropzone(ta) {
  ta.classList.add('ac-dropzone');
  const stop = (e) => { e.preventDefault(); e.stopPropagation(); };
  ta.addEventListener('dragenter', (e) => { stop(e); ta.classList.add('is-dragover'); });
  ta.addEventListener('dragover', (e) => { stop(e); ta.classList.add('is-dragover'); });
  ta.addEventListener('dragleave', (e) => { stop(e); ta.classList.remove('is-dragover'); });
  ta.addEventListener('drop', (e) => {
    stop(e);
    ta.classList.remove('is-dragover');
    const file = e.dataTransfer && e.dataTransfer.files && e.dataTransfer.files[0];
    if (!file) return;
    const reader = new FileReader();
    reader.onload = () => {
      ta.value = (typeof reader.result === 'string') ? reader.result : '';
      ta.dispatchEvent(new Event('input', { bubbles: true }));
      try { ta.focus(); } catch {}
    };
    reader.readAsText(file);
  });
}

// Build one labelled input for a field descriptor {key,label,type,tip,...}.
function _buildField(f, value, kind, isSet) {
  const wrap = document.createElement('div');
  if (f.type === 'textarea' || f.full || f.hint || f.tip) wrap.style.gridColumn = '1 / -1';

  // A helper link (static, or built live from the project ID) sits directly
  // under the field TITLE — above the input — so it reads label → link → field.
  const linkEl = (f.link && f.link.url) ? _fieldLink(f.link) : null;

  if (f.type !== 'checkbox') {
    const lab = document.createElement('label');
    lab.className = 'ac-label';
    lab.textContent = f.label + (f.required ? ' *' : '');
    const tip = _tipBadge(f.tip);
    if (tip) lab.appendChild(tip);
    wrap.appendChild(lab);
    if (linkEl) wrap.appendChild(linkEl);
  }

  let inp;
  let noteEl = null;             // live "who is this for" note under a select
  if (f.type === 'select' && f.custom) {
    // A curated dropdown PLUS a "Custom…" entry that reveals a text box, so a value
    // outside the list can still be typed (e.g. any Google Cloud zone). The visible
    // <select> is only a DRIVER: the hidden-until-needed TEXT box carries the
    // data-key and holds the real value, so _gather always reads the true string —
    // a picked option is mirrored into it, a custom one is typed straight into it.
    const CUSTOM = '__custom__';
    const sel = document.createElement('select');
    sel.className = 'ac-input';
    const known = new Set();
    const notes = {};
    (f.options || []).forEach(o => {
      const opt = document.createElement('option');
      if (typeof o === 'string') { opt.value = o; opt.textContent = o; known.add(o); }
      else {
        opt.value = o.value; opt.textContent = o.label || o.value; known.add(o.value);
        if (o.note) notes[o.value] = o.note;
      }
      sel.appendChild(opt);
    });
    const customOpt = document.createElement('option');
    customOpt.value = CUSTOM;
    customOpt.textContent = f.custom_label || 'Custom…';
    sel.appendChild(customOpt);

    const txt = document.createElement('input');
    txt.type = 'text';
    txt.className = 'ac-input';
    txt.style.marginTop = '6px';
    if (f.custom_placeholder) txt.placeholder = f.custom_placeholder;
    txt.dataset.key = f.key;                 // the real field lives here
    txt.dataset.kind = kind;

    const cur = (value != null ? value : (f.default != null ? f.default : ''));
    txt.value = cur;
    const isKnown = known.has(cur);
    sel.value = isKnown ? cur : CUSTOM;       // unlisted saved value → "Custom…"
    txt.classList.toggle('ac-hidden', isKnown);

    if (Object.keys(notes).length) {
      noteEl = document.createElement('div');
      noteEl.className = 'ac-field-note';
    }
    const sync = () => {
      if (sel.value === CUSTOM) {
        txt.classList.remove('ac-hidden');               // reveal the box, keep any typed value
        try { txt.focus(); } catch {}
      } else {
        txt.value = sel.value;                // the picked zone becomes the value
        txt.classList.add('ac-hidden');
      }
      if (noteEl) noteEl.textContent = notes[sel.value] || '';
    };
    sel.addEventListener('change', sync);
    if (noteEl) noteEl.textContent = notes[sel.value] || '';

    wrap.appendChild(sel);
    wrap.appendChild(txt);
    inp = null;                               // appended above; skip the generic add
  } else if (f.type === 'select') {
    inp = document.createElement('select');
    inp.className = 'ac-input';
    const notes = {};
    (f.options || []).forEach(o => {
      const opt = document.createElement('option');
      if (typeof o === 'string') { opt.value = o; opt.textContent = o; }
      else {
        opt.value = o.value; opt.textContent = o.label || o.value;
        if (o.note) notes[o.value] = o.note;
      }
      inp.appendChild(opt);
    });
    inp.value = (value != null ? value : (f.default != null ? f.default : ''));
    if (Object.keys(notes).length) {
      noteEl = document.createElement('div');
      noteEl.className = 'ac-field-note';
      const sync = () => { noteEl.textContent = notes[inp.value] || ''; };
      inp.addEventListener('change', sync);
      sync();
    }
  } else if (f.type === 'checkbox') {
    const row = document.createElement('label');
    row.classList.add('ac-deploy-row-flex');
    inp = document.createElement('input');
    inp.type = 'checkbox';
    inp.checked = (value != null ? !!value : !!f.default);
    inp.dataset.type = 'checkbox';
    row.appendChild(inp);
    const span = document.createElement('span');
    span.className = 'ac-label';
    span.style.margin = '0';
    span.textContent = f.label;
    row.appendChild(span);
    const tip = _tipBadge(f.tip);
    if (tip) row.appendChild(tip);
    wrap.appendChild(row);
  } else if (f.type === 'textarea') {
    inp = document.createElement('textarea');
    inp.className = 'ac-input';
    inp.rows = 4;
    inp.style.fontFamily = 'var(--font-mono)';
    inp.value = (value != null ? value : '');
    if (isSet) inp.placeholder = '••• saved — leave blank to keep';
    else if (f.placeholder) inp.placeholder = f.placeholder;
    if (f.dropzone) _wireDropzone(inp);
  } else {
    inp = document.createElement('input');
    inp.type = (f.type === 'password' || kind === 'cred') ? 'password' : (f.type === 'number' ? 'number' : 'text');
    inp.className = 'ac-input';
    inp.value = (value != null ? value : (f.default != null && kind === 'cfg' ? f.default : ''));
    if (isSet) inp.placeholder = '••• saved — leave blank to keep';
    else if (f.placeholder) inp.placeholder = f.placeholder;
  }

  if (inp) { inp.dataset.key = f.key; inp.dataset.kind = kind; }
  if (inp && f.type !== 'checkbox') wrap.appendChild(inp);
  if (noteEl) wrap.appendChild(noteEl);

  // Checkbox rows have no separate title line — keep their link (rare) below.
  if (linkEl && !linkEl.parentNode) wrap.appendChild(linkEl);

  if (f.hint) {
    const h = document.createElement('div');
    h.className = 'ac-hint';
    h.style.fontSize = '11px';
    h.style.marginTop = '3px';
    h.textContent = f.hint;
    wrap.appendChild(h);
  }
  return wrap;
}

function _gather(kind) {
  const out = {};
  // Config reads the settings host AND the progressive "rest" host (a staged
  // target splits its settings across both); secrets read the cloud-key list. The
  // admin password + GitHub token are no longer gathered here — they ride the
  // shared Repo-details bar and are injected in _saveSettings.
  const sel = kind === 'cfg'
    ? '#ac-deploy-config [data-key], #ac-deploy-config-rest [data-key]'
    : '#ac-deploy-creds [data-key]';
  document.querySelectorAll(sel).forEach(inp => {
    const key = inp.dataset.key;
    if (!key) return;
    if (inp.dataset.type === 'checkbox') out[key] = !!inp.checked;
    else out[key] = (inp.value != null ? inp.value : '').trim ? inp.value.trim() : inp.value;
  });
  return out;
}

// ── Log + streaming ────────────────────────────────────────────────────────
function _logReset() {
  const log = _qs('ac-deploy-log');
  if (log) { log.hidden = false; log.textContent = ''; }
}
function _logLine(text, level) {
  const log = _qs('ac-deploy-log');
  if (!log) return;
  const mark = level === 'ok' ? '✓ ' : level === 'err' ? '✗ ' : level === 'warn' ? '! ' : '· ';
  log.textContent += mark + text + '\n';
  log.scrollTop = log.scrollHeight;
}

async function _stream(path, body, onDone) {
  const res = await fetch(apiPath(path), {
    method: 'POST',
    headers: { 'Content-Type': 'application/json', ...authHeaders() },
    body: JSON.stringify({ requesting_user_id: _userId(), ...body }),
  });
  if (!res.ok) {
    let detail = res.statusText;
    try { const j = await res.json(); detail = j.detail || detail; } catch {}
    throw new Error(detail || ('HTTP ' + res.status));
  }
  const ctype = res.headers.get('content-type') || '';
  if (!ctype.includes('ndjson') || !res.body || !res.body.getReader) {
    const obj = await res.json();
    onDone(obj.result || obj);
    return;
  }
  const reader = res.body.getReader();
  const dec = new TextDecoder();
  let buf = '';
  const drain = (chunk) => {
    buf += chunk;
    let nl;
    while ((nl = buf.indexOf('\n')) >= 0) {
      const line = buf.slice(0, nl).trim();
      buf = buf.slice(nl + 1);
      if (!line) continue;
      let ev;
      try { ev = JSON.parse(line); } catch { continue; }
      if (ev.phase === 'done') onDone(ev.result || {});
      else if (ev.message) _logLine(ev.message, ev.level);
    }
  };
  for (;;) {
    const { value, done } = await reader.read();
    if (done) break;
    drain(dec.decode(value, { stream: true }));
  }
  drain(dec.decode());
}

// ── Actions ────────────────────────────────────────────────────────────────
async function _onProviderChange() {
  _preserve = null;              // a real target switch — don't carry values across
  _renderProvider();
  if (!isAdmin()) return;
  try { await _post('/admin/deploy/select', { provider: _provider() }); } catch {}
}

async function _saveSettings() {
  if (!isAdmin()) return false;
  _setStatus('Saving…');
  try {
    // Inject the shared repo details into the cloud target: the repo URL +
    // visibility ride the (non-secret) config; the access token (private only) +
    // admin password ride the vault. The provider then clones that repo (see its
    // deploy()); the token/password are auto-discarded with the cloud key.
    const repo = _readSharedRepo();
    const config = { ..._gather('cfg'), repo_url: repo.github_url, visibility: repo.visibility };
    if (repo.branch) config.branch = repo.branch;   // blank → provider default (main)
    // "Include this app's configuration": the manager (run_deploy) reads these off
    // the saved config, gathers + encrypts the chosen sections with the admin
    // password, and writes them onto the new server as bootstrap.json.
    config.embed_config = repo.embed_config;
    config.embed_sections = repo.embed_sections;
    await _post('/admin/deploy/config', { provider: _provider(), config });
    const creds = _gather('cred');
    if (repo.admin_password) creds.admin_password = repo.admin_password;
    if (repo.visibility === 'private' && repo.token) creds.github_token = repo.token;
    // Only POST credentials when something is set (blank = keep stored).
    if (Object.values(creds).some(v => String(v || '').trim())) {
      await _post('/admin/deploy/credentials', { provider: _provider(), values: creds });
    }
    // Remember the shared repo (URL + visibility) for next time, independent of
    // which cloud target this was.
    _persistSharedRepo();
    _setStatus('Saved.', 'ok');
    _captureForm();              // keep the typed key + VM fields across the reload
    await _load();
    return true;
  } catch (e) { _setStatus(e.message, 'err'); return false; }
}

async function _test() {
  if (!isAdmin()) return;
  await _saveSettings();
  _setStatus('Testing…');
  try {
    const r = await _post('/admin/deploy/test', { provider: _provider() });
    _setStatus(r.detail || (r.ok ? 'Connected.' : 'Failed'), r.ok ? 'ok' : 'err');
  } catch (e) { _setStatus(e.message, 'err'); }
}

async function _deploy() {
  if (!isAdmin() || _busy) return;
  const p = _current();
  if (!p) return;
  // Disable Activate the instant it's clicked — a disabled <button> stops
  // dispatching clicks, so this closes the double-click race that could spin up
  // two instances (the _busy guard below isn't set until after the async save +
  // confirm, leaving a window open). It stays disabled until the page is
  // refreshed; only a cancelled confirm re-enables it so an accidental cancel
  // doesn't lock the admin out.
  const goBtn = _qs('ac-deploy-go');
  if (goBtn) goBtn.disabled = true;
  // Persist whatever's typed first, so the deploy uses the latest settings/key.
  const saved = await _saveSettings();
  if (!saved) { if (goBtn) goBtn.disabled = false; return; }
  // Word the confirm to match the target: a cloud target CREATES a billable
  // server; an "existing server" target (creates_server === false) just installs
  // onto a machine the admin already owns.
  const confirmMsg = (p.creates_server === false)
    ? ('Install WebAgent onto ' + (p.display_name || 'the server') + '?\n\n'
        + 'This connects to the server you specified and installs WebAgent on it. '
        + 'It replaces any existing WebAgent install there.')
    : ('Deploy WebAgent to ' + (p.display_name || 'the cloud') + '?\n\n'
        + 'This creates a real, billable server on your cloud account.');
  if (!window.confirm(confirmMsg)) { if (goBtn) goBtn.disabled = false; return; }
  _busy = true;
  _setStatus('Deploying…');
  _logReset();
  _logLine('Starting deploy to ' + (p.display_name || _provider()) + '…');
  try {
    await _stream('/admin/deploy/deploy', { provider: _provider() }, (result) => {
      if (result.ok) {
        _logLine(result.message || 'Done.', 'ok');
        _setStatus('Deployed.', 'ok');
        _sessionDeploy = { provider: _provider(), dep: result };  // remember for this session only
        _renderDeployResult(result);   // show now; survives the _load() re-render, clears on page reopen
      } else {
        _logLine(result.message || 'Failed.', 'err');
        _setStatus(result.message || 'Failed', 'err');
      }
    });
    await _load();
  } catch (e) { _logLine(e.message, 'err'); _setStatus(e.message, 'err'); }
  finally { _busy = false; }
}

async function _destroy() {
  if (!isAdmin() || _busy) return;
  const p = _current();
  const dep = (p && p.deployment) || {};
  if (!dep.server) return;
  // A cloud target's tear-down DELETES the machine; an "existing server" target
  // (creates_server === false) only stops the app and leaves the machine untouched.
  const destroyMsg = (p && p.creates_server === false)
    ? ('Stop WebAgent on "' + dep.server + '"?\n\n'
        + 'This stops and disables the WebAgent service on that server. The machine '
        + 'itself and its files are left untouched.')
    : ('Tear down the server "' + dep.server + '"?\n\nThis permanently deletes it.');
  if (!window.confirm(destroyMsg)) return;
  _busy = true;
  _setStatus('Tearing down…');
  _logReset();
  try {
    await _stream('/admin/deploy/destroy', { provider: _provider() }, (result) => {
      _logLine(result.message || (result.ok ? 'Done.' : 'Failed.'), result.ok ? 'ok' : 'err');
      _setStatus(result.ok ? 'Torn down.' : (result.message || 'Failed'), result.ok ? 'ok' : 'err');
      if (result.ok) _sessionDeploy = null;   // drop the session banner — the server is gone
    });
    await _load();
  } catch (e) { _logLine(e.message, 'err'); _setStatus(e.message, 'err'); }
  finally { _busy = false; }
}

// ── Reset form ──────────────────────────────────────────────────────────────
// Clears deploy output/state only, so Activate can be re-run with the same
// settings. All form fields (project, key, region, etc.) are left untouched.
function _resetForm() {
  // Clear deploy result + log.
  const resultEl = _qs('ac-deploy-result');
  if (resultEl) { resultEl.hidden = true; resultEl.innerHTML = ''; }
  const logEl = _qs('ac-deploy-log');
  if (logEl) { logEl.hidden = true; logEl.textContent = ''; }
  // Re-enable the Activate button.
  const goBtn = _qs('ac-deploy-go');
  if (goBtn) goBtn.disabled = false;
  // Reset the More panel.
  _resetMorePanel();
  // Forget the session deploy so the address banner doesn't persist.
  _sessionDeploy = null;
  // Drop the preserved-state snapshot — the next save/activate will capture afresh.
  _preserve = null;
  // Re-evaluate cloud stages so the buttons come back (for progressive targets
  // that gated them after deploy).
  _applyCloudStages();
  _setStatus('Ready to deploy again.');
}

// ── Init ───────────────────────────────────────────────────────────────────
export function initDeploy() {
  const sel = _qs('ac-deploy-provider');
  if (sel && !sel.dataset.wired) {
    sel.dataset.wired = '1';
    sel.addEventListener('change', _onProviderChange);
  }
  _qs('ac-deploy-test')?.addEventListener('click', _test);
  _qs('ac-deploy-save')?.addEventListener('click', _saveSettings);
  _qs('ac-deploy-go')?.addEventListener('click', _deploy);
  _qs('ac-deploy-reset')?.addEventListener('click', _resetForm);
  _qs('ac-deploy-destroy')?.addEventListener('click', _destroy);
  // "More" toggles the advanced-actions panel (test / save / tear-down).
  const moreBtn = _qs('ac-deploy-more-btn');
  if (moreBtn && !moreBtn.dataset.wired) {
    moreBtn.dataset.wired = '1';
    moreBtn.addEventListener('click', () => {
      const panel = _qs('ac-deploy-more-panel');
      if (!panel) return;
      const opening = panel.hidden;
      panel.hidden = !opening;
      moreBtn.setAttribute('aria-expanded', String(opening));
      moreBtn.textContent = opening ? 'More ▴' : 'More ▾';
    });
  }
  // Saved-servers picker (shown only for the profile-aware SSH target).
  _qs('ac-deploy-servers')?.addEventListener('change', _onServerSelect);
  _qs('ac-deploy-server-save')?.addEventListener('click', _onServerSave);
  // Delete is press-and-HOLD (no confirm dialog): pointerdown starts the hold, a
  // plain click is swallowed so only a completed hold ever deletes. --dz-hold makes
  // the CSS fill run for exactly _DEL_HOLD_MS.
  const _delBtn = _qs('ac-deploy-server-delete');
  if (_delBtn) {
    _delBtn.style.setProperty('--dz-hold', _DEL_HOLD_MS + 'ms');
    _delBtn.addEventListener('pointerdown', _onServerDeletePointerDown);
    _delBtn.addEventListener('click', (e) => e.preventDefault());
  }
  // Deploy-target dropdown → reveal one panel (cloud / local / linux / win / mac).
  const target = _qs('ac-deploy-target');
  if (target && !target.dataset.wired) {
    target.dataset.wired = '1';
    target.addEventListener('change', _syncTargetPanel);
  }
  _initManualRows();            // the Linux/Termux, Windows + macOS install rows
  _initBootRows();              // the Export / Import setup-bundle rows
  _initInstances();             // the local-deployments list + register-a-checkout form
  _loadInstances();             // paint the hub + siblings straight away

  // Re-load whenever the Data Settings section is shown (wired in nav.js).
  window.__refreshDeploy = _load;
  // The Repo header's clone button is a toggle driven single-click from
  // new-deployment.js (which warms this module first, then calls this).
  window.__deployToggleClone = _toggleCloneCurrentRepo;
}

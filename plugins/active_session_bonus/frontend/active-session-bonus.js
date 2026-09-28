import { authHeaders } from '/ui/shared/js/left-login.js?v=253';
import { apiPath } from '/ui/shared/js/config.js';
import { getActive } from '/ui/shared/js/accounts.js';

const API = '/api/v1/active-session-bonus';

function installStyles() {
  const href = new URL('./active-session-bonus.css', import.meta.url).href;
  if (document.querySelector(`link[data-active-session-bonus-style][href="${href}"]`)) return;
  const link = document.createElement('link');
  link.rel = 'stylesheet'; link.href = href; link.dataset.activeSessionBonusStyle = 'true';
  document.head.appendChild(link);
}
installStyles();
const HOUSE_CREDITS_PER_CENT = 10;
const offers = new Map();
// The server supplies the payable floor. Each offer supplies its own frozen
// start and expiry timestamps, which determine its linear countdown window.
const decay = { floor: 10 };
let timer = null;
let refreshTimer = null;
let renderQueued = false;
// The first offer response is a baseline, not a stream of claim events. Without
// this marker, a persisted claimed offer looks newly claimed after every reload.
let offersHydrated = false;
const activating = new Set();

async function request(path, options = {}) {
  const response = await fetch(apiPath(API + path), {
    ...options, headers: { ...authHeaders(), ...(options.headers || {}) }, cache: 'no-store',
  });
  if (!response.ok) throw new Error(`bonus request failed (${response.status})`);
  return response.json();
}

function wholeHouseCredits(amount) {
  return Math.max(0, Math.floor(Number(amount || 0) / HOUSE_CREDITS_PER_CENT) * HOUSE_CREDITS_PER_CENT);
}

// The live amount is the remaining fraction of this offer's frozen window,
// bounded by the payable floor.  A repaint or reload mid-countdown therefore
// renders the same linear value the server would lock at that instant.
function currentAmount(offer, now = Date.now()) {
  if (offer.paused_amount != null) return wholeHouseCredits(offer.paused_amount);
  const initial = Number(offer.initial_amount ?? offer.remaining_amount ?? 0);
  const floor = Math.min(initial, decay.floor);
  const offeredAt = Date.parse(offer.offered_at);
  const expiresAt = Date.parse(offer.expires_at);
  if (!Number.isFinite(offeredAt) || !Number.isFinite(expiresAt) || expiresAt <= offeredAt) return initial;
  const remainingFraction = Math.max(0, Math.min(1, (expiresAt - now) / (expiresAt - offeredAt)));
  return Math.max(floor, Math.ceil(floor + (initial - floor) * remainingFraction));
}

function nextAmountChangeAt(offer, now) {
  const initial = Number(offer.initial_amount ?? offer.remaining_amount ?? 0);
  const floor = Math.min(initial, decay.floor);
  const current = currentAmount(offer, now);
  const offeredAt = Date.parse(offer.offered_at);
  const expiresAt = Date.parse(offer.expires_at);
  const range = initial - floor;
  if (current <= floor || range <= 0 || !Number.isFinite(offeredAt) || !Number.isFinite(expiresAt) || expiresAt <= offeredAt) return Infinity;
  // `current` is rounded up. It becomes `current - 1` at this exact fraction
  // through the range, so avoid a needless once-per-second repaint loop.
  return offeredAt + (expiresAt - offeredAt) * (initial - current + 1) / range;
}

function remainingMinutes(offer, now = Date.now()) {
  const expiresAt = Date.parse(offer.expires_at);
  if (!Number.isFinite(expiresAt)) return 0;
  return Math.max(0, Math.ceil((expiresAt - now) / 60000));
}

function text(offer) {
  const amount = currentAmount(offer);
  if (offer.status === 'claimed') return `+◇${wholeHouseCredits(offer.claimed_amount)} added to your balance`;
  if (offer.status === 'activated' || offer.status === 'awaiting_progress' || offer.status === 'qualified') {
    return `◇${amount} locked · reply to advance this task and claim it`;
  }
  // This compact, live value is calculated from the server-issued timestamps,
  // rather than decrementing whatever value happened to arrive in the last poll.
  return `◇${amount} ${remainingMinutes(offer)}m`;
}

function queueRender() {
  if (renderQueued) return;
  renderQueued = true;
  requestAnimationFrame(() => {
    renderQueued = false;
    render();
  });
}

// Repaint on the offer's next visible linear-value boundary, not on an interval
// relative to module load. Long timers therefore update only when the rounded
// credit amount changes, while a chat opened midway stays exactly in step.
function scheduleLiveRender() {
  clearTimeout(timer);
  timer = null;
  const now = Date.now();
  const nextBoundary = Math.min(...[...offers.values()]
    .filter(offer => offer.status === 'offered' && offer.paused_amount == null)
    .map(offer => nextAmountChangeAt(offer, now)));
  if (!Number.isFinite(nextBoundary)) return;
  timer = setTimeout(() => {
    queueRender();
    scheduleLiveRender();
  }, Math.max(50, nextBoundary - now + 20));
}

function render() {
  document.querySelectorAll('[data-sid]').forEach(node => {
    const offer = offers.get(node.dataset.sid);
    const old = node.querySelector('.active-session-bonus-label');
    if (!offer) { old?.remove(); return; }
    const label = old || document.createElement('span');
    label.className = 'active-session-bonus-label';
    label.textContent = text(offer);
    label.dataset.status = offer.status;
    // Notification rows keep their visible content in these text containers;
    // appending beside their close button puts the reward outside that flow.
    if (!old) (node.querySelector('.sn-pill-text, .sn-row-text') || node).appendChild(label);
  });
}

function showToast(message) {
  const toast = document.createElement('div');
  toast.className = 'active-session-bonus-toast'; toast.textContent = message;
  document.body.appendChild(toast); setTimeout(() => toast.remove(), 3200);
}

async function refreshBalance() {
  const user = getActive?.();
  if (!user?.user_id) return;
  try {
    const wallet = await fetch(apiPath(`/api/v1/billing/wallet?user_id=${encodeURIComponent(user.user_id)}`), {headers: authHeaders(), cache: 'no-store'}).then(r => r.ok ? r.json() : null);
    const amount = Math.round(Number(wallet?.available_cents ?? wallet?.balance_cents ?? 0) * HOUSE_CREDITS_PER_CENT).toLocaleString();
    for (const id of ['dropdown-credits-value', 'mobile-nav-credits-value']) {
      const node = document.getElementById(id); if (node) node.textContent = amount;
    }
  } catch (_) { /* Optional balance repaint must not affect chat. */ }
}

async function refresh() {
  try {
    const data = await request('/offers');
    // Both sides use the issued start and expiry timestamps; the server supplies
    // the payable floor that bounds the linear interpolation.
    if (Number.isFinite(Number(data.decay?.floor))) decay.floor = Number(data.decay.floor);
    const next = new Map();
    for (const offer of data.offers || []) {
      const previous = offers.get(offer.session_id);
      next.set(offer.session_id, offer);
      // A claim discovered after baseline hydration is new. A claim in the first
      // response is historical state and must not replay its balance toast.
      if (offersHydrated && offer.status === 'claimed' && previous?.status !== 'claimed') {
        showToast(`+◇${wholeHouseCredits(offer.claimed_amount)} added to your balance`);
        refreshBalance();
        window.dispatchEvent(new CustomEvent('active-session-bonus:claimed', {detail: offer}));
      }
    }
    offers.clear(); next.forEach((value, key) => offers.set(key, value));
    offersHydrated = true;
    render(); scheduleLiveRender();
    // An offer can arrive after the user already opened this chat.  Activate it
    // here as well as on session changes so that delayed polling cannot lose a
    // valid reply made in response to its source closer.
    activate(activeSessionFromUrl());
  } catch (_) { /* Plugin absence/API reload leaves normal UI unchanged. */ }
}

async function activate(sessionId) {
  const offer = offers.get(sessionId);
  if (!offer || offer.status !== 'offered' || activating.has(offer.id)) return;
  activating.add(offer.id);
  try {
    const response = await request(`/offers/${encodeURIComponent(offer.id)}/activate`, {method: 'POST'});
    offer.status = response.status; offer.paused_amount = response.paused_amount; offer.remaining_amount = response.paused_amount;
    offers.set(sessionId, offer); render();
  } catch (_) { refresh();
  } finally { activating.delete(offer.id); }
}

function activeSessionFromUrl() { return new URLSearchParams(location.search).get('session'); }
window.addEventListener('webagent-active-session-changed', event => activate(event.detail?.sessionId));
// The chat shell mutates constantly (streaming text, cursors, timers).  Do not
// rescan the full document on each mutation; only repaint when a session row
// is actually inserted or removed, and coalesce that work into one frame.
const sessionRowObserver = new MutationObserver(records => {
  const changedSessionRows = records.some(record =>
    [...record.addedNodes, ...record.removedNodes].some(node =>
      node.nodeType === Node.ELEMENT_NODE &&
      (node.matches?.('[data-sid]') || node.querySelector?.('[data-sid]'))
    )
  );
  if (changedSessionRows) queueRender();
});
sessionRowObserver.observe(document.documentElement, {childList: true, subtree: true});
refresh().then(() => activate(activeSessionFromUrl()));
refreshTimer = setInterval(refresh, 30000);
window.addEventListener('beforeunload', () => {
  clearTimeout(timer);
  clearInterval(refreshTimer);
  sessionRowObserver.disconnect();
});

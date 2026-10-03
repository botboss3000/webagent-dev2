// Repository notices are server-authored FYI, separate from chat connection errors.
export function repositoryNoticeRenderer(documentObject = globalThis.document) {
  let region = null;
  return text => {
    const parent = documentObject?.getElementById('chat-above-pill');
    if (!parent) return;
    if (!region || region.parentNode !== parent) {
      region = documentObject.createElement('p');
      region.className = 'ac-hint v3-repository-notice';
      region.setAttribute('role', 'status');
      region.setAttribute('aria-live', 'polite');
      parent.prepend(region);
    }
    region.textContent = text;
    region.hidden = !text;
  };
}

export function createAdministratorNotices({ request, render = repositoryNoticeRenderer(),
  setIntervalFn = globalThis.setInterval, clearIntervalFn = globalThis.clearInterval }) {
  let session = null, generation = 0, interval = null, inFlight = false, seen = '';
  function stop() {
    ++generation; session = null; seen = '';
    if (interval !== null) clearIntervalFn(interval);
    interval = null; render('');
  }
  async function poll() {
    if (!session || inFlight) return;
    const id = session, turn = generation;
    inFlight = true;
    try {
      const result = await request('/sessions/' + encodeURIComponent(id) + '/administrator/notices');
      if (turn !== generation || id !== session) return;
      const notice = result?.notice;
      if (!notice || typeof notice.snapshot_sha256 !== 'string' ||
          !/^[a-f0-9]{64}$/.test(notice.snapshot_sha256) || typeof notice.dirty !== 'boolean' ||
          !Number.isSafeInteger(notice.ahead) || notice.ahead < 0 ||
          typeof notice.notification !== 'string' || !notice.notification.trim() ||
          notice.notification.length > 150) {
        render(''); return;
      }
      if (notice.snapshot_sha256 !== seen) {
        seen = notice.snapshot_sha256; render(notice.notification);
      }
    } catch {
      // A failed live read withdraws only this notice, leaving transport errors intact.
      if (turn === generation && id === session) render('');
    } finally {
      inFlight = false;
      // A pending old-session request never overlaps the replacement poll or renders into it.
      if (turn !== generation && session) void poll();
    }
  }
  function start(id) {
    stop();
    if (typeof id !== 'string' || !id || id.length > 128) return;
    session = id;
    interval = setIntervalFn(() => { void poll(); }, 10000);
    void poll();
  }
  return { start, stop };
}

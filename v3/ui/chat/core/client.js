import { createAdministratorNotices } from './administrator-notices.js';

// V3 transport only: cookie/CSRF request policy is supplied by the shell.
export function createChatClient({request, note, EventSourceClass = EventSource}) {
  const administratorNotices = createAdministratorNotices({ request });
  let source = null, epoch = 0;
  function disconnect() { ++epoch; administratorNotices.stop(); source?.close(); source = null; }
  async function open(id, render, publish) {
    disconnect();
    const turn = epoch;
    const data = await request('/sessions/' + encodeURIComponent(id));
    if (turn !== epoch) return;
    render(data);
    administratorNotices.start(id);
    let last = data.last_seq || 0;
    source = new EventSourceClass('/api/v3/sessions/' + encodeURIComponent(id) + '/events?after=' + last);
    source.onmessage = event => {
      if (turn !== epoch) return;
      try {
        const item = JSON.parse(event.data);
        if (item.seq <= last) return;
        last = item.seq;
        publish(item);
      } catch { note('Unable to read chat event. Reload to recover.'); }
    };
    source.onerror = () => { if (turn === epoch) note('Connection interrupted. Reconnecting…'); };
    source.onopen = () => { if (turn === epoch) note(''); };
  }
  return {
    open, disconnect,
    sessions: (status = 'active') => request('/sessions?status=' + encodeURIComponent(status)),
    search: ({q, status = 'active', continuation}) => {
      const params = new URLSearchParams({q, status, limit: '20'});
      if (continuation) params.set('continuation', continuation);
      return request('/sessions/search?' + params);
    },
    patch: (id, body) => request('/sessions/' + encodeURIComponent(id), 'PATCH', body),
    recycle: (id, body) => request('/sessions/' + encodeURIComponent(id), 'DELETE', body),
    restore: (id, body) => request('/sessions/' + encodeURIComponent(id) + '/restore', 'POST', body),
    read: (id, through_seq) => request('/sessions/' + encodeURIComponent(id) + '/read', 'PUT', {through_seq}),
    create: body => request('/sessions', 'POST', body),
    send: (id, body) => request('/sessions/' + id + '/messages', 'POST', body),
    cancel: (id, body) => request('/runs/' + id + '/cancel', 'POST', body),
    decide: (id, body) => request('/approvals/' + id + '/decision', 'POST', body),
    recover: (id, body) => request('/runs/' + id + '/recovery', 'POST', body),
    resume: (id, body) => request('/runs/' + id + '/resume', 'POST', body)
  };
}

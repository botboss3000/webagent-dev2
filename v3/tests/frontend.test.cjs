const { test } = require('node:test');
const assert = require('node:assert/strict');
const vm = require('node:vm');
const fs = require('node:fs');
const path = require('node:path');

test('reload restores the partial reply, replay is deduplicated, and final text renders safely', async () => {
  class Element {
    constructor() {
      this.children = []; this.textContent = ''; this.hidden = false;
      this.value = ''; this.dataset = {};
      this.classList = { toggle() { return false; }, contains() { return false; }, add() {} };
      this.style = { setProperty() {} };
    }
    append(...nodes) { this.children.push(...nodes); }
    replaceChildren(...nodes) { this.children = nodes; }
    setAttribute() {}
    querySelectorAll() { return []; }
  }
  const elements = new Map();
  const get = id => {
    if (!elements.has(id)) elements.set(id, new Element());
    return elements.get(id);
  };
  let stream;
  class EventSource {
    constructor() { stream = this; }
    close() {}
  }
  const responses = {
    '/auth/setup-status': { initialized: true },
    '/auth/me': { csrf_token: 'csrf' },
    '/status': { provider: { connected: true }, policy: { rules: {} }, runs: [] },
    '/sessions': { sessions: [{ id: 's', title: 'test' }] },
    '/sessions/s': {
      session: { id: 's', title: 'test' },
      messages: [{ role: 'user', content: 'Question' }],
      runs: [], last_seq: 10, partial: { run_id: 'r', text: 'Saved prefix' },
    },
  };
  const context = {
    document: { getElementById: get, createElement: () => new Element(), body: new Element() },
    location: { href: 'http://v3/app?session_id=s' },
    history: { replaceState() {} },
    localStorage: { getItem() { return 'dark'; }, setItem() {} },
    URL, crypto: require('node:crypto').webcrypto, EventSource,
    setInterval() { return 1; }, clearInterval() {},
    fetch: async url => ({ ok: true, status: 200, json: async () => responses[url.replace('/api/v3', '')] }),
    createAdministratorNotices: () => ({ start() {}, stop() {} }),
  };
  const clientSource = fs.readFileSync(path.join(__dirname, '../ui/chat/core/client.js'), 'utf8')
    .replace(/^import .*;\n/gm, '').replace('export function', 'function');
  vm.runInNewContext(clientSource + '\nglobalThis.createChatClient = createChatClient;', context);
  const published = [];
  const client = context.createChatClient({
    request: async url => responses[url], note: () => {}, EventSourceClass: EventSource,
  });
  await client.open('s', data => {
    assert.equal(data.partial.text, 'Saved prefix');
  }, item => published.push(item));
  const emit = item => stream.onmessage({ data: JSON.stringify(item) });
  emit({ seq: 11, kind: 'delta', payload: { run_id: 'r', text: ' plus new' } });
  emit({ seq: 11, kind: 'delta', payload: { run_id: 'r', text: ' duplicate' } });
  emit({ seq: 12, kind: 'message', payload: { run_id: 'r', role: 'assistant', content: '<script>unsafe()</script>' } });
  assert.deepEqual(published.map(item => item.seq), [11, 12]);
  assert.equal(published[1].payload.content, '<script>unsafe()</script>');
  client.disconnect();
  const viewSource = fs.readFileSync(path.join(__dirname, '../ui/chat/core/message-view.js'), 'utf8')
    .replace(/^import .*;\n/gm, '').replace('export function', 'function');
  const viewContext = {
    document: { createElement: () => new Element(), createTextNode: text => ({ textContent: text }) },
    window: {}, currentChatProfile: () => ({}), _refreshLucideIcons() {},
    createBubbleGutter: () => new Element(), URL, location: context.location,
  };
  vm.runInNewContext(viewSource + '\nglobalThis.createMessage = createMessage;', viewContext);
  const bubble = viewContext.createMessage({
    id: 'final', seq: 12, run_id: 'r', role: 'assistant', content: published[1].payload.content,
  });
  const body = bubble.children[1].children[0];
  assert.equal(body.children[0].textContent, '<script>unsafe()</script>');
  assert.equal(body.innerHTML, undefined);
});

test('first-run setup validates passwords and creates a v3 admin', async () => {
  const nodes = new Map();
  const get = id => {
    if (!nodes.has(id)) nodes.set(id, {
      value: '', textContent: '', style: {}, disabled: false,
      addEventListener(type, handler) { this[type] = handler; },
    });
    return nodes.get(id);
  };
  get('username').value = 'operator';
  get('password').value = 'password-123';
  get('confirm-password').value = 'different';
  const requests = [];
  const redirects = [];
  const context = {
    document: { getElementById: get, body: { classList: { toggle() {} } } },
    localStorage: { getItem() { return 'dark'; } },
    location: { replace(path) { redirects.push(path); } },
    fetch: async (path, options) => {
      requests.push({ path, options });
      return { ok: true, json: async () => path.endsWith('setup-status') ? { initialized: false } : {} };
    },
  };
  vm.runInNewContext(fs.readFileSync(path.join(__dirname, '../ui/setup.js'), 'utf8'), context);
  await new Promise(setImmediate);
  await get('setup-form').submit({ preventDefault() {} });
  assert.equal(requests.length, 1);
  assert.equal(get('error-msg').textContent, 'Passwords do not match.');
  get('confirm-password').value = 'password-123';
  await get('setup-form').submit({ preventDefault() {} });
  assert.equal(requests[1].path, '/api/v3/auth/setup-admin');
  assert.equal(JSON.parse(requests[1].options.body).username, 'operator');
  assert.deepEqual(redirects, ['/app']);
});

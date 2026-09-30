const { test } = require('node:test');
const assert = require('node:assert/strict');
const vm = require('node:vm');
const fs = require('node:fs');
const path = require('node:path');

test('reload restores streamed text, replay is deduplicated, and final output stays safe', async () => {
  class Element {
    constructor() {
      this.children = []; this.textContent = ''; this.hidden = false;
      this.value = ''; this.dataset = {};
      this.classList = { toggle() { return false; }, contains() { return false; } };
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
  };
  vm.runInNewContext(fs.readFileSync(path.join(__dirname, '../ui/app.js'), 'utf8'), context);
  for (let i = 0; i < 10; i++) await new Promise(setImmediate);
  const transcript = get('chat-messages-inner');
  assert.equal(transcript.children.length, 2);
  assert.equal(transcript.children[1].children[1].textContent, 'Saved prefix');
  const emit = item => stream.onmessage({ data: JSON.stringify(item) });
  emit({ seq: 11, kind: 'delta', payload: { run_id: 'r', text: ' plus new' } });
  emit({ seq: 11, kind: 'delta', payload: { run_id: 'r', text: ' duplicate' } });
  assert.equal(transcript.children[1].children[1].textContent, 'Saved prefix plus new');
  emit({ seq: 12, kind: 'message', payload: { run_id: 'r', role: 'assistant', content: '<script>unsafe()</script>' } });
  assert.equal(transcript.children.length, 2);
  assert.equal(transcript.children[1].children[1].textContent, '<script>unsafe()</script>');
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

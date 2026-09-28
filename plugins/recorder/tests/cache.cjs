// Service-worker cache-generation contract. Run with:
// node plugins/recorder/tests/cache.cjs
const fs = require('node:fs');
const vm = require('node:vm');
const assert = require('node:assert/strict');

const source = fs.readFileSync('sw.js', 'utf8');
const stores = new Map();
const requests = [];

function key(request) {
  return typeof request === 'string' ? request : request.url;
}

function cache(name) {
  if (!stores.has(name)) stores.set(name, new Map());
  const entries = stores.get(name);
  return {
    async match(request) {
      const response = entries.get(key(request));
      return response ? response.clone() : undefined;
    },
    async put(request, response) {
      entries.set(key(request), response.clone());
    },
    async add(request) {
      entries.set(key(request), new Response('precache', {status: 200}));
    },
  };
}

const cacheStorage = {
  async open(name) { return cache(name); },
  async match(request) {
    for (const name of stores.keys()) {
      const found = await cache(name).match(request);
      if (found) return found;
    }
    return undefined;
  },
  async keys() { return [...stores.keys()]; },
  async delete(name) { return stores.delete(name); },
};

async function worker(generation) {
  let fetchHandler, installHandler;
  const context = vm.createContext({
    URL, Request, Response, Promise, Map, Set, Error, console,
    caches: cacheStorage,
    fetch: async request => {
      requests.push({generation, request});
      return new Response(`${generation}:${request.url}`, {status: 200});
    },
    self: {
      location: {origin: 'https://dev.webagent.live', href: 'https://dev.webagent.live/app'},
      addEventListener(type, handler) {
        if (type === 'fetch') fetchHandler = handler;
        if (type === 'install') installHandler = handler;
      },
      skipWaiting() {},
      clients: {matchAll: async () => [], claim() {}},
    },
  });
  const code = source.replace(/webagent-v\d+/g, `webagent-${generation}`);
  new vm.Script(code, {filename: 'sw.js'}).runInContext(context);
  const run = async request => {
    let responsePromise;
    fetchHandler({request, respondWith(value) { responsePromise = value; }});
    return responsePromise;
  };
  run.install = async () => {
    let wait;
    installHandler({waitUntil(value) { wait = value; }});
    await wait;
  };
  return run;
}

(async () => {
  const v717 = await worker('v717');
  const unversioned = new Request('https://cdn.jsdelivr.net/npm/webagent/genui.js?theme=dark');
  const first = await v717(unversioned);
  assert.equal(first.status, 200);
  assert.equal(requests.length, 1);
  const firstURL = new URL(requests[0].request.url);
  assert.equal(firstURL.searchParams.get('theme'), 'dark');
  assert.equal(firstURL.searchParams.get('__wa_build'), 'webagent-v717');

  // CDN assets remain stale-while-revalidate: cached content is returned, but
  // the revalidation request still carries the generation qualifier.
  const second = await v717(unversioned);
  assert.equal(await second.text(), await first.clone().text());
  await new Promise(resolve => setImmediate(resolve));
  assert.equal(requests.length, 2);
  assert.equal(new URL(requests[1].request.url).searchParams.get('__wa_build'), 'webagent-v717');

  // Same-generation executable imports are cache-first after one qualified fetch.
  const moduleRequest = new Request('https://dev.webagent.live/ui/genui.js?x=1');
  await v717(moduleRequest);
  await new Promise(resolve => setImmediate(resolve));
  const beforeCachedHit = requests.length;
  const cached = await v717(moduleRequest);
  assert.equal(requests.length, beforeCachedHit, 'same generation must not refetch code');
  assert.match(await cached.text(), /v717/);
  const moduleURL = new URL(requests[beforeCachedHit - 1].request.url);
  assert.equal(moduleURL.searchParams.get('x'), '1');
  assert.equal(moduleURL.searchParams.get('__wa_build'), 'webagent-v717');

  // A new worker generation gets a fresh qualified URL even when the original
  // request is byte-for-byte identical.
  const v718 = await worker('v718');
  await v718(moduleRequest);
  await new Promise(resolve => setImmediate(resolve));
  const last = requests.at(-1);
  assert.equal(new URL(last.request.url).searchParams.get('__wa_build'), 'webagent-v718');
  assert.match(await (await v718(moduleRequest)).text(), /v718/);
  assert.equal(requests.at(-1).generation, 'v718');

  // Navigations stay network-first and preserve their URL exactly.
  const navigation = new Request('https://dev.webagent.live/app');
  Object.defineProperty(navigation, 'mode', {value: 'navigate'});
  await v718(navigation);
  const navRequest = requests.at(-1).request;
  assert.equal(navRequest.url, navigation.url);
  assert.equal(new URL(navRequest.url).searchParams.has('__wa_build'), false);
  const v719 = await worker('v719');
  const beforeInstall = requests.length;
  await v719.install();
  const installRequests = requests.slice(beforeInstall);
  const precachedCode = installRequests.find(({request}) => request.url.includes('/ui/shared/js/appearance.js'));
  assert(precachedCode, 'install must fetch a precached code asset');
  assert.equal(new URL(precachedCode.request.url).searchParams.get('__wa_build'), 'webagent-v719');
  console.log('PASS CDN generation qualification, same-generation code cache hit, next-generation refetch, unchanged navigation, and install precache');
})().catch(error => { console.error(error); process.exitCode = 1; });

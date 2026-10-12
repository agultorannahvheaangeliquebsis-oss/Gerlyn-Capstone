'use strict';
const test = require('node:test');
const assert = require('node:assert/strict');
const fs = require('fs');
const os = require('os');
const path = require('path');

const { createHandler } = require('../../src/protocol');
const { LocalResources, LAUNCH_PATH } = require('../../src/resources');

const ORIGIN = 'https://gerlyn.up.railway.app';

function setup({ online = true, network } = {}) {
  const root = fs.mkdtempSync(path.join(os.tmpdir(), 'gerlyn-proto-'));
  const put = (rel, text) => {
    const f = path.join(root, rel);
    fs.mkdirSync(path.dirname(f), { recursive: true });
    fs.writeFileSync(f, text);
  };
  put('bundled/static/css/gerlyn.css', 'LOCAL-PLAIN');
  put('bundled/static/css/app.0123456789ab.css', 'LOCAL-HASHED');
  put('pages/launch.html', 'LAUNCH');
  const calls = [];
  const state = { online };
  const handler = createHandler({
    getOrigin: () => ORIGIN,
    resources: new LocalResources({ bundleDir: path.join(root, 'bundled'), pagesDir: path.join(root, 'pages') }),
    isOnline: () => state.online,
    fetchNetwork: async (request, init) => {
      calls.push({ url: request.url, method: request.method, init });
      return network(request, init);
    },
    staticTimeoutMs: 50,
  });
  return { handler, calls, state };
}

const ok = (text, status = 200) => async () => new Response(text, { status });
const down = async () => { throw new TypeError('net::ERR_INTERNET_DISCONNECTED'); };
const req = (p, init) => new Request(`${ORIGIN}${p}`, init);

test('hashed static files come from the bundle without touching the network', async () => {
  const { handler, calls } = setup({ network: ok('NET') });
  const res = await handler(req('/static/css/app.0123456789ab.css'));
  assert.equal(await res.text(), 'LOCAL-HASHED');
  assert.equal(res.headers.get('cache-control'), 'public, max-age=31536000, immutable');
  assert.equal(calls.length, 0);
});

test('unhashed static files: network first when online', async () => {
  const { handler, calls } = setup({ network: ok('NET') });
  const res = await handler(req('/static/css/gerlyn.css'));
  assert.equal(await res.text(), 'NET');
  assert.equal(calls.length, 1);
});

test('unhashed static files: bundle when the network is down', async () => {
  const { handler } = setup({ network: down });
  assert.equal(await (await handler(req('/static/css/gerlyn.css'))).text(), 'LOCAL-PLAIN');
});

test('unhashed static files: bundle first when known offline (no wait)', async () => {
  const { handler, calls } = setup({ online: false, network: ok('NET') });
  assert.equal(await (await handler(req('/static/css/gerlyn.css'))).text(), 'LOCAL-PLAIN');
  assert.equal(calls.length, 0);
});

test('unhashed static files: a slow server falls back to the bundle', async () => {
  // AbortSignal.timeout() timers are unref'd; a real socket would keep the loop
  // alive, so the fake does the same.
  const slow = (request, init) => new Promise((resolve, reject) => {
    const keepAlive = setTimeout(() => {}, 1000);
    init.signal.addEventListener('abort', () => {
      clearTimeout(keepAlive);
      reject(new DOMException('timeout', 'TimeoutError'));
    });
  });
  const { handler } = setup({ network: slow });
  assert.equal(await (await handler(req('/static/css/gerlyn.css'))).text(), 'LOCAL-PLAIN');
});

test('unhashed static files: a 5xx falls back, a 404 is honoured', async () => {
  let s = setup({ network: ok('boom', 503) });
  assert.equal(await (await s.handler(req('/static/css/gerlyn.css'))).text(), 'LOCAL-PLAIN');
  s = setup({ network: ok('gone', 404) });
  const res = await s.handler(req('/static/css/gerlyn.css'));
  assert.equal(res.status, 404);
});

test('a static file that is not bundled goes to the network (and fails honestly)', async () => {
  const { handler } = setup({ network: down });
  const res = await handler(req('/static/js/not-in-bundle.js'));
  assert.equal(res.type, 'error');
});

test('documents and API calls are never answered locally', async () => {
  const { handler, calls } = setup({ online: false, network: down });
  for (const p of ['/', '/inventory/', '/sales/add/', '/api/products/', '/offline/ping/']) {
    assert.equal((await handler(req(p))).type, 'error', p);
  }
  assert.equal(calls.length, 5);
});

test('writes are never answered locally, even for a /static path', async () => {
  const { handler, calls } = setup({ online: false, network: ok('NET', 201) });
  const res = await handler(req('/static/css/gerlyn.css', { method: 'POST', body: 'x' }));
  assert.equal(res.status, 201);
  assert.equal(calls.length, 1);
  assert.equal(calls[0].method, 'POST');
});

test('a 3xx from the network is returned as is, not followed', async () => {
  const redirect = async () => new Response(null, { status: 302, headers: { Location: '/login/' } });
  const { handler } = setup({ network: redirect });
  const res = await handler(req('/'));
  assert.equal(res.status, 302);
  assert.equal(res.headers.get('location'), '/login/');
});

test('launch page is served locally even with no network, never cached', async () => {
  const { handler, calls } = setup({ online: false, network: down });
  const res = await handler(req(LAUNCH_PATH));
  assert.equal(await res.text(), 'LAUNCH');
  assert.equal(res.headers.get('cache-control'), 'no-store');
  assert.equal(calls.length, 0);
});

test('HEAD returns headers and no body', async () => {
  const { handler } = setup({ online: false, network: down });
  const res = await handler(req('/static/css/gerlyn.css', { method: 'HEAD' }));
  assert.equal(res.status, 200);
  assert.equal(await res.text(), '');
  assert.equal(res.headers.get('content-length'), String('LOCAL-PLAIN'.length));
});

test('network errors are reported to the observer and surface as a network error', async () => {
  const seen = [];
  const handler = createHandler({
    getOrigin: () => ORIGIN,
    resources: new LocalResources({ bundleDir: os.tmpdir(), pagesDir: os.tmpdir() }),
    isOnline: () => true,
    fetchNetwork: down,
    onNetworkError: (e, url) => seen.push([e.message, url]),
  });
  assert.equal((await handler(req('/inventory/'))).type, 'error');
  assert.equal(seen.length, 1);
  assert.match(seen[0][0], /ERR_INTERNET_DISCONNECTED/);
});

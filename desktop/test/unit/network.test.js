'use strict';
const test = require('node:test');
const assert = require('node:assert/strict');
const { EventEmitter } = require('node:events');

const { createNetworkFetch, requestHeaders } = require('../../src/network');

/** A stand-in for electron.net whose behaviour each test scripts. */
function fakeNet(script) {
  const created = [];
  return {
    created,
    request(options) {
      const req = new EventEmitter();
      req.options = options;
      req.written = [];
      req.write = (b) => req.written.push(Buffer.from(b));
      req.abort = () => { req.aborted = true; };
      req.end = () => setImmediate(() => script(req));
      created.push(req);
      return req;
    },
  };
}

function respond(req, { status = 200, headers = {}, chunks = ['ok'], statusMessage = 'OK' } = {}) {
  const res = new EventEmitter();
  Object.assign(res, { statusCode: status, statusMessage, headers });
  req.emit('response', res);
  setImmediate(() => {
    chunks.forEach((c) => res.emit('data', Buffer.from(c)));
    res.emit('end');
  });
}

const fetchWith = (net) => createNetworkFetch({ net, session: { name: 'ses' } });
const R = (url, init) => new Request(url, init);

test('a redirect is returned as a 3xx with Location, not followed', async () => {
  const net = fakeNet((req) => {
    req.emit('redirect', 302, 'GET', 'https://s.example/login/', { 'set-cookie': ['a=1; Path=/'] });
    req.emit('error', new Error('Redirect was cancelled')); // what the loader does next
  });
  const res = await fetchWith(net)(R('https://s.example/'));
  assert.equal(res.status, 302);
  assert.equal(res.headers.get('location'), 'https://s.example/login/');
  assert.match(res.headers.get('set-cookie'), /a=1/);
});

test('streams the body and keeps status and headers', async () => {
  const net = fakeNet((req) => respond(req, { status: 201, headers: { 'content-type': 'text/plain' }, chunks: ['he', 'llo'] }));
  const res = await fetchWith(net)(R('https://s.example/x'));
  assert.equal(res.status, 201);
  assert.equal(res.headers.get('content-type'), 'text/plain');
  assert.equal(await res.text(), 'hello');
});

test('decoded body: content-encoding / content-length are not forwarded', async () => {
  const net = fakeNet((req) => respond(req, {
    headers: { 'content-encoding': 'gzip', 'content-length': '12', 'content-type': 'text/css' }, chunks: ['body{}'],
  }));
  const res = await fetchWith(net)(R('https://s.example/a.css'));
  assert.equal(res.headers.get('content-encoding'), null);
  assert.equal(res.headers.get('content-length'), null);
  assert.equal(await res.text(), 'body{}');
});

test('multiple Set-Cookie headers survive', async () => {
  const net = fakeNet((req) => respond(req, { headers: { 'set-cookie': ['a=1', 'b=2'] } }));
  const res = await fetchWith(net)(R('https://s.example/'));
  assert.deepEqual(res.headers.getSetCookie(), ['a=1', 'b=2']);
});

test('304 and HEAD produce no body', async () => {
  let net = fakeNet((req) => respond(req, { status: 304, chunks: [] }));
  assert.equal((await fetchWith(net)(R('https://s.example/x'))).status, 304);
  net = fakeNet((req) => respond(req, { chunks: [] }));
  const head = await fetchWith(net)(R('https://s.example/x', { method: 'HEAD' }));
  assert.equal(await head.text(), '');
});

test('request: method, body and content type are forwarded; cookies are left to the session', async () => {
  const net = fakeNet((req) => respond(req));
  const body = new URLSearchParams({ a: '1' });
  await fetchWith(net)(R('https://s.example/post', {
    method: 'POST', body, headers: { Cookie: 'sid=zzz', 'X-CSRFToken': 't' },
  }));
  const [req] = net.created;
  assert.equal(req.options.method, 'POST');
  assert.equal(req.options.redirect, 'manual');
  assert.equal(req.options.bypassCustomProtocolHandlers, true);
  assert.equal(req.options.credentials, 'include');
  assert.deepEqual(req.options.session, { name: 'ses' });
  assert.equal(Buffer.concat(req.written).toString(), 'a=1');
  const names = Object.keys(req.options.headers).map((n) => n.toLowerCase());
  assert.ok(names.includes('x-csrftoken'));
  assert.ok(names.includes('content-type'));
  assert.ok(!names.includes('cookie'));
});

test('credentials: omit is respected', async () => {
  const net = fakeNet((req) => respond(req));
  await fetchWith(net)(R('https://s.example/x', { credentials: 'omit' }));
  assert.equal(net.created[0].options.credentials, 'omit');
});

test('Referer / Origin are restored for same-origin form posts', () => {
  const post = R('https://s.example/sales/add/', { method: 'POST', body: 'x', referrer: 'https://s.example/sales/add/' });
  const h = requestHeaders(post, 'POST');
  assert.equal(h.Referer, 'https://s.example/sales/add/');
  assert.equal(h.Origin, 'https://s.example');
  const get = requestHeaders(R('https://s.example/x', { referrer: 'https://s.example/y' }), 'GET');
  assert.equal(get.Referer, 'https://s.example/y');
  assert.equal(get.Origin, undefined);
});

test('a network error rejects', async () => {
  const net = fakeNet((req) => req.emit('error', new Error('net::ERR_CONNECTION_REFUSED')));
  await assert.rejects(fetchWith(net)(R('https://s.example/')), /ERR_CONNECTION_REFUSED/);
});

test('an aborted signal rejects and aborts the request', async () => {
  const ctrl = new AbortController();
  const net = fakeNet(() => { /* never answers */ });
  const p = fetchWith(net)(R('https://s.example/'), { signal: ctrl.signal });
  setImmediate(() => ctrl.abort(new DOMException('timeout', 'TimeoutError')));
  await assert.rejects(p, /timeout/);
  assert.equal(net.created[0].aborted, true);
});

'use strict';
const test = require('node:test');
const assert = require('node:assert/strict');

const { ConnectivityMonitor, PING_PATH } = require('../../src/monitor');

const json = (body, status = 200) => async () => new Response(JSON.stringify(body), { status });

function monitor(fetchNetwork, changes = []) {
  return new ConnectivityMonitor({
    getOrigin: () => 'https://gerlyn.example.com',
    fetchNetwork,
    onChange: (v) => changes.push(v),
    timeoutMs: 100,
  });
}

test('unknown until probed; online on {ok:true}', async () => {
  const changes = [];
  const m = monitor(json({ ok: true, authenticated: false }), changes);
  assert.equal(m.isOnline(), null);
  assert.equal(await m.probe(), true);
  assert.equal(m.isOnline(), true);
  assert.deepEqual(changes, [true]);
});

test('probes the app ping endpoint, not just any URL', async () => {
  let seen;
  const m = monitor(async (url) => { seen = url; return new Response('{"ok":true}'); });
  await m.probe();
  assert.ok(seen.startsWith(`https://gerlyn.example.com${PING_PATH}`));
});

test('a captive portal / error page is offline, not online', async () => {
  for (const f of [
    async () => new Response('<html>Please sign in to the WiFi</html>', { status: 200 }),
    json({ ok: false }),
    json({ ok: true }, 503),
    async () => { throw new TypeError('net::ERR_NAME_NOT_RESOLVED'); },
  ]) {
    const m = monitor(f);
    assert.equal(await m.probe(), false);
    assert.equal(m.isOnline(), false);
  }
});

test('onChange fires on transitions only', async () => {
  const changes = [];
  let up = true;
  const m = monitor(async () => {
    if (!up) throw new TypeError('down');
    return new Response('{"ok":true}');
  }, changes);
  await m.probe(); await m.probe();
  up = false;
  await m.probe(); await m.probe();
  up = true;
  await m.probe();
  assert.deepEqual(changes, [true, false, true]);
});

test('concurrent probes share one request', async () => {
  let n = 0;
  const m = monitor(async () => { n += 1; return new Response('{"ok":true}'); });
  await Promise.all([m.probe(), m.probe(), m.probe()]);
  assert.equal(n, 1);
});

// Run with:  node --test offline_sync/js_tests/
//
// Exercises the real static/js/offline-sync.js and templates/sw.js against the
// fakes in harness.mjs. The Django side of the same contract is covered by
// offline_sync/tests.py.
import assert from 'node:assert/strict';
import { beforeEach, describe, it } from 'node:test';
import { FakeServer, bootClient, bootServiceWorker, fakeForm, tick } from './harness.mjs';

const saleForm = () => fakeForm('/sales/add/', [
  ['csrfmiddlewaretoken', 'abc'],
  ['mode_of_payment', 'Cash'], ['amount_tendered', '500'],
  ['product_id', '11'], ['quantity', '2'],
  ['product_id', '12'], ['quantity', '1'],
]);

beforeEach(() => { globalThis.__idb = null; globalThis.__local = null; });

describe('queueing while offline', () => {
  it('saves a supported form on the device instead of submitting it', async () => {
    const server = new FakeServer();
    server.reachable = false;
    const page = await bootClient({ server, offline: true });
    const ev = await page.submit(saleForm());

    assert.equal(ev.defaultPrevented, true, 'the native submit must be cancelled');
    const ops = page.opsFor('7');
    assert.equal(ops.length, 1);
    assert.equal(ops[0].type, 'sale.walkin');
    assert.equal(ops[0].status, 'pending');
    assert.equal(server.calls.length, 0, 'nothing is sent while offline');
    assert.equal(page.navState.reloaded, 1, 'the till is cleared for the next customer');
    assert.match(page.session.getItem('gvOfflineFlash'), /saved on this device/);
  });

  it('never stores the CSRF token and keeps repeated fields in order', async () => {
    const server = new FakeServer();
    server.reachable = false;
    const page = await bootClient({ server, offline: true });
    await page.submit(saleForm());
    const entries = page.opsFor('7')[0].entries;
    assert.ok(!entries.some(([k]) => k === 'csrfmiddlewaretoken'));
    assert.deepEqual(entries.filter(([k]) => k === 'product_id').map(([, v]) => v), ['11', '12']);
    assert.deepEqual(entries.filter(([k]) => k === 'quantity').map(([, v]) => v), ['2', '1']);
  });

  it('shows Offline and counts the pending change', async () => {
    const server = new FakeServer();
    server.reachable = false;
    const page = await bootClient({ server, offline: true });
    await page.submit(saleForm());
    assert.equal(page.pillLabel(), 'Offline');
  });

  it('gives each submission its own operation id', async () => {
    const server = new FakeServer();
    server.reachable = false;
    const page = await bootClient({ server, offline: true });
    await page.submit(saleForm());
    await page.submit(saleForm());
    const ids = page.opsFor('7').map((o) => o.id);
    assert.equal(new Set(ids).size, 2);
  });

  it('a double click queues one change, not two', async () => {
    const server = new FakeServer();
    server.reachable = false;
    const page = await bootClient({ server, offline: true });
    const form = saleForm();
    await Promise.all([page.submit(form), page.submit(form)]);
    assert.equal(page.opsFor('7').length, 1);
  });

  it('records the receive target and count base for the server to check', async () => {
    const server = new FakeServer();
    server.reachable = false;
    const page = await bootClient({ server, offline: true });
    await page.submit(fakeForm('/procurement/deliveries/42/receive/', [['item_id', '9'], ['quantity_received', '4']]));
    const op = page.opsFor('7')[0];
    assert.equal(op.type, 'procurement.receive_delivery');
    assert.equal(op.target, 42);
  });
});

describe('what is NOT queued', () => {
  it('blocks sensitive POSTs while offline with an explanation', async () => {
    const server = new FakeServer();
    server.reachable = false;
    const page = await bootClient({ server, offline: true });
    for (const action of [
      '/procurement/5/approve/', '/billing/9/confirm/', '/sales/4/refund/',
      '/sales/refunds/2/approve/', '/inventory/withdrawals/3/approve/', '/sales/4/status/Completed/',
    ]) {
      const ev = await page.submit(fakeForm(action, [['x', '1']]));
      assert.equal(ev.defaultPrevented, true, action);
    }
    assert.equal(page.dump().length, 0, 'none of them may enter the queue');
    assert.ok(page.toastsText().some((t) => /live connection/.test(t)));
  });

  it('leaves a submit alone when the page itself already refused it', async () => {
    const server = new FakeServer();
    const page = await bootClient({ server });
    await page.submit(saleForm(), { preventedByPage: true });
    assert.equal(page.dump().length, 0);
  });

  it('does not touch GET forms or other origins', async () => {
    const server = new FakeServer();
    server.reachable = false;
    const page = await bootClient({ server, offline: true });
    const get = fakeForm('/inventory/stock/', []); get.attrs.method = 'get';
    const ext = fakeForm('https://elsewhere.example/post', []);
    assert.equal((await page.submit(get)).defaultPrevented, false);
    assert.equal((await page.submit(ext)).defaultPrevented, false);
  });
});

describe('synchronising', () => {
  it('queues offline, then syncs on reconnect and only then says Synced', async () => {
    const server = new FakeServer();
    server.reachable = false;
    const page = await bootClient({ server, offline: true });
    await page.submit(saleForm());
    assert.equal(page.opsFor('7')[0].status, 'pending');

    server.reachable = true;
    await page.fire('online');

    const op = page.opsFor('7')[0];
    assert.equal(op.status, 'synced');
    assert.equal(op.result.label, 'SO-1');
    assert.deepEqual(op.entries, [], 'the data is dropped once the server has it');
    assert.equal(server.calls.length, 1);
    assert.equal(server.calls[0].csrf, 'tok');
    assert.equal(page.pillLabel(), 'Synced');
  });

  it('sends the fields exactly as entered plus the user and operation identity', async () => {
    const server = new FakeServer();
    server.reachable = false;
    const page = await bootClient({ server, offline: true });
    await page.submit(saleForm());
    server.reachable = true;
    await page.fire('online');
    const { env, fields } = server.calls[0];
    assert.equal(env.op_type, 'sale.walkin');
    assert.equal(env.client_user_id, '7');
    assert.match(env.client_op_id, /^[0-9a-f]{32}$/);
    assert.deepEqual(fields.filter(([k]) => k === 'mode_of_payment'), [['mode_of_payment', 'Cash']]);
  });

  it('syncs several changes in the order they were made', async () => {
    const server = new FakeServer();
    server.reachable = false;
    const page = await bootClient({ server, offline: true });
    await page.submit(saleForm());
    await page.submit(fakeForm('/inventory/stock/adjust/', [['product_id', '11'], ['counted_quantity', '5']]));
    server.reachable = true;
    await page.fire('online');
    assert.deepEqual(server.calls.map((c) => c.env.op_type), ['sale.walkin', 'inventory.count_correction']);
  });

  it('keeps a rejected change, shows why, and does not call it synced', async () => {
    const server = new FakeServer();
    server.policy = () => ({ status: 'rejected', message: 'Not enough stock for Cooking Oil — available: 0, needed: 2.' });
    server.reachable = false;
    const page = await bootClient({ server, offline: true });
    await page.submit(saleForm());
    server.reachable = true;
    await page.fire('online');

    const op = page.opsFor('7')[0];
    assert.equal(op.status, 'rejected');
    assert.match(op.message, /Not enough stock/);
    assert.ok(op.entries.length > 0, 'the change is kept so it can be retried');
    assert.equal(page.pillLabel(), 'Sync Failed');
  });

  it('reports a conflict with the server figure and keeps the change', async () => {
    const server = new FakeServer();
    server.policy = () => ({ status: 'conflict', message: 'The system quantity changed while you were offline.', current: { quantity_on_hand: 4 } });
    server.reachable = false;
    const page = await bootClient({ server, offline: true });
    await page.submit(fakeForm('/inventory/stock/adjust/', [['product_id', '11'], ['counted_quantity', '7']]));
    server.reachable = true;
    await page.fire('online');
    const op = page.opsFor('7')[0];
    assert.equal(op.status, 'conflict');
    assert.equal(op.current.quantity_on_hand, 4);
  });

  it('a dropped connection mid-sync leaves the change pending, then the retry is deduplicated', async () => {
    const server = new FakeServer();
    server.reachable = false;
    const page = await bootClient({ server, offline: true });
    await page.submit(saleForm());

    // The server commits the sale, but the response never reaches the browser.
    server.reachable = true;
    server.failNext = 'drop';
    await page.fire('online');
    assert.equal(server.applied.length, 1, 'the server did apply it');
    assert.equal(page.opsFor('7')[0].status, 'pending', 'the client cannot know, so it is NOT synced');

    await page.fire('online');
    assert.equal(server.applied.length, 1, 'the retry did not apply it again');
    assert.equal(page.opsFor('7')[0].status, 'synced');
  });

  it('a server error is a failure that stays queued, not a success', async () => {
    const server = new FakeServer();
    server.reachable = false;
    const page = await bootClient({ server, offline: true });
    await page.submit(saleForm());
    server.reachable = true;
    server.failNext = { status: 500 };
    await page.fire('online');
    const op = page.opsFor('7')[0];
    assert.equal(op.status, 'failed');
    assert.notEqual(op.status, 'synced');
    assert.equal(server.applied.length, 0);
    assert.equal(page.pillLabel(), 'Sync Failed');
  });

  it('does not sync when the session has ended, and says so', async () => {
    const server = new FakeServer();
    server.reachable = false;
    const page = await bootClient({ server, offline: true });
    await page.submit(saleForm());
    server.reachable = true;
    server.authenticated = false;
    await page.fire('online');
    assert.equal(server.calls.length, 0);
    assert.equal(page.opsFor('7')[0].status, 'pending');
  });

  it('will not sync another account\'s queue', async () => {
    const server = new FakeServer();
    server.reachable = false;
    const first = await bootClient({ server, userId: '7', offline: true });
    await first.submit(saleForm());

    // Someone else signs in on the same device.
    const second = await bootClient({ server, userId: '9' });
    server.userId = '9';
    server.reachable = true;
    await second.fire('online');
    assert.equal(server.calls.length, 0, 'user 9 must not send user 7\'s change');
    assert.equal(second.opsFor('7')[0].status, 'pending');
    assert.equal(second.opsFor('9').length, 0);
  });

  it('treats a response for a different operation id as not confirming this one', async () => {
    const server = new FakeServer();
    server.policy = () => ({ status: 'applied', message: 'ok', result: {} });
    const realFetch = server.fetch.bind(server);
    server.fetch = async (u, o) => {
      const res = await realFetch(u, o);
      if (!String(u).startsWith('/offline/sync/')) return res;
      const body = await res.json();
      return new Response(JSON.stringify({ ...body, client_op_id: 'someone-elses-op-id-000000' }), {
        status: 200, headers: { 'Content-Type': 'application/json' },
      });
    };
    server.reachable = false;
    const page = await bootClient({ server, offline: true });
    await page.submit(saleForm());
    server.reachable = true;
    await page.fire('online');
    assert.notEqual(page.opsFor('7')[0].status, 'synced');
  });

  it('retries a transient failure but not a rejection, until a person acts', async () => {
    const server = new FakeServer();
    server.policy = () => ({ status: 'rejected', message: 'No.' });
    server.reachable = false;
    const page = await bootClient({ server, offline: true });
    await page.submit(saleForm());
    server.reachable = true;
    await page.fire('online');
    const before = server.calls.length;
    await page.fire('online');
    assert.equal(server.calls.length, before, 'a rejected change is not re-sent automatically');
  });
});

describe('the POS stock overlay', () => {
  it('subtracts what this device has already sold offline', async () => {
    const server = new FakeServer();
    server.reachable = false;
    const page = await bootClient({ server, offline: true });
    const form = saleForm();
    form.elements = { branch: { value: '3' } };
    await page.submit(form);

    const map = { 11: { 3: { qty: 5, status: 'In Stock' } }, 12: { 3: { qty: 1, status: 'Low Stock' } } };
    page.win.GerlynOffline.adjustStockMap(map);
    assert.equal(map[11][3].qty, 3);
    assert.equal(map[12][3].qty, 0);
    assert.equal(map[12][3].status, 'No Stock');
  });

  it('stops subtracting once the sale is confirmed on the server', async () => {
    const server = new FakeServer();
    server.reachable = false;
    const page = await bootClient({ server, offline: true });
    await page.submit(saleForm());
    server.reachable = true;
    await page.fire('online');
    const map = { 11: { 3: { qty: 5, status: 'In Stock' } } };
    page.win.GerlynOffline.adjustStockMap(map);
    assert.equal(map[11][3].qty, 5);
  });
});

describe('service worker', () => {
  const html = (body) => new Response(`<html><head><title>x</title></head><body>${body}</body></html>`, {
    status: 200, headers: { 'Content-Type': 'text/html; charset=utf-8' },
  });

  it('serves the live page online and keeps a copy', async () => {
    let live = true;
    const sw = bootServiceWorker(async () => { if (!live) throw new TypeError('offline'); return html('LIVE'); });
    const res = await sw.request('/inventory/stock/');
    assert.match(await (await res).text(), /LIVE/);
    live = false;
    const offline = await (await sw.request('/inventory/stock/')).text();
    assert.match(offline, /LIVE/, 'the saved copy is served when the network fails');
    assert.match(offline, /name="gerlyn-saved-copy"/, 'and is marked as a saved copy');
  });

  it('prefers the network when it is there (never serves stale over live)', async () => {
    let body = 'ONE';
    const sw = bootServiceWorker(async () => html(body));
    await sw.request('/sales/add/');
    body = 'TWO';
    assert.match(await (await sw.request('/sales/add/')).text(), /TWO/);
  });

  it('shows the offline page for a navigation that was never saved', async () => {
    const sw = bootServiceWorker(async () => { throw new TypeError('offline'); });
    const res = await sw.request('/procurement/', { navigate: true });
    const text = await res.text();
    assert.match(text, /You are offline/);
    assert.match(text, /data-gv-offline-fallback/);
    assert.match(text, /offline-sync\.js/, 'the fallback page carries the queue panel');
  });

  it('lets a background fetch of an unsaved page fail honestly', async () => {
    const sw = bootServiceWorker(async () => { throw new TypeError('offline'); });
    await assert.rejects(async () => { await (await sw.request('/procurement/')); });
  });

  it('does not save redirects, errors, downloads or other sections', async () => {
    const sw = bootServiceWorker(async (req) => {
      const u = new URL(req.url);
      if (u.pathname.includes('/redir/')) return new Response(null, { status: 302, headers: { Location: '/login/' } });
      return html('PAGE');
    });
    assert.equal(await sw.request('/billing/5/'), null, 'billing is never saved');
    assert.equal(await sw.request('/reports/sales/'), null);
    assert.equal(await sw.request('/inventory/products/export/'), null, 'exports are never saved');
    assert.equal(await sw.request('/procurement/5/print/'), null);
    assert.equal(await sw.request('/procurement/portal/'), null, 'the supplier portal is never saved');
  });

  it('never intercepts a write', async () => {
    const sw = bootServiceWorker(async () => html('X'));
    assert.equal(await sw.request('/sales/add/', { method: 'POST', body: 'a=1' }), null);
  });

  it('drops saved pages when the session boundary is crossed', async () => {
    const sw = bootServiceWorker(async () => html('SECRET'));
    await sw.request('/inventory/stock/');
    assert.ok(sw.store.get('gerlyn-pages-vtest').size > 0);
    await sw.request('/logout/');
    assert.equal(sw.store.has('gerlyn-pages-vtest'), false);
  });

  it('offline, a bounce to the login page explains itself and still wipes saved pages', async () => {
    let live = true;
    const sw = bootServiceWorker(async () => { if (!live) throw new TypeError('offline'); return html('SECRET'); });
    await sw.request('/inventory/stock/');
    live = false;
    const res = await sw.request('/login/', { navigate: true });
    assert.match(await res.text(), /You are offline/);
    assert.equal(sw.store.has('gerlyn-pages-vtest'), false, 'an idle timeout must not leave saved pages behind');
  });

  it('drops saved pages when the page says the user changed', async () => {
    const sw = bootServiceWorker(async () => html('SECRET'));
    await sw.request('/inventory/stock/');
    await sw.handlers.message({ data: { type: 'CLEAR_PAGES' }, waitUntil: (p) => p });
    await tick();
    assert.equal(sw.store.has('gerlyn-pages-vtest'), false);
  });
});

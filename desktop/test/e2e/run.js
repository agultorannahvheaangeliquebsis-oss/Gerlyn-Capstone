'use strict';
/**
 * End-to-end test of the desktop shell.
 *
 * Run with Electron, not Node:   electron test/e2e/run.js [setup]
 *
 * It boots the REAL application code (src/app.js) with a throw-away profile
 * against a stand-in server (fake-server.js) that serves the repository's real
 * templates/sw.js and static/js/*.js, so what is exercised is the shipped
 * service worker + offline queue running inside the shell.
 */
const fs = require('fs');
const os = require('os');
const path = require('path');
const { app } = require('electron');

const { createFakeServer } = require('./fake-server');

const MODE = process.argv[2] === 'setup' ? 'setup' : 'main';
const profile = fs.mkdtempSync(path.join(os.tmpdir(), 'gerlyn-e2e-'));
app.setPath('userData', profile);

const results = [];
function check(name, ok, detail) {
  results.push({ name, ok: !!ok });
  console.log(`${ok ? '  PASS' : '  FAIL'}  ${name}${!ok && detail !== undefined ? `  -> ${JSON.stringify(detail)}` : ''}`);
}

const sleep = (ms) => new Promise((r) => setTimeout(r, ms));

async function until(label, fn, { timeout = 20000, interval = 150 } = {}) {
  const started = Date.now();
  let last;
  while (Date.now() - started < timeout) {
    try {
      last = await fn();
      if (last) return last;
    } catch (e) { last = e; }
    await sleep(interval);
  }
  throw new Error(`timed out waiting for: ${label} (last: ${last && last.message || last})`);
}

async function main() {
  const fake = createFakeServer();
  await fake.start();
  const external = [];

  if (MODE === 'main') process.env.GERLYN_SERVER_URL = fake.origin;
  else delete process.env.GERLYN_SERVER_URL;

  const { run } = require('../../src/app');
  const ctx = run({ showWindows: false, openExternal: (url) => { external.push(url); } });
  await ctx.ready;

  if (MODE === 'setup') await setupScenarios(ctx, fake, profile);
  else await mainScenarios(ctx, fake, external);

  await fake.stop();
}

// ── main scenarios ──────────────────────────────────────────────────────────

async function mainScenarios(ctx, fake, external) {
  const win = ctx.win;
  const wc = win.webContents;
  const js = (code) => wc.executeJavaScript(code, true);
  const urlPath = () => { try { return new URL(wc.getURL()).pathname; } catch (e) { return ''; } };
  const settled = () => wc.isLoading() === false;

  if (process.env.E2E_DEBUG) {
    for (const ev of ['did-start-navigation', 'did-navigate', 'did-fail-load', 'did-finish-load', 'did-redirect-navigation']) {
      wc.on(ev, (_e, ...a) => console.log(`    [${ev}]`, ...a.filter((x) => typeof x !== 'object').slice(0, 4)));
    }
    wc.on('console-message', (_e, level, message) => console.log(`    [console:${level}] ${message}`));
  }

  console.log('\n[1] launch, sign-in, shell hardening');
  await until('login page', () => urlPath() === '/login/' && settled());
  check('opens the configured server (redirected to /login/ when signed out)', true);

  await js('document.querySelector("form").submit()');
  await until('signed in', () => urlPath() === '/sales/add/' && settled());
  check('sign-in sets the cookie and lands on the POS page', urlPath() === '/sales/add/');

  check('page sees gerlynDesktop.isDesktop', (await js('window.gerlynDesktop && window.gerlynDesktop.isDesktop')) === true);
  check('page has no Node (require/process undefined)',
    (await js('typeof require + "," + typeof process + "," + typeof module')) === 'undefined,undefined,undefined');
  const ua = await js('navigator.userAgent');
  check('user agent carries the GerlynPLUS-Desktop token', /GerlynPLUS-Desktop\/\d/.test(ua), ua);
  check('user agent no longer says Electron', !/Electron\//.test(ua), ua);

  console.log('\n[1b] an ordinary online form post through the shell');
  await js('document.getElementById("pos-submit").click()');
  await until('redirected to the sale', () => urlPath() === '/sales/1/' && settled());
  check('POST + 302 lands on the redirect target URL (not the pre-redirect URL)', urlPath() === '/sales/1/', urlPath());
  check('the flash message is shown once, on the target page',
    (await js('document.getElementById("flash") && document.getElementById("flash").textContent')) === 'FLASH:Sale recorded');
  check('server saw Origin = the server origin (needed by Django CSRF)', fake.state.lastPost.origin === fake.origin, fake.state.lastPost);
  check('server saw a same-origin Referer', (fake.state.lastPost.referer || '').startsWith(fake.origin), fake.state.lastPost);
  check('the session cookie was sent on the post', /sid=u7/.test(fake.state.lastPost.cookie || ''), fake.state.lastPost);
  check('it was applied directly, once', fake.state.sales.length === 1 && fake.state.sales[0].direct === true);
  fake.state.sales.length = 0; // the scenarios below count queued sales from zero
  wc.loadURL(`${fake.origin}/sales/add/`);
  await until('back on the POS page', () => urlPath() === '/sales/add/' && settled());
  check('compressed static files reach the page decoded', fake.state.gzipServed > 0
    && (await js(`getComputedStyle(document.body).fontFamily`)) !== '', fake.state.gzipServed);

  console.log('\n[2] service worker + saved pages inside the shell');
  await until('service worker controlling', () => js('!!navigator.serviceWorker.controller').then((v) => v || js(
    'navigator.serviceWorker.ready.then(()=>!!navigator.serviceWorker.controller)')), { timeout: 20000 });
  check('service worker registered and controlling the page', true);

  await js(`Promise.all(['/sales/add/','/sales/','/inventory/','/procurement/'].map(u => fetch(u, {credentials:'same-origin'})))`);
  const saved = await js(`caches.keys().then(async ks => { const k = ks.find(n => n.startsWith('gerlyn-pages-')); if (!k) return 0; return (await (await caches.open(k)).keys()).length; })`);
  check('Inventory/Sales/Procurement pages were saved by the worker', saved >= 4, saved);

  const online = await js(`fetch('/static/css/gerlyn.css?x=1', {cache:'no-store'}).then(r => ({ s: r.status, src: r.headers.get('X-Gerlyn-Desktop-Source'), from: r.headers.get('X-From') }))`);
  check('online: unhashed static is served by the server (network first)', online.s === 200 && online.from === 'server' && !online.src, online);

  const lucide = await js(`fetch('https://unpkg.com/lucide@1.31.0/dist/umd/lucide.js').then(r => r.headers.get('X-Gerlyn-Desktop-Source'))`);
  check('mirrored lucide icons are served from the bundle, not unpkg', lucide === 'bundle', lucide);
  const fontCss = await js(`fetch('https://fonts.googleapis.com/css2?family=Roboto:wght@300;400;500;700&display=swap').then(r => r.headers.get('X-Gerlyn-Desktop-Source'))`);
  check('mirrored Roboto stylesheet is served from the bundle', fontCss === 'bundle', fontCss);

  console.log('\n[3] server goes away: pages, assets and the queue keep working');
  await fake.stop();
  const offlineStatic = await js(`fetch('/static/js/qty-stepper.js').then(r => ({ s: r.status, src: r.headers.get('X-Gerlyn-Desktop-Source') }))`);
  check('offline: a static file never visited before comes from the bundle',
    offlineStatic.s === 200 && offlineStatic.src === 'bundle', offlineStatic);

  wc.reload();
  await until('saved copy shown', async () => settled() && (await js('document.title')) === 'Walk-in Sale' &&
    (await js('!!document.querySelector("meta[name=gerlyn-saved-copy]")')));
  check('offline: reload shows the saved copy of the POS page', true);

  await until('offline queue ready', () => js('!!window.GerlynOffline'));
  await js('document.getElementById("pos-submit").click()');
  await until('operation queued', async () => (await js('GerlynOffline.pending()')) === 1, { timeout: 15000 });
  check('offline: submitting a sale queues it on the device (pending = 1)', true);
  check('offline: nothing reached the server', fake.state.sales.length === 0, fake.state.sales.length);
  await until('pill says Offline', async () => (await js('GerlynOffline.status().label')) === 'Offline');
  check('offline: status pill reads Offline', true);

  console.log('\n[4] connectivity returns: automatic, exactly-once sync');
  await fake.start();
  await js('GerlynOffline.syncNow()');
  await until('queue drained', async () => (await js('GerlynOffline.pending()')) === 0, { timeout: 20000 });
  check('online: the queued sale synced and the queue is empty', true);
  check('online: the server recorded exactly one sale', fake.state.sales.length === 1, fake.state.sales.length);
  check('the sale carries the client operation id', !!fake.state.sales[0].opId, fake.state.sales[0]);

  console.log('\n[5] lost response must not duplicate the transaction');
  await fake.stop();
  await js('GerlynOffline.syncNow()'); // probe fails -> goes offline
  await js('document.getElementById("pos-submit").click()');
  await until('second op queued', async () => (await js('GerlynOffline.pending()')) === 1, { timeout: 15000 });
  await fake.start();
  fake.state.dropAfterApply = 1; // the server applies it, then the response never arrives
  await js('GerlynOffline.syncNow()');
  await until('server applied it', () => fake.state.sales.length === 2);
  check('server applied the sale even though the response was lost', fake.state.sales.length === 2);
  // Whether Chromium re-sends on the reset keep-alive connection by itself or
  // the queue retries, the second delivery must be absorbed by the ledger.
  await js('GerlynOffline.syncNow()');
  await until('confirmed', async () => (await js('GerlynOffline.pending()')) === 0, { timeout: 20000 });
  check('the change ends confirmed as synced', true);
  check('the retry was answered as a duplicate by the ledger', fake.state.duplicateAnswers >= 1, fake.state.duplicateAnswers);
  check('and the transaction was applied exactly once', fake.state.sales.length === 2, fake.state.sales.length);

  console.log('\n[6] changes never sync under a different account');
  await fake.stop();
  await js('GerlynOffline.syncNow()');
  await js('document.getElementById("pos-submit").click()');
  await until('third op queued', async () => (await js('GerlynOffline.pending()')) === 1, { timeout: 15000 });
  // The server is back, but its ping answers 503 while accounts are switched,
  // so user 7's own page cannot sync its change early and make the timing of
  // this scenario depend on a race.
  fake.state.pingDown = true;
  await fake.start();
  const syncBefore = fake.state.syncRequests.length;

  // Switch accounts the way a person does: sign out, then sign in.
  const signInAs = async (userNo) => {
    wc.loadURL(`${fake.origin}/logout/`);
    await until('login page', () => urlPath() === '/login/' && settled());
    await js(`document.querySelector('input[name=user]').value = '${userNo}'; document.querySelector('form').submit()`);
    await until(`page for user ${userNo}`, async () => urlPath() === '/sales/add/' && settled() &&
      (await js('document.querySelector("meta[name=csrf-token]").content')) === `csrf-${userNo}`);
  };

  await signInAs(9);
  // Now user 9 is genuinely online and authenticated...
  fake.state.pingDown = false;
  await js('GerlynOffline.syncNow()');
  await sleep(1500);
  // ...yet user 7's change must stay put.
  check('user 9 sees none of user 7\'s queued changes', (await js('GerlynOffline.pending()')) === 0);
  const strays = fake.state.syncRequests.slice(syncBefore);
  // A request from the previous page can land after sign-out; it carries no
  // session and must have been refused, never applied.
  check('nothing was sent under user 9\'s session', strays.every((r) => r.user !== '9'), strays);
  check('server still holds exactly 2 sales', fake.state.sales.length === 2, fake.state.sales.length);

  await signInAs(7);
  // Signing back in is all it takes: the page finds the change and syncs it by itself.
  await until('user 7 change synced automatically', async () => fake.state.sales.length === 3
    && (await js('GerlynOffline.pending()')) === 0, { timeout: 25000 });
  check('signing back in as the owner of the change syncs it (3 sales total)', fake.state.sales.length === 3, fake.state.sales.length);

  console.log('\n[7] start-up with the server unreachable');
  await fake.stop();
  await js('GerlynOffline.syncNow()');
  wc.loadURL(`${fake.origin}/`);
  await until('launch page', async () => urlPath() === '/__gerlyn_desktop__/launch' && settled());
  check('unreachable server shows the local launch page', true);
  check('launch page rendered with bundled stylesheet', (await js('getComputedStyle(document.body).margin')) !== '' &&
    (await js('document.getElementById("launch-title").textContent')) === "Can't reach the server");
  check('launch page offers the saved pages', (await js('!document.getElementById("launch-saved").hidden')) === true);
  await fake.start();
  await until('returned to the app', () => urlPath() === '/sales/add/' && settled(), { timeout: 20000 });
  check('launch page returns to the app on its own when the server answers', true);

  console.log('\n[8] shell cannot be steered off the server');
  external.length = 0;
  const before = wc.getURL();
  await js(`window.location.href = 'https://example.com/steal'`);
  await sleep(800);
  check('foreign navigation is blocked', wc.getURL() === before, wc.getURL());
  check('...and offered to the default browser instead', external.includes('https://example.com/steal'), external);

  external.length = 0;
  await js(`window.open('file:///c:/windows/win.ini'); window.open('javascript:alert(1)'); void 0`);
  await sleep(500);
  check('window.open(file:/javascript:) is refused outright', external.length === 0, external);

  console.log('\n[9] permissions');
  const geo = await js(`new Promise(res => navigator.geolocation.getCurrentPosition(() => res('granted'), e => res('denied:' + e.code)))`);
  check('geolocation is denied', geo === 'denied:1', geo);
  const mic = await js(`navigator.mediaDevices.getUserMedia({audio:true}).then(() => 'granted', e => e.name)`);
  check('microphone is denied', mic === 'NotAllowedError', mic);
  const cam = await js(`navigator.mediaDevices.getUserMedia({video:true}).then(s => { s.getTracks().forEach(t => t.stop()); return 'granted'; }, e => e.name)`);
  check('camera (barcode scanner) is not blocked by policy', cam !== 'NotAllowedError', cam);

  console.log('\n[10] process-level');
  const logText = fs.readFileSync(ctx.log.file, 'utf8');
  check('the log records connectivity changes', /connectivity: (online|offline)/.test(logText));
  check('the log holds no query strings', !/\?[a-z]+=/i.test(logText.split('\n').filter((l) => /load failed/.test(l)).join('\n')));
}

// ── first-run setup ─────────────────────────────────────────────────────────

async function setupScenarios(ctx, fake, profileDir) {
  console.log('\n[setup] first run with no server configured');
  const win = await until('setup window', () => ctx.setupWin);
  const wc = win.webContents;
  const js = (code) => wc.executeJavaScript(code, true);
  await until('setup page loaded', () => !wc.isLoading());

  check('no server configured -> the setup screen opens, not a blank app', ctx.win === null && ctx.server === null);
  check('setup page styles apply under its CSP', (await js('getComputedStyle(document.body).backgroundColor')) === 'rgb(244, 246, 245)');
  check('setup page exposes only its own bridge', (await js('typeof gerlynSetup.save')) === 'function'
    && (await js('typeof require')) === 'undefined');

  const bad = await js(`gerlynSetup.save('ftp://example.com')`);
  check('rejects a non-web address', bad.ok === false && /https/i.test(bad.error), bad);
  const insecure = await js(`gerlynSetup.save('http://example.com')`);
  check('rejects plain http to a public host', insecure.ok === false, insecure);
  const unreachable = await js(`gerlynSetup.save('https://127.0.0.1:9')`);
  check('unreachable server is reported, with a way to save anyway', unreachable.ok === false && unreachable.unreachable === true, unreachable);

  const good = await js(`gerlynSetup.save(${JSON.stringify(fake.origin)})`);
  check('a reachable server is accepted', good.ok === true, good);
  await until('main window opened', () => ctx.win && ctx.win.webContents.getURL().startsWith(fake.origin));
  check('the main window opens on the saved server', true);
  const saved = JSON.parse(fs.readFileSync(path.join(profileDir, 'settings.json'), 'utf8'));
  check('the address is persisted as an origin', saved.serverUrl === fake.origin, saved);
}

// ── harness ─────────────────────────────────────────────────────────────────

const watchdog = setTimeout(() => {
  console.error('\nE2E watchdog: gave up after 150s');
  app.exit(2);
}, 150000);

app.whenReady().then(async () => {
  let failure = null;
  try {
    await main();
  } catch (error) {
    failure = error;
    console.error(`\nERROR: ${error && error.stack || error}`);
  }
  clearTimeout(watchdog);
  const failed = results.filter((r) => !r.ok).length;
  console.log(`\n${results.length - failed}/${results.length} checks passed${failure ? ' (aborted by an error)' : ''}`);
  app.exit(failed || failure ? 1 : 0);
});

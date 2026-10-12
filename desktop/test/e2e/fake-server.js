'use strict';
/**
 * A stand-in for the Django site, just faithful enough to run the REAL
 * client code (templates/sw.js, static/js/*.js) inside the desktop shell.
 *
 * What it mirrors from offline_sync/views.py: the ping and sync contracts,
 * the per-user operation ledger (a repeated id is answered `duplicate: true`
 * and never applied twice), and the wrong-user refusal. It is NOT a test of
 * Django: those rules are covered by offline_sync/tests.py.
 */
const http = require('http');
const fs = require('fs');
const path = require('path');
const zlib = require('zlib');

const REPO = path.resolve(__dirname, '..', '..', '..');
const STATIC_DIR = path.join(REPO, 'static');
const SW_SOURCE = fs.readFileSync(path.join(REPO, 'templates', 'sw.js'), 'utf8')
  .replace('{{ APP_VERSION }}', 'e2e');

const TYPES = {
  '.css': 'text/css', '.js': 'text/javascript', '.png': 'image/png', '.jpg': 'image/jpeg',
  '.json': 'application/json', '.ico': 'image/x-icon', '.svg': 'image/svg+xml',
};

function parseCookies(header) {
  const out = {};
  String(header || '').split(/;\s*/).forEach((part) => {
    const i = part.indexOf('=');
    if (i > 0) out[part.slice(0, i)] = decodeURIComponent(part.slice(i + 1));
  });
  return out;
}

function createFakeServer() {
  const state = {
    sales: [],            // what actually got recorded
    ledger: new Map(),    // `${user}:${opId}` -> result
    syncRequests: [],     // every /offline/sync/ call that reached us
    dropAfterApply: 0,    // apply, then cut the connection (a lost response)
    pingDown: false,
    gzipServed: 0,
    duplicateAnswers: 0, // sync requests the ledger recognised and did not re-apply
    lastPost: null,    // headers of the last direct (online) form post
  };
  let port = 0;
  let server = null;
  const sockets = new Set();

  function userOf(req) {
    const sid = parseCookies(req.headers.cookie).sid;
    return sid === 'u7' || sid === 'u9' ? sid.slice(1) : null;
  }

  function send(res, status, body, headers = {}) {
    res.writeHead(status, { 'Cache-Control': 'no-store', ...headers });
    res.end(body);
  }

  function json(res, status, body) {
    send(res, status, JSON.stringify(body), { 'Content-Type': 'application/json' });
  }

  function page(user, title, body) {
    const cfg = JSON.stringify({
      userId: String(user), pingUrl: '/offline/ping/', syncUrl: '/offline/sync/', version: 'e2e',
    });
    return `<!doctype html><html lang="en"><head><meta charset="utf-8">
<title>${title}</title>
<meta name="csrf-token" content="csrf-${user}">
<link rel="stylesheet" href="/static/css/gerlyn.css">
<link rel="stylesheet" href="https://fonts.googleapis.com/css2?family=Roboto:wght@300;400;500;700&amp;display=swap">
</head><body>
<div class="topbar"><div class="row"></div></div>
<main id="main"><h1>${title}</h1>${body}</main>
<script type="application/json" id="gvOfflineConfig">${cfg}</script>
<script src="/static/js/pwa.js"></script>
<script src="/static/js/offline-sync.js"></script>
</body></html>`;
  }

  const POS_FORM = `<form id="pos-form" method="post" action="/sales/add/">
<input type="hidden" name="csrfmiddlewaretoken" value="x">
<input name="product_id" value="42"><input name="quantity" value="2">
<span id="pos-item-count">2 items</span> <span id="pos-grand-total">P200.00</span>
<button type="submit" id="pos-submit">Complete sale</button></form>`;

  async function readForm(req) {
    const request = new Request('http://fake.local/', {
      method: 'POST', headers: req.headers, body: req, duplex: 'half',
    });
    return request.formData();
  }

  async function handle(req, res) {
    const url = new URL(req.url, 'http://fake.local');
    const user = userOf(req);
    const p = url.pathname;

    if (p === '/healthz/') return send(res, 200, 'ok');

    if (p === '/sw.js') {
      return send(res, 200, SW_SOURCE, { 'Content-Type': 'text/javascript', 'Cache-Control': 'no-cache' });
    }

    if (p.startsWith('/static/')) {
      const rel = decodeURIComponent(p.slice('/static/'.length));
      const file = path.resolve(STATIC_DIR, rel);
      if (!file.startsWith(STATIC_DIR + path.sep) || !fs.existsSync(file) || !fs.statSync(file).isFile()) {
        return send(res, 404, 'not found');
      }
      const headers = {
        'Content-Type': TYPES[path.extname(file)] || 'application/octet-stream',
        'X-From': 'server',
        'Cache-Control': 'no-cache',
        Vary: 'Accept-Encoding',
      };
      let data = fs.readFileSync(file);
      // WhiteNoise compresses; the shell must hand the page a decoded body.
      if (/\bgzip\b/.test(req.headers['accept-encoding'] || '')) {
        data = zlib.gzipSync(data);
        headers['Content-Encoding'] = 'gzip';
        state.gzipServed += 1;
      }
      return send(res, 200, data, headers);
    }

    if (p === '/offline/ping/') {
      if (state.pingDown) return send(res, 503, 'down');
      const body = { ok: true, authenticated: !!user, server_time: new Date().toISOString() };
      if (user) Object.assign(body, { user_id: Number(user), role: 'Branch Manager', csrf: `csrf-${user}`, operations: {} });
      return json(res, 200, body);
    }

    if (p === '/offline/sync/' && req.method === 'POST') {
      const entry = { user, at: Date.now() };
      state.syncRequests.push(entry);
      if (!user) return json(res, 401, { status: 'unauthenticated' });
      const form = await readForm(req);
      const envelope = JSON.parse(form.get('_offline'));
      Object.assign(entry, { clientUser: String(envelope.client_user_id), opId: envelope.client_op_id });
      const opId = envelope.client_op_id;
      if (String(envelope.client_user_id) !== user) {
        return json(res, 409, { status: 'wrong_user', client_op_id: opId, message: 'Queued by a different account.' });
      }
      const key = `${user}:${opId}`;
      if (state.ledger.has(key)) {
        state.duplicateAnswers += 1;
        return json(res, 200, { client_op_id: opId, status: 'applied', duplicate: true, result: state.ledger.get(key) });
      }
      if (envelope.op_type !== 'sale.walkin') {
        return json(res, 400, { status: 'online_only', client_op_id: opId, message: 'Online only.' });
      }
      const sale = {
        id: state.sales.length + 1,
        opId,
        user,
        product: form.get('product_id'),
        quantity: Number(form.get('quantity')),
      };
      state.sales.push(sale);
      const result = { label: `SO-${String(sale.id).padStart(4, '0')}`, url: `/sales/${sale.id}/` };
      state.ledger.set(key, result);
      if (state.dropAfterApply > 0) {
        state.dropAfterApply -= 1;
        req.socket.destroy(); // applied on the server, response never arrives
        return undefined;
      }
      return json(res, 200, { client_op_id: opId, status: 'applied', duplicate: false, message: 'Sale recorded.', result });
    }

    if (p === '/login/') {
      if (req.method === 'POST') {
        const params = new URLSearchParams(await new Promise((resolve) => {
          let data = '';
          req.on('data', (c) => { data += c; });
          req.on('end', () => resolve(data));
        }));
        return send(res, 302, '', {
          Location: '/sales/add/',
          'Set-Cookie': `sid=u${params.get('user') || '7'}; Path=/; HttpOnly; Max-Age=1800`,
        });
      }
      return send(res, 200, `<!doctype html><title>Login</title><form method="post" action="/login/">
<input name="user" value="7"><button type="submit">Sign in</button></form>`, { 'Content-Type': 'text/html' });
    }

    if (p === '/logout/') {
      return send(res, 302, '', { Location: '/login/', 'Set-Cookie': 'sid=; Path=/; Max-Age=0' });
    }

    if (p === '/') return send(res, 302, '', { Location: user ? '/sales/add/' : '/login/' });

    const needsLogin = ['/sales/', '/inventory/', '/procurement/'].some((x) => p.startsWith(x));
    if (needsLogin && !user) return send(res, 302, '', { Location: '/login/' });

    if (p === '/sales/add/' && req.method === 'POST') {
      state.lastPost = {
        origin: req.headers.origin || null,
        referer: req.headers.referer || null,
        contentType: req.headers['content-type'] || null,
        cookie: req.headers.cookie || null,
      };
      state.sales.push({ id: state.sales.length + 1, opId: null, user, direct: true });
      // Like django.contrib.messages: shown on the NEXT page, once.
      return send(res, 302, '', {
        Location: `/sales/${state.sales.length}/`,
        'Set-Cookie': 'flash=Sale%20recorded; Path=/; HttpOnly',
      });
    }
    if (p === '/sales/add/') return send(res, 200, page(user, 'Walk-in Sale', POS_FORM), { 'Content-Type': 'text/html; charset=utf-8' });
    if (p.startsWith('/sales/') || p.startsWith('/inventory/') || p.startsWith('/procurement/')) {
      const flash = parseCookies(req.headers.cookie).flash;
      const extra = flash ? { 'Set-Cookie': 'flash=; Path=/; Max-Age=0' } : {};
      const note = flash ? `<p id="flash">FLASH:${flash}</p>` : '';
      return send(res, 200, page(user, `Page ${p}`, `${note}<p>content of ${p}</p>`),
        { 'Content-Type': 'text/html; charset=utf-8', ...extra });
    }

    return send(res, 404, 'not found');
  }

  function listen() {
    return new Promise((resolve, reject) => {
      server = http.createServer((req, res) => {
        handle(req, res).catch((error) => {
          if (!res.headersSent) send(res, 500, String(error && error.stack || error));
        });
      });
      server.on('connection', (s) => { sockets.add(s); s.on('close', () => sockets.delete(s)); });
      server.once('error', reject);
      server.listen(port, '127.0.0.1', () => {
        port = server.address().port;
        resolve();
      });
    });
  }

  return {
    state,
    start: listen,
    stop() {
      return new Promise((resolve) => {
        if (!server) return resolve();
        sockets.forEach((s) => s.destroy());
        server.close(() => resolve());
        server = null;
        return undefined;
      });
    },
    get port() { return port; },
    get origin() { return `http://127.0.0.1:${port}`; },
  };
}

module.exports = { createFakeServer };

/* Offline queue for the internal system (Inventory, Sales, Procurement).
 *
 * What it does
 *   - Decides whether the app is really reachable (a ping, not just
 *     navigator.onLine) and shows Online / Offline / Pending Sync / Synced /
 *     Sync Failed in the top bar.
 *   - When a supported form is submitted without a connection, saves it to
 *     IndexedDB instead of losing it, and replays it against /offline/sync/
 *     when the server is reachable again.
 *   - Keeps saved pages fresh (warms the sidebar pages while online) so there
 *     is something to look at and enter data into offline. The pages
 *     themselves are cached by the service worker (templates/sw.js).
 *
 * What it never does
 *   - Never marks a change "Synced" on anything but the server's own
 *     {status: 'applied'} for that exact operation id.
 *   - Never queues anything outside ROUTES. Everything else — approvals,
 *     payment verification, refunds, account changes — is blocked while
 *     offline with an explanation, and the server refuses it regardless
 *     (offline_sync.registry is the authority; ROUTES only mirrors it).
 *   - Never sends a change as anyone but the user who queued it.
 *
 * Network failures leave a change Pending. Only a definite answer from the
 * server moves it to Synced (applied) or Sync Failed (rejected / conflict).
 */
(function () {
  'use strict';
  if (window.GerlynOffline) return;

  // ── Config ────────────────────────────────────────────────────────────
  var cfg = {};
  try {
    var cfgEl = document.getElementById('gvOfflineConfig');
    if (cfgEl) cfg = JSON.parse(cfgEl.textContent) || {};
  } catch (e) { cfg = {}; }

  function lsGet(key) { try { return window.localStorage.getItem(key); } catch (e) { return null; } }
  function lsSet(key, value) { try { window.localStorage.setItem(key, value); } catch (e) { /* private mode */ } }
  function ssGet(key) { try { return window.sessionStorage.getItem(key); } catch (e) { return null; } }
  function ssSet(key, value) { try { window.sessionStorage.setItem(key, value); } catch (e) { /* ignore */ } }
  function ssDel(key) { try { window.sessionStorage.removeItem(key); } catch (e) { /* ignore */ } }

  var onFallbackPage = !!(document.body && document.body.hasAttribute('data-gv-offline-fallback'));
  if (cfg.userId) lsSet('gvOfflineUser', String(cfg.userId));
  // The offline fallback page has no server-rendered config; it acts for
  // whoever was last signed in on this device.
  var userId = cfg.userId ? String(cfg.userId) : (onFallbackPage ? lsGet('gvOfflineUser') : null);
  if (!userId) return; // anonymous page: nothing to queue, nothing to show

  var PING_URL = cfg.pingUrl || '/offline/ping/';
  var SYNC_URL = cfg.syncUrl || '/offline/sync/';

  // Saved page copies belong to whoever was signed in. When the user changes,
  // ask the worker to drop them (it also drops them at /login/ and /logout/).
  var lastPagesUser = lsGet('gvOfflinePagesUser');
  if (cfg.userId && lastPagesUser && lastPagesUser !== String(cfg.userId)) {
    postToWorker({ type: 'CLEAR_PAGES' });
    lsSet('gvOfflineWarmAt', '0');
  }
  if (cfg.userId) lsSet('gvOfflinePagesUser', String(cfg.userId));

  function postToWorker(message) {
    try {
      if (navigator.serviceWorker && navigator.serviceWorker.controller) {
        navigator.serviceWorker.controller.postMessage(message);
      }
    } catch (e) { /* no worker: nothing saved to clear */ }
  }

  // ── What can be queued ────────────────────────────────────────────────
  // Mirrors offline_sync.registry.OPERATIONS. Purchase orders are Branch
  // Manager only there: an Owner raising one authorizes the spend, which is
  // the kind of act that must happen live.
  var ROUTES = [
    { re: /^\/sales\/add\/$/, type: 'sale.walkin', label: 'Walk-in sale', after: 'reload' },
    { re: /^\/inventory\/warehouse\/request\/$/, type: 'inventory.floor_refill', label: 'Floor refill request', after: 'clean' },
    { re: /^\/inventory\/stock\/adjust\/$/, type: 'inventory.count_correction', label: 'Stock count correction', after: 'reload' },
    { re: /^\/procurement\/add\/$/, type: 'procurement.create_po', label: 'Purchase order request', after: '/procurement/' },
    { re: /^\/procurement\/deliveries\/(\d+)\/receive\/$/, type: 'procurement.receive_delivery', label: 'Delivery receipt', after: '/procurement/deliveries/', target: 1 },
    // Data entry. Only ever CREATING a record or a request: editing one
    // offline would overwrite whatever changed while the device was away.
    { re: /^\/inventory\/products\/add\/$/, type: 'inventory.product_add', label: 'New product', after: '/inventory/products/' },
    { re: /^\/inventory\/categories\/add\/$/, type: 'inventory.category_add', label: 'New product category', after: 'clean' },
    { re: /^\/inventory\/withdrawals\/new\/$/, type: 'inventory.write_off', label: 'Write-off request', after: '/inventory/withdrawals/?view=writeoffs' },
    { re: /^\/procurement\/suppliers\/add\/$/, type: 'procurement.supplier_add', label: 'New supplier', after: '/procurement/suppliers/' },
    { re: /^\/procurement\/suppliers\/categories\/add\/$/, type: 'procurement.supplier_category_add', label: 'Supplier category', after: '/procurement/suppliers/categories/' },
    { re: /^\/procurement\/suppliers\/(\d+)\/products\/add\/$/, type: 'procurement.supplier_product_add', label: 'Supplier product', after: 'reload', target: 1 }
  ];

  function matchRoute(pathname) {
    for (var i = 0; i < ROUTES.length; i++) {
      var m = ROUTES[i].re.exec(pathname);
      if (m) return { route: ROUTES[i], match: m };
    }
    return null;
  }

  // Plain-language list of everything ROUTES can queue, for messages that must
  // not promise more (or less) than the queue really does.
  var QUEUEABLE_SUMMARY = 'walk-in sales, floor refill requests, stock count corrections, '
    + 'write-off requests, new products and categories, new suppliers, supplier categories '
    + 'and supplier products, purchase order requests and delivery receipts';

  // Labels of the queueable forms actually present on this page (modals
  // included), so a saved copy only claims what can be done on it.
  function queueableLabelsOnPage() {
    var labels = [];
    var forms = document.querySelectorAll('form[method]');
    for (var i = 0; i < forms.length; i++) {
      var form = forms[i];
      if ((form.getAttribute('method') || '').toLowerCase() !== 'post') continue;
      var action = form.getAttribute('action');
      if (!action) continue;
      var hit = null;
      try { hit = matchRoute(new URL(action, location.href).pathname); } catch (e) { hit = null; }
      if (hit && labels.indexOf(hit.route.label) === -1) labels.push(hit.route.label);
    }
    return labels;
  }

  // Pages worth keeping a saved copy of (mirrors the worker's allow-list).
  var WARM_PREFIXES = ['/inventory/', '/sales/', '/procurement/'];
  var WARM_DENY = /\/(print|pdf|export|download|receipt|payment-proof|portal|import)(\/|$)|\/logout\/|\/delete\//;

  // ── Small helpers ─────────────────────────────────────────────────────
  function uuid() {
    if (window.crypto && crypto.randomUUID) return crypto.randomUUID().replace(/-/g, '');
    var bytes = new Uint8Array(16);
    (window.crypto || window.msCrypto).getRandomValues(bytes);
    return Array.prototype.map.call(bytes, function (b) { return ('0' + b.toString(16)).slice(-2); }).join('');
  }

  function el(tag, attrs, children) {
    var node = document.createElement(tag);
    Object.keys(attrs || {}).forEach(function (k) {
      if (k === 'text') node.textContent = attrs[k];
      else if (k === 'class') node.className = attrs[k];
      else node.setAttribute(k, attrs[k]);
    });
    (children || []).forEach(function (c) { if (c) node.appendChild(c); });
    return node;
  }

  function timeLabel(iso) {
    try {
      return new Date(iso).toLocaleTimeString([], { hour: 'numeric', minute: '2-digit' });
    } catch (e) { return ''; }
  }

  function getCsrf() {
    if (state.csrf) return state.csrf;
    var meta = document.querySelector('meta[name="csrf-token"]');
    if (meta && meta.content) return meta.content;
    var m = /(?:^|; )csrftoken=([^;]+)/.exec(document.cookie);
    return m ? decodeURIComponent(m[1]) : '';
  }

  // ── State ─────────────────────────────────────────────────────────────
  var state = {
    online: navigator.onLine !== false,
    probed: false,
    authenticated: true,      // until a ping says otherwise
    serverUserId: null,
    csrf: '',
    syncing: false,
    lastProbeOk: 0,
    lastSyncedAt: 0,
    justSynced: false,
    appliedSinceReload: 0
  };
  var ops = [];               // this user's queued operations, oldest first
  var listeners = [];

  // ── IndexedDB outbox ──────────────────────────────────────────────────
  var DB_NAME = 'gerlyn-offline';
  var STORE = 'ops';
  var dbPromise = null;

  function openDb() {
    if (dbPromise) return dbPromise;
    dbPromise = new Promise(function (resolve, reject) {
      if (!window.indexedDB) { reject(new Error('IndexedDB unavailable')); return; }
      var req = indexedDB.open(DB_NAME, 1);
      req.onupgradeneeded = function () {
        var db = req.result;
        if (!db.objectStoreNames.contains(STORE)) {
          var store = db.createObjectStore(STORE, { keyPath: 'id' });
          store.createIndex('userId', 'userId', { unique: false });
        }
      };
      req.onsuccess = function () { resolve(req.result); };
      req.onerror = function () { reject(req.error); };
    });
    return dbPromise;
  }

  function withStore(mode, fn) {
    return openDb().then(function (db) {
      return new Promise(function (resolve, reject) {
        var tx = db.transaction(STORE, mode);
        var result;
        try { result = fn(tx.objectStore(STORE)); } catch (e) { reject(e); return; }
        tx.oncomplete = function () { resolve(result && result.result !== undefined ? result.result : result); };
        tx.onerror = function () { reject(tx.error); };
        tx.onabort = function () { reject(tx.error); };
      });
    });
  }

  function dbPut(op) { return withStore('readwrite', function (s) { return s.put(op); }); }
  function dbDelete(id) { return withStore('readwrite', function (s) { return s.delete(id); }); }
  function dbAllForUser() {
    return withStore('readonly', function (s) {
      return s.index('userId').getAll(IDBKeyRange.only(userId));
    }).then(function (rows) {
      return (rows || []).sort(function (a, b) { return a.createdAt < b.createdAt ? -1 : 1; });
    });
  }

  var channel = null;
  try { channel = ('BroadcastChannel' in window) ? new BroadcastChannel('gerlyn-offline') : null; } catch (e) { channel = null; }
  function announce() { try { if (channel) channel.postMessage({ from: userId }); } catch (e) { /* ignore */ } }

  function reloadOps() {
    return dbAllForUser().then(function (rows) {
      ops = rows;
      mirrorPendingStock();
      render();
      return ops;
    }).catch(function () { ops = []; render(); return ops; });
  }

  function saveOp(op) {
    return dbPut(op).then(function () {
      var i = ops.findIndex(function (o) { return o.id === op.id; });
      if (i === -1) ops.push(op); else ops[i] = op;
      mirrorPendingStock();
      render();
      announce();
    });
  }

  // ── Status model ──────────────────────────────────────────────────────
  // op.status: pending | syncing | synced | failed | rejected | conflict
  //   failed    -> a transient problem (server error, session ended); retried
  //   rejected  -> the server refused it; nothing was saved; needs a person
  //   conflict  -> the data moved on since it was queued; needs a person
  function needsReview(op) { return op.status === 'rejected' || op.status === 'conflict'; }
  function isUnsynced(op) { return op.status !== 'synced'; }

  function overall() {
    var pending = 0, failed = 0, syncing = 0;
    ops.forEach(function (op) {
      if (op.status === 'synced') return;
      if (op.status === 'syncing') syncing++;
      else if (op.status === 'pending') pending++;
      else failed++;
    });
    var label, kind;
    if (!state.online) { label = 'Offline'; kind = 'offline'; }
    else if (syncing) { label = 'Syncing…'; kind = 'pending'; }
    else if (failed) { label = 'Sync Failed'; kind = 'failed'; }
    else if (pending) { label = 'Pending Sync'; kind = 'pending'; }
    else if (state.justSynced) { label = 'Synced'; kind = 'synced'; }
    else { label = 'Online'; kind = 'online'; }
    return { label: label, kind: kind, pending: pending, failed: failed, syncing: syncing, count: pending + failed + syncing };
  }

  var STATUS_TEXT = {
    pending: 'Pending Sync', syncing: 'Syncing…', synced: 'Synced',
    failed: 'Sync Failed', rejected: 'Sync Failed', conflict: 'Sync Failed'
  };

  // ── POS stock overlay ─────────────────────────────────────────────────
  // The saved POS page carries the stock figures from when it was loaded, and
  // offline sales have not touched the server's yet. Keep a synchronous mirror
  // of what this device has already sold so the page can subtract it (the
  // POS script calls GerlynOffline.adjustStockMap before it first renders).
  // The server still enforces real stock on sync; this only stops the till
  // from happily selling the same last unit twice offline.
  var STOCK_KEY = 'gvOfflinePendingStock';

  function mirrorPendingStock() {
    var map = {};
    ops.forEach(function (op) {
      if (op.type !== 'sale.walkin' || op.status === 'synced') return;
      var branch = op.meta && op.meta.branchId;
      if (!branch) return;
      var ids = [], qtys = [];
      (op.entries || []).forEach(function (pair) {
        if (pair[0] === 'product_id') ids.push(String(pair[1]));
        else if (pair[0] === 'quantity') qtys.push(parseInt(pair[1], 10) || 0);
      });
      ids.forEach(function (pid, i) {
        if (!pid) return;
        map[pid] = map[pid] || {};
        map[pid][branch] = (map[pid][branch] || 0) + (qtys[i] || 0);
      });
    });
    lsSet(STOCK_KEY, JSON.stringify({ userId: userId, map: map }));
  }

  function adjustStockMap(stockMap) {
    try {
      var saved = JSON.parse(lsGet(STOCK_KEY) || 'null');
      if (!saved || saved.userId !== userId) return stockMap;
      Object.keys(saved.map).forEach(function (pid) {
        Object.keys(saved.map[pid]).forEach(function (bid) {
          var info = stockMap[pid] && stockMap[pid][bid];
          if (!info) return;
          info.qty = Math.max(0, info.qty - saved.map[pid][bid]);
          if (info.qty === 0) info.status = 'No Stock';
        });
      });
    } catch (e) { /* leave the map as the server rendered it */ }
    return stockMap;
  }

  // ── Connectivity ──────────────────────────────────────────────────────
  var probing = null;

  function setOnline(value) {
    var changed = state.online !== value;
    state.online = value;
    if (changed) render();
  }

  function probe() {
    if (probing) return probing;
    var ctrl = ('AbortController' in window) ? new AbortController() : null;
    var timer = setTimeout(function () { if (ctrl) ctrl.abort(); }, 4000);
    probing = fetch(PING_URL + '?t=' + Date.now(), {
      cache: 'no-store', credentials: 'same-origin', signal: ctrl ? ctrl.signal : undefined,
      headers: { 'Accept': 'application/json' }
    }).then(function (res) {
      if (!res.ok) throw new Error('ping ' + res.status);
      return res.json();
    }).then(function (data) {
      clearTimeout(timer);
      if (!data || data.ok !== true) throw new Error('bad ping');
      state.probed = true;
      state.authenticated = !!data.authenticated;
      state.serverUserId = data.user_id != null ? String(data.user_id) : null;
      if (data.csrf) state.csrf = data.csrf;
      state.lastProbeOk = Date.now();
      setOnline(true);
      return true;
    }).catch(function () {
      clearTimeout(timer);
      state.probed = true;
      setOnline(false);
      return false;
    }).then(function (ok) {
      probing = null;
      render();
      return ok;
    });
    return probing;
  }

  function canSyncNow() {
    return state.online && state.authenticated && state.serverUserId === userId;
  }

  // ── Sync ──────────────────────────────────────────────────────────────
  function buildBody(op) {
    var fd = new FormData();
    (op.entries || []).forEach(function (pair) {
      var value = pair[1];
      if (typeof value === 'string') fd.append(pair[0], value);
      else fd.append(pair[0], value, value.name || 'file');
    });
    fd.append('_offline', JSON.stringify({
      client_op_id: op.id,
      op_type: op.type,
      client_user_id: userId,
      queued_at: op.createdAt,
      target_pk: op.target || null,
      meta: op.meta || {}
    }));
    return fd;
  }

  function applyAnswer(op, answer) {
    // The one place a change becomes "synced": the server said so, about
    // this exact operation.
    if (answer.status === 'applied' && answer.client_op_id === op.id) {
      op.status = 'synced';
      op.syncedAt = new Date().toISOString();
      op.message = answer.message || '';
      op.result = answer.result || {};
      op.entries = [];   // the data is on the server now; do not keep a copy
      state.appliedSinceReload++;
      return;
    }
    if (answer.status === 'conflict') op.status = 'conflict';
    else op.status = 'rejected';
    op.message = answer.message || 'The server did not accept this change.';
    op.current = answer.current || null;
  }

  function sendOne(op, retriedCsrf) {
    return fetch(SYNC_URL, {
      method: 'POST', body: buildBody(op), credentials: 'same-origin',
      headers: { 'X-CSRFToken': getCsrf(), 'X-Requested-With': 'XMLHttpRequest', 'Accept': 'application/json' }
    }).then(function (res) {
      var finalUrl = res.url || '';
      var isJson = (res.headers.get('Content-Type') || '').indexOf('application/json') !== -1;
      if (res.status === 401 || (res.redirected && /\/login\//.test(finalUrl))) {
        state.authenticated = false;
        return { stop: true, op: op, set: { status: 'failed', message: 'Your session ended. Sign in to continue syncing — nothing was lost.' } };
      }
      if (res.status === 403 && !isJson && !retriedCsrf) {
        // An HTML 403 is Django's CSRF page: almost always a stale token after
        // a long offline spell. (A JSON 403 is the server refusing the change
        // itself, handled below.) Fetch a fresh one and send once more.
        return probe().then(function (ok) {
          if (!ok) return { stop: true, op: op, set: { status: 'pending' } };
          return sendOne(op, true);
        });
      }
      if (!isJson && res.status !== 200) {
        return { stop: true, op: op, set: { status: 'failed', message: 'The server did not accept the request (' + res.status + '). It will be retried.' } };
      }
      if (res.status >= 500) {
        return { stop: true, op: op, set: { status: 'failed', message: 'The server could not process this yet. It will be retried.' } };
      }
      return res.json().then(function (answer) {
        if (res.status === 200) return { op: op, answer: answer };
        // 400 / 403 / 409: a definite refusal of this change.
        return { op: op, answer: { status: 'rejected', message: answer.message || 'The server refused this change.', client_op_id: op.id } };
      }).catch(function () {
        return { stop: true, op: op, set: { status: 'failed', message: 'Unexpected response from the server. It will be retried.' } };
      });
    });
  }

  function syncAll() {
    if (state.syncing) return Promise.resolve();
    if (!canSyncNow()) return Promise.resolve();
    var run = function () { return doSync(); };
    if (navigator.locks && navigator.locks.request) {
      // One tab at a time. (The server would catch a duplicate anyway.)
      return navigator.locks.request('gerlyn-offline-sync', { ifAvailable: true }, function (lock) {
        if (!lock) return null;
        return run();
      });
    }
    return run();
  }

  function doSync() {
    state.syncing = true;
    return reloadOps().then(function () {
      var queue = ops.filter(function (op) { return op.status === 'pending' || op.status === 'failed' || op.status === 'syncing'; });
      var chain = Promise.resolve();
      var stopped = false;
      queue.forEach(function (op) {
        chain = chain.then(function () {
          if (stopped) return null;
          op.status = 'syncing';
          op.attempts = (op.attempts || 0) + 1;
          op.lastAttemptAt = new Date().toISOString();
          return saveOp(op).then(function () { return sendOne(op, false); }).then(function (out) {
            if (out.stop) {
              stopped = true;
              Object.keys(out.set).forEach(function (k) { op[k] = out.set[k]; });
            } else {
              applyAnswer(op, out.answer);
            }
            return saveOp(op);
          }).catch(function () {
            // No definite answer (network dropped, request aborted). The
            // change stays pending and is safe to send again: the server
            // de-duplicates on the operation id.
            stopped = true;
            op.status = 'pending';
            setOnline(false);
            return saveOp(op);
          });
        });
      });
      return chain;
    }).then(function () {
      state.syncing = false;
      var anyApplied = ops.some(function (o) { return o.status === 'synced' && Date.now() - Date.parse(o.syncedAt || 0) < 60000; });
      if (anyApplied && overall().count === 0) {
        state.justSynced = true;
        state.lastSyncedAt = Date.now();
        setTimeout(function () { state.justSynced = false; render(); }, 8000);
      }
      render();
      emit('synced');
    }).catch(function () {
      state.syncing = false;
      render();
    });
  }

  // ── Queueing a form ───────────────────────────────────────────────────
  function readText(selector) {
    var node = document.querySelector(selector);
    return node ? (node.textContent || '').trim() : '';
  }

  function summarize(route, form, entries) {
    var get = function (name) {
      return entries.filter(function (p) { return p[0] === name; }).map(function (p) { return p[1]; });
    };
    var n;
    switch (route.type) {
      case 'sale.walkin':
        return [readText('#pos-item-count'), readText('#pos-grand-total')].filter(Boolean).join(' · ');
      case 'inventory.floor_refill':
        n = get('quantity_requested').filter(function (q) { return parseInt(q, 10) > 0; }).length;
        return n + ' item' + (n === 1 ? '' : 's');
      case 'inventory.count_correction':
        var sel = form.querySelector('[name="product_id"]');
        var name = sel && sel.selectedOptions && sel.selectedOptions[0] ? sel.selectedOptions[0].textContent.trim() : '';
        return (name ? name + ' · ' : '') + 'counted ' + (get('counted_quantity')[0] || '?');
      case 'procurement.create_po':
        n = get('product_id').filter(Boolean).length;
        var sup = form.querySelector('[name="supplier"]');
        var supName = sup && sup.selectedOptions && sup.selectedOptions[0] ? sup.selectedOptions[0].textContent.trim() : '';
        return (supName ? supName + ' · ' : '') + n + ' item' + (n === 1 ? '' : 's');
      case 'procurement.receive_delivery':
        var heading = document.querySelector('#receive-modal h2, #receive-modal .modal-head h2');
        return heading ? heading.textContent.trim() : 'PO #' + (route.targetValue || '');
      case 'inventory.product_add':
        return (get('product_name')[0] || '').trim();
      case 'inventory.category_add':
        return (get('category_name')[0] || '').trim();
      case 'inventory.write_off':
        n = get('quantity_requested').filter(function (q) { return parseInt(q, 10) > 0; }).length;
        return n + ' item' + (n === 1 ? '' : 's');
      case 'procurement.supplier_add':
        return (get('supplier_name')[0] || get('username')[0] || '').trim();
      case 'procurement.supplier_category_add':
        return (get('name')[0] || '').trim();
      case 'procurement.supplier_product_add':
        var prod = form.querySelector('[name="product"]');
        return prod && prod.selectedOptions && prod.selectedOptions[0] ? prod.selectedOptions[0].textContent.trim() : '';
      default:
        return '';
    }
  }

  function serialize(form, submitter) {
    var fd;
    try { fd = submitter ? new FormData(form, submitter) : new FormData(form); }
    catch (e) {
      fd = new FormData(form);
      if (submitter && submitter.name) fd.append(submitter.name, submitter.value);
    }
    var entries = [];
    fd.forEach(function (value, key) {
      if (key === 'csrfmiddlewaretoken') return;
      if (typeof value === 'string') { entries.push([key, value]); return; }
      if (value && (value.size > 0 || value.name)) entries.push([key, value]); // skip an empty file input
    });
    return entries;
  }

  function queueForm(form, submitter, hit) {
    var route = hit.route;
    var entries = serialize(form, submitter);
    var meta = {};
    var branchField = form.elements && form.elements.branch ? form.elements.branch.value : '';
    if (route.type === 'sale.walkin') {
      meta.branchId = String(branchField || window.POS_FIXED_BRANCH_ID || '');
    }
    if (route.type === 'inventory.count_correction') {
      var base = parseInt(readText('#adjust-system-quantity'), 10);
      meta.base_quantity = isNaN(base) ? null : base;
    }
    var target = route.target ? hit.match[route.target] : null;
    var routeView = { type: route.type, targetValue: target };

    var op = {
      id: uuid(),
      userId: userId,
      type: route.type,
      label: route.label,
      summary: summarize(routeView, form, entries),
      target: target ? parseInt(target, 10) : null,
      entries: entries,
      meta: meta,
      createdAt: new Date().toISOString(),
      status: 'pending',
      attempts: 0,
      message: '',
      origin: location.pathname + location.search
    };

    return saveOp(op).then(function () {
      ssSet('gvOfflineFlash', route.label + ' saved on this device. It will sync automatically when the connection returns.');
      if (route.after === 'reload') window.location.reload();
      // 'clean': the same page without the query string that opened its modal.
      else if (route.after === 'clean') window.location.assign(window.location.pathname);
      else window.location.assign(route.after);
    });
  }

  // ── Form interception ─────────────────────────────────────────────────
  var bypass = false;
  var buttonSnapshots = new WeakMap();

  function snapshotButtons(form) {
    var list = [];
    form.querySelectorAll('button[type="submit"], input[type="submit"], button:not([type])').forEach(function (b) {
      list.push({ node: b, disabled: b.disabled, html: b.innerHTML, value: b.value });
    });
    buttonSnapshots.set(form, list);
  }

  function restoreButtons(form) {
    (buttonSnapshots.get(form) || []).forEach(function (s) {
      s.node.disabled = s.disabled;
      if (s.node.tagName === 'BUTTON') s.node.innerHTML = s.html;
    });
  }

  function formUrl(form) {
    try { return new URL(form.getAttribute('action') || location.href, location.href); }
    catch (e) { return null; }
  }

  function offlineNow() {
    return state.probed ? !state.online : navigator.onLine === false;
  }

  function handleSubmit(e) {
    var form = e.target;
    if (bypass || !form || form.tagName !== 'FORM') return;
    if ((form.getAttribute('method') || 'get').toLowerCase() !== 'post') return;
    var url = formUrl(form);
    if (!url || url.origin !== location.origin) return;
    // Another handler (validation, a confirm dialog) already stopped this
    // submit: respect that.
    if (e.defaultPrevented) return;
    // Already being decided (the connection check is in flight): a second
    // click must not become a second queued change.
    if (form.__gvDeciding) {
      e.preventDefault();
      e.stopImmediatePropagation();
      return;
    }

    var hit = matchRoute(url.pathname);
    var submitter = e.submitter || null;

    if (!hit) {
      if (offlineNow()) {
        e.preventDefault();
        e.stopImmediatePropagation();
        restoreButtons(form);
        toast('This action needs a live connection, so it can\'t be saved offline. Reconnect and try again. '
          + 'Only ' + QUEUEABLE_SUMMARY + ' can be saved offline.', 'warn');
      }
      return;
    }

    if (!window.indexedDB) {
      if (offlineNow()) {
        e.preventDefault();
        toast('This browser cannot save changes for later. Reconnect to continue.', 'warn');
      }
      return;
    }

    // Reachable, and confirmed recently: submit exactly as before.
    if (state.online && state.probed && Date.now() - state.lastProbeOk < 10000) return;

    // Unknown or stale: decide before the browser navigates away from a form
    // it may not be able to send.
    e.preventDefault();
    e.stopImmediatePropagation();
    form.__gvDeciding = true;
    probe().then(function (ok) {
      if (ok) {
        // The page's own submit handlers already ran once (and may have
        // disabled the button). Put it back so the real submit is whole.
        restoreButtons(form);
        bypass = true;
        try {
          if (typeof form.requestSubmit === 'function') form.requestSubmit(submitter || undefined);
          else form.submit();
        } finally { bypass = false; }
        return null;
      }
      return queueForm(form, submitter, hit).catch(function () {
        restoreButtons(form);
        toast('Could not save this on the device. Nothing was submitted — please try again.', 'error');
      });
    });
  }

  // ── Notices ───────────────────────────────────────────────────────────
  var toastHost = null;
  function toast(message, kind) {
    if (!toastHost) {
      toastHost = el('div', { class: 'gv-sync-toasts', role: 'status', 'aria-live': 'polite' });
      document.body.appendChild(toastHost);
    }
    var node = el('div', { class: 'gv-sync-toast gv-sync-toast--' + (kind || 'info'), text: message });
    toastHost.appendChild(node);
    setTimeout(function () { if (node.parentNode) node.parentNode.removeChild(node); }, 7000);
  }

  function emit(name) {
    listeners.forEach(function (fn) { try { fn(name); } catch (e) { /* listener fault */ } });
    try { document.dispatchEvent(new CustomEvent('gerlyn:offline-' + name)); } catch (e) { /* old browser */ }
  }

  // ── UI ────────────────────────────────────────────────────────────────
  var ui = {};

  function buildUi() {
    ui.pill = el('button', { type: 'button', class: 'gv-sync-pill gv-sync-pill--online', 'aria-haspopup': 'dialog', 'aria-expanded': 'false', title: 'Connection and offline changes' }, [
      el('span', { class: 'gv-sync-dot', 'aria-hidden': 'true' }),
      (ui.label = el('span', { class: 'gv-sync-label', text: 'Online' })),
      (ui.count = el('span', { class: 'gv-sync-count', hidden: 'hidden' }))
    ]);
    ui.list = el('div', { class: 'gv-sync-list' });
    ui.status = el('p', { class: 'gv-sync-state' });
    ui.reload = el('button', { type: 'button', class: 'btn btn-sm btn-primary', text: 'Reload to refresh figures', hidden: 'hidden' });
    ui.syncBtn = el('button', { type: 'button', class: 'btn btn-sm', text: 'Sync now' });
    ui.clearBtn = el('button', { type: 'button', class: 'btn btn-sm', text: 'Clear synced' });
    ui.panel = el('div', { class: 'gv-sync-panel', role: 'dialog', 'aria-label': 'Offline changes', hidden: 'hidden' }, [
      el('div', { class: 'gv-sync-panel__head' }, [
        el('h2', { text: 'Offline changes' }),
        (ui.close = el('button', { type: 'button', class: 'gv-sync-close', 'aria-label': 'Close', text: '×' }))
      ]),
      ui.status,
      ui.list,
      el('div', { class: 'gv-sync-panel__foot' }, [ui.syncBtn, ui.clearBtn, ui.reload])
    ]);

    var host = document.querySelector('.topbar .row');
    var wrap = el('div', { class: 'gv-sync' }, [ui.pill, ui.panel]);
    if (host) {
      host.insertBefore(wrap, host.firstChild);
    } else {
      wrap.classList.add('gv-sync--float');
      document.body.appendChild(wrap);
    }

    ui.pill.addEventListener('click', function () { togglePanel(); });
    ui.close.addEventListener('click', function () { togglePanel(false); });
    ui.syncBtn.addEventListener('click', function () {
      probe().then(function (ok) {
        if (!ok) { toast('Still offline. Your changes are safe on this device.', 'warn'); return; }
        if (!state.authenticated) { toast('Sign in again to continue syncing.', 'warn'); return; }
        ops.forEach(function (o) { if (o.status === 'failed') o.status = 'pending'; });
        syncAll();
      });
    });
    ui.clearBtn.addEventListener('click', function () {
      var done = ops.filter(function (o) { return o.status === 'synced'; });
      Promise.all(done.map(function (o) { return dbDelete(o.id); })).then(reloadOps).then(announce);
    });
    ui.reload.addEventListener('click', function () { window.location.reload(); });
    document.addEventListener('click', function (ev) {
      if (!ui.panel.hidden && !ui.panel.contains(ev.target) && !ui.pill.contains(ev.target)) togglePanel(false);
    });
    document.addEventListener('keydown', function (ev) { if (ev.key === 'Escape') togglePanel(false); });
  }

  function togglePanel(force) {
    var open = force === undefined ? ui.panel.hidden : force;
    ui.panel.hidden = !open;
    ui.pill.setAttribute('aria-expanded', open ? 'true' : 'false');
    if (open) render();
  }

  function opRow(op) {
    var kind = op.status === 'synced' ? 'synced'
      : op.status === 'syncing' || op.status === 'pending' ? 'pending' : 'failed';
    var children = [
      el('div', { class: 'gv-sync-item__top' }, [
        el('strong', { text: op.label }),
        el('span', { class: 'gv-sync-badge gv-sync-badge--' + kind, text: STATUS_TEXT[op.status] || op.status })
      ])
    ];
    if (op.summary) children.push(el('div', { class: 'gv-sync-item__meta', text: op.summary }));
    children.push(el('div', { class: 'gv-sync-item__meta', text: 'Saved ' + timeLabel(op.createdAt) }));
    if (op.status === 'synced' && op.result && op.result.label) {
      var link = op.result.url
        ? el('a', { href: op.result.url, text: op.result.label })
        : el('span', { text: op.result.label });
      children.push(el('div', { class: 'gv-sync-item__meta' }, [document.createTextNode('Recorded as '), link]));
    }
    if (op.message && op.status !== 'synced' && op.status !== 'pending' && op.status !== 'syncing') {
      children.push(el('div', { class: 'gv-sync-item__error', text: op.message }));
    }
    if (op.status === 'conflict' && op.current && typeof op.current.quantity_on_hand === 'number') {
      children.push(el('div', { class: 'gv-sync-item__meta', text: 'System quantity now: ' + op.current.quantity_on_hand }));
    }
    if (op.status !== 'synced') {
      var actions = [];
      if (op.status === 'failed' || op.status === 'rejected') {
        actions.push(actionBtn('Retry', function () { retryOp(op); }));
      }
      if (op.status !== 'syncing') {
        actions.push(actionBtn('Discard', function () { discardOp(op); }, true));
      }
      children.push(el('div', { class: 'gv-sync-item__actions' }, actions));
    }
    return el('div', { class: 'gv-sync-item gv-sync-item--' + kind }, children);
  }

  function actionBtn(text, fn, danger) {
    var b = el('button', { type: 'button', class: 'btn btn-sm' + (danger ? ' btn-danger' : ''), text: text });
    b.addEventListener('click', fn);
    return b;
  }

  function retryOp(op) {
    op.status = 'pending';
    op.message = '';
    saveOp(op).then(function () { return probe(); }).then(function () { syncAll(); });
  }

  function discardOp(op) {
    var ok = window.confirm('Discard this saved change? It has NOT been applied on the server and will be lost.');
    if (!ok) return;
    dbDelete(op.id).then(reloadOps).then(announce);
  }

  function render() {
    if (!ui.pill) return;
    var s = overall();
    ui.pill.className = 'gv-sync-pill gv-sync-pill--' + s.kind;
    ui.label.textContent = s.label;
    if (s.count > 0) { ui.count.hidden = false; ui.count.textContent = String(s.count); }
    else { ui.count.hidden = true; }
    ui.pill.setAttribute('aria-label', s.label + (s.count ? ', ' + s.count + ' change' + (s.count === 1 ? '' : 's') : ''));

    var line;
    if (!state.online) line = 'You are offline. Changes you save are kept on this device and sync automatically when the connection returns.';
    else if (!state.authenticated) line = 'Your session has ended. Sign in to sync the changes below — they are safe on this device.';
    else if (state.serverUserId && state.serverUserId !== userId) line = 'These changes were saved by a different account on this device. Sign in as that account to sync them.';
    else if (s.count === 0) line = 'Connected. Nothing is waiting to sync.';
    else line = 'Connected. Changes marked Pending Sync are sent automatically.';
    ui.status.textContent = line;

    var visible = ops.filter(function (o) { return o.status !== 'synced'; });
    var recent = ops.filter(function (o) { return o.status === 'synced'; }).slice(-5).reverse();
    ui.list.textContent = '';
    if (!visible.length && !recent.length) {
      ui.list.appendChild(el('p', { class: 'gv-meta', text: 'No offline changes.' }));
    }
    visible.concat(recent).forEach(function (op) { ui.list.appendChild(opRow(op)); });
    ui.clearBtn.hidden = !recent.length;
    ui.syncBtn.hidden = !visible.length;
    ui.reload.hidden = !(state.appliedSinceReload > 0);
  }

  function showSavedCopyNotice() {
    var meta = document.querySelector('meta[name="gerlyn-saved-copy"]');
    if (!meta) return;
    var when = meta.content ? timeLabel(meta.content) : '';
    var main = document.getElementById('main') || document.body;
    var here = queueableLabelsOnPage();
    var tail = here.length
      ? ' You can still enter here: ' + here.join(', ') + '. These sync when you are back online; every other action needs a connection.'
      : ' Changes on this page need a connection. Only ' + QUEUEABLE_SUMMARY + ' can be saved offline, from their own pages.';
    var banner = el('div', { class: 'gv-saved-copy', role: 'status' }, [
      el('strong', { text: 'Saved copy' }),
      document.createTextNode(' — this page was saved' + (when ? ' at ' + when : '') + ' and may not show the latest figures.' + tail)
    ]);
    main.insertBefore(banner, main.firstChild);
  }

  // ── Keeping pages available ───────────────────────────────────────────
  function warmPages() {
    var last = parseInt(lsGet('gvOfflineWarmAt') || '0', 10);
    if (Date.now() - last < 30 * 60 * 1000) return;
    if (!canSyncNow() || onFallbackPage) return;
    lsSet('gvOfflineWarmAt', String(Date.now()));

    var seen = {};
    var urls = [];
    function consider(href) {
      try {
        var u = new URL(href, location.href);
        if (u.origin !== location.origin) return;
        if (!WARM_PREFIXES.some(function (p) { return u.pathname.indexOf(p) === 0; })) return;
        if (WARM_DENY.test(u.pathname)) return;
        var key = u.pathname + u.search;
        if (seen[key]) return;
        seen[key] = true;
        urls.push(key);
      } catch (e) { /* not a URL */ }
    }
    document.querySelectorAll('.sidebar a[href], .bottom-nav a[href], .main-nav a[href], aside a[href]').forEach(function (a) {
      consider(a.getAttribute('href'));
    });
    // Receive buttons on the deliveries list open a per-PO modal by URL.
    document.querySelectorAll('a[href*="receive="]').forEach(function (a) { consider(a.getAttribute('href')); });
    urls = urls.slice(0, 14);
    // "Add product" is a page of its own rather than a modal on a sidebar page,
    // so it is only available offline if a copy was kept. (A redirect — no
    // permission — is simply not saved.)
    if (urls.some(function (u) { return u.indexOf('/inventory/products/') === 0; })) {
      urls.push('/inventory/products/add/');
    }

    var chain = Promise.resolve();
    urls.forEach(function (path) {
      chain = chain.then(function () {
        if (!state.online) return null;
        return fetch(path, { credentials: 'same-origin', headers: { 'X-Gerlyn-Warm': '1' } })
          .then(function () { return new Promise(function (r) { setTimeout(r, 300); }); })
          .catch(function () { return null; });
      });
    });
  }

  // ── Wiring ────────────────────────────────────────────────────────────
  function schedule() {
    var delay = (!state.online || overall().count > 0) ? 15000 : 45000;
    setTimeout(function () {
      if (document.visibilityState === 'hidden' && overall().count === 0) { schedule(); return; }
      probe().then(function (ok) {
        if (ok && overall().count > 0) syncAll();
        schedule();
      });
    }, delay);
  }

  function init() {
    buildUi();
    showSavedCopyNotice();

    var flash = ssGet('gvOfflineFlash');
    if (flash) { ssDel('gvOfflineFlash'); toast(flash, 'info'); }
    if (onFallbackPage) togglePanel(true);

    // Capture phase: remember the buttons before any page script touches them.
    document.addEventListener('submit', function (ev) {
      if (ev.target && ev.target.tagName === 'FORM') snapshotButtons(ev.target);
    }, true);
    // Bubble phase: by now the form's own validation has had its say.
    document.addEventListener('submit', handleSubmit, false);

    window.addEventListener('online', function () {
      probe().then(function (ok) { if (ok) syncAll().then(warmPages); });
    });
    window.addEventListener('offline', function () { setOnline(false); });
    document.addEventListener('visibilitychange', function () {
      if (document.visibilityState === 'visible') {
        probe().then(function (ok) { if (ok && overall().count > 0) syncAll(); });
      }
    });
    if (channel) channel.onmessage = function () { reloadOps(); };

    reloadOps().then(function () {
      // A tab closed mid-send leaves "syncing" rows; the server de-duplicates,
      // so sending them again is safe.
      var stuck = ops.filter(function (o) { return o.status === 'syncing'; });
      return Promise.all(stuck.map(function (o) { o.status = 'pending'; return saveOp(o); }));
    }).then(function () { return probe(); }).then(function (ok) {
      if (ok && overall().count > 0) return syncAll();
      return null;
    }).then(function () {
      setTimeout(warmPages, 3000);
      schedule();
    });
  }

  window.GerlynOffline = {
    adjustStockMap: adjustStockMap,
    syncNow: function () { return probe().then(function (ok) { return ok ? syncAll() : null; }); },
    status: function () { return overall(); },
    pending: function () { return ops.filter(isUnsynced).length; },
    onChange: function (fn) { listeners.push(fn); },
    needsReview: function () { return ops.filter(needsReview).length; }
  };

  if (document.readyState === 'loading') document.addEventListener('DOMContentLoaded', init);
  else init();
})();

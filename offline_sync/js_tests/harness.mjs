// Fakes just big enough to run static/js/offline-sync.js and templates/sw.js
// unmodified under Node. Not a browser: there is no layout, no real
// IndexedDB engine and no real service-worker lifecycle. What it does let us
// prove is the logic that matters -- what gets queued, what is sent, and what
// the client is willing to call "synced".
import fs from 'node:fs';
import path from 'node:path';
import vm from 'node:vm';
import { fileURLToPath } from 'node:url';

const here = path.dirname(fileURLToPath(import.meta.url));
const root = path.resolve(here, '..', '..');
export const CLIENT_SRC = fs.readFileSync(path.join(root, 'static/js/offline-sync.js'), 'utf8');
export const SW_SRC = fs.readFileSync(path.join(root, 'templates/sw.js'), 'utf8')
  .replace('{{ APP_VERSION }}', 'test');

export const tick = async (n = 12) => { for (let i = 0; i < n; i++) await new Promise((r) => setImmediate(r)); };

// ── DOM ────────────────────────────────────────────────────────────────────
class FakeNode {
  constructor(tag) {
    this.tagName = String(tag || '').toUpperCase();
    this.children = []; this.attrs = {}; this.listeners = {};
    this.parentNode = null; this._text = ''; this.className = '';
    this.hidden = false; this.style = {}; this.dataset = {};
    this.classList = { add: (c) => { this.className += ' ' + c; } };
  }
  setAttribute(k, v) { this.attrs[k] = String(v); if (k === 'hidden') this.hidden = true; }
  getAttribute(k) { return k in this.attrs ? this.attrs[k] : null; }
  hasAttribute(k) { return k in this.attrs; }
  appendChild(c) { c.parentNode = this; this.children.push(c); return c; }
  insertBefore(c) { c.parentNode = this; this.children.unshift(c); return c; }
  removeChild(c) { this.children = this.children.filter((x) => x !== c); }
  addEventListener(t, fn) { (this.listeners[t] = this.listeners[t] || []).push(fn); }
  contains(n) { return n === this || this.children.some((c) => c.contains && c.contains(n)); }
  get textContent() { return this._text + this.children.map((c) => c.textContent || '').join(''); }
  set textContent(v) { this.children = []; this._text = String(v); }
  querySelector() { return null; }
  querySelectorAll() { return []; }
  text() { return this.textContent; }
  find(pred, out = []) { if (pred(this)) out.push(this); this.children.forEach((c) => c.find && c.find(pred, out)); return out; }
}

export function fakeForm(action, fields, extra = {}) {
  const form = new FakeNode('form');
  form.attrs = { action, method: 'post' };
  form.__fields = fields;
  form.elements = extra.elements || {};
  form.querySelector = extra.querySelector || (() => null);
  form.querySelectorAll = () => [];
  return form;
}

// ── IndexedDB ──────────────────────────────────────────────────────────────
export function fakeIndexedDB() {
  const stores = {};
  const db = {
    objectStoreNames: { contains: (n) => n in stores },
    createObjectStore(name, opts) {
      stores[name] = { keyPath: opts.keyPath, rows: new Map() };
      return { createIndex() {} };
    },
    transaction(name) {
      const store = stores[name];
      const tx = { oncomplete: null, onerror: null, onabort: null };
      queueMicrotask(() => queueMicrotask(() => tx.oncomplete && tx.oncomplete()));
      tx.objectStore = () => ({
        put(v) { store.rows.set(v[store.keyPath], structuredClone(v)); return { result: undefined }; },
        delete(k) { store.rows.delete(k); return { result: undefined }; },
        index() {
          return {
            getAll(range) {
              const rows = [...store.rows.values()].filter((r) => r.userId === range.value);
              return { result: rows.map((r) => structuredClone(r)) };
            },
          };
        },
      });
      return tx;
    },
  };
  return {
    stores,
    open() {
      const req = { result: db, onupgradeneeded: null, onsuccess: null, onerror: null };
      queueMicrotask(() => {
        if (!Object.keys(stores).length && req.onupgradeneeded) req.onupgradeneeded();
        queueMicrotask(() => req.onsuccess && req.onsuccess());
      });
      return req;
    },
  };
}

// ── The server the client talks to ─────────────────────────────────────────
// Mirrors the contract of offline_sync/views.py closely enough to test the
// client's reading of it. (The real thing is covered by offline_sync/tests.py.)
export class FakeServer {
  constructor() {
    this.reachable = true;
    this.authenticated = true;
    this.userId = '7';
    this.ledger = new Map();      // op id -> {status, message, result}
    this.applied = [];            // what actually "happened", in order
    this.calls = [];              // every sync request that reached us
    this.policy = () => ({ status: 'applied', message: 'Recorded.', result: { label: 'SO-1', url: '/sales/1/' } });
    this.failNext = null;         // {status: 500} | 'drop' (response lost after commit)
  }
  json(body, status = 200) {
    return new Response(JSON.stringify(body), { status, headers: { 'Content-Type': 'application/json' } });
  }
  async fetch(url, opts = {}) {
    if (!this.reachable) throw new TypeError('Failed to fetch');
    const u = String(url);
    if (u.startsWith('/offline/ping/')) {
      return this.json({
        ok: true, authenticated: this.authenticated,
        user_id: this.authenticated ? Number(this.userId) : undefined,
        csrf: 'tok', operations: {},
      });
    }
    if (u.startsWith('/offline/sync/')) {
      if (!this.authenticated) return this.json({ status: 'unauthenticated' }, 401);
      const env = JSON.parse(opts.body.get('_offline'));
      this.calls.push({ env, fields: opts.body.fields(), csrf: opts.headers['X-CSRFToken'] });
      if (String(env.client_user_id) !== this.userId) {
        return this.json({ status: 'wrong_user', message: 'queued by a different account' }, 409);
      }
      const prior = this.ledger.get(env.client_op_id);
      if (prior && prior.status === 'applied') {
        return this.json({ client_op_id: env.client_op_id, ...prior, duplicate: true });
      }
      if (this.failNext && this.failNext.status) {
        const s = this.failNext.status; this.failNext = null;
        return this.json({ status: 'error' }, s);
      }
      const answer = { client_op_id: env.client_op_id, ...this.policy(env) };
      this.ledger.set(env.client_op_id, answer);
      if (answer.status === 'applied') this.applied.push(env.client_op_id);
      if (this.failNext === 'drop') { this.failNext = null; throw new TypeError('Failed to fetch'); }
      return this.json(answer);
    }
    return this.json({}, 404);
  }
}

// A FormData that can be built from a fake form and inspected by FakeServer.
class FakeFormData {
  constructor(form, submitter) {
    this._p = [];
    if (form && form.__fields) form.__fields.forEach(([k, v]) => this._p.push([k, v]));
    if (submitter && submitter.name) this._p.push([submitter.name, submitter.value]);
  }
  append(k, v) { this._p.push([k, v]); }
  get(k) { const p = this._p.find((x) => x[0] === k); return p ? p[1] : null; }
  fields() { return this._p.filter(([k]) => k !== '_offline'); }
  forEach(fn) { this._p.forEach(([k, v]) => fn(v, k)); }
}

// ── Boot the real client in a fresh fake page ──────────────────────────────
export async function bootClient({ server, userId = '7', config = true, offline = false, flash = null, pendingStock = null } = {}) {
  const idb = globalThis.__idb || (globalThis.__idb = fakeIndexedDB());
  const store = () => { const m = new Map(); return { getItem: (k) => (m.has(k) ? m.get(k) : null), setItem: (k, v) => m.set(k, String(v)), removeItem: (k) => m.delete(k), m }; };
  const local = globalThis.__local || (globalThis.__local = store());
  const session = store();
  if (flash) session.setItem('gvOfflineFlash', flash);
  if (pendingStock) local.setItem('gvOfflinePendingStock', JSON.stringify(pendingStock));

  const listeners = { document: [], window: {} };
  const body = new FakeNode('body');
  const cfgEl = new FakeNode('script');
  cfgEl._text = JSON.stringify({ userId, pingUrl: '/offline/ping/', syncUrl: '/offline/sync/', version: 'test' });

  const toasts = [];
  const doc = {
    readyState: 'complete', visibilityState: 'visible', cookie: '', body,
    createElement: (t) => new FakeNode(t),
    createTextNode: (t) => { const n = new FakeNode('#text'); n._text = t; return n; },
    getElementById: (id) => (id === 'gvOfflineConfig' && config ? cfgEl : null),
    querySelector: () => null, querySelectorAll: () => [],
    addEventListener: (type, fn, capture) => listeners.document.push({ type, fn, capture: !!capture }),
    dispatchEvent: () => true,
  };
  const navState = { reloaded: 0, assigned: [] };
  const win = {
    document: doc,
    location: {
      pathname: '/sales/add/', search: '', href: 'http://localhost/sales/add/', origin: 'http://localhost',
      reload() { navState.reloaded++; }, assign(u) { navState.assigned.push(u); },
    },
    localStorage: local, sessionStorage: session, indexedDB: idb,
    navigator: { onLine: !offline },
    addEventListener: (t, fn) => { (listeners.window[t] = listeners.window[t] || []).push(fn); },
    confirm: () => true,
    CustomEvent: class { constructor(n) { this.type = n; } },
    FormData: FakeFormData,
    IDBKeyRange: { only: (v) => ({ value: v }) },
    crypto: globalThis.crypto,
    POS_FIXED_BRANCH_ID: 3,
  };
  win.window = win;

  const timers = [];
  const saved = {};
  const set = (k, v) => { saved[k] = Object.getOwnPropertyDescriptor(globalThis, k); Object.defineProperty(globalThis, k, { value: v, configurable: true, writable: true }); };
  Object.entries({
    window: win, document: doc, localStorage: local, sessionStorage: session,
    location: win.location,
    indexedDB: idb, IDBKeyRange: win.IDBKeyRange, FormData: FakeFormData,
    CustomEvent: win.CustomEvent, BroadcastChannel: undefined,
    fetch: (u, o) => server.fetch(u, o),
    setTimeout: (fn, ms) => { timers.push({ fn, ms }); return timers.length; },
    clearTimeout: () => {},
    GerlynOffline: undefined,
  }).forEach(([k, v]) => set(k, v));
  Object.defineProperty(globalThis, 'navigator', { value: win.navigator, configurable: true, writable: true });
  win.setTimeout = globalThis.setTimeout;
  win.GerlynOffline = undefined;

  vm.runInThisContext(CLIENT_SRC, { filename: 'offline-sync.js' });
  await tick();

  // Dispatch a submit the way a browser does: capture listeners, the form's
  // own handlers (none here), then bubble listeners.
  async function submit(form, { submitter = null, preventedByPage = false } = {}) {
    const ev = {
      type: 'submit', target: form, submitter, defaultPrevented: preventedByPage, stopped: false,
      preventDefault() { this.defaultPrevented = true; },
      stopImmediatePropagation() { this.stopped = true; },
    };
    for (const l of listeners.document.filter((x) => x.type === 'submit' && x.capture)) l.fn(ev);
    for (const l of listeners.document.filter((x) => x.type === 'submit' && !x.capture)) {
      if (ev.stopped) break;
      l.fn(ev);
    }
    await tick(20);
    return ev;
  }

  async function fire(windowEvent) {
    (listeners.window[windowEvent] || []).forEach((fn) => fn({}));
    await tick(30);
  }

  const dump = () => [...idb.stores.ops.rows.values()];
  return {
    win, doc, body, idb, local, session, navState, timers, submit, fire, dump,
    opsFor: (uid) => dump().filter((o) => o.userId === uid),
    ui: () => body.find((n) => n.tagName === 'DIV' || n.tagName === 'BUTTON' || n.tagName === 'SPAN'),
    pillLabel: () => {
      const pill = body.find((n) => n.className && n.className.includes('gv-sync-label'))[0];
      return pill ? pill.textContent : null;
    },
    toastsText: () => body.find((n) => n.className && n.className.includes('gv-sync-toast ')).map((n) => n.textContent),
    reset() { globalThis.__idb = null; globalThis.__local = null; },
  };
}

// ── Service worker under a fake runtime ────────────────────────────────────
export function bootServiceWorker(network) {
  const store = new Map();   // cache name -> Map(url -> Response)
  const caches = {
    async open(name) {
      if (!store.has(name)) store.set(name, new Map());
      const m = store.get(name);
      return {
        async match(req) { const r = m.get(typeof req === 'string' ? req : req.url); return r ? r.clone() : undefined; },
        async put(req, res) { m.set(typeof req === 'string' ? req : req.url, res); },
        async add(u) { const r = await network(new Request(new URL(u, 'http://localhost/'))); m.set(u, r); },
        async delete(req) { return m.delete(typeof req === 'string' ? req : req.url); },
        async keys() { return [...m.keys()].map((u) => ({ url: u })); },
      };
    },
    async match(req) {
      for (const m of store.values()) { const r = m.get(typeof req === 'string' ? req : req.url); if (r) return r.clone(); }
      return undefined;
    },
    async keys() { return [...store.keys()]; },
    async delete(name) { return store.delete(name); },
  };
  const handlers = {};
  const selfObj = {
    location: { origin: 'http://localhost' },
    addEventListener: (t, fn) => { handlers[t] = fn; },
    skipWaiting: () => {}, clients: { claim: () => {} },
  };
  const ctx = vm.createContext({
    self: selfObj, caches, fetch: (r) => network(r), Response, Request, Headers, URL,
    Promise, Date, Object, Array, console,
  });
  vm.runInContext(SW_SRC, ctx, { filename: 'sw.js' });

  async function request(url, init = {}) {
    // mode: 'navigate' cannot be set on a real Request, so a navigation is
    // modelled by a plain object carrying just what the worker reads.
    const req = init.navigate
      ? { url: new URL(url, 'http://localhost/').href, method: 'GET', mode: 'navigate', headers: new Headers() }
      : new Request(new URL(url, 'http://localhost/'), init);
    let responded = null;
    const waits = [];
    handlers.fetch({
      request: req,
      respondWith: (p) => { responded = Promise.resolve(p); },
      waitUntil: (p) => waits.push(p),
    });
    await Promise.all(waits);
    return responded ? responded : null; // null = the worker let the network handle it
  }
  return { request, store, handlers, caches };
}

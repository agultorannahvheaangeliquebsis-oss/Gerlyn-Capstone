// Served by core.views.service_worker_view at /sw.js (root scope, not
// /static/js/, so it can control the whole site). This file is a Django
// template — the version number baked into the cache names below is what makes
// cache-busting work: bump core/version.py on any deploy that changes a
// cached asset, and every byte of this file changes, so the browser sees
// it as a genuinely new service worker instead of silently reusing the
// old cache.
//
// Three jobs:
//   1. App shell  — static CSS/JS/images, so a page that is already open (or
//                   revisited) still renders without a connection.
//   2. Saved pages — Inventory, Sales and Procurement pages, NETWORK FIRST.
//                   Online you always get the live page; the saved copy is
//                   only ever served when the network fails. It is stamped so
//                   the page can say "saved copy from 10:42" — never passed
//                   off as live. See static/js/offline-sync.js.
//   3. Fallback   — a small page for a navigation that has neither.
//
// What is deliberately NOT here: any POST. Offline changes are queued by the
// page into IndexedDB and replayed by offline-sync.js against /offline/sync/;
// the worker never touches a write.
const VERSION = '{{ APP_VERSION }}';
const CACHE_NAME = 'gerlyn-app-shell-v' + VERSION;
const PAGE_CACHE = 'gerlyn-pages-v' + VERSION;
const RUNTIME_CACHE = 'gerlyn-runtime-v' + VERSION;
const KNOWN_CACHES = [CACHE_NAME, PAGE_CACHE, RUNTIME_CACHE];

const APP_SHELL = [
  '/static/css/gerlyn.css',
  '/static/css/storefront.css',
  '/static/css/account.css',
  '/static/js/live-search.js',
  '/static/js/toast.js',
  '/static/js/address-picker.js',
  '/static/js/pwa.js',
  '/static/js/offline-sync.js',
  // Both marks: logo.jpg is what the internal shell, the auth screens and
  // the About page still point at (the mark is solid white artwork, flattened
  // onto a green square so it has something to sit on); logo.png is the same
  // artwork with no fill behind it, which is what the storefront header and
  // footer use so the mark sits straight on the green band.
  '/static/img/logo.jpg',
  '/static/img/logo.png',
  '/static/img/icons/icon-192.png',
  '/static/img/icons/icon-512.png',
  '/static/manifest.json',
];

// The one third-party script the internal shell loads (icons). Version-pinned
// in the URL, so a cached copy can never be stale.
const LUCIDE_URL = 'https://unpkg.com/lucide@1.31.0/dist/umd/lucide.js';

// Pages worth keeping a saved copy of. Everything else (billing, reports,
// accounts, the storefront, supplier portal…) always needs the network.
const PAGE_PREFIXES = ['/inventory/', '/sales/', '/procurement/'];
// Never saved: documents, downloads, and anything proof-of-payment.
const PAGE_DENY = /\/(print|pdf|export|download|receipt|payment-proof|portal|import)(\/|$)/;
const MAX_SAVED_PAGES = 80;

self.addEventListener('install', (event) => {
  event.waitUntil(
    caches.open(CACHE_NAME).then((cache) =>
      // One missing file must not abort the whole install.
      Promise.allSettled(APP_SHELL.map((url) => cache.add(url)))
    )
  );
  // Activate straight away. The offline queue is only useful on a device that
  // actually has the new worker; waiting for someone to find the Download
  // page's "Update" button would leave every existing install without it.
  // This does not reload any open page.
  self.skipWaiting();
});

self.addEventListener('activate', (event) => {
  event.waitUntil(
    caches.keys().then((cacheNames) =>
      Promise.all(
        cacheNames
          .filter((name) => name.startsWith('gerlyn-') && !KNOWN_CACHES.includes(name))
          .map((name) => caches.delete(name))
      )
    )
  );
  self.clients.claim();
});

self.addEventListener('message', (event) => {
  const data = event.data || {};
  if (data.type === 'SKIP_WAITING') {
    self.skipWaiting();
  } else if (data.type === 'CLEAR_PAGES') {
    // Sent by the page when the signed-in user changes.
    event.waitUntil(caches.delete(PAGE_CACHE));
  }
});

function isSavablePage(url) {
  return PAGE_PREFIXES.some((p) => url.pathname.startsWith(p)) && !PAGE_DENY.test(url.pathname);
}

async function trimPages(cache) {
  const keys = await cache.keys();
  const extra = keys.length - MAX_SAVED_PAGES;
  for (let i = 0; i < extra; i++) await cache.delete(keys[i]); // oldest first
}

// Re-wrap a response so the moment it was saved travels with it.
async function stamp(response) {
  const headers = new Headers(response.headers);
  headers.set('X-Gerlyn-Saved-At', new Date().toISOString());
  return new Response(await response.blob(), {
    status: response.status, statusText: response.statusText, headers,
  });
}

// A saved page is handed back with its save time injected, so the page's own
// script can show the "saved copy" notice. Done in the worker because a page
// cannot read the headers of its own navigation.
async function annotate(response) {
  const savedAt = response.headers.get('X-Gerlyn-Saved-At') || '';
  const html = await response.text();
  const tag = '<meta name="gerlyn-saved-copy" content="' + savedAt + '">';
  const body = html.includes('</head>') ? html.replace('</head>', tag + '</head>') : tag + html;
  const headers = new Headers(response.headers);
  headers.set('Content-Type', 'text/html; charset=utf-8');
  headers.delete('Content-Length');
  return new Response(body, { status: 200, statusText: 'OK', headers });
}

async function savedPageFirstOnFailure(request) {
  const cache = await caches.open(PAGE_CACHE);
  try {
    const response = await fetch(request);
    const type = response.headers.get('Content-Type') || '';
    // A redirect means we were bounced somewhere else (login, role home) —
    // that is not a copy of what was asked for.
    if (response.ok && !response.redirected && type.includes('text/html')) {
      const copy = await stamp(response.clone());
      await cache.put(request.url, copy);
      trimPages(cache);
    }
    return response;
  } catch (err) {
    const hit = await cache.match(request.url, { ignoreVary: true });
    if (hit) return annotate(hit);
    if (request.mode === 'navigate') return offlineFallback();
    throw err;
  }
}

function offlineFallback() {
  const html = '<!DOCTYPE html><html lang="en"><head><meta charset="UTF-8">'
    + '<meta name="viewport" content="width=device-width, initial-scale=1">'
    + '<title>Offline — GERLYN</title>'
    + '<link rel="stylesheet" href="/static/css/gerlyn.css">'
    + '</head><body class="gv-offline-fallback" data-gv-offline-fallback="1">'
    + '<main class="gv-offline-fallback__card">'
    + '<h1>You are offline</h1>'
    + '<p>This page was not saved on this device, so it cannot be shown without a connection.</p>'
    + '<p>Pages you opened recently while online (Inventory, Sales and Procurement) are still '
    + 'available. Anything you entered offline is kept on this device and will sync '
    + 'automatically when the connection returns.</p>'
    + '<p><button type="button" class="btn btn-primary" onclick="location.reload()">Try again</button> '
    + '<button type="button" class="btn" onclick="history.back()">Go back</button></p>'
    + '</main>'
    + '<script src="/static/js/offline-sync.js"></script>'
    + '</body></html>';
  return new Response(html, {
    status: 200,
    headers: { 'Content-Type': 'text/html; charset=utf-8', 'Cache-Control': 'no-store' },
  });
}

// Static files: answer from cache, refresh behind it. Templates reference
// hashed names in production, so match the exact URL rather than a fixed list.
async function staticAsset(request) {
  const cache = await caches.open(RUNTIME_CACHE);
  const cached = (await cache.match(request)) || (await caches.match(request));
  const refresh = fetch(request)
    .then((response) => {
      if (response && (response.ok || response.type === 'opaque')) {
        cache.put(request, response.clone());
      }
      return response;
    })
    .catch(() => cached);
  return cached || refresh;
}

self.addEventListener('fetch', (event) => {
  const request = event.request;
  if (request.method !== 'GET') return; // writes are the page's business, never ours

  if (request.url === LUCIDE_URL) {
    event.respondWith(staticAsset(request));
    return;
  }

  const url = new URL(request.url);
  if (url.origin !== self.location.origin) return;

  // Session boundaries. A saved page was rendered for whoever was signed in;
  // once that session ends (logout, idle timeout, someone else signing in)
  // those copies must not outlive it.
  if (url.pathname === '/login/' || url.pathname === '/logout/') {
    event.waitUntil(caches.delete(PAGE_CACHE));
    // The idle timeout lands here. Offline there is nowhere to sign in to:
    // say so (and where the unsynced changes are) rather than show the
    // browser's error page. Online this is a plain pass-through.
    event.respondWith(
      fetch(request).catch((err) => {
        if (request.mode === 'navigate') return offlineFallback();
        throw err;
      })
    );
    return;
  }

  if (url.pathname.startsWith('/static/') || APP_SHELL.includes(url.pathname)) {
    event.respondWith(staticAsset(request));
    return;
  }

  if (isSavablePage(url)) {
    event.respondWith(savedPageFirstOnFailure(request));
  }
  // Anything else: straight to the network, exactly as before.
});

// Served by core.views.service_worker_view at /sw.js (root scope, not
// /static/js/, so it can control the whole site). This file is a Django
// template — the version number baked into CACHE_NAME below is what makes
// cache-busting work: bump core/version.py on any deploy that changes a
// cached asset, and every byte of this file changes, so the browser sees
// it as a genuinely new service worker instead of silently reusing the
// old cache.
const CACHE_NAME = 'gerlyn-app-shell-v{{ APP_VERSION }}';

// App shell only — static assets, never pages. Product listings, prices,
// cart, and checkout must always hit the network fresh; caching those
// risks showing stale stock or prices while "offline-available".
const APP_SHELL = [
  '/static/css/gerlyn.css',
  '/static/css/storefront.css',
  '/static/css/account.css',
  '/static/js/live-search.js',
  '/static/js/toast.js',
  '/static/js/address-picker.js',
  '/static/js/pwa.js',
  '/static/img/logo.jpg',
  '/static/img/icons/icon-192.png',
  '/static/img/icons/icon-512.png',
  '/static/manifest.json',
];

self.addEventListener('install', (event) => {
  event.waitUntil(
    caches.open(CACHE_NAME).then((cache) => cache.addAll(APP_SHELL))
  );
  // Deliberately NOT calling skipWaiting() here — a newly-installed worker
  // sits in "waiting" until the user confirms via the Download page's
  // "Update" button (see pwa.js), so the reload happens on their terms,
  // not silently mid-session.
});

self.addEventListener('activate', (event) => {
  event.waitUntil(
    caches.keys().then((cacheNames) =>
      Promise.all(
        cacheNames
          .filter((name) => name !== CACHE_NAME)
          .map((name) => caches.delete(name))
      )
    )
  );
  self.clients.claim();
});

self.addEventListener('message', (event) => {
  if (event.data && event.data.type === 'SKIP_WAITING') {
    self.skipWaiting();
  }
});

self.addEventListener('fetch', (event) => {
  if (event.request.method !== 'GET') return;

  const url = new URL(event.request.url);
  if (url.origin !== self.location.origin || !APP_SHELL.includes(url.pathname)) {
    return; // not app-shell — let the browser handle it normally (network)
  }

  event.respondWith(
    caches.match(event.request).then((cached) => {
      const networkFetch = fetch(event.request)
        .then((response) => {
          if (response && response.ok) {
            const copy = response.clone();
            caches.open(CACHE_NAME).then((cache) => cache.put(event.request, copy));
          }
          return response;
        })
        .catch(() => cached);
      return cached || networkFetch;
    })
  );
});

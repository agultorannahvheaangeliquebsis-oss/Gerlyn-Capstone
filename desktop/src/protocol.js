'use strict';
/**
 * The request handler installed on the app's session.
 *
 * Everything is a pass-through to the network EXCEPT GET/HEAD requests for
 * files we ship locally. For those:
 *
 *   hashed /static files, mirrored CDN files   local copy first. The name
 *                                              pins the content, so it cannot
 *                                              be stale, and it costs nothing.
 *   other /static files                        network first (so a deploy is
 *                                              picked up immediately), with
 *                                              the local copy used when the
 *                                              server is unreachable, slow,
 *                                              or answers 5xx.
 *   /__gerlyn_desktop__/*                      the launch page, always local.
 *
 * Documents, API calls, form posts and the offline queue's own requests are
 * never answered locally: a saved page or a synthesized "success" in their
 * place would be exactly the silent data loss the queue exists to prevent.
 * If the network is down for those, the failure is passed on untouched so
 * the service worker (saved pages) and offline-sync.js (queue) can do their
 * jobs as designed.
 */
const fs = require('fs');

const STATIC_NETWORK_TIMEOUT_MS = 3000;

function localResponse(hit, method, body) {
  const headers = {
    'Content-Type': hit.type,
    'Content-Length': String(body.length),
    'X-Gerlyn-Desktop-Source': 'bundle',
  };
  if (hit.kind === 'page') {
    headers['Cache-Control'] = 'no-store';
  } else {
    headers['Cache-Control'] = hit.immutable ? 'public, max-age=31536000, immutable' : 'no-cache';
    // Mirrored fonts are fetched in CORS mode by @font-face.
    headers['Access-Control-Allow-Origin'] = '*';
    headers['Access-Control-Expose-Headers'] = 'X-Gerlyn-Desktop-Source';
  }
  return new Response(method === 'HEAD' ? null : body, { status: 200, headers });
}

/**
 * @param {object} deps
 * @param {() => string} deps.getOrigin       the pinned server origin
 * @param {import('./resources').LocalResources} deps.resources
 * @param {() => boolean} deps.isOnline       monitor's current belief
 * @param {(request: Request, init?: object) => Promise<Response>} deps.fetchNetwork
 * @param {(error: Error, url: string) => void} [deps.onNetworkError]
 * @param {number} [deps.staticTimeoutMs]
 */
function createHandler({
  getOrigin, resources, isOnline, fetchNetwork, onNetworkError, staticTimeoutMs = STATIC_NETWORK_TIMEOUT_MS,
}) {
  async function network(request, init) {
    try {
      // fetchNetwork (network.js) returns 3xx answers untouched: following
      // them here would make the page "load" at the pre-redirect URL.
      return await fetchNetwork(request, init || {});
    } catch (error) {
      if (onNetworkError) onNetworkError(error, request.url);
      throw error;
    }
  }

  async function local(hit, method) {
    try {
      return localResponse(hit, method, await fs.promises.readFile(hit.file));
    } catch (e) {
      return null;
    }
  }

  async function networkFirst(request, hit, method) {
    const signals = [AbortSignal.timeout(staticTimeoutMs)];
    if (request.signal) signals.push(request.signal);
    try {
      const res = await network(request, { signal: AbortSignal.any(signals) });
      if (res.status < 500) return res;
    } catch (e) {
      // fall through to the bundled copy
    }
    return (await local(hit, method)) || network(request);
  }

  async function route(request) {
    const method = request.method.toUpperCase();
    if (method === 'GET' || method === 'HEAD') {
      let url = null;
      try {
        url = new URL(request.url);
      } catch (e) {
        url = null;
      }
      const hit = url && resources.lookup(url, getOrigin());
      if (hit) {
        if (hit.kind === 'page' || hit.immutable || isOnline() === false) {
          const res = await local(hit, method);
          if (res) return res;
          return network(request);
        }
        return networkFirst(request, hit, method);
      }
    }
    return network(request);
  }

  // A failed request must look to the page exactly like a failed network
  // request (fetch() rejects, a navigation fails), which is what the service
  // worker and the offline queue rely on. onNetworkError has already recorded
  // why.
  return async function handle(request) {
    try {
      return await route(request);
    } catch (error) {
      return Response.error();
    }
  };
}

module.exports = { createHandler, STATIC_NETWORK_TIMEOUT_MS };

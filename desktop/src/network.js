'use strict';
/**
 * Forward a request the app does not answer locally to the real network, as
 * faithfully as the page itself would have.
 *
 * Why this exists instead of `session.fetch(request)`:
 *   - session.fetch FOLLOWS redirects internally. The page would then "load"
 *     at the pre-redirect URL (wrong location and relative links) and, worse,
 *     one-time Django flash messages would be consumed by a request whose
 *     result the page never sees.
 *   - Electron's fetch rejects outright for redirect: 'manual'.
 * ClientRequest in manual mode reports the redirect (status, target, headers)
 * without following it, so the 3xx is handed back untouched and Chromium
 * follows it with the right URL, method and cookies.
 *
 * Cookies, TLS, HTTP/2, caching and decompression are the Chromium network
 * stack's, exactly as for a normal page load.
 */

// Set by the network stack or forbidden to forward.
const SKIP_REQUEST = new Set([
  'host', 'connection', 'content-length', 'cookie', 'transfer-encoding', 'accept-encoding',
  'keep-alive', 'upgrade', 'te', 'trailer', 'proxy-connection', 'expect',
]);
// The stack has already decoded the body; the original framing no longer applies.
const SKIP_RESPONSE = new Set(['content-encoding', 'content-length', 'transfer-encoding', 'connection', 'keep-alive']);
const NULL_BODY_STATUS = new Set([101, 204, 205, 304]);

function toResponseHeaders(source, extra) {
  const headers = new Headers();
  Object.entries(source || {}).forEach(([name, value]) => {
    if (SKIP_RESPONSE.has(name.toLowerCase())) return;
    (Array.isArray(value) ? value : [value]).forEach((v) => {
      try { headers.append(name, String(v)); } catch (e) { /* unrepresentable header: drop it */ }
    });
  });
  Object.entries(extra || {}).forEach(([name, value]) => headers.set(name, value));
  return headers;
}

function requestHeaders(request, method) {
  const out = {};
  request.headers.forEach((value, name) => {
    if (!SKIP_REQUEST.has(name.toLowerCase())) out[name] = value;
  });
  const has = (name) => Object.keys(out).some((k) => k.toLowerCase() === name);

  // The fetch layer hides Referer/Origin from handlers. Django's CSRF check
  // over https needs one of them, so restore what the browser would have sent.
  let referrer = null;
  try {
    if (request.referrer && request.referrer !== 'about:client') referrer = new URL(request.referrer);
  } catch (e) { referrer = null; }
  if (referrer && !has('referer') && request.referrerPolicy !== 'no-referrer') out.Referer = referrer.href;
  if (referrer && !has('origin') && method !== 'GET' && method !== 'HEAD') out.Origin = referrer.origin;
  return out;
}

/**
 * @param {{net: object, session: object}} deps  Electron's `net` and the app session
 * @returns {(request: Request, init?: {signal?: AbortSignal}) => Promise<Response>}
 */
function createNetworkFetch({ net, session }) {
  return async function fetchNetwork(request, init = {}) {
    const method = request.method.toUpperCase();
    const signal = init.signal || request.signal;
    if (signal && signal.aborted) throw signal.reason || new DOMException('Aborted', 'AbortError');

    const body = method === 'GET' || method === 'HEAD' || !request.body
      ? null
      : Buffer.from(await request.arrayBuffer());

    return new Promise((resolve, reject) => {
      let settled = false;
      let onAbort = null;
      const finish = (fn, value) => {
        if (settled) return;
        settled = true;
        if (signal && onAbort) signal.removeEventListener('abort', onAbort);
        fn(value);
      };

      const req = net.request({
        method,
        url: request.url,
        headers: requestHeaders(request, method),
        session,
        redirect: 'manual',
        credentials: request.credentials === 'omit' ? 'omit' : 'include',
        bypassCustomProtocolHandlers: true,
        priority: 'highest',
      });

      if (signal) {
        onAbort = () => {
          finish(reject, signal.reason || new DOMException('Aborted', 'AbortError'));
          try { req.abort(); } catch (e) { /* already finished */ }
        };
        signal.addEventListener('abort', onAbort, { once: true });
      }

      req.on('redirect', (statusCode, _method, redirectUrl, responseHeaders) => {
        // Not followed: the loader cancels itself, and Chromium re-issues it.
        finish(resolve, new Response(null, {
          status: statusCode,
          headers: toResponseHeaders(responseHeaders, { location: redirectUrl }),
        }));
      });

      req.on('response', (res) => {
        const status = res.statusCode;
        const headers = toResponseHeaders(res.headers);
        const meta = { status, statusText: res.statusMessage || '', headers };

        if (method === 'HEAD' || NULL_BODY_STATUS.has(status)) {
          res.on('data', () => {});
          res.on('error', () => {});
          finish(resolve, new Response(null, meta));
          return;
        }

        const stream = new ReadableStream({
          start(controller) {
            res.on('data', (chunk) => controller.enqueue(new Uint8Array(chunk)));
            res.on('end', () => controller.close());
            res.on('error', (e) => controller.error(e));
            res.on('aborted', () => controller.error(new TypeError('network connection lost')));
          },
          cancel() {
            try { req.abort(); } catch (e) { /* already finished */ }
          },
        });
        finish(resolve, new Response(stream, meta));
      });

      // After a manual redirect the loader reports "Redirect was cancelled";
      // by then the 3xx has been delivered, so it is not an error for us.
      req.on('error', (error) => finish(reject, new TypeError(error && error.message || String(error))));

      if (body && body.length) req.write(body);
      req.end();
    });
  };
}

module.exports = { createNetworkFetch, requestHeaders, toResponseHeaders };

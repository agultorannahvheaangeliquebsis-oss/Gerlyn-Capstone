'use strict';
/**
 * Pure decisions about what the app window is allowed to do. Kept free of
 * Electron imports so they can be unit-tested and so main.js only has to wire
 * them to events.
 */

// Permissions the GerlynPLUS pages legitimately use. The camera is for the
// barcode scanner; clipboard-write for "copy" buttons. Everything else
// (geolocation, midi, usb, ...) is refused.
const ALLOWED_PERMISSIONS = new Set([
  'media',
  'clipboard-sanitized-write',
  'fullscreen',
  'notifications',
]);

function sameOrigin(url, origin) {
  try {
    return new URL(url).origin === origin;
  } catch (e) {
    return false;
  }
}

/** May the app window itself navigate to `url`? Only the pinned server. */
function allowNavigation(url, origin) {
  return sameOrigin(url, origin);
}

/** May `url` be handed to the operating system's default browser? */
function allowExternal(url) {
  try {
    const { protocol, username, password } = new URL(url);
    return (protocol === 'https:' || protocol === 'http:') && !username && !password;
  } catch (e) {
    return false;
  }
}

/** Decision for window.open / target=_blank. */
function windowOpenAction(url, origin) {
  if (sameOrigin(url, origin)) return 'allow';
  if (allowExternal(url)) return 'external';
  return 'deny';
}

/** Permission requests are only ever granted to the pinned server's pages. */
function allowPermission(permission, requestingUrl, origin) {
  return ALLOWED_PERMISSIONS.has(permission) && sameOrigin(requestingUrl, origin);
}

module.exports = {
  ALLOWED_PERMISSIONS,
  sameOrigin,
  allowNavigation,
  allowExternal,
  windowOpenAction,
  allowPermission,
};

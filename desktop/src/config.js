'use strict';
/**
 * Which GerlynPLUS server this desktop app talks to.
 *
 * The desktop app is a shell around the live Django site, so the one piece of
 * configuration that matters is the server's origin. It is resolved, in order,
 * from:
 *   1. GERLYN_SERVER_URL        (environment; support / testing)
 *   2. <userData>/settings.json (set from the in-app "Server" screen)
 *   3. <resources>/app-config.json (baked in at build time)
 *
 * Only an *origin* is ever kept. Everything the app does afterwards -- what it
 * may navigate to, which requests it answers from the local bundle, what the
 * offline queue is allowed to talk to -- is pinned to it.
 */
const fs = require('fs');
const path = require('path');

const SETTINGS_FILE = 'settings.json';

// RFC 1918 + loopback + link-local + *.local: hosts where plain http is a
// deliberate on-premises deployment rather than an accident.
function isPrivateHost(hostname) {
  const host = hostname.replace(/^\[|\]$/g, '').toLowerCase();
  if (host === 'localhost' || host.endsWith('.localhost') || host.endsWith('.local')) return true;
  if (host === '::1') return true;
  const m = /^(\d{1,3})\.(\d{1,3})\.(\d{1,3})\.(\d{1,3})$/.exec(host);
  if (!m) return false;
  const [a, b] = [Number(m[1]), Number(m[2])];
  if ([a, b, Number(m[3]), Number(m[4])].some((n) => n > 255)) return false;
  return a === 10 || a === 127 || (a === 172 && b >= 16 && b <= 31) ||
    (a === 192 && b === 168) || (a === 169 && b === 254);
}

/**
 * Turn whatever a person typed into a pinned origin, or say why not.
 * @returns {{ok: true, origin: string, insecure: boolean} | {ok: false, error: string}}
 */
function normalizeServerUrl(input) {
  let text = String(input == null ? '' : input).trim();
  if (!text) return { ok: false, error: 'Enter the address of your GerlynPLUS server.' };
  if (!/^[a-z][a-z0-9+.-]*:\/\//i.test(text)) text = `https://${text}`;

  let url;
  try {
    url = new URL(text);
  } catch (e) {
    return { ok: false, error: 'That does not look like a web address.' };
  }
  if (url.protocol !== 'https:' && url.protocol !== 'http:') {
    return { ok: false, error: 'Only https:// addresses can be used.' };
  }
  if (url.username || url.password) {
    return { ok: false, error: 'Do not put a user name or password in the address.' };
  }
  if (!url.hostname) return { ok: false, error: 'That address has no host name.' };
  if (url.protocol === 'http:' && !isPrivateHost(url.hostname)) {
    return {
      ok: false,
      error: 'Plain http:// is only allowed for this computer or a private network address. '
        + 'Use the https:// address of your server.',
    };
  }
  return { ok: true, origin: url.origin, insecure: url.protocol === 'http:' };
}

function readJson(file) {
  try {
    return JSON.parse(fs.readFileSync(file, 'utf8'));
  } catch (e) {
    return null;
  }
}

function readSettings(userDataDir) {
  const data = readJson(path.join(userDataDir, SETTINGS_FILE));
  return data && typeof data === 'object' ? data : {};
}

// Write-then-rename so a crash mid-write cannot leave a half-written file.
function writeSettings(userDataDir, patch) {
  fs.mkdirSync(userDataDir, { recursive: true });
  const target = path.join(userDataDir, SETTINGS_FILE);
  const next = { ...readSettings(userDataDir), ...patch };
  const tmp = `${target}.${process.pid}.tmp`;
  fs.writeFileSync(tmp, JSON.stringify(next, null, 2), 'utf8');
  fs.renameSync(tmp, target);
  return next;
}

/**
 * @returns {{origin: string, source: 'env'|'settings'|'build', locked: boolean} | null}
 *   `locked` means the in-app Server screen cannot change it (environment).
 */
function resolveServer({ env = process.env, userDataDir, resourcesDir }) {
  const candidates = [];
  if (env.GERLYN_SERVER_URL) candidates.push(['env', env.GERLYN_SERVER_URL, true]);
  if (userDataDir) candidates.push(['settings', readSettings(userDataDir).serverUrl, false]);
  if (resourcesDir) {
    const built = readJson(path.join(resourcesDir, 'app-config.json'));
    if (built) candidates.push(['build', built.serverUrl, false]);
  }
  for (const [source, value, locked] of candidates) {
    if (!value) continue;
    const parsed = normalizeServerUrl(value);
    if (parsed.ok) return { origin: parsed.origin, source, locked };
  }
  return null;
}

module.exports = {
  SETTINGS_FILE,
  isPrivateHost,
  normalizeServerUrl,
  readSettings,
  writeSettings,
  resolveServer,
};

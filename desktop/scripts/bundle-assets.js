#!/usr/bin/env node
'use strict';
/**
 * Build-time step: gather everything the desktop app serves locally.
 *
 *   desktop/bundled/static/      the Django project's static files
 *   desktop/bundled/external/    the third-party files the templates load
 *                                (lucide icons, Google Fonts) + a manifest
 *   desktop/app-config.json      the server address baked into this build
 *   desktop/build/icon.png       installer / window icon, from the web app's own
 *
 * Static source, first that exists:
 *   --static=<dir> | GERLYN_STATIC_DIR | ../staticfiles (collectstatic output,
 *   hashed names) | ../static (the source tree).
 *
 * Needs the network once, for the external files. Pass
 * --allow-missing-external (or GERLYN_BUNDLE_OFFLINE_OK=1) to build without
 * them; icons and fonts then fall back to the saved-page cache / system fonts.
 */
const fs = require('fs');
const path = require('path');
const crypto = require('crypto');

const { normalizeServerUrl } = require('../src/config');

const DESKTOP = path.resolve(__dirname, '..');
const REPO = path.resolve(DESKTOP, '..');
const OUT = path.join(DESKTOP, 'bundled');
const LOCK_FILE = path.join(__dirname, 'external.lock.json');

// Only hosts the templates are known to use. Anything else found in a
// template is reported, not fetched.
const EXTERNAL_HOSTS = new Set(['unpkg.com', 'fonts.googleapis.com']);
const FONT_FILE_HOST = 'fonts.gstatic.com';
// Google serves WOFF2 only to browsers it recognises.
const BROWSER_UA = 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 '
  + '(KHTML, like Gecko) Chrome/130.0.0.0 Safari/537.36';

const EXCLUDE_TOP_LEVEL = new Set(['admin', 'debug_toolbar', 'rest_framework']);
const EXCLUDE_FILE = /\.(gz|br|zst)$|^staticfiles\.json$/;

function args() {
  const out = { allowMissingExternal: process.env.GERLYN_BUNDLE_OFFLINE_OK === '1' };
  for (const a of process.argv.slice(2)) {
    if (a.startsWith('--static=')) out.static = a.slice('--static='.length);
    else if (a === '--allow-missing-external') out.allowMissingExternal = true;
  }
  return out;
}

function sha256(buf) {
  return crypto.createHash('sha256').update(buf).digest('hex');
}

function walk(dir, visit, rel = '') {
  for (const entry of fs.readdirSync(dir, { withFileTypes: true })) {
    const next = rel ? `${rel}/${entry.name}` : entry.name;
    if (entry.isDirectory()) walk(path.join(dir, entry.name), visit, next);
    else if (entry.isFile()) visit(path.join(dir, entry.name), next);
  }
}

function pickStaticSource(opt) {
  const candidates = [
    opt.static,
    process.env.GERLYN_STATIC_DIR,
    path.join(REPO, 'staticfiles'),
    path.join(REPO, 'static'),
  ].filter(Boolean).map((p) => path.resolve(p));
  for (const dir of candidates) {
    try {
      if (fs.statSync(dir).isDirectory() && fs.readdirSync(dir).length) return dir;
    } catch (e) { /* try next */ }
  }
  throw new Error(`No static files found. Looked in:\n  ${candidates.join('\n  ')}`);
}

function copyStatic(source) {
  const dest = path.join(OUT, 'static');
  let files = 0;
  let bytes = 0;
  walk(source, (abs, rel) => {
    if (EXCLUDE_TOP_LEVEL.has(rel.split('/')[0])) return;
    if (EXCLUDE_FILE.test(path.basename(rel))) return;
    const target = path.join(dest, rel);
    fs.mkdirSync(path.dirname(target), { recursive: true });
    fs.copyFileSync(abs, target);
    files += 1;
    bytes += fs.statSync(abs).size;
  });
  return { files, bytes };
}

// ── external files ──────────────────────────────────────────────────────────

function findTemplateUrls() {
  const found = new Set();
  const skipped = new Set();
  walk(path.join(REPO, 'templates'), (abs) => {
    if (!abs.endsWith('.html') && !abs.endsWith('.js')) return;
    const text = fs.readFileSync(abs, 'utf8');
    for (const m of text.matchAll(/(?:src|href)\s*=\s*"(https?:\/\/[^"]+)"/g)) {
      const url = m[1].replace(/&amp;/g, '&');
      let parsed;
      try { parsed = new URL(url); } catch (e) { continue; }
      // A bare origin is a preconnect hint (or a plain outbound link), not a file.
      if (parsed.pathname === '/' && !parsed.search) continue;
      if (EXTERNAL_HOSTS.has(parsed.host)) found.add(parsed.href);
      else if (parsed.host !== FONT_FILE_HOST) skipped.add(url);
    }
    // sw.js names the icon script as a JS constant.
    for (const m of text.matchAll(/'(https:\/\/unpkg\.com\/[^']+)'/g)) found.add(new URL(m[1]).href);
  });
  return { found: [...found].sort(), skipped: [...skipped].sort() };
}

async function download(url) {
  const res = await fetch(url, { headers: { 'User-Agent': BROWSER_UA }, signal: AbortSignal.timeout(30000) });
  if (!res.ok) throw new Error(`${url} -> HTTP ${res.status}`);
  return Buffer.from(await res.arrayBuffer());
}

function loadLock() {
  try {
    return JSON.parse(fs.readFileSync(LOCK_FILE, 'utf8'));
  } catch (e) {
    return {};
  }
}

async function bundleExternal() {
  const { found, skipped } = findTemplateUrls();
  const lock = loadLock();
  const files = {};
  const extDir = path.join(OUT, 'external');
  fs.mkdirSync(extDir, { recursive: true });
  let lockChanged = false;

  const store = (name, buf, type) => {
    fs.mkdirSync(path.dirname(path.join(extDir, name)), { recursive: true });
    fs.writeFileSync(path.join(extDir, name), buf);
    return { file: name, type, sha256: sha256(buf) };
  };

  for (const href of found) {
    const u = new URL(href);
    const buf = await download(href);
    const digest = sha256(buf);

    if (u.host === 'unpkg.com') {
      // Version-pinned in the URL, so its bytes must never change.
      if (lock[href] && lock[href] !== digest) {
        throw new Error(`${href}\n  hash changed: locked ${lock[href]}\n  fetched    ${digest}`);
      }
      if (!lock[href]) { lock[href] = digest; lockChanged = true; }
      const name = `unpkg/${u.pathname.replace(/^\//, '').replace(/[@/]/g, '_')}`;
      files[href] = store(name, buf, 'text/javascript; charset=utf-8');
      continue;
    }

    // Google Fonts CSS: also mirror every font file it points at.
    let css = buf.toString('utf8');
    const fontUrls = [...css.matchAll(/url\((https:\/\/fonts\.gstatic\.com\/[^)]+)\)/g)].map((m) => m[1]);
    for (const fontUrl of new Set(fontUrls)) {
      const fu = new URL(fontUrl);
      if (fu.host !== FONT_FILE_HOST) continue;
      const name = `gstatic/${fu.pathname.replace(/^\//, '')}`;
      files[fu.href] = store(name, await download(fontUrl), 'font/woff2');
    }
    files[href] = store(`fonts/${sha256(Buffer.from(href)).slice(0, 12)}.css`, Buffer.from(css), 'text/css; charset=utf-8');
  }

  if (lockChanged) fs.writeFileSync(LOCK_FILE, `${JSON.stringify(lock, null, 2)}\n`);
  fs.writeFileSync(
    path.join(extDir, 'manifest.json'),
    JSON.stringify({ generatedAt: new Date().toISOString(), files }, null, 2),
  );
  return { count: Object.keys(files).length, skipped, lockChanged };
}

// ── config + icon ───────────────────────────────────────────────────────────

function writeAppConfig() {
  const target = path.join(DESKTOP, 'app-config.json');
  let serverUrl = process.env.GERLYN_SERVER_URL || '';
  if (!serverUrl) {
    try { serverUrl = JSON.parse(fs.readFileSync(target, 'utf8')).serverUrl || ''; } catch (e) { /* none */ }
  }
  if (serverUrl) {
    const parsed = normalizeServerUrl(serverUrl);
    if (!parsed.ok) throw new Error(`GERLYN_SERVER_URL is not usable: ${parsed.error}`);
    serverUrl = parsed.origin;
  }
  fs.writeFileSync(target, `${JSON.stringify({ serverUrl }, null, 2)}\n`);
  return serverUrl;
}

function syncIcons(staticSource) {
  const icon512 = [path.join(staticSource, 'img', 'icons', 'icon-512.png'),
    path.join(REPO, 'static', 'img', 'icons', 'icon-512.png')].find((p) => fs.existsSync(p));
  if (icon512) {
    fs.mkdirSync(path.join(DESKTOP, 'build'), { recursive: true });
    fs.copyFileSync(icon512, path.join(DESKTOP, 'build', 'icon.png'));
  }
}

async function main() {
  const opt = args();
  const source = pickStaticSource(opt);

  // `bundled/` is wholly generated; clear it so removed files do not linger.
  if (path.basename(OUT) !== 'bundled' || path.dirname(OUT) !== DESKTOP) throw new Error('refusing to clean unexpected path');
  fs.rmSync(OUT, { recursive: true, force: true });
  fs.mkdirSync(OUT, { recursive: true });

  const copied = copyStatic(source);
  console.log(`static   ${copied.files} files (${(copied.bytes / 1024).toFixed(0)} KB) from ${path.relative(REPO, source) || '.'}`);

  syncIcons(source);

  try {
    const ext = await bundleExternal();
    console.log(`external ${ext.count} files mirrored (icons, fonts)`);
    if (ext.lockChanged) console.log('         scripts/external.lock.json updated - commit it');
    for (const url of ext.skipped) console.warn(`warning  not bundled (not an allow-listed host): ${url}`);
  } catch (error) {
    if (!opt.allowMissingExternal) {
      console.error(`\nCould not mirror the external files: ${error.message}\n`
        + 'Re-run with network access, or pass --allow-missing-external to build without them.');
      process.exit(1);
    }
    console.warn(`warning  external files not bundled: ${error.message}`);
    fs.mkdirSync(path.join(OUT, 'external'), { recursive: true });
  }

  const serverUrl = writeAppConfig();
  console.log(`config   server = ${serverUrl || '(not set: the app will ask on first run)'}`);
}

main().catch((error) => {
  console.error(error.message || error);
  process.exit(1);
});

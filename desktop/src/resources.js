'use strict';
/**
 * What the desktop app can answer from files that ship inside the installer.
 *
 * Three kinds of local resource, all looked up by URL and never by anything
 * a page can influence beyond the URL itself:
 *
 *   static    <origin>/static/**   the site's own CSS/JS/images, copied from
 *                                  the Django project at build time
 *   external  third-party files    the few things the templates load from a
 *                                  CDN (icons, Roboto) mirrored at build time
 *   page      <origin>/__gerlyn_desktop__/*   the offline launch page
 *
 * Nothing here talks to the network; see protocol.js for how these are
 * combined with it.
 */
const fs = require('fs');
const path = require('path');

const DESKTOP_PREFIX = '/__gerlyn_desktop__/';
const LAUNCH_PATH = `${DESKTOP_PREFIX}launch`;

// Files under src/pages/ that may be served. An allow-list, so a URL can never
// be used to read anything else out of the application folder.
const PAGE_FILES = {
  launch: { file: 'launch.html', type: 'text/html; charset=utf-8' },
  'launch.js': { file: 'launch.js', type: 'text/javascript; charset=utf-8' },
  'launch.css': { file: 'launch.css', type: 'text/css; charset=utf-8' },
};

// WhiteNoise's ManifestStaticFilesStorage names files `name.<12 hex>.ext`.
// Those names are content-addressed, so a bundled copy can never be stale.
const HASHED_RE = /\.[0-9a-f]{12}\.[A-Za-z0-9]+$/;

const TYPES = {
  '.css': 'text/css; charset=utf-8',
  '.js': 'text/javascript; charset=utf-8',
  '.mjs': 'text/javascript; charset=utf-8',
  '.json': 'application/json; charset=utf-8',
  '.html': 'text/html; charset=utf-8',
  '.svg': 'image/svg+xml',
  '.png': 'image/png',
  '.jpg': 'image/jpeg',
  '.jpeg': 'image/jpeg',
  '.gif': 'image/gif',
  '.webp': 'image/webp',
  '.ico': 'image/x-icon',
  '.woff': 'font/woff',
  '.woff2': 'font/woff2',
  '.ttf': 'font/ttf',
  '.map': 'application/json; charset=utf-8',
  '.txt': 'text/plain; charset=utf-8',
};

function contentType(file) {
  return TYPES[path.extname(file).toLowerCase()] || 'application/octet-stream';
}

/** `rel` (already URL-decoded) joined under `root`, or null if it escapes. */
function safeJoin(root, rel) {
  if (!rel || rel.includes('\0') || rel.includes('\\') || /^[a-z]:/i.test(rel)) return null;
  if (rel.split('/').some((part) => part === '..')) return null;
  const full = path.resolve(root, rel);
  const base = path.resolve(root) + path.sep;
  return full.startsWith(base) ? full : null;
}

function isFile(file) {
  try {
    return fs.statSync(file).isFile();
  } catch (e) {
    return false;
  }
}

class LocalResources {
  /**
   * @param {{bundleDir: string, pagesDir: string}} dirs
   *   bundleDir holds `static/` and `external/` (see scripts/bundle-assets.js)
   */
  constructor({ bundleDir, pagesDir }) {
    this.bundleDir = bundleDir;
    this.pagesDir = pagesDir;
    this.staticDir = path.join(bundleDir, 'static');
    this.externalDir = path.join(bundleDir, 'external');
    this.external = this._loadExternalManifest();
  }

  _loadExternalManifest() {
    try {
      const raw = JSON.parse(fs.readFileSync(path.join(this.externalDir, 'manifest.json'), 'utf8'));
      return raw && typeof raw.files === 'object' ? raw.files : {};
    } catch (e) {
      return {};
    }
  }

  /** Has a build-time bundle been produced for this install? */
  hasStaticBundle() {
    try {
      return fs.statSync(this.staticDir).isDirectory();
    } catch (e) {
      return false;
    }
  }

  /**
   * @param {URL} url
   * @param {string} origin the pinned server origin
   * @returns {{kind: 'page'|'static'|'external', file: string, type: string,
   *            immutable: boolean} | null}
   */
  lookup(url, origin) {
    if (url.origin === origin) {
      if (url.pathname.startsWith(DESKTOP_PREFIX)) {
        const entry = PAGE_FILES[url.pathname.slice(DESKTOP_PREFIX.length)];
        if (!entry) return null;
        return {
          kind: 'page', file: path.join(this.pagesDir, entry.file), type: entry.type, immutable: false,
        };
      }
      if (url.pathname.startsWith('/static/')) {
        let rel;
        try {
          rel = decodeURIComponent(url.pathname.slice('/static/'.length));
        } catch (e) {
          return null;
        }
        const file = safeJoin(this.staticDir, rel);
        if (!file || !isFile(file)) return null;
        return { kind: 'static', file, type: contentType(file), immutable: HASHED_RE.test(rel) };
      }
      return null;
    }

    const entry = this.external[url.href];
    if (!entry) return null;
    const file = safeJoin(this.externalDir, entry.file);
    if (!file || !isFile(file)) return null;
    return { kind: 'external', file, type: entry.type || contentType(file), immutable: true };
  }
}

module.exports = {
  DESKTOP_PREFIX,
  LAUNCH_PATH,
  PAGE_FILES,
  HASHED_RE,
  contentType,
  safeJoin,
  LocalResources,
};

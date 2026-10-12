'use strict';
const test = require('node:test');
const assert = require('node:assert/strict');
const fs = require('fs');
const os = require('os');
const path = require('path');

const { LocalResources, HASHED_RE, safeJoin, LAUNCH_PATH } = require('../../src/resources');

const ORIGIN = 'https://gerlyn.up.railway.app';

function fixture() {
  const root = fs.mkdtempSync(path.join(os.tmpdir(), 'gerlyn-res-'));
  const put = (rel, text = 'x') => {
    const f = path.join(root, rel);
    fs.mkdirSync(path.dirname(f), { recursive: true });
    fs.writeFileSync(f, text);
  };
  put('bundled/static/css/gerlyn.css', 'body{}');
  put('bundled/static/css/gerlyn.0123456789ab.css', 'body{}');
  put('bundled/static/img/a b.png', 'png');
  put('bundled/external/unpkg/lucide.js', 'lucide');
  fs.writeFileSync(path.join(root, 'bundled/external/manifest.json'), JSON.stringify({
    files: { 'https://unpkg.com/lucide@1.31.0/dist/umd/lucide.js': { file: 'unpkg/lucide.js', type: 'text/javascript' } },
  }));
  put('secret.txt', 'top secret');
  put('pages/launch.html', '<html>');
  return {
    root,
    res: new LocalResources({ bundleDir: path.join(root, 'bundled'), pagesDir: path.join(root, 'pages') }),
  };
}

const u = (s) => new URL(s, ORIGIN);

test('static files resolve, with type and hashed/unhashed distinction', () => {
  const { res } = fixture();
  const plain = res.lookup(u('/static/css/gerlyn.css'), ORIGIN);
  assert.equal(plain.kind, 'static');
  assert.equal(plain.type, 'text/css; charset=utf-8');
  assert.equal(plain.immutable, false);
  assert.equal(res.lookup(u('/static/css/gerlyn.0123456789ab.css'), ORIGIN).immutable, true);
});

test('encoded names are decoded before lookup', () => {
  const { res } = fixture();
  assert.equal(res.lookup(u('/static/img/a%20b.png'), ORIGIN).kind, 'static');
});

test('path traversal cannot leave the bundle', () => {
  const { res } = fixture();
  for (const p of ['/static/../secret.txt', '/static/%2e%2e/secret.txt', '/static/..%2f..%2fsecret.txt',
    '/static/css/..%5c..%5c..%5csecret.txt', '/static/%00', '/static//etc/passwd']) {
    assert.equal(res.lookup(u(p), ORIGIN), null, p);
  }
  assert.equal(safeJoin('/a/b', '../c'), null);
  assert.equal(safeJoin('/a/b', 'c:/windows'), null);
  assert.equal(safeJoin('/a/b', 'x\\..\\y'), null);
});

test('unknown static file and non-static paths are not local', () => {
  const { res } = fixture();
  assert.equal(res.lookup(u('/static/css/missing.css'), ORIGIN), null);
  assert.equal(res.lookup(u('/inventory/'), ORIGIN), null);
  assert.equal(res.lookup(u('/api/products/'), ORIGIN), null);
  assert.equal(res.lookup(u('/offline/sync/'), ORIGIN), null);
});

test('another origin is never served from /static', () => {
  const { res } = fixture();
  assert.equal(res.lookup(new URL('https://evil.example.com/static/css/gerlyn.css'), ORIGIN), null);
});

test('mirrored external files are matched by exact URL only', () => {
  const { res } = fixture();
  const hit = res.lookup(new URL('https://unpkg.com/lucide@1.31.0/dist/umd/lucide.js'), ORIGIN);
  assert.equal(hit.kind, 'external');
  assert.equal(hit.immutable, true);
  assert.equal(res.lookup(new URL('https://unpkg.com/lucide@9.9.9/dist/umd/lucide.js'), ORIGIN), null);
  assert.equal(res.lookup(new URL('https://unpkg.com/lucide@1.31.0/dist/umd/lucide.js?x=1'), ORIGIN), null);
});

test('launch page: only listed files, only on the pinned origin', () => {
  const { res } = fixture();
  assert.equal(res.lookup(u(LAUNCH_PATH), ORIGIN).kind, 'page');
  assert.equal(res.lookup(u('/__gerlyn_desktop__/../secret.txt'), ORIGIN), null);
  assert.equal(res.lookup(u('/__gerlyn_desktop__/app.js'), ORIGIN), null);
  assert.equal(res.lookup(new URL(`https://evil.example.com${LAUNCH_PATH}`), ORIGIN), null);
});

test('hashed-name detection matches WhiteNoise names only', () => {
  assert.equal(HASHED_RE.test('css/gerlyn.0123456789ab.css'), true);
  assert.equal(HASHED_RE.test('css/gerlyn.css'), false);
  assert.equal(HASHED_RE.test('js/jquery.min.js'), false);
  assert.equal(HASHED_RE.test('img/logo.deadbeef.png'), false); // 8 hex is not 12
});

test('a missing bundle is reported, not fatal', () => {
  const res = new LocalResources({ bundleDir: path.join(os.tmpdir(), 'nope-' + Date.now()), pagesDir: os.tmpdir() });
  assert.equal(res.hasStaticBundle(), false);
  assert.equal(res.lookup(u('/static/css/gerlyn.css'), ORIGIN), null);
});

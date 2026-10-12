'use strict';
const test = require('node:test');
const assert = require('node:assert/strict');
const fs = require('fs');
const os = require('os');
const path = require('path');

const config = require('../../src/config');

function tmp() {
  return fs.mkdtempSync(path.join(os.tmpdir(), 'gerlyn-cfg-'));
}

test('normalizeServerUrl: bare host becomes https origin', () => {
  const r = config.normalizeServerUrl('gerlyn.up.railway.app');
  assert.deepEqual(r, { ok: true, origin: 'https://gerlyn.up.railway.app', insecure: false });
});

test('normalizeServerUrl: path, query and fragment are dropped', () => {
  const r = config.normalizeServerUrl('https://gerlyn.example.com/some/where?x=1#y');
  assert.equal(r.origin, 'https://gerlyn.example.com');
});

test('normalizeServerUrl: rejects empty, junk and other schemes', () => {
  for (const bad of ['', '   ', 'ftp://example.com', 'javascript:alert(1)', 'file:///c:/x', 'http://']) {
    assert.equal(config.normalizeServerUrl(bad).ok, false, `should reject ${JSON.stringify(bad)}`);
  }
});

test('normalizeServerUrl: credentials in the address are refused', () => {
  assert.equal(config.normalizeServerUrl('https://user:pw@example.com').ok, false);
});

test('normalizeServerUrl: http only for loopback / private networks', () => {
  for (const ok of ['http://localhost:8000', 'http://127.0.0.1:8000', 'http://192.168.1.50',
    'http://10.0.0.7:8080', 'http://172.16.4.2', 'http://store-pc.local']) {
    const r = config.normalizeServerUrl(ok);
    assert.equal(r.ok, true, ok);
    assert.equal(r.insecure, true);
  }
  for (const bad of ['http://example.com', 'http://8.8.8.8', 'http://172.32.0.1', 'http://192.169.1.1']) {
    assert.equal(config.normalizeServerUrl(bad).ok, false, bad);
  }
});

test('resolveServer: env beats settings beats build config', () => {
  const userData = tmp();
  const resources = tmp();
  fs.writeFileSync(path.join(resources, 'app-config.json'), JSON.stringify({ serverUrl: 'https://build.example.com' }));

  let r = config.resolveServer({ env: {}, userDataDir: userData, resourcesDir: resources });
  assert.deepEqual(r, { origin: 'https://build.example.com', source: 'build', locked: false });

  config.writeSettings(userData, { serverUrl: 'https://settings.example.com' });
  r = config.resolveServer({ env: {}, userDataDir: userData, resourcesDir: resources });
  assert.deepEqual(r, { origin: 'https://settings.example.com', source: 'settings', locked: false });

  r = config.resolveServer({ env: { GERLYN_SERVER_URL: 'https://env.example.com' }, userDataDir: userData, resourcesDir: resources });
  assert.deepEqual(r, { origin: 'https://env.example.com', source: 'env', locked: true });
});

test('resolveServer: an unusable value falls through instead of winning', () => {
  const userData = tmp();
  config.writeSettings(userData, { serverUrl: 'https://ok.example.com' });
  const r = config.resolveServer({ env: { GERLYN_SERVER_URL: 'ftp://nope' }, userDataDir: userData, resourcesDir: tmp() });
  assert.equal(r.origin, 'https://ok.example.com');
});

test('resolveServer: nothing configured gives null (first-run setup)', () => {
  assert.equal(config.resolveServer({ env: {}, userDataDir: tmp(), resourcesDir: tmp() }), null);
});

test('writeSettings merges and leaves no temp file behind', () => {
  const dir = tmp();
  config.writeSettings(dir, { serverUrl: 'https://a.example.com' });
  config.writeSettings(dir, { other: 1 });
  assert.deepEqual(config.readSettings(dir), { serverUrl: 'https://a.example.com', other: 1 });
  assert.deepEqual(fs.readdirSync(dir), ['settings.json']);
});

test('readSettings tolerates a corrupt file', () => {
  const dir = tmp();
  fs.writeFileSync(path.join(dir, 'settings.json'), '{not json');
  assert.deepEqual(config.readSettings(dir), {});
});

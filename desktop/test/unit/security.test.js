'use strict';
const test = require('node:test');
const assert = require('node:assert/strict');

const security = require('../../src/security');

const ORIGIN = 'https://gerlyn.up.railway.app';

test('navigation stays on the pinned origin', () => {
  assert.equal(security.allowNavigation(`${ORIGIN}/inventory/`, ORIGIN), true);
  assert.equal(security.allowNavigation('https://evil.example.com/', ORIGIN), false);
  // look-alikes: userinfo trick, subdomain, other scheme/port
  assert.equal(security.allowNavigation(`https://gerlyn.up.railway.app@evil.example.com/`, ORIGIN), false);
  assert.equal(security.allowNavigation('https://gerlyn.up.railway.app.evil.example.com/', ORIGIN), false);
  assert.equal(security.allowNavigation('http://gerlyn.up.railway.app/', ORIGIN), false);
  assert.equal(security.allowNavigation('https://gerlyn.up.railway.app:8443/', ORIGIN), false);
  assert.equal(security.allowNavigation('file:///c:/windows/win.ini', ORIGIN), false);
  assert.equal(security.allowNavigation('not a url', ORIGIN), false);
});

test('only web links may be handed to the operating system', () => {
  assert.equal(security.allowExternal('https://www.facebook.com/x'), true);
  assert.equal(security.allowExternal('http://example.com'), true);
  for (const bad of ['file:///c:/x.exe', 'javascript:alert(1)', 'ms-msdt:/id', 'smb://host/share',
    'https://user:pw@example.com', 'data:text/html,hi', '']) {
    assert.equal(security.allowExternal(bad), false, bad);
  }
});

test('window.open: same origin allowed, web links external, the rest denied', () => {
  assert.equal(security.windowOpenAction(`${ORIGIN}/sales/1/print/`, ORIGIN), 'allow');
  assert.equal(security.windowOpenAction('https://example.com', ORIGIN), 'external');
  assert.equal(security.windowOpenAction('file:///c:/x', ORIGIN), 'deny');
  assert.equal(security.windowOpenAction('about:blank', ORIGIN), 'deny');
});

test('permissions: allow-listed and only for the pinned origin', () => {
  assert.equal(security.allowPermission('media', `${ORIGIN}/sales/add/`, ORIGIN), true);
  assert.equal(security.allowPermission('media', 'https://evil.example.com/', ORIGIN), false);
  for (const p of ['geolocation', 'midi', 'hid', 'usb', 'serial', 'openExternal', 'pointerLock']) {
    assert.equal(security.allowPermission(p, `${ORIGIN}/`, ORIGIN), false, p);
  }
});

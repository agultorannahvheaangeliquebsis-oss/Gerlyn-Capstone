/* Offline launch page for the GerlynPLUS desktop app.
 *
 * Shown when the server cannot be reached at start-up or when a page that was
 * not saved on this computer is opened offline. It does three things:
 *   - keeps checking the server and returns to where you were as soon as it
 *     answers;
 *   - links to the pages the service worker has saved (Inventory, Sales,
 *     Procurement);
 *   - (via offline-sync.js, loaded by launch.html) lists the changes waiting
 *     to sync, so a restart never makes saved work invisible.
 *
 * It decides nothing about data. Whether a change is saved is the server's
 * answer to the offline queue, not anything on this page.
 */
(function () {
  'use strict';

  var POLL_MS = 5000;
  var params = new URLSearchParams(window.location.search);

  // Only ever return to a path on this same origin, never an arbitrary URL.
  function safeTarget() {
    var raw = params.get('from') || '/';
    try {
      var u = new URL(raw, window.location.origin);
      if (u.origin !== window.location.origin) return '/';
      if (u.pathname.indexOf('/__gerlyn_desktop__/') === 0) return '/';
      return u.pathname + u.search + u.hash;
    } catch (e) {
      return '/';
    }
  }

  var target = safeTarget();
  var detail = params.get('detail') || '';
  var $ = function (id) { return document.getElementById(id); };
  var certProblem = /CERT|SSL|TLS/i.test(detail);

  function setStatus(text, state) {
    $('launch-status').textContent = text;
    $('launch-status').parentNode.setAttribute('data-state', state || 'waiting');
  }

  // The secure connection could not be verified. Never keep retrying quietly
  // or offer a way round it: that is the one failure to put in front of a person.
  if (certProblem) {
    $('launch-title').textContent = 'The secure connection could not be verified';
    $('launch-reason').textContent = 'GerlynPLUS refused to connect because the server\'s '
      + 'security certificate was not accepted. Do not enter any information. '
      + 'Contact whoever manages your GerlynPLUS server.';
    setStatus('Not connected', 'waiting');
    $('launch-retry').textContent = 'Try again';
  }

  if (target !== '/') {
    $('launch-note').textContent = 'Could not open: ' + target;
  }
  $('launch-saved').hidden = false;

  var checking = false;
  function check(manual) {
    if (checking) return;
    checking = true;
    if (manual) setStatus('Checking the connection…', 'waiting');
    fetch('/offline/ping/?t=' + Date.now(), {
      cache: 'no-store', credentials: 'same-origin', headers: { Accept: 'application/json' }
    }).then(function (res) {
      if (!res.ok) throw new Error('ping ' + res.status);
      return res.json();
    }).then(function (data) {
      if (!data || data.ok !== true) throw new Error('bad ping');
      setStatus('Connected — opening GerlynPLUS…', 'online');
      window.location.replace(target);
    }).catch(function () {
      setStatus(certProblem
        ? 'Not connected'
        : 'Still offline. Trying again every few seconds…', 'waiting');
    }).then(function () { checking = false; });
  }

  $('launch-retry').addEventListener('click', function () { check(true); });
  window.addEventListener('online', function () { check(false); });
  if (!certProblem) {
    setInterval(function () { check(false); }, POLL_MS);
  }
  check(false);
})();

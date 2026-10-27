/* The delivery-areas strip's "x".
 *
 * The strip is dismissed with a real POST (orders:service_banner_dismiss), so
 * it works without this file and the choice is remembered by the session, not
 * by the browser. This only hides the strip at once and sends the same POST in
 * the background, instead of reloading the page the shopper is reading.
 */
(function () {
  'use strict';

  var form = document.querySelector('[data-service-banner-dismiss]');
  var banner = document.querySelector('[data-service-banner]');
  if (!form || !banner || !window.fetch) return;

  form.addEventListener('submit', function (event) {
    event.preventDefault();
    var body = new FormData(form);
    // Hidden first: a failed request only means it comes back on the next page.
    banner.style.display = 'none';
    fetch(form.action, {
      method: 'POST',
      body: body,
      credentials: 'same-origin',
      headers: { 'X-Requested-With': 'XMLHttpRequest' }
    }).catch(function () { /* the next page load shows it again; nothing to do */ });
  });
})();

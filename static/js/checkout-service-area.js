/* Checkout: province -> municipality -> barangay, and "do we deliver there?".
 *
 * Two jobs.
 *
 * 1. The three lists. The province list is rendered by the server; this fills
 *    the municipality list from the province and the barangay list from the
 *    municipality (the same PSGC endpoints the address picker uses), so the
 *    value posted is a code the branch's serviceable areas can be matched on.
 *    "Other" is always offered; it opens a text box and the words are checked
 *    by name on the server.
 *
 * 2. The warning. Whenever the address in use changes (and a delivery is the
 *    chosen fulfillment) the form asks orders:area_check about it. If the
 *    branch does not deliver there the page says so inline, offers "Switch to
 *    Pickup" and "Choose another branch", and turns the Place Order button off.
 *
 * Both are advice. checkout() runs the same rule again on submit and is what
 * actually refuses an order, so a blocked button here is a courtesy and never
 * the only guard.
 */
(function () {
  'use strict';

  var place = document.getElementById('delivery_place');
  var form = document.getElementById('checkout-form');
  if (!place || !form) return;

  var OTHER = '__other__';
  var provinceSel = document.getElementById('addr_province');
  var citySel = document.getElementById('addr_city');
  var brgySel = document.getElementById('addr_barangay');
  var provinceOther = document.getElementById('addr_province_other');
  var cityOther = document.getElementById('addr_city_other');
  var brgyOther = document.getElementById('addr_barangay_other');
  var fulfillment = document.getElementById('fulfillment_type');
  var submit = document.getElementById('place-order-submit');
  var submitLabel = submit ? submit.querySelector('[data-submit-label]') : null;

  var panel = document.getElementById('area-check');
  var banner = document.getElementById('area-check-banner');
  var others = document.getElementById('area-check-others');
  var actions = document.getElementById('area-check-actions');
  var switchButton = document.getElementById('area-switch-pickup');

  var citiesUrl = place.getAttribute('data-cities-url');
  var barangaysUrl = place.getAttribute('data-barangays-url');
  var checkUrl = place.getAttribute('data-check-url');
  var switchUrl = place.getAttribute('data-switch-url');

  var blocked = false;
  var checkSerial = 0;
  var checkTimer = null;

  // ---------------------------------------------------------------- lists

  function option(value, text, selected) {
    var el = document.createElement('option');
    el.value = value;
    el.textContent = text;
    if (selected) el.selected = true;
    return el;
  }

  function fillSelect(select, placeholder, rows, wanted) {
    select.innerHTML = '';
    select.appendChild(option('', placeholder, !wanted));
    rows.forEach(function (row) {
      select.appendChild(option(row.code, row.name, row.code === wanted));
    });
    select.appendChild(option(OTHER, 'Other…', wanted === OTHER));
  }

  function loadList(url, param, code) {
    if (!code || code === OTHER) return Promise.resolve([]);
    return fetch(url + '?' + param + '=' + encodeURIComponent(code), { credentials: 'same-origin' })
      .then(function (response) { return response.ok ? response.json() : { results: [] }; })
      .then(function (data) { return data.results || []; })
      .catch(function () { return []; });
  }

  function syncOtherBoxes() {
    // Without JavaScript the boxes stay open; here they open only for "Other".
    provinceOther.hidden = provinceSel.value !== OTHER;
    cityOther.hidden = citySel.value !== OTHER;
    brgyOther.hidden = brgySel.value !== OTHER;
  }

  // A newer choice supersedes an older request still in flight: without this,
  // a slow answer for the first province could land after the second one's
  // and leave the list belonging to a province that is no longer selected.
  var listSerial = { city: 0, barangay: 0 };

  function loadCities(provinceCode, wantedCity) {
    var serial = ++listSerial.city;
    if (provinceCode === OTHER || !provinceCode) {
      // No list to offer: only "Other" (typed words) for an unlisted province.
      fillSelect(citySel, 'Select municipality…', [], provinceCode ? OTHER : '');
      return loadBarangays(provinceCode ? OTHER : '', '');
    }
    return loadList(citiesUrl, 'province', provinceCode).then(function (rows) {
      if (serial !== listSerial.city) return null;
      fillSelect(citySel, 'Select municipality…', rows, wantedCity);
      return loadBarangays(citySel.value, '');
    });
  }

  function loadBarangays(cityCode, wantedBarangay) {
    var serial = ++listSerial.barangay;
    if (cityCode === OTHER || !cityCode) {
      fillSelect(brgySel, 'Select barangay…', [], cityCode ? OTHER : '');
      syncOtherBoxes();
      return Promise.resolve();
    }
    return loadList(barangaysUrl, 'city', cityCode).then(function (rows) {
      if (serial !== listSerial.barangay) return;
      fillSelect(brgySel, 'Select barangay…', rows, wantedBarangay);
      syncOtherBoxes();
    });
  }

  provinceSel.addEventListener('change', function () {
    loadCities(provinceSel.value, '').then(function () { syncOtherBoxes(); scheduleCheck(); });
    syncOtherBoxes();
  });
  citySel.addEventListener('change', function () {
    loadBarangays(citySel.value, '').then(scheduleCheck);
    syncOtherBoxes();
  });
  brgySel.addEventListener('change', function () { syncOtherBoxes(); scheduleCheck(); });
  [provinceOther, cityOther, brgyOther].forEach(function (box) {
    box.addEventListener('input', scheduleCheck);
  });

  // ------------------------------------------------------------ the check

  function usingDelivery() {
    return fulfillment && fulfillment.value === 'Delivery';
  }

  function addressSource() {
    var radio = form.querySelector('input[name="address_source"]:checked');
    if (radio) return radio.value;
    var hidden = form.querySelector('input[type="hidden"][name="address_source"]');
    return hidden ? hidden.value : 'typed';
  }

  function params() {
    var q = new URLSearchParams();
    q.set('address_source', addressSource());
    [['addr_province', provinceSel], ['addr_province_other', provinceOther],
     ['addr_city', citySel], ['addr_city_other', cityOther],
     ['addr_barangay', brgySel], ['addr_barangay_other', brgyOther]
    ].forEach(function (pair) { q.set(pair[0], pair[1].value); });
    return q;
  }

  function setBlocked(value) {
    blocked = value;
    if (!submit) return;
    submit.disabled = value;
    if (value) {
      submit.setAttribute('aria-disabled', 'true');
      submit.title = 'We do not deliver to this address. Switch to Pickup or choose another branch.';
    } else {
      submit.removeAttribute('aria-disabled');
      submit.removeAttribute('title');
    }
  }

  function setLabel() {
    var text = 'Place Order';
    if (fulfillment && fulfillment.value === 'Delivery') text = 'Place delivery order';
    if (fulfillment && fulfillment.value === 'Pickup') text = 'Place pickup order';
    if (submit) submit.setAttribute('data-default-label', text);
    if (submitLabel && !(submit && submit.getAttribute('aria-busy'))) submitLabel.textContent = text;
  }

  function show(kind, message, others_text, offerSwitch) {
    panel.hidden = false;
    banner.className = 'banner banner-' + kind;
    banner.textContent = message;
    if (others_text) { others.textContent = others_text; others.hidden = false; } else { others.hidden = true; }
    actions.hidden = !offerSwitch;
  }

  function hidePanel() {
    panel.hidden = true;
    banner.textContent = '';
    others.hidden = true;
    actions.hidden = true;
  }

  function render(data) {
    if (data.status === 'no_branch') { hidePanel(); setBlocked(false); return; }
    if (!data.configured) { hidePanel(); setBlocked(false); return; }
    if (data.ok) {
      var bits = ['We deliver to ' + data.place + ' from ' + data.branch + '.'];
      if (data.delivery_fee !== null) bits.push('Delivery fee ₱' + data.delivery_fee + ', arranged with the branch.');
      if (data.minimum_order !== null) bits.push('Minimum order ₱' + data.minimum_order + '.');
      show('success', bits.join(' '), '', false);
      setBlocked(false);
      return;
    }
    var note = '';
    if (data.other_branches && data.other_branches.length) {
      note = 'These branches deliver there: ' + data.other_branches.join(', ') + '.';
    }
    if (data.status === 'incomplete') {
      // Nothing wrong yet, the shopper just has not finished choosing.
      show('info', data.message, '', false);
    } else {
      show('error', data.message, note, true);
    }
    setBlocked(true);
  }

  function runCheck() {
    if (!usingDelivery()) { hidePanel(); setBlocked(false); return; }
    var serial = ++checkSerial;
    fetch(checkUrl + '?' + params().toString(), {
      credentials: 'same-origin',
      headers: { 'X-Requested-With': 'XMLHttpRequest' }
    })
      .then(function (response) { return response.json(); })
      .then(function (data) { if (serial === checkSerial) render(data); })
      .catch(function () {
        // Cannot reach the check: do not lock the shopper out. The server
        // decides on submit either way.
        if (serial === checkSerial) { hidePanel(); setBlocked(false); }
      });
  }

  function scheduleCheck() {
    clearTimeout(checkTimer);
    checkTimer = setTimeout(runCheck, 250);
  }

  // ------------------------------------------------------------- actions

  function csrfToken() {
    var field = form.querySelector('input[name="csrfmiddlewaretoken"]');
    return field ? field.value : '';
  }

  if (switchButton) {
    switchButton.addEventListener('click', function () {
      var body = params();
      // Tell the server the shopper is stepping away from this address, so the
      // pickup order can be confirmed to them in the app. Best effort: the
      // switch itself is just the choice below.
      fetch(switchUrl, {
        method: 'POST',
        credentials: 'same-origin',
        headers: {
          'X-CSRFToken': csrfToken(),
          'X-Requested-With': 'XMLHttpRequest',
          'Content-Type': 'application/x-www-form-urlencoded'
        },
        body: body.toString()
      }).catch(function () {}).then(function () {
        fulfillment.value = 'Pickup';
        fulfillment.dispatchEvent(new Event('change', { bubbles: true }));
        hidePanel();
        setBlocked(false);
        fulfillment.scrollIntoView({ block: 'center', behavior: 'smooth' });
      });
    });
  }

  fulfillment.addEventListener('change', function () { setLabel(); scheduleCheck(); });
  form.addEventListener('change', function (event) {
    if (event.target && event.target.name === 'address_source') scheduleCheck();
  });

  // Capture-phase and first in line: a blocked form must not reach the
  // double-submit guard, which would grey the button out as if it had sent.
  form.addEventListener('submit', function (event) {
    if (blocked && usingDelivery()) {
      event.preventDefault();
      event.stopImmediatePropagation();
      panel.scrollIntoView({ block: 'center', behavior: 'smooth' });
    }
  }, true);

  // Back/forward restores the page from the bfcache with the double-submit
  // guard re-enabling the button; ask again once that has run.
  window.addEventListener('pageshow', function (event) {
    if (event.persisted) setTimeout(runCheck, 0);
  });

  // The pin-on-map step (checkout-location.js) fills the lists by code.
  window.GerlynCheckoutArea = {
    setPlace: function (codes) {
      provinceSel.value = codes.province || '';
      return loadCities(provinceSel.value, codes.city || '')
        .then(function () {
          if (codes.city && citySel.value !== codes.city) return null;
          return loadBarangays(citySel.value, codes.barangay || '');
        })
        .then(function () { syncOtherBoxes(); scheduleCheck(); });
    }
  };

  // --------------------------------------------------------------- start

  var draftProvince = place.getAttribute('data-draft-province') || '';
  var draftCity = place.getAttribute('data-draft-city') || '';
  var draftBarangay = place.getAttribute('data-draft-barangay') || '';
  syncOtherBoxes();
  loadCities(draftProvince, draftCity).then(function () {
    return loadBarangays(citySel.value, draftBarangay);
  }).then(function () {
    syncOtherBoxes();
    setLabel();
    scheduleCheck();
  });
})();

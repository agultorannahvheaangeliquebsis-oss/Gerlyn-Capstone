// Run with:  node --test orders/js_tests/checkout_location.test.mjs
//
// Runs the real static/js/checkout-location.js against a tiny fake DOM. Not a
// browser: it proves the logic the privacy requirements hang on -- declining
// never asks the browser for a location, every way of not sharing leaves no
// coordinates in the form, and a browser refusal is handled with the same
// friendly message instead of an error. The server side of the same contract
// is orders/tests_delivery_location.py.
import assert from 'node:assert/strict';
import fs from 'node:fs';
import path from 'node:path';
import vm from 'node:vm';
import { describe, it } from 'node:test';
import { fileURLToPath } from 'node:url';

const here = path.dirname(fileURLToPath(import.meta.url));
const SRC = fs.readFileSync(path.join(here, '..', '..', 'static/js/checkout-location.js'), 'utf8');

const DECLINED = 'Location access was declined. You can type your address instead.';

class El {
  constructor(attrs = {}) {
    this.attrs = attrs; this.value = ''; this.textContent = ''; this.disabled = false;
    this.style = {}; this.checked = false; this.listeners = {};
  }
  getAttribute(name) { return name in this.attrs ? this.attrs[name] : null; }
  addEventListener(type, fn) { (this.listeners[type] = this.listeners[type] || []).push(fn); }
  fire(type, event = {}) { (this.listeners[type] || []).forEach((fn) => fn({ target: this, ...event })); }
  click() { this.fire('click'); }
}

// A checkout page: optionally with a saved address (and optionally a pin on it).
function boot({ saved = false, savedPin = false, geolocation = 'allow', areaScript = true } = {}) {
  const ids = {};
  for (const id of [
    'delivery_location_status', 'delivery_latitude', 'delivery_longitude', 'delivery-coords',
    'delivery-location-feedback', 'delivery-use-location', 'delivery-decline-location',
    'delivery-pin-on-map', 'delivery_typed_fields', 'addr_street',
  ]) ids[id] = new El();
  ids['delivery-coords'].textContent = 'none';

  const block = new El({
    'data-has-saved': saved ? '1' : '0',
    'data-saved-pin': savedPin ? '1' : '0',
  });
  ids.delivery_address_field = block;
  const radios = { saved: new El(), typed: new El() };
  radios.saved.checked = saved;
  radios.typed.checked = !saved;
  block.querySelector = (selector) => {
    const m = /value="(saved|typed)"/.exec(selector);
    return saved && m ? radios[m[1]] : null;
  };

  const calls = { geolocation: 0 };
  const navigator = {};
  if (geolocation !== 'none') {
    navigator.geolocation = {
      getCurrentPosition(ok, fail) {
        calls.geolocation += 1;
        if (geolocation === 'allow') ok({ coords: { latitude: 14.951266, longitude: 120.900549 } });
        else fail({ code: geolocation === 'timeout' ? 3 : 1 });
      },
    };
  }
  const window = { AddressPicker: { open: (options) => { calls.picker = options; } } };
  // checkout-service-area.js owns the province / municipality / barangay lists
  // and exposes this for the picker's result.
  if (areaScript) window.GerlynCheckoutArea = { setPlace: (codes) => { calls.place = codes; } };
  const document = { getElementById: (id) => ids[id] || null };
  vm.runInNewContext(SRC, { document, navigator, window });
  return { ids, block, radios, calls };
}

const useLocation = (page) => page.ids['delivery-use-location'].click();
const decline = (page) => page.ids['delivery-decline-location'].click();
const field = (page, id) => page.ids[id].value;

describe('page load', () => {
  it('never asks the browser for a location by itself', () => {
    const page = boot();
    assert.equal(page.calls.geolocation, 0);
    assert.equal(field(page, 'delivery_location_status'), '');
    assert.equal(page.ids['delivery-coords'].textContent, 'none');
  });
});

describe("Don't share my location", () => {
  it('never calls the browser and stores no coordinates', () => {
    const page = boot();
    decline(page);

    assert.equal(page.calls.geolocation, 0);
    assert.equal(field(page, 'delivery_location_status'), 'declined');
    assert.equal(field(page, 'delivery_latitude'), '');
    assert.equal(field(page, 'delivery_longitude'), '');
    assert.equal(page.ids['delivery-coords'].textContent, 'none');
    assert.match(page.ids['delivery-location-feedback'].textContent, /Location not shared/);
  });

  it('clears a pin that was captured earlier for this order', () => {
    const page = boot();
    useLocation(page);
    assert.equal(field(page, 'delivery_location_status'), 'granted');
    assert.notEqual(field(page, 'delivery_latitude'), '');

    decline(page);

    assert.equal(field(page, 'delivery_location_status'), 'declined');
    assert.equal(field(page, 'delivery_latitude'), '');
    assert.equal(field(page, 'delivery_longitude'), '');
    assert.equal(page.ids['delivery-coords'].textContent, 'none');
    assert.equal(page.calls.geolocation, 1, 'only the earlier, explicit request');
  });

  it('also withdraws the pin saved on the address for this order', () => {
    const page = boot({ saved: true, savedPin: true });
    assert.match(page.ids['delivery-coords'].textContent, /saved on your address/);

    decline(page);

    assert.equal(page.ids['delivery-coords'].textContent, 'none');
  });
});

describe('Use my current location', () => {
  it('keeps the pin, rounded for display, only after the shopper asks', () => {
    const page = boot();
    useLocation(page);

    assert.equal(page.calls.geolocation, 1);
    assert.equal(field(page, 'delivery_location_status'), 'granted');
    assert.equal(field(page, 'delivery_latitude'), '14.951266');
    assert.equal(field(page, 'delivery_longitude'), '120.900549');
    assert.equal(page.ids['delivery-coords'].textContent, '14.9513, 120.9005 (this order only)');
  });

  for (const mode of ['deny', 'timeout']) {
    it(`handles the browser saying no (${mode}) with a friendly message and no pin`, () => {
      const page = boot({ geolocation: mode });
      useLocation(page);

      assert.equal(field(page, 'delivery_location_status'), 'denied');
      assert.equal(field(page, 'delivery_latitude'), '');
      assert.equal(field(page, 'delivery_longitude'), '');
      assert.equal(page.ids['delivery-location-feedback'].textContent, DECLINED);
      assert.equal(page.ids['delivery-use-location'].disabled, false, 'the shopper can try again');
    });
  }

  it('handles a browser with no geolocation at all the same way', () => {
    const page = boot({ geolocation: 'none' });
    assert.doesNotThrow(() => useLocation(page));

    assert.equal(field(page, 'delivery_location_status'), 'denied');
    assert.equal(page.ids['delivery-location-feedback'].textContent, DECLINED);
  });

  it('a denial after an earlier pin leaves no coordinates behind', () => {
    const page = boot({ geolocation: 'deny' });
    // A pin already sitting in the form from an earlier, successful request.
    page.ids.delivery_latitude.value = '14.9';
    page.ids.delivery_longitude.value = '120.9';

    useLocation(page);

    assert.equal(field(page, 'delivery_latitude'), '');
    assert.equal(field(page, 'delivery_longitude'), '');
  });
});

describe('typed address', () => {
  it('is always shown when there is no saved address', () => {
    const page = boot({ saved: false });
    assert.equal(page.ids.delivery_typed_fields.style.display, '');
  });

  it('stays hidden behind the saved address until a different one is chosen', () => {
    const page = boot({ saved: true });
    assert.equal(page.ids.delivery_typed_fields.style.display, 'none');

    page.radios.saved.checked = false;
    page.radios.typed.checked = true;
    page.block.fire('change', { target: { name: 'address_source' } });

    assert.equal(page.ids.delivery_typed_fields.style.display, '');
  });

  it('a refusal of location does not force a saved-address shopper onto the typed form', () => {
    const page = boot({ saved: true, geolocation: 'deny' });
    useLocation(page);

    assert.equal(page.ids.delivery_typed_fields.style.display, 'none');
  });
});

describe('Pin on map', () => {
  it('fills the street, selects the place by code and shares the pin only if one was placed', () => {
    const page = boot({ saved: true });
    page.ids['delivery-pin-on-map'].click();
    assert.ok(page.calls.picker, 'the shared picker opened');

    page.calls.picker.onConfirm(
      {
        street_detail: '14 Rizal St.', latitude: '14.951266', longitude: '120.900549',
        province: '0314', city: '031403', barangay: '031403014',
      },
      { barangay: 'Poblacion', city: 'Baliuag' },
    );

    assert.equal(page.ids.addr_street.value, '14 Rizal St.');
    // The province / municipality / barangay lists take PSGC codes, so the
    // delivery-area check sees a place it can match, not words.
    // (JSON round trip: the object comes from the vm sandbox's own realm.)
    assert.deepEqual(
      JSON.parse(JSON.stringify(page.calls.place)),
      { province: '0314', city: '031403', barangay: '031403014' },
    );
    assert.equal(field(page, 'delivery_location_status'), 'granted');
    assert.equal(page.radios.typed.checked, true);
  });

  it('still works if the place lists are not on the page', () => {
    const page = boot({ saved: true, areaScript: false });
    page.ids['delivery-pin-on-map'].click();

    page.calls.picker.onConfirm(
      { street_detail: '14 Rizal St.', latitude: '14.951266', longitude: '120.900549', province: '0314' },
      {},
    );

    assert.equal(field(page, 'delivery_location_status'), 'granted');
  });

  it('is skippable: confirming without a pin shares nothing', () => {
    const page = boot();
    page.ids['delivery-pin-on-map'].click();

    page.calls.picker.onConfirm({ street_detail: '14 Rizal St.', latitude: '', longitude: '' }, {});

    assert.equal(field(page, 'delivery_location_status'), '');
    assert.equal(field(page, 'delivery_latitude'), '');
  });
});

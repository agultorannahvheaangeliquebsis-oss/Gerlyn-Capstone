/* Checkout: the optional location pin for a delivery.
 *
 * Location is never required here and can never block the order. Three things
 * can happen, and the form carries the result in location_status:
 *
 *   granted   the shopper shared a pin (GPS, or placed on the map)
 *   declined  the shopper pressed "Don't share my location". The browser is
 *             never asked, and any pin already held for this order is cleared.
 *   denied    the browser said no: permission refused, timed out, or no
 *             geolocation at all. Same friendly message, same fallback.
 *
 * Only `granted` sends coordinates; the server ignores them otherwise and
 * re-validates everything (orders.delivery). Geolocation is requested only on
 * the click of "Use my current location", never on page load.
 */
(function () {
  'use strict';

  var block = document.getElementById('delivery_address_field');
  if (!block) return;

  var status = document.getElementById('delivery_location_status');
  var latInput = document.getElementById('delivery_latitude');
  var lngInput = document.getElementById('delivery_longitude');
  var coordsLabel = document.getElementById('delivery-coords');
  var feedback = document.getElementById('delivery-location-feedback');
  var useButton = document.getElementById('delivery-use-location');
  var declineButton = document.getElementById('delivery-decline-location');
  var mapButton = document.getElementById('delivery-pin-on-map');
  var typedFields = document.getElementById('delivery_typed_fields');
  var hasSaved = block.getAttribute('data-has-saved') === '1';
  var savedHasPin = block.getAttribute('data-saved-pin') === '1';

  var DECLINED_MESSAGE = 'Location access was declined. You can type your address instead.';

  function say(message) {
    if (feedback) feedback.textContent = message;
  }

  function usingTypedAddress() {
    if (!hasSaved) return true;
    var typed = block.querySelector('input[name="address_source"][value="typed"]');
    return !!(typed && typed.checked);
  }

  // Shows what would travel with the order right now, so "nothing is stored"
  // is something the shopper can read rather than take on trust.
  function renderCoordinates() {
    var text = 'none';
    if (latInput.value && lngInput.value) {
      text = Number(latInput.value).toFixed(4) + ', ' + Number(lngInput.value).toFixed(4) + ' (this order only)';
    } else if (savedHasPin && !usingTypedAddress() && status.value !== 'declined') {
      text = 'the pin saved on your address (this order only)';
    }
    coordsLabel.textContent = text;
  }

  function clearPin() {
    latInput.value = '';
    lngInput.value = '';
  }

  // A shopper with a saved address keeps using it, so the typed fields stay
  // out of the way until "Use a different address" is picked. A shopper with
  // no saved address always sees them. Without JavaScript they are always
  // shown, and the server only reads them when the typed address is chosen.
  function syncTypedFields() {
    if (typedFields) typedFields.style.display = usingTypedAddress() ? '' : 'none';
  }

  function showTypedAddress() {
    // Offered after a refusal of location, but never forced on a shopper who
    // has a saved address: that address is already a typed one.
    syncTypedFields();
  }

  function sharePin(lat, lng) {
    latInput.value = Number(lat).toFixed(6);
    lngInput.value = Number(lng).toFixed(6);
    status.value = 'granted';
    renderCoordinates();
  }

  function denied() {
    clearPin();
    status.value = 'denied';
    say(DECLINED_MESSAGE);
    showTypedAddress();
    renderCoordinates();
    if (useButton) useButton.disabled = false;
  }

  if (useButton) {
    useButton.addEventListener('click', function () {
      if (!navigator.geolocation) {
        denied();
        return;
      }
      useButton.disabled = true;
      say('Waiting for location permission…');
      navigator.geolocation.getCurrentPosition(function (position) {
        sharePin(position.coords.latitude, position.coords.longitude);
        say('Location found. It goes to the branch with this delivery only. Your address still goes on the order.');
        useButton.disabled = false;
      }, denied, {
        enableHighAccuracy: false,
        timeout: 10000,
        maximumAge: 300000
      });
    });
  }

  if (declineButton) {
    declineButton.addEventListener('click', function () {
      // No geolocation call, and no coordinates left behind from an earlier
      // attempt or from the map.
      clearPin();
      status.value = 'declined';
      say('Location not shared. Nothing was stored. We will deliver to your address.');
      showTypedAddress();
      renderCoordinates();
    });
  }

  // Optional map pin, via the shared address picker. Whatever the shopper
  // confirms there fills the typed fields and, if they placed a pin, shares it.
  if (mapButton) {
    mapButton.addEventListener('click', function () {
      if (!window.AddressPicker) return;
      var street = document.getElementById('addr_street');
      var barangay = document.getElementById('addr_barangay');
      var city = document.getElementById('addr_city');
      window.AddressPicker.open({
        title: 'Pin your delivery address',
        showType: false,
        initial: { street_detail: street ? street.value : '', text: '' },
        onConfirm: function (values, labels) {
          var typed = block.querySelector('input[name="address_source"][value="typed"]');
          if (typed) typed.checked = true;
          if (street && values.street_detail) street.value = values.street_detail;
          if (barangay && labels && labels.barangay) barangay.value = labels.barangay;
          if (city && labels && labels.city) city.value = labels.city;
          if (values.latitude && values.longitude) {
            sharePin(values.latitude, values.longitude);
            say('Pin placed for this delivery only. You can still change the address above.');
          } else {
            renderCoordinates();
          }
        }
      });
    });
  }

  block.addEventListener('change', function (event) {
    if (event.target && event.target.name === 'address_source') {
      syncTypedFields();
      renderCoordinates();
    }
  });

  syncTypedFields();
  renderCoordinates();
})();

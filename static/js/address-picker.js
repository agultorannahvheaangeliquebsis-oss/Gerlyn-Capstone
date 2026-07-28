/* Shared map-based address picker: cascading Region/Province/City/Barangay
 * selects, a debounced street search (proxied to Nominatim server-side),
 * and a Leaflet map with a draggable pin. One modal instance per page,
 * re-targeted per use via AddressPicker.open({ formAction, title, initial }).
 *
 * Dragging/tapping the pin reverse-geocodes into the "Full Address" text
 * field, but that field stays a normal editable textarea — whatever text
 * is in it at submit time is what gets saved as the address, never raw
 * coordinates. Lat/lng travel alongside purely for the map thumbnail and
 * delivery accuracy.
 */
window.AddressPicker = (function () {
  var urls = window.ADDRESS_PICKER_URLS || {};
  var DEFAULT_CENTER = [14.9557, 120.8994]; // Baliuag, Bulacan — sensible default before a pin is set

  var map = null;
  var marker = null;
  var els = {};
  var searchDebounceTimer = null;
  var currentOptions = {};

  function cacheEls() {
    if (els.modal) return;
    els.modal = document.getElementById('address-picker-modal');
    els.title = document.getElementById('address-picker-title');
    els.form = document.getElementById('address-picker-form');
    els.typeInput = document.getElementById('ap-type-input');
    els.typeButtons = els.modal.querySelectorAll('.type-pill');
    els.region = document.getElementById('ap-region');
    els.province = document.getElementById('ap-province');
    els.city = document.getElementById('ap-city');
    els.barangay = document.getElementById('ap-barangay');
    els.street = document.getElementById('ap-street-input');
    els.suggestions = document.getElementById('ap-suggestions');
    els.lat = document.getElementById('ap-latitude-input');
    els.lng = document.getElementById('ap-longitude-input');
    els.text = document.getElementById('ap-text-input');
    els.mapDiv = document.getElementById('ap-map');

    els.region.addEventListener('change', onRegionChange);
    els.province.addEventListener('change', onProvinceChange);
    els.city.addEventListener('change', onCityChange);
    els.barangay.addEventListener('change', onBarangayChange);
    els.street.addEventListener('input', onStreetInput);

    document.addEventListener('click', function (e) {
      if (!els.suggestions.hidden && e.target !== els.street && !els.suggestions.contains(e.target)) {
        hideSuggestions();
      }
    });
    els.modal.addEventListener('click', function (e) {
      if (e.target === els.modal) close();
    });
  }

  function fetchJSON(url, params) {
    var qs = params ? '?' + new URLSearchParams(params).toString() : '';
    return fetch(url + qs).then(function (res) { return res.json(); });
  }

  function fillSelect(selectEl, results, placeholder) {
    var html = '<option value="">' + placeholder + '</option>';
    for (var i = 0; i < results.length; i++) {
      html += '<option value="' + results[i].code + '">' + escapeHtml(results[i].name) + '</option>';
    }
    selectEl.innerHTML = html;
    selectEl.disabled = results.length === 0;
  }

  function resetSelect(selectEl, placeholder) {
    selectEl.innerHTML = '<option value="">' + placeholder + '</option>';
    selectEl.disabled = true;
  }

  function escapeHtml(s) {
    var div = document.createElement('div');
    div.textContent = s == null ? '' : String(s);
    return div.innerHTML;
  }

  function onRegionChange() {
    resetSelect(els.province, 'Select province…');
    resetSelect(els.city, 'Select city/municipality…');
    resetSelect(els.barangay, 'Select barangay…');
    els.street.disabled = true;
    if (!els.region.value) return;
    fetchJSON(urls.provinces, { region: els.region.value }).then(function (data) {
      fillSelect(els.province, data.results, 'Select province…');
    });
  }

  function onProvinceChange() {
    resetSelect(els.city, 'Select city/municipality…');
    resetSelect(els.barangay, 'Select barangay…');
    els.street.disabled = true;
    if (!els.province.value) return;
    fetchJSON(urls.cities, { province: els.province.value }).then(function (data) {
      fillSelect(els.city, data.results, 'Select city/municipality…');
    });
  }

  function onCityChange() {
    resetSelect(els.barangay, 'Select barangay…');
    els.street.disabled = true;
    if (!els.city.value) return;
    fetchJSON(urls.barangays, { city: els.city.value }).then(function (data) {
      fillSelect(els.barangay, data.results, 'Select barangay…');
    });
  }

  function onBarangayChange() {
    els.street.disabled = !els.barangay.value;
    if (els.barangay.value) els.street.focus();
  }

  function localityLabel() {
    var parts = [];
    [els.barangay, els.city, els.province].forEach(function (sel) {
      if (sel.value && sel.selectedOptions[0]) parts.push(sel.selectedOptions[0].textContent);
    });
    return parts.join(', ');
  }

  function onStreetInput() {
    clearTimeout(searchDebounceTimer);
    var q = els.street.value.trim();
    if (q.length < 3) { hideSuggestions(); return; }
    searchDebounceTimer = setTimeout(function () { doSearch(q); }, 450);
  }

  function doSearch(q) {
    fetchJSON(urls.search, { q: q, locality: localityLabel() }).then(function (data) {
      renderSuggestions(data.results || []);
    });
  }

  function renderSuggestions(results) {
    if (!results.length) { hideSuggestions(); return; }
    var html = '';
    for (var i = 0; i < results.length; i++) {
      html += '<div class="search-result" data-idx="' + i + '"><span class="search-result-name">' + escapeHtml(results[i].label) + '</span></div>';
    }
    els.suggestions.innerHTML = html;
    els.suggestions.hidden = false;
    var rows = els.suggestions.children;
    for (var j = 0; j < rows.length; j++) {
      (function (result) {
        rows[j].addEventListener('click', function () {
          placeMarker(parseFloat(result.lat), parseFloat(result.lon), false);
          els.text.value = result.label;
          hideSuggestions();
        });
      })(results[j]);
    }
  }

  function hideSuggestions() {
    els.suggestions.hidden = true;
    els.suggestions.innerHTML = '';
  }

  function ensureMap() {
    if (map) return;
    map = L.map(els.mapDiv).setView(DEFAULT_CENTER, 14);
    L.tileLayer('https://{s}.tile.openstreetmap.org/{z}/{x}/{y}.png', {
      maxZoom: 19,
      attribution: '&copy; <a href="https://www.openstreetmap.org/copyright">OpenStreetMap</a> contributors'
    }).addTo(map);
    map.on('click', function (e) { placeMarker(e.latlng.lat, e.latlng.lng, true); });
  }

  function placeMarker(lat, lng, reverseGeocode) {
    if (!marker) {
      marker = L.marker([lat, lng], { draggable: true }).addTo(map);
      marker.on('dragend', function () {
        var pos = marker.getLatLng();
        applyLatLng(pos.lat, pos.lng);
        reverseGeocodeTo(pos.lat, pos.lng);
      });
    } else {
      marker.setLatLng([lat, lng]);
    }
    map.setView([lat, lng], 17);
    applyLatLng(lat, lng);
    if (reverseGeocode) reverseGeocodeTo(lat, lng);
  }

  function applyLatLng(lat, lng) {
    els.lat.value = lat.toFixed(6);
    els.lng.value = lng.toFixed(6);
  }

  function reverseGeocodeTo(lat, lng) {
    fetchJSON(urls.reverse, { lat: lat, lon: lng }).then(function (data) {
      if (data.label) els.text.value = data.label;
    });
  }

  function selectType(btn) {
    for (var i = 0; i < els.typeButtons.length; i++) els.typeButtons[i].classList.remove('is-selected');
    btn.classList.add('is-selected');
    els.typeInput.value = btn.dataset.type;
  }

  function selectCascade(initial) {
    if (!initial.region) return Promise.resolve();
    return fetchJSON(urls.regions).then(function (data) {
      fillSelect(els.region, data.results, 'Select region…');
      els.region.value = initial.region;
      if (!initial.province) return;
      return fetchJSON(urls.provinces, { region: initial.region }).then(function (d) {
        fillSelect(els.province, d.results, 'Select province…');
        els.province.value = initial.province;
        if (!initial.city) return;
        return fetchJSON(urls.cities, { province: initial.province }).then(function (d2) {
          fillSelect(els.city, d2.results, 'Select city/municipality…');
          els.city.value = initial.city;
          if (!initial.barangay) return;
          return fetchJSON(urls.barangays, { city: initial.city }).then(function (d3) {
            fillSelect(els.barangay, d3.results, 'Select barangay…');
            els.barangay.value = initial.barangay;
            els.street.disabled = false;
          });
        });
      });
    });
  }

  function resetForm() {
    els.form.reset();
    resetSelect(els.province, 'Select province…');
    resetSelect(els.city, 'Select city/municipality…');
    resetSelect(els.barangay, 'Select barangay…');
    els.street.disabled = true;
    els.street.value = '';
    els.text.value = '';
    els.lat.value = '';
    els.lng.value = '';
    for (var i = 0; i < els.typeButtons.length; i++) {
      els.typeButtons[i].classList.toggle('is-selected', els.typeButtons[i].dataset.type === 'HOME');
    }
    els.typeInput.value = 'HOME';
    hideSuggestions();
    if (marker) { map.removeLayer(marker); marker = null; }
    if (map) map.setView(DEFAULT_CENTER, 14);
  }

  function collectValues() {
    return {
      type: els.typeInput.value,
      region: els.region.value,
      province: els.province.value,
      city: els.city.value,
      barangay: els.barangay.value,
      street_detail: els.street.value,
      text: els.text.value,
      latitude: els.lat.value,
      longitude: els.lng.value
    };
  }

  function save() {
    if (!els.text.value.trim()) {
      els.text.focus();
      return;
    }
    if (currentOptions.onConfirm) {
      currentOptions.onConfirm(collectValues());
      close();
    } else if (currentOptions.formAction) {
      els.form.action = currentOptions.formAction;
      els.form.submit();
    }
  }

  function open(options) {
    cacheEls();
    options = options || {};
    currentOptions = options;
    els.title.textContent = options.title || 'Add Address';
    resetForm();

    var regionsLoaded = els.region.options.length > 1
      ? Promise.resolve()
      : fetchJSON(urls.regions).then(function (data) { fillSelect(els.region, data.results, 'Select region…'); });

    regionsLoaded.then(function () {
      els.modal.classList.add('is-open');
      setTimeout(function () {
        ensureMap();
        map.invalidateSize();
      }, 30);

      if (options.initial) {
        var initial = options.initial;
        for (var i = 0; i < els.typeButtons.length; i++) {
          els.typeButtons[i].classList.toggle('is-selected', els.typeButtons[i].dataset.type === initial.type);
        }
        els.typeInput.value = initial.type || 'HOME';
        els.text.value = initial.text || '';

        selectCascade(initial).then(function () {
          els.street.value = initial.street_detail || '';
          if (initial.latitude && initial.longitude) {
            setTimeout(function () {
              placeMarker(parseFloat(initial.latitude), parseFloat(initial.longitude), false);
            }, 60);
          }
        });
      }
    });
  }

  function close() {
    if (els.modal) els.modal.classList.remove('is-open');
    hideSuggestions();
  }

  return { open: open, close: close, selectType: selectType, save: save };
})();

/* Shared, dependency-free address picker. Geocoding and raster tiles are
 * same-origin Django requests; provider URLs and credentials never enter the
 * browser. The map remains usable as a coordinate grid when tiles are absent.
 */
window.AddressPicker = (function () {
  'use strict';

  var urls = window.ADDRESS_PICKER_URLS || {};
  var DEFAULT_CENTER = { lat: 12.8797, lng: 121.7740 };
  var TILE_SIZE = 256;
  var els = {};
  var currentOptions = {};
  var center = { lat: DEFAULT_CENTER.lat, lng: DEFAULT_CENTER.lng };
  var marker = null;
  var zoom = 6;
  var searchTimer = null;
  var lookupSequence = 0;
  var textDirty = false;
  var mapBound = false;

  function cacheEls() {
    if (els.modal) return;
    els.modal = document.getElementById('address-picker-modal');
    els.title = document.getElementById('address-picker-title');
    els.form = document.getElementById('address-picker-form');
    els.typeInput = document.getElementById('ap-type-input');
    els.typeGroup = document.getElementById('ap-type-group');
    els.typeButtons = els.modal.querySelectorAll('.type-pill');
    els.region = document.getElementById('ap-region');
    els.province = document.getElementById('ap-province');
    els.city = document.getElementById('ap-city');
    els.barangay = document.getElementById('ap-barangay');
    els.street = document.getElementById('ap-street-input');
    els.lat = document.getElementById('ap-latitude-input');
    els.lng = document.getElementById('ap-longitude-input');
    els.text = document.getElementById('ap-text-input');
    els.map = document.getElementById('ap-map');
    els.tilePane = document.getElementById('ap-tile-pane');
    els.marker = document.getElementById('ap-marker');
    els.status = document.getElementById('ap-status');
    els.locate = document.getElementById('ap-locate-button');
    els.reverseBox = document.getElementById('ap-reverse-confirm');
    els.reverseLabel = document.getElementById('ap-reverse-label');

    els.region.addEventListener('change', onRegionChange);
    els.province.addEventListener('change', onProvinceChange);
    els.city.addEventListener('change', onCityChange);
    els.barangay.addEventListener('change', onBarangayChange);
    els.street.addEventListener('input', function () { addressChanged(true); });
    els.text.addEventListener('input', function () { textDirty = true; });
    els.locate.addEventListener('click', function () { locateAddress(true); });
    document.getElementById('ap-use-reverse').addEventListener('click', function () {
      els.text.value = els.reverseLabel.textContent;
      textDirty = true;
      els.reverseBox.hidden = true;
      setStatus('success', 'Full address updated. Structured address fields were left unchanged.');
    });
    document.getElementById('ap-zoom-in').addEventListener('click', function (event) {
      event.stopPropagation(); setZoom(zoom + 1);
    });
    document.getElementById('ap-zoom-out').addEventListener('click', function (event) {
      event.stopPropagation(); setZoom(zoom - 1);
    });
    els.modal.addEventListener('click', function (event) { if (event.target === els.modal) close(); });
    bindMap();
  }

  function fetchJSON(url, params) {
    var query = params ? '?' + new URLSearchParams(params).toString() : '';
    return fetch(url + query, { headers: { Accept: 'application/json' } })
      .then(function (response) {
        return response.json().catch(function () { return {}; }).then(function (data) {
          data.httpStatus = response.status;
          return data;
        });
      })
      .catch(function () {
        return { status: 'service_error', message: 'The location service could not be reached.' };
      });
  }

  function fillSelect(select, results, placeholder) {
    var html = '<option value="">' + placeholder + '</option>';
    (results || []).forEach(function (result) {
      html += '<option value="' + escapeHtml(result.code) + '">' + escapeHtml(result.name) + '</option>';
    });
    select.innerHTML = html;
    select.disabled = !results || results.length === 0;
  }

  function resetSelect(select, placeholder) {
    select.innerHTML = '<option value="">' + placeholder + '</option>';
    select.disabled = true;
  }

  function escapeHtml(value) {
    var div = document.createElement('div');
    div.textContent = value == null ? '' : String(value);
    return div.innerHTML;
  }

  function selectedText(select) {
    return select.value && select.selectedOptions.length ? select.selectedOptions[0].textContent.trim() : '';
  }

  function composeAddress() {
    var parts = [els.street.value.trim(), selectedText(els.barangay), selectedText(els.city), selectedText(els.province), selectedText(els.region), 'Philippines'];
    var seen = {};
    return parts.filter(function (part) {
      var key = part.toLowerCase();
      if (!part || seen[key]) return false;
      seen[key] = true;
      return true;
    }).join(', ');
  }

  function addressChanged(debounce) {
    var composed = composeAddress();
    if (!textDirty || !els.text.value.trim()) els.text.value = composed;
    clearTimeout(searchTimer);
    if (!els.city.value) return;
    searchTimer = setTimeout(function () { locateAddress(false); }, debounce ? 700 : 350);
  }

  function onRegionChange() {
    resetSelect(els.province, 'Select province...');
    resetSelect(els.city, 'Select city/municipality...');
    resetSelect(els.barangay, 'Select barangay...');
    els.street.disabled = true;
    els.street.value = '';
    addressChanged(false);
    if (!els.region.value) return;
    fetchJSON(urls.provinces, { region: els.region.value }).then(function (data) {
      fillSelect(els.province, data.results, 'Select province...');
    });
  }

  function onProvinceChange() {
    resetSelect(els.city, 'Select city/municipality...');
    resetSelect(els.barangay, 'Select barangay...');
    els.street.disabled = true;
    els.street.value = '';
    addressChanged(false);
    if (!els.province.value) return;
    fetchJSON(urls.cities, { province: els.province.value }).then(function (data) {
      fillSelect(els.city, data.results, 'Select city/municipality...');
    });
  }

  function onCityChange() {
    resetSelect(els.barangay, 'Select barangay...');
    els.street.disabled = true;
    els.street.value = '';
    addressChanged(false);
    if (!els.city.value) return;
    fetchJSON(urls.barangays, { city: els.city.value }).then(function (data) {
      fillSelect(els.barangay, data.results, 'Select barangay...');
    });
  }

  function onBarangayChange() {
    els.street.disabled = !els.barangay.value;
    addressChanged(false);
    if (els.barangay.value) els.street.focus();
  }

  function locateAddress(manual) {
    clearTimeout(searchTimer);
    if (!els.city.value) {
      if (manual) setStatus('no_result', 'Select at least a city or municipality before locating.');
      return;
    }
    var sequence = ++lookupSequence;
    setStatus('loading', 'Locating address...');
    els.locate.disabled = true;
    fetchJSON(urls.search, {
      region: els.region.value, province: els.province.value, city: els.city.value,
      barangay: els.barangay.value, street_detail: els.street.value.trim()
    }).then(function (data) {
      if (sequence !== lookupSequence) return;
      els.locate.disabled = false;
      if (data.status === 'ok' && data.result) {
        placeMarker(Number(data.result.latitude), Number(data.result.longitude), { center: true, reverse: false });
        setStatus('success', data.result.approximate && data.message
          ? data.message
          : 'Location found. Drag the pin if it needs adjustment.');
      } else {
        setStatus(data.status || 'service_error', data.message || 'The location service could not complete the request.');
      }
    });
  }

  function setStatus(state, message) {
    els.status.dataset.state = state || '';
    els.status.textContent = message;
  }

  function selectType(button) {
    Array.prototype.forEach.call(els.typeButtons, function (item) { item.classList.remove('is-selected'); });
    button.classList.add('is-selected');
    els.typeInput.value = button.dataset.type;
  }

  function selectCascade(initial) {
    if (!initial.region) return Promise.resolve();
    return fetchJSON(urls.regions).then(function (data) {
      fillSelect(els.region, data.results, 'Select region...');
      els.region.value = initial.region;
      if (!initial.province) return;
      return fetchJSON(urls.provinces, { region: initial.region }).then(function (provinces) {
        fillSelect(els.province, provinces.results, 'Select province...');
        els.province.value = initial.province;
        if (!initial.city) return;
        return fetchJSON(urls.cities, { province: initial.province }).then(function (cities) {
          fillSelect(els.city, cities.results, 'Select city/municipality...');
          els.city.value = initial.city;
          if (!initial.barangay) return;
          return fetchJSON(urls.barangays, { city: initial.city }).then(function (barangays) {
            fillSelect(els.barangay, barangays.results, 'Select barangay...');
            els.barangay.value = initial.barangay;
            els.street.disabled = false;
          });
        });
      });
    });
  }

  function resetForm() {
    els.form.reset();
    resetSelect(els.province, 'Select province...');
    resetSelect(els.city, 'Select city/municipality...');
    resetSelect(els.barangay, 'Select barangay...');
    els.street.disabled = true;
    els.lat.value = '';
    els.lng.value = '';
    marker = null;
    center = { lat: DEFAULT_CENTER.lat, lng: DEFAULT_CENTER.lng };
    zoom = 6;
    textDirty = false;
    els.marker.hidden = true;
    els.reverseBox.hidden = true;
    clearError();
    setStatus('', 'Select the address fields, then locate it or place the pin manually.');
    Array.prototype.forEach.call(els.typeButtons, function (button) {
      button.classList.toggle('is-selected', button.dataset.type === 'HOME');
    });
    els.typeInput.value = 'HOME';
    renderMap();
  }

  function collectValues() {
    return {
      type: els.typeInput.value, region: els.region.value, province: els.province.value,
      city: els.city.value, barangay: els.barangay.value,
      street_detail: els.street.value.trim(), text: els.text.value.trim(),
      latitude: els.lat.value, longitude: els.lng.value
    };
  }

  // The place NAMES behind the codes collectValues() returns. The checkout's
  // "pin on map" step needs words to put in its typed-address fields.
  function selectedLabel(select) {
    var option = select.options[select.selectedIndex];
    return option && option.value ? option.text : '';
  }

  function collectLabels() {
    return {
      region: selectedLabel(els.region), province: selectedLabel(els.province),
      city: selectedLabel(els.city), barangay: selectedLabel(els.barangay)
    };
  }

  function showError(message, focus) {
    var box = document.getElementById('ap-error');
    box.textContent = message;
    box.hidden = false;
    if (focus) focus.focus();
  }

  function clearError() {
    var box = document.getElementById('ap-error');
    box.textContent = '';
    box.hidden = true;
  }

  function save() {
    if (!els.text.value.trim()) {
      showError('Enter the full address before saving.', els.text);
      return;
    }
    if ((els.lat.value && !els.lng.value) || (!els.lat.value && els.lng.value)) {
      showError('Latitude and longitude must be provided together.', els.lat.value ? els.lng : els.lat);
      return;
    }
    clearError();
    if (currentOptions.onConfirm) {
      currentOptions.onConfirm(collectValues(), collectLabels());
      close();
    } else if (currentOptions.formAction) {
      els.form.action = currentOptions.formAction;
      els.form.submit();
    }
  }

  function open(options) {
    cacheEls();
    currentOptions = options || {};
    els.title.textContent = currentOptions.title || 'Add Address';
    els.typeGroup.hidden = currentOptions.showType === false;
    resetForm();
    var loaded = els.region.options.length > 1 ? Promise.resolve() : fetchJSON(urls.regions).then(function (data) {
      fillSelect(els.region, data.results, 'Select region...');
    });
    loaded.then(function () {
      els.modal.classList.add('is-open');
      var initial = currentOptions.initial || {};
      if (initial.type) {
        Array.prototype.forEach.call(els.typeButtons, function (button) {
          button.classList.toggle('is-selected', button.dataset.type === initial.type);
        });
        els.typeInput.value = initial.type;
      }
      els.text.value = initial.text || '';
      textDirty = false;
      selectCascade(initial).then(function () {
        els.street.value = initial.street_detail || '';
        if (initial.latitude !== null && initial.latitude !== undefined && initial.latitude !== '' &&
            initial.longitude !== null && initial.longitude !== undefined && initial.longitude !== '') {
          placeMarker(Number(initial.latitude), Number(initial.longitude), { center: true, reverse: false });
        } else {
          renderMap();
        }
      });
    });
  }

  function close() {
    clearTimeout(searchTimer);
    lookupSequence += 1;
    if (els.modal) els.modal.classList.remove('is-open');
  }

  function project(lat, lng, atZoom) {
    var scale = TILE_SIZE * Math.pow(2, atZoom);
    var boundedLat = Math.max(-85.05112878, Math.min(85.05112878, lat));
    var sin = Math.sin(boundedLat * Math.PI / 180);
    return {
      x: (lng + 180) / 360 * scale,
      y: (0.5 - Math.log((1 + sin) / (1 - sin)) / (4 * Math.PI)) * scale
    };
  }

  function unproject(x, y, atZoom) {
    var scale = TILE_SIZE * Math.pow(2, atZoom);
    var lng = x / scale * 360 - 180;
    var n = Math.PI - 2 * Math.PI * y / scale;
    return { lat: 180 / Math.PI * Math.atan(Math.sinh(n)), lng: lng };
  }

  function tileUrl(z, x, y) {
    return urls.tile.replace(/\/0\/0\/0\/$/, '/' + z + '/' + x + '/' + y + '/');
  }

  function renderMap() {
    if (!els.map || !els.map.clientWidth) return;
    var width = els.map.clientWidth;
    var height = els.map.clientHeight;
    var centerPixel = project(center.lat, center.lng, zoom);
    var left = centerPixel.x - width / 2;
    var top = centerPixel.y - height / 2;
    var firstX = Math.floor(left / TILE_SIZE);
    var firstY = Math.floor(top / TILE_SIZE);
    var lastX = Math.floor((left + width) / TILE_SIZE);
    var lastY = Math.floor((top + height) / TILE_SIZE);
    var count = Math.pow(2, zoom);
    els.tilePane.innerHTML = '';
    for (var tileY = firstY; tileY <= lastY; tileY += 1) {
      if (tileY < 0 || tileY >= count) continue;
      for (var tileX = firstX; tileX <= lastX; tileX += 1) {
        var wrappedX = ((tileX % count) + count) % count;
        var image = document.createElement('img');
        image.className = 'ap-map-tile';
        image.alt = '';
        image.draggable = false;
        image.src = tileUrl(zoom, wrappedX, tileY);
        image.style.left = (tileX * TILE_SIZE - left) + 'px';
        image.style.top = (tileY * TILE_SIZE - top) + 'px';
        image.addEventListener('error', function () { this.remove(); });
        els.tilePane.appendChild(image);
      }
    }
    renderMarker();
  }

  function renderMarker() {
    if (!marker) { els.marker.hidden = true; return; }
    var centerPixel = project(center.lat, center.lng, zoom);
    var markerPixel = project(marker.lat, marker.lng, zoom);
    els.marker.style.left = (els.map.clientWidth / 2 + markerPixel.x - centerPixel.x) + 'px';
    els.marker.style.top = (els.map.clientHeight / 2 + markerPixel.y - centerPixel.y) + 'px';
    els.marker.hidden = false;
  }

  function locationAt(clientX, clientY) {
    var rect = els.map.getBoundingClientRect();
    var centerPixel = project(center.lat, center.lng, zoom);
    return unproject(
      centerPixel.x + clientX - rect.left - rect.width / 2,
      centerPixel.y + clientY - rect.top - rect.height / 2,
      zoom
    );
  }

  function placeMarker(lat, lng, options) {
    if (!Number.isFinite(lat) || !Number.isFinite(lng) || lat < -90 || lat > 90 || lng < -180 || lng > 180) {
      showError('Enter valid coordinates within latitude -90 to 90 and longitude -180 to 180.');
      return;
    }
    marker = { lat: lat, lng: lng };
    els.lat.value = lat.toFixed(6);
    els.lng.value = lng.toFixed(6);
    if (options && options.center) {
      center = { lat: lat, lng: lng };
      if (zoom < 15) zoom = 15;
    }
    renderMap();
    if (options && options.reverse) reversePin();
  }

  function reversePin() {
    if (!marker) return;
    els.reverseBox.hidden = true;
    setStatus('loading', 'Coordinates updated. Looking up the address near the pin...');
    fetchJSON(urls.reverse, { lat: marker.lat.toFixed(6), lon: marker.lng.toFixed(6) }).then(function (data) {
      if (data.status === 'ok' && data.label) {
        els.reverseLabel.textContent = data.label;
        els.reverseBox.hidden = false;
        setStatus('success', 'Pin moved. Confirm the suggested full address only if it is correct.');
      } else {
        setStatus(data.status || 'service_error', data.message || 'Coordinates were updated, but reverse lookup failed.');
      }
    });
  }

  function setZoom(next) {
    zoom = Math.max(3, Math.min(19, next));
    renderMap();
  }

  function bindMap() {
    if (mapBound) return;
    mapBound = true;
    var drag = null;
    els.map.addEventListener('pointerdown', function (event) {
      if (event.target.closest('.ap-map-controls') || event.target === els.marker || els.marker.contains(event.target)) return;
      var startPixel = project(center.lat, center.lng, zoom);
      drag = { x: event.clientX, y: event.clientY, center: startPixel, moved: false };
      els.map.setPointerCapture(event.pointerId);
    });
    els.map.addEventListener('pointermove', function (event) {
      if (!drag) return;
      var dx = event.clientX - drag.x;
      var dy = event.clientY - drag.y;
      if (Math.abs(dx) + Math.abs(dy) > 4) drag.moved = true;
      if (drag.moved) {
        center = unproject(drag.center.x - dx, drag.center.y - dy, zoom);
        renderMap();
      }
    });
    els.map.addEventListener('pointerup', function (event) {
      if (!drag) return;
      var moved = drag.moved;
      drag = null;
      if (!moved) {
        var position = locationAt(event.clientX, event.clientY);
        placeMarker(position.lat, position.lng, { reverse: true });
      }
    });

    var markerDrag = null;
    els.marker.addEventListener('pointerdown', function (event) {
      event.stopPropagation();
      markerDrag = true;
      els.marker.setPointerCapture(event.pointerId);
    });
    els.marker.addEventListener('pointermove', function (event) {
      if (!markerDrag) return;
      var position = locationAt(event.clientX, event.clientY);
      placeMarker(position.lat, position.lng, { reverse: false });
    });
    els.marker.addEventListener('pointerup', function () {
      if (!markerDrag) return;
      markerDrag = false;
      reversePin();
    });
    els.marker.addEventListener('keydown', function (event) {
      var offsets = { ArrowLeft: [-12, 0], ArrowRight: [12, 0], ArrowUp: [0, -12], ArrowDown: [0, 12] };
      if (!offsets[event.key] || !marker) return;
      event.preventDefault();
      var pixel = project(marker.lat, marker.lng, zoom);
      var position = unproject(pixel.x + offsets[event.key][0], pixel.y + offsets[event.key][1], zoom);
      placeMarker(position.lat, position.lng, { reverse: false });
      clearTimeout(searchTimer);
      searchTimer = setTimeout(reversePin, 400);
    });
    window.addEventListener('resize', renderMap);
  }

  return { open: open, close: close, selectType: selectType, save: save };
})();

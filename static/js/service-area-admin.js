/* Delivery Areas (Owner): the add form's Province -> Municipality -> Barangay lists.
 *
 * The lists come from the same PSGC endpoints the checkout and address picker
 * use. Which levels are shown follows "This entry covers": a province entry
 * needs only the province, a municipality entry adds the municipality, a
 * barangay entry adds the barangay. The server re-checks every code and looks
 * the name up itself (branches.service_area_views), so this is only a helper.
 */
(function () {
  'use strict';

  var form = document.getElementById('area-add-form');
  if (!form) return;

  var typeSel = document.getElementById('area_type');
  var provinceSel = document.getElementById('area_province');
  var citySel = document.getElementById('area_city');
  var brgySel = document.getElementById('area_barangay');
  var citiesUrl = form.getAttribute('data-cities-url');
  var barangaysUrl = form.getAttribute('data-barangays-url');

  function fill(select, placeholder, rows, wanted) {
    select.innerHTML = '';
    var first = document.createElement('option');
    first.value = '';
    first.textContent = placeholder;
    select.appendChild(first);
    rows.forEach(function (row) {
      var option = document.createElement('option');
      option.value = row.code;
      option.textContent = row.name;
      if (row.code === wanted) option.selected = true;
      select.appendChild(option);
    });
  }

  function load(url, param, code) {
    if (!code) return Promise.resolve([]);
    return fetch(url + '?' + param + '=' + encodeURIComponent(code), { credentials: 'same-origin' })
      .then(function (response) { return response.ok ? response.json() : { results: [] }; })
      .then(function (data) { return data.results || []; })
      .catch(function () { return []; });
  }

  function syncLevels() {
    var type = typeSel.value;
    Array.prototype.forEach.call(form.querySelectorAll('[data-area-level]'), function (field) {
      var shown = field.getAttribute('data-area-level').split(' ').indexOf(type) !== -1;
      field.style.display = shown ? '' : 'none';
      var select = field.querySelector('select');
      if (select) select.required = shown;
    });
  }

  function loadCities(wantedCity, wantedBarangay) {
    return load(citiesUrl, 'province', provinceSel.value).then(function (rows) {
      fill(citySel, 'Select municipality…', rows, wantedCity);
      return loadBarangays(wantedBarangay);
    });
  }

  function loadBarangays(wantedBarangay) {
    return load(barangaysUrl, 'city', citySel.value).then(function (rows) {
      fill(brgySel, 'Select barangay…', rows, wantedBarangay);
    });
  }

  typeSel.addEventListener('change', syncLevels);
  provinceSel.addEventListener('change', function () { loadCities('', ''); });
  citySel.addEventListener('change', function () { loadBarangays(''); });

  syncLevels();
  // Restore a refused submit's choices.
  if (provinceSel.value) {
    loadCities(form.getAttribute('data-selected-city') || '', form.getAttribute('data-selected-barangay') || '');
  }
})();

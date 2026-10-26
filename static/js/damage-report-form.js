/*
 * Damage report forms (staff "Report Damaged Product" and the customer
 * "Report a problem"): photo previews with the same rules the server enforces,
 * and the staff form's product -> batch -> quantity narrowing.
 *
 * Everything here is a convenience. The server re-validates every photo (real
 * JPEG/PNG header, size, count) and every quantity, so nothing depends on this
 * script having run - with JavaScript off the forms still post.
 */
(function () {
  'use strict';

  var ALLOWED_TYPES = ['image/jpeg', 'image/png'];

  function setupPhotos(form) {
    var input = form.querySelector('[data-dr-photos]');
    if (!input) return;
    var list = form.querySelector('[data-dr-photo-list]');
    var errorEl = form.querySelector('[data-dr-photo-error]');
    var maxPhotos = parseInt(form.getAttribute('data-max-photos'), 10) || 5;
    var maxMb = parseFloat(form.getAttribute('data-max-photo-mb')) || 5;
    var maxBytes = maxMb * 1024 * 1024;
    var needPhoto = form.hasAttribute('data-require-photo');
    var urls = [];

    function showError(message) {
      if (!errorEl) return;
      errorEl.textContent = message || '';
      errorEl.hidden = !message;
    }

    function clearPreviews() {
      urls.forEach(function (url) { URL.revokeObjectURL(url); });
      urls = [];
      if (list) list.innerHTML = '';
    }

    function problemWith(files) {
      if (files.length > maxPhotos) {
        return 'You can attach at most ' + maxPhotos + ' photos.';
      }
      for (var i = 0; i < files.length; i++) {
        var file = files[i];
        if (ALLOWED_TYPES.indexOf(file.type) === -1) {
          return '"' + file.name + '" must be a JPG or PNG picture.';
        }
        if (file.size > maxBytes) {
          return '"' + file.name + '" is larger than ' + maxMb + ' MB.';
        }
      }
      return '';
    }

    input.addEventListener('change', function () {
      var files = Array.prototype.slice.call(input.files || []);
      clearPreviews();
      var problem = problemWith(files);
      if (problem) {
        // Drop the whole selection: a half-accepted set would silently post
        // fewer photos than the person thinks they attached.
        input.value = '';
        showError(problem);
        return;
      }
      showError('');
      if (!list) return;
      files.forEach(function (file) {
        var url = URL.createObjectURL(file);
        urls.push(url);
        var item = document.createElement('li');
        var img = document.createElement('img');
        img.src = url;
        img.alt = 'Preview of ' + file.name;
        var caption = document.createElement('span');
        caption.textContent = file.name;
        item.appendChild(img);
        item.appendChild(caption);
        list.appendChild(item);
      });
    });

    form.addEventListener('submit', function (event) {
      if (needPhoto && !(input.files && input.files.length)) {
        event.preventDefault();
        showError('Add at least one photo of the damage.');
        input.focus();
      }
    });
  }

  function setupCascade(form) {
    var stock = form.querySelector('[data-dr-stock]');
    var batch = form.querySelector('[data-dr-batch]');
    var qty = form.querySelector('[data-dr-qty]');
    var hint = form.querySelector('[data-dr-qty-hint]');
    if (!stock || !batch || !qty) return;

    function sync() {
      var key = stock.value;
      var i;
      for (i = 0; i < batch.options.length; i++) {
        var option = batch.options[i];
        if (!option.value) continue;
        var match = option.getAttribute('data-key') === key;
        option.hidden = !match;
        option.disabled = !match;
      }
      if (batch.selectedIndex > 0 && batch.options[batch.selectedIndex].disabled) {
        batch.value = '';
      }

      var source = batch.value ? batch.options[batch.selectedIndex]
        : (stock.value ? stock.options[stock.selectedIndex] : null);
      var max = source ? parseInt(source.getAttribute('data-reportable'), 10) : NaN;
      if (isNaN(max)) {
        qty.removeAttribute('max');
        if (hint) hint.textContent = 'It cannot be more than what is on hand.';
        return;
      }
      qty.max = String(max);
      if (hint) {
        hint.textContent = max > 0
          ? 'Up to ' + max + ' can be reported' + (batch.value ? ' from this batch.' : '.')
          : 'Everything on hand is already covered by an open damage report.';
      }
    }

    stock.addEventListener('change', sync);
    batch.addEventListener('change', sync);
    sync();
  }

  // Customer form: choosing an item caps the quantity at what is left to report.
  function setupItems(form) {
    var radios = form.querySelectorAll('[data-dr-item]');
    var qty = form.querySelector('[data-dr-qty]');
    if (!radios.length || !qty) return;

    function sync() {
      var chosen = form.querySelector('[data-dr-item]:checked');
      var max = chosen ? parseInt(chosen.getAttribute('data-max'), 10) : NaN;
      if (isNaN(max)) { qty.removeAttribute('max'); return; }
      qty.max = String(max);
      if (qty.value && parseInt(qty.value, 10) > max) qty.value = String(max);
    }

    Array.prototype.forEach.call(radios, function (radio) {
      radio.addEventListener('change', sync);
    });
    sync();
  }

  document.addEventListener('DOMContentLoaded', function () {
    var forms = document.querySelectorAll('[data-damage-form]');
    Array.prototype.forEach.call(forms, function (form) {
      setupPhotos(form);
      setupCascade(form);
      setupItems(form);
    });
  });
})();

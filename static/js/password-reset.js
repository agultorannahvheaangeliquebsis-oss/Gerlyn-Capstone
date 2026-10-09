(function () {
  'use strict';

  var EYE = '<path d="M1 12s4-8 11-8 11 8 11 8-4 8-11 8-11-8-11-8z"/><circle cx="12" cy="12" r="3"/>';
  var EYE_OFF = '<path d="M17.94 17.94A10.07 10.07 0 0 1 12 20c-7 0-11-8-11-8a18.45 18.45 0 0 1 5.06-5.94"/>'
    + '<path d="M9.9 4.24A9.12 9.12 0 0 1 12 4c7 0 11 8 11 8a18.5 18.5 0 0 1-2.16 3.19"/>'
    + '<path d="M14.12 14.12a3 3 0 1 1-4.24-4.24"/><line x1="1" y1="1" x2="23" y2="23"/>';

  document.querySelectorAll('[data-password-toggle]').forEach(function (button) {
    var input = document.getElementById(button.getAttribute('data-password-toggle'));
    var icon = button.querySelector('svg');
    if (!input || !icon) return;

    button.addEventListener('click', function () {
      var show = input.type === 'password';
      input.type = show ? 'text' : 'password';
      button.setAttribute('aria-pressed', show ? 'true' : 'false');
      button.setAttribute('aria-label', show ? 'Hide password' : 'Show password');
      icon.innerHTML = show ? EYE_OFF : EYE;
      var value = input.value;
      input.focus();
      input.setSelectionRange(value.length, value.length);
    });
  });

  var passwordForm = document.querySelector('[data-password-form]');
  if (passwordForm) {
    var primary = passwordForm.querySelector('[data-password-primary]');
    var confirm = passwordForm.querySelector('[data-password-confirm]');
    var mismatch = document.getElementById('password-match-error');
    var checkMatch = function () {
      var invalid = Boolean(confirm.value && primary.value !== confirm.value);
      confirm.setCustomValidity(invalid ? 'The two passwords do not match.' : '');
      if (mismatch) mismatch.hidden = !invalid;
    };
    primary.addEventListener('input', checkMatch);
    confirm.addEventListener('input', checkMatch);
  }

  document.querySelectorAll('[data-loading-form]').forEach(function (form) {
    var button = form.querySelector('button[type="submit"]');
    var label = button && button.querySelector('[data-submit-label]');
    if (!button) return;
    var originalLabel = label ? label.textContent : '';

    form.addEventListener('submit', function () {
      if (!form.checkValidity()) return;
      button.disabled = true;
      button.setAttribute('aria-busy', 'true');
      if (label) label.textContent = form.getAttribute('data-loading-label') || 'Submitting…';
    });

    window.addEventListener('pageshow', function (event) {
      if (!event.persisted) return;
      button.disabled = false;
      button.removeAttribute('aria-busy');
      if (label) label.textContent = originalLabel;
    });
  });
})();

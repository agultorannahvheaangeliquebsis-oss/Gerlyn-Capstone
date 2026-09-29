/* GCash reference number: exactly 13 digits, shown as "1234 567 890123".
   Same rule as billing.validators.clean_gcash_reference — spaces and dashes
   are ignored, anything else that is not a digit is refused. The server is
   the authority; this only says so before the round trip.

   GerlynGCashRef.sync(input, isGCash) is called by every page that has a
   reference field, each time the payment method changes:
     - GCash: numeric keypad, 13-digit hint, and an inline message under the
       field. The message uses setCustomValidity, so the form is not valid —
       Save stays disabled, native submit is blocked — until the number is.
     - anything else: every trace of it is removed, so a number typed for
       GCash never blocks a form from a field that is no longer shown.

   Loaded without defer: billing_form.html and cart.html call it from inline
   scripts that run before deferred scripts would. */
(function () {
  var LENGTH = 13;
  var HINT = '13 digits, e.g. 1234 567 890123';
  var GENERIC_PLACEHOLDER = 'e.g. BT-2026-0001';

  function problem(value) {
    var raw = (value || '').trim();
    if (!raw) return ''; // blank is the field's "required", not a format error
    var digits = raw.replace(/[\s-]/g, '');
    if (/\D/.test(digits)) {
      return 'A GCash reference number is digits only — no letters or symbols.';
    }
    if (digits.length !== LENGTH) {
      return 'A GCash reference number is exactly ' + LENGTH +
        ' digits — you have entered ' + digits.length + '.';
    }
    return '';
  }

  function messageEl(input) {
    var id = input.id + '-gcash-error';
    var el = document.getElementById(id);
    if (!el) {
      el = document.createElement('p');
      el.id = id;
      el.className = 'field-error field-error--ref';
      el.setAttribute('role', 'alert');
      el.hidden = true;
      input.insertAdjacentElement('afterend', el);
    }
    return el;
  }

  /* Validity always tracks the value (so Save is right from the first
     keystroke); the message waits until the value is definitely wrong — a
     stray character, or too many digits — or the field has been left. "9 of
     13" while someone is still typing is not yet an error. */
  function check(input, show) {
    var msg = messageEl(input);
    var text = problem(input.value);
    var digits = input.value.replace(/[\s-]/g, '');
    var definitelyWrong = /\D/.test(digits) || digits.length > LENGTH;
    input.setCustomValidity(text);
    var visible = !!text && (show || definitelyWrong);
    msg.textContent = visible ? text : '';
    msg.hidden = !visible;
    input.classList.toggle('is-invalid', visible);
    if (visible) {
      input.setAttribute('aria-invalid', 'true');
      input.setAttribute('aria-describedby', msg.id);
    } else {
      input.removeAttribute('aria-invalid');
      input.removeAttribute('aria-describedby');
    }
  }

  function onInput(e) { check(e.target, false); }
  function onBlur(e) { check(e.target, true); }

  function sync(input, isGCash) {
    if (!input) return;
    if (isGCash) {
      input.setAttribute('inputmode', 'numeric');
      input.setAttribute('maxlength', '20'); // 13 digits + separators
      input.setAttribute('placeholder', HINT);
      if (!input.dataset.gcashRef) {
        input.dataset.gcashRef = '1';
        input.addEventListener('input', onInput);
        input.addEventListener('blur', onBlur);
      }
      check(input, false);
    } else {
      if (input.dataset.gcashRef) {
        delete input.dataset.gcashRef;
        input.removeEventListener('input', onInput);
        input.removeEventListener('blur', onBlur);
      }
      input.setCustomValidity('');
      input.classList.remove('is-invalid');
      input.removeAttribute('aria-invalid');
      input.removeAttribute('aria-describedby');
      input.removeAttribute('inputmode');
      input.setAttribute('maxlength', '100');
      input.setAttribute('placeholder', GENERIC_PLACEHOLDER);
      var msg = document.getElementById(input.id + '-gcash-error');
      if (msg) { msg.textContent = ''; msg.hidden = true; }
    }
  }

  window.GerlynGCashRef = { sync: sync, problem: problem };
})();

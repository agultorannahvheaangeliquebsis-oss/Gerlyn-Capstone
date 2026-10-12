// Plain-English validation messages for constrained inputs.
//
// The browser already refuses a form whose number is above `max`, below
// `min`, off `step`, empty while `required`, or longer than `maxlength` —
// but its wording ("Value must be less than or equal to 250000.") is
// different in every browser and says nothing about WHICH field. This
// replaces only the wording, never the rule: the limits themselves live in
// core/limits.py and arrive here as the min/max/maxlength attributes, and the
// server re-checks all of them regardless.
//
// Opt in per field with data-label="Retail price"; add data-currency for a
// peso amount so the limit reads "₱250,000.00" instead of "250000".
(function () {
  document.querySelectorAll('[data-account-role]').forEach(function (role) {
    function updateRoleLimits() {
      var customer = role.value === 'CUSTOMER';
      ['first_name', 'last_name', 'email'].forEach(function (name) {
        var el = role.form.querySelector('[name="' + name + '"]');
        if (!el) return;
        el.required = customer;
        if (name !== 'email') el.maxLength = customer ? 50 : 100;
      });
    }
    role.addEventListener('change', updateRoleLimits);
    updateRoleLimits();
  });

  function passwordMessage(el) {
    if (!el.hasAttribute('data-account-password') || !el.value) return '';
    var special = "!@#$%^&*()_+-=[]{}|;:'\",.<>/?`~\\";
    if (!/[A-Z]/.test(el.value)) return 'Password must contain at least one uppercase letter.';
    if (!/[0-9]/.test(el.value)) return 'Password must contain at least one number.';
    if (!Array.from(el.value).some(function (c) { return special.indexOf(c) !== -1; })) {
      return 'Password must contain at least one special character (e.g. ! @ # $ % * -).';
    }
    return '';
  }

  function number(value, currency) {
    var n = Number(value);
    if (!isFinite(n)) return value;
    return currency
      ? '₱' + n.toLocaleString('en-PH', { minimumFractionDigits: 2, maximumFractionDigits: 2 })
      : n.toLocaleString('en-PH');
  }

  function messageFor(el) {
    var label = el.dataset.label || el.getAttribute('aria-label');
    if (!label) return '';
    var v = el.validity;
    var money = el.hasAttribute('data-currency');
    if (v.valueMissing) return label + ' is required.';
    if (v.badInput) return label + ' must be a valid number.';
    if (v.rangeOverflow) return label + ' cannot be more than ' + number(el.max, money) + '.';
    if (v.rangeUnderflow) {
      if (money && Number(el.min) <= 0.01) return label + ' must be more than ₱0.';
      return label + ' must be at least ' + number(el.min, money) + '.';
    }
    if (v.stepMismatch) {
      return money ? label + ' can have at most 2 decimal places.' : label + ' must be a whole number.';
    }
    if (v.tooLong) return label + ' cannot be longer than ' + el.maxLength + ' characters.';
    if (v.tooShort) return label + ' must contain at least ' + el.minLength + ' characters.';
    if (v.typeMismatch) return 'Enter a valid ' + label.toLowerCase() + '.';
    if (v.patternMismatch && el.dataset.accountInput) {
      if (el.name === 'contact_number') return 'Enter a valid PH mobile number, e.g. 0917 123 4567 or +63 917 123 4567.';
      if (el.name === 'username') return 'Use letters, numbers or @ . + - _ for your username.';
      return 'Use letters, spaces, hyphens, apostrophes or periods for names.';
    }
    return '';
  }

  // Trim identity fields just as Django forms do; passwords remain exact.
  document.addEventListener('blur', function (event) {
    var el = event.target;
    if (!el || !el.dataset.accountInput) return;
    el.value = el.value.trim();
    if (el.name === 'username') el.value = el.value.normalize('NFKC');
    el.setCustomValidity('');
  }, true);

  document.addEventListener('submit', function (event) {
    var form = event.target;
    var first = form.querySelector('[data-account-input="first_name"]');
    var last = form.querySelector('[data-account-input="last_name"]');
    // Internal role display names share one 100-character database column.
    if (first && last && first.maxLength === 100) {
      last.setCustomValidity('');
      if ((first.value.trim() + ' ' + last.value.trim()).trim().length > 100) {
        last.setCustomValidity('First and last name together cannot be longer than 100 characters.');
        event.preventDefault();
        last.reportValidity();
      }
    }
  });

  // `invalid` does not bubble, so listen in the capture phase.
  document.addEventListener('invalid', function (event) {
    var el = event.target;
    if (!el || !el.setCustomValidity) return;
    if (el.validity.customError) return;
    el.setCustomValidity('');          // re-derive from the current state
    var text = messageFor(el);
    if (text) el.setCustomValidity(text);
  }, true);

  // A custom message sticks until cleared, so drop it as soon as the value
  // changes — otherwise a corrected field would still read as invalid.
  ['input', 'change'].forEach(function (name) {
    document.addEventListener(name, function (event) {
      var el = event.target;
      if (el && el.setCustomValidity) el.setCustomValidity(passwordMessage(el));
    }, true);
  });
  document.querySelectorAll('[data-account-password]').forEach(function (el) {
    el.setCustomValidity(passwordMessage(el));
  });
})();

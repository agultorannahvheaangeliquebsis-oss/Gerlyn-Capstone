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
    return '';
  }

  // `invalid` does not bubble, so listen in the capture phase.
  document.addEventListener('invalid', function (event) {
    var el = event.target;
    if (!el || !el.setCustomValidity) return;
    el.setCustomValidity('');          // re-derive from the current state
    var text = messageFor(el);
    if (text) el.setCustomValidity(text);
  }, true);

  // A custom message sticks until cleared, so drop it as soon as the value
  // changes — otherwise a corrected field would still read as invalid.
  ['input', 'change'].forEach(function (name) {
    document.addEventListener(name, function (event) {
      var el = event.target;
      if (el && el.setCustomValidity) el.setCustomValidity('');
    }, true);
  });
})();

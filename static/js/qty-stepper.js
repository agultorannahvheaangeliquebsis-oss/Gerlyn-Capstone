// − / + buttons for a quantity field.
//
// Progressive enhancement over a real <input type="number">: with this script
// blocked the input still types, validates and submits, and the two buttons
// are aria-hidden and untabbable so they were never part of the keyboard path
// to begin with.
//
// Delegated on document so a stepper inside markup swapped in later (a
// re-rendered grid, a modal) works without re-running anything.
(function () {
  function clamp(input, next) {
    var min = parseInt(input.getAttribute('min'), 10);
    if (isNaN(min)) min = 1;
    var max = parseInt(input.getAttribute('max'), 10);
    if (next < min) next = min;
    if (!isNaN(max) && next > max) next = max;
    return next;
  }

  document.addEventListener('click', function (e) {
    var btn = e.target.closest('[data-qty-step]');
    if (!btn) return;
    var stepper = btn.closest('[data-qty-stepper]');
    if (!stepper) return;
    var input = stepper.querySelector('input[type="number"]');
    if (!input) return;

    var step = parseInt(btn.getAttribute('data-qty-step'), 10) || 0;
    var current = parseInt(input.value, 10);
    // A blank or half-typed field reads as NaN; treat it as the minimum
    // rather than letting NaN propagate into the value and wipe the input.
    if (isNaN(current)) current = clamp(input, 0);

    input.value = clamp(input, current + step);
    // Anything listening for a change (validation, a live total) should hear
    // this the same as it would a typed edit.
    input.dispatchEvent(new Event('change', { bubbles: true }));
  });
})();

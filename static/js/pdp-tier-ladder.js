// Buy-more-save-more ladder (product detail page): keeps the tier rows and
// the quantity input in sync in both directions.
//
//   - Clicking a row sets the quantity input to that row's threshold and
//     fires 'change', so anything else listening on the field (there is
//     nothing else today, but the stepper buttons dispatch the same event)
//     sees a normal edit rather than a script writing to it silently.
//   - Typing in the quantity field, or using the +/- steppers (which change
//     the same input — see qty-stepper.js), highlights whichever row that
//     quantity has actually earned: the highest threshold at or below it.
//
// Only present on the interactive ladder (product_detail.html passes
// can_buy as `interactive` to partials/_tier_ladder.html) — a guest or an
// out-of-stock page renders the same rows with no [data-tier-ladder]
// wrapper and no radio inputs, so this script has nothing to attach to and
// exits on the first line.
(function () {
  var ladder = document.querySelector('[data-tier-ladder]');
  var qty = document.getElementById('quantity');
  if (!ladder || !qty) return;

  var rows = Array.prototype.slice.call(ladder.querySelectorAll('[data-tier-row]'));

  function selectForQuantity(value) {
    var n = parseInt(value, 10);
    if (isNaN(n)) return;

    var best = null;
    rows.forEach(function (row) {
      var rowQty = parseInt(row.getAttribute('data-tier-qty'), 10);
      if (n >= rowQty && (!best || rowQty > parseInt(best.getAttribute('data-tier-qty'), 10))) {
        best = row;
      }
    });
    // Below every threshold (including the base "1"): nothing qualifies yet,
    // so nothing is shown as selected rather than falsely highlighting the
    // base row for a quantity of 0.
    rows.forEach(function (row) {
      var isBest = row === best;
      row.classList.toggle('is-selected', isBest);
      var radio = row.querySelector('input[type="radio"]');
      if (radio) radio.checked = isBest;
    });
  }

  rows.forEach(function (row) {
    row.addEventListener('click', function () {
      qty.value = row.getAttribute('data-tier-qty');
      qty.dispatchEvent(new Event('change', { bubbles: true }));
    });
  });

  qty.addEventListener('input', function () { selectForQuantity(qty.value); });
  qty.addEventListener('change', function () { selectForQuantity(qty.value); });

  selectForQuantity(qty.value);
})();

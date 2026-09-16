// Purchase Option ladder (product detail page): keeps the tier rows and the
// quantity input in sync in both directions, and enforces the bulk row's
// minimum once it is picked.
//
//   - Clicking a row is the shopper's explicit choice: it sets the quantity
//     input's floor ([min]) to that row's threshold, jumps the quantity up
//     to meet it if it doesn't already, and shows "Minimum quantity: N"
//     when that floor is above 1. The floor also gates the +/- steppers for
//     free, since qty-stepper.js already reads [min] off the same input.
//   - Typing in the quantity field, or using the steppers, can move UP into
//     a better tier (typing 15 auto-checks the 10+ row) but never below the
//     floor the active row set — undercutting it would leave the radio
//     saying "10+" while the field charged the 1-can price. To go back down
//     to a lower tier, the shopper clicks that row, which resets the floor
//     first.
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
  var hint = document.querySelector('[data-qty-min-hint]');
  var hintN = hint ? hint.querySelector('[data-qty-min-n]') : null;

  function thresholdOf(row) { return parseInt(row.getAttribute('data-tier-qty'), 10); }

  function bestRowFor(n) {
    var best = null;
    rows.forEach(function (row) {
      var t = thresholdOf(row);
      if (n >= t && (!best || t > thresholdOf(best))) best = row;
    });
    return best;
  }

  function selectRow(row) {
    rows.forEach(function (r) {
      var isPick = r === row;
      r.classList.toggle('is-selected', isPick);
      var radio = r.querySelector('input[type="radio"]');
      if (radio) radio.checked = isPick;
    });
    var min = thresholdOf(row);
    qty.setAttribute('min', min);
    if (hint) hint.hidden = min <= 1;
    if (hintN) hintN.textContent = min;
  }

  // Explicit choice: clicking a card both sets the floor and moves the
  // quantity up to meet it, so the field never disagrees with the card that
  // is visibly selected.
  rows.forEach(function (row) {
    row.addEventListener('click', function () {
      var min = thresholdOf(row);
      var current = parseInt(qty.value, 10);
      selectRow(row);
      if (isNaN(current) || current < min) {
        qty.value = min;
        qty.dispatchEvent(new Event('change', { bubbles: true }));
      }
    });
  });

  // Typing or stepping: clamp to [min, max] first (the stepper's own
  // clamp() in qty-stepper.js already respects both, this covers direct
  // typing), then let a qualifying higher amount auto-upgrade the selected
  // row.
  function onQtyChange() {
    var n = parseInt(qty.value, 10);
    if (isNaN(n)) return;
    var floor = parseInt(qty.getAttribute('min'), 10) || 1;
    var ceiling = parseInt(qty.getAttribute('max'), 10);
    if (n < floor) n = floor;
    if (!isNaN(ceiling) && n > ceiling) n = ceiling;
    if (n !== parseInt(qty.value, 10)) qty.value = n;
    var best = bestRowFor(n);
    if (best && !best.classList.contains('is-selected')) selectRow(best);
  }

  qty.addEventListener('input', onQtyChange);
  qty.addEventListener('change', onQtyChange);

  selectRow(bestRowFor(parseInt(qty.value, 10) || 1) || rows[0]);
})();

// Dashboard "Open Orders" — Columns menu. Client-side show/hide of the
// Customer and Channel columns only: no request, nothing persisted, so a
// reload restores every column. The Columns <details> ships `hidden` and is
// revealed here, so without JS the control is simply not offered.
// Cells carry data-oo-col="<name>"; hiding is a class on the table that
// gerlyn.css turns into display:none. No build, no deps.
(function () {
  const menu = document.getElementById('oo-columns-menu');
  const table = document.getElementById('open-orders-table');
  if (!menu) return;
  menu.hidden = false;
  if (!table) return;  // empty state: nothing to toggle, menu stays inert

  menu.querySelectorAll('[data-oo-col-toggle]').forEach(function (box) {
    box.addEventListener('change', function () {
      table.classList.toggle('oo-hide-' + box.dataset.ooColToggle, !box.checked);
    });
  });
})();

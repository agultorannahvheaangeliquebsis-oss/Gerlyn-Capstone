// Collapsible sections in the Products catalog's Filters rail.
//
// Two shapes, one behaviour:
//   * every Category department that has shelves folds them away behind a
//     chevron, so the rail opens as a short list of departments instead of
//     every shelf in the store;
//   * the whole Branch list folds the same way.
//
// A section starts collapsed UNLESS one of its own boxes is ticked — an
// active filter is never hidden from the shopper who set it. Ticking a box
// while a section is open leaves it open; catalog-live.js only swaps the
// grid, never this rail, so the open/closed state set here survives a live
// filter change.
//
// Progressive enhancement: the toggle buttons are rendered `hidden` and are
// only revealed here. With this script blocked there is no button, nothing
// is `hidden`, and the rail is the same fully-expanded list it has always
// been — no filter is unreachable.
(function () {
  var rail = document.querySelector('form[data-catalog-live-filters]');
  if (!rail) return;

  // Collapse a region by id: toggle `hidden` on it and aria-expanded on the
  // button that controls it. `expand` omitted flips the current state.
  function setOpen(button, region, expand) {
    if (expand === undefined) expand = button.getAttribute('aria-expanded') !== 'true';
    button.setAttribute('aria-expanded', expand ? 'true' : 'false');
    region.hidden = !expand;
  }

  function wire(button) {
    var region = document.getElementById(button.getAttribute('aria-controls'));
    if (!region) return;

    // Anything ticked decides the starting state — an active filter is never
    // folded away. Shelves live inside the region; a department's own box
    // lives in the row beside the button, not inside the region, so check
    // both places.
    var row = button.closest('.rail-dept-row');
    var hasActive = !!region.querySelector('input[type="checkbox"]:checked')
      || !!(row && row.querySelector('input[type="checkbox"]:checked'));

    button.hidden = false;
    setOpen(button, region, hasActive);

    button.addEventListener('click', function () { setOpen(button, region); });
  }

  rail.querySelectorAll('.rail-disclose').forEach(wire);
})();

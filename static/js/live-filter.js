// Real-time, in-page filter shared by admin list pages with a search box
// (Users, Stock per Branch, Products, Suppliers, Order Console). No
// round-trip: hides already-rendered rows as the user types, matching
// against each row's data-filter-text attribute. Same approach as
// catalog-filter.js, generalized via data attributes so any list page can
// opt in without its own bespoke script.
(function () {
  document.querySelectorAll('form[data-live-filter]').forEach(function (form) {
    const input = form.querySelector('input[type="search"]');
    if (!input) return;

    const rows = Array.from(document.querySelectorAll(form.dataset.liveFilter));
    if (!rows.length) return;

    const emptyRow = document.querySelector(form.dataset.filterEmptyTarget || '[data-filter-empty]');
    const emptyQuerySpan = emptyRow ? emptyRow.querySelector('[data-filter-empty-query]') : null;

    // Typing already filters live — don't reload the page on Enter/Search click.
    form.addEventListener('submit', function (e) { e.preventDefault(); });

    function filter() {
      const query = input.value.trim().toLowerCase();
      let visible = 0;

      rows.forEach(function (row) {
        const match = !query || (row.dataset.filterText || '').toLowerCase().includes(query);
        row.hidden = !match;
        if (match) visible++;
      });

      if (emptyRow) {
        const empty = visible === 0;
        emptyRow.hidden = !empty;
        if (empty && emptyQuerySpan) emptyQuerySpan.textContent = input.value.trim();
      }
    }

    input.addEventListener('input', filter);
    if (input.value.trim()) filter();
  });
})();

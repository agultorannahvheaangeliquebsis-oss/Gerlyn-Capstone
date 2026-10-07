// Real-time, in-page filter shared by admin list pages with a search box
// (Users, Stock per Branch, Products, Suppliers, Order Console). No
// round-trip: hides already-rendered rows as the user types, matching
// against each row's data-filter-text attribute. Same approach as
// catalog-filter.js, generalized via data attributes so any list page can
// opt in without its own bespoke script.
//
// Paginated lists add `data-server-search` to the form: the in-page filter
// still gives instant feedback on the rows already shown, but those are only
// ONE page of the results, so after a short pause (or on Enter) the form is
// submitted and the server searches every page. The GET form carries no
// `page` field, so a new search always lands back on page 1.
(function () {
  document.querySelectorAll('form[data-live-filter]').forEach(function (form) {
    const input = form.querySelector('input[type="search"]');
    if (!input) return;

    const rows = Array.from(document.querySelectorAll(form.dataset.liveFilter));
    if (!rows.length) return;

    const emptyRow = document.querySelector(form.dataset.filterEmptyTarget || '[data-filter-empty]');
    const emptyQuerySpan = emptyRow ? emptyRow.querySelector('[data-filter-empty-query]') : null;

    const serverSearch = form.hasAttribute('data-server-search');
    const refocusKey = 'gv-refocus:' + location.pathname;

    // Typing already filters live — don't reload the page on Enter/Search
    // click, unless this list is paginated and needs the server's answer.
    if (!serverSearch) {
      form.addEventListener('submit', function (e) { e.preventDefault(); });
    } else {
      let timer = null;
      let lastSubmitted = input.value.trim();
      input.addEventListener('input', function () {
        clearTimeout(timer);
        if (input.value.trim() === lastSubmitted) return;
        timer = setTimeout(function () {
          lastSubmitted = input.value.trim();
          try { sessionStorage.setItem(refocusKey, '1'); } catch (e) {}
          form.submit();
        }, 500);
      });
      form.addEventListener('submit', function () { clearTimeout(timer); });
      // The page reloaded because of typing: put the caret back where it was.
      try {
        if (sessionStorage.getItem(refocusKey)) {
          sessionStorage.removeItem(refocusKey);
          input.focus();
          const end = input.value.length;
          input.setSelectionRange(end, end);
        }
      } catch (e) {}
    }

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

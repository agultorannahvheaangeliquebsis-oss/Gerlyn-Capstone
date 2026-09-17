// Live-updates the Products catalog (search box + the Filters rail's
// category/branch checkboxes) without a full-page reload. Debounced +
// abortable, same pattern as the header's live-search.js. Progressive
// enhancement: if this never runs, both forms are plain method="GET" and
// submit normally — the server renders the exact same result via
// orders.customer_views.product_catalog.
//
// Two separate <form> elements feed one grid: the header's search box
// (data-catalog-live) and the catalog page's Filters rail
// (data-catalog-live-filters). They can't be one <form> — they sit in
// different parts of the page — so this reads whichever of the two are
// present and merges their state into a single query on every change.
(function () {
  const searchForm = document.querySelector('form[data-catalog-live]');
  const filterForm = document.querySelector('form[data-catalog-live-filters]');
  const summary = document.getElementById('result-summary');
  const grid = document.getElementById('pgrid');
  const categoryFacet = document.getElementById('category-facet');
  if ((!searchForm && !filterForm) || !summary || !grid || !categoryFacet) return;

  const searchInput = searchForm ? searchForm.querySelector('input[type="search"]') : null;

  const DEBOUNCE_MS = 250;
  let debounceTimer = null;
  let abortController = null;
  let requestVersion = 0;

  // Sort has no control in either form (it's a row of plain links). Its
  // href carries filter_qs — the rail's state at the time the SERVER last
  // rendered the page — which goes stale the instant a live filter change
  // below updates the grid without a reload: ticking/unticking a box after
  // that never touches those hrefs, so clicking Sort would silently discard
  // whatever the shopper had just changed and reapply whatever filters were
  // active on the last full page load instead. Reading sort from
  // `sortOverride`/the current URL, but everything else fresh from the live
  // DOM state every time, is what keeps sort from ever clobbering a filter
  // change made since.
  function targetUrl(sortOverride) {
    const current = new URLSearchParams(window.location.search);
    const params = new URLSearchParams();

    const q = searchInput ? searchInput.value.trim() : current.get('q');
    if (q) params.set('q', q);

    if (filterForm) {
      // The rail's hidden <input name="branch" value=""> guarantees a plain
      // submit always sends a `branch` key, even with every box unticked —
      // that's what tells product_catalog "this is an explicit empty
      // filter, show every branch" rather than "no branch filter in this
      // request, fall back to the session branch's stock". FormData preserves
      // that hidden field's empty entry, but the filter below was dropping
      // every falsy value including it, so an all-unticked submit built a
      // URL with NO `branch` key at all — indistinguishable from never
      // having filtered, which is why the grid stayed pinned to the session
      // branch instead of showing all of them. Appending it explicitly here
      // reproduces the hidden input's guarantee for the fetch()'d URL too.
      params.append('branch', '');
      // getAll, not get: both category and branch are checklists, so there
      // can be several values of each in play.
      new FormData(filterForm).forEach(function (value, key) {
        if ((key === 'category' || key === 'branch') && value) {
          params.append(key, value);
        }
      });
    } else {
      current.getAll('category').forEach(function (c) { params.append('category', c); });
      current.getAll('branch').forEach(function (b) { if (b) params.append('branch', b); });
    }

    const sort = sortOverride !== undefined ? sortOverride : current.get('sort');
    if (sort) params.set('sort', sort);
    const qs = params.toString();
    return window.location.pathname + (qs ? '?' + qs : '');
  }

  // The sort pills live outside #result-summary/#pgrid, so a load() doesn't
  // touch them on its own — this is what keeps the clicked pill showing as
  // the active one instead of freezing on whichever was active at the last
  // full page render.
  function setActiveSort(value) {
    document.querySelectorAll('.sort-pill').forEach(function (a) {
      const isActive = a.dataset.sort === value;
      a.classList.toggle('active', isActive);
      if (isActive) a.setAttribute('aria-current', 'true');
      else a.removeAttribute('aria-current');
    });
  }

  function load(url) {
    if (abortController) abortController.abort();
    abortController = new AbortController();
    const thisRequest = ++requestVersion;
    url = url || targetUrl();

    fetch(url, {
      signal: abortController.signal,
      cache: 'no-store',
      headers: { 'X-Requested-With': 'XMLHttpRequest' },
    })
      .then(function (res) {
        if (!res.ok) throw new Error('catalog fetch failed');
        return res.json();
      })
      .then(function (data) {
        if (thisRequest !== requestVersion) return;
        const openRegions = Array.from(
          filterForm.querySelectorAll('.rail-disclose[aria-expanded="true"]')
        ).map(function (button) { return button.getAttribute('aria-controls'); });

        categoryFacet.innerHTML = data.category_html;
        summary.innerHTML = data.summary_html;
        grid.innerHTML = data.grid_html;
        filterForm.dispatchEvent(new CustomEvent('catalog:facet-updated', {
          detail: { openRegions: openRegions },
        }));
        history.replaceState(null, '', url);
      })
      .catch(function (err) {
        // Network/parse failure — leave the current results as-is rather
        // than clearing them. A real reload (bookmark, refresh) still works
        // if this keeps failing.
        if (err.name !== 'AbortError') { /* no-op */ }
      });
  }

  if (searchForm && searchInput) {
    searchForm.addEventListener('submit', function (e) {
      e.preventDefault();
      clearTimeout(debounceTimer);
      load();
    });
    searchInput.addEventListener('input', function () {
      clearTimeout(debounceTimer);
      debounceTimer = setTimeout(function () { load(); }, DEBOUNCE_MS);
    });
  }

  if (filterForm) {
    // Every checkbox in the rail — ticking one IS "Apply" now; there is no
    // button left to press.
    filterForm.addEventListener('change', function (event) {
      if (event.target.matches('input[type="checkbox"]')) load();
    });

    // Clear filters is a plain link to a fresh (filter-less) URL for
    // JS-off. With JS running, a plain load() from it only ever refreshed
    // the grid+summary — the boxes themselves stayed checked, since nothing
    // else here ever un-ticks them, which is what made Clear look broken.
    // Un-ticking every box (and emptying the search field, which the
    // link's own URL already drops) before the fetch is what makes the
    // rail's own state match what Clear just asked for.
    filterForm.addEventListener('click', function (event) {
      const clearLink = event.target.closest('.rail-clear');
      if (clearLink && filterForm.contains(clearLink)) {
        event.preventDefault();
        filterForm.querySelectorAll('input[type="checkbox"]').forEach(function (box) {
          box.checked = false;
        });
        if (searchInput) searchInput.value = '';
        load(clearLink.getAttribute('href'));
      }
    });
  }

  // Sort pills: live too, and built from the rail's CURRENT state (see
  // targetUrl's comment) rather than the href's frozen filter_qs — clicking
  // Sort must never require re-picking a branch you already unticked live.
  document.querySelectorAll('.sort-pill').forEach(function (a) {
    a.addEventListener('click', function (e) {
      e.preventDefault();
      const value = a.dataset.sort;
      load(targetUrl(value));
      setActiveSort(value);
    });
  });
})();

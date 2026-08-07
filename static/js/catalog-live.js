// Live-updates the Products catalog (search box + category select) without
// a full-page reload. Debounced + abortable, same pattern as the header's
// live-search.js. Progressive enhancement: if this never runs, the form is
// plain method="GET" and submits normally — the server renders the exact
// same result via orders.customer_views.product_catalog.
(function () {
  const form = document.querySelector('form[data-catalog-live]');
  const summary = document.getElementById('result-summary');
  const grid = document.getElementById('pgrid');
  if (!form || !summary || !grid) return;

  const input = form.querySelector('input[type="search"]');
  const select = form.querySelector('select[name="category"]');
  if (!input) return;

  const DEBOUNCE_MS = 250;
  let debounceTimer = null;
  let abortController = null;

  function targetUrl() {
    const params = new URLSearchParams();
    if (input.value.trim()) params.set('q', input.value.trim());
    if (select && select.value) params.set('category', select.value);
    const qs = params.toString();
    return window.location.pathname + (qs ? '?' + qs : '');
  }

  function load() {
    if (abortController) abortController.abort();
    abortController = new AbortController();
    const url = targetUrl();

    fetch(url, {
      signal: abortController.signal,
      headers: { 'X-Requested-With': 'XMLHttpRequest' },
    })
      .then(function (res) {
        if (!res.ok) throw new Error('catalog fetch failed');
        return res.json();
      })
      .then(function (data) {
        summary.innerHTML = data.summary_html;
        grid.innerHTML = data.grid_html;
        history.replaceState(null, '', url);
      })
      .catch(function (err) {
        // Network/parse failure — leave the current results as-is rather
        // than clearing them; the user can still hit Search for a real reload.
        if (err.name !== 'AbortError') { /* no-op */ }
      });
  }

  form.addEventListener('submit', function (e) {
    e.preventDefault();
    clearTimeout(debounceTimer);
    load();
  });

  input.addEventListener('input', function () {
    clearTimeout(debounceTimer);
    debounceTimer = setTimeout(load, DEBOUNCE_MS);
  });

  if (select) {
    select.addEventListener('change', load);
  }
})();

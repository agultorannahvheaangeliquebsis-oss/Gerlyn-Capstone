// Live search dropdown for the storefront header. No build step, no deps.
(function () {
  const input = document.getElementById('live-search-input');
  const dropdown = document.getElementById('live-search-results');
  if (!input || !dropdown) return;

  const suggestUrl = input.dataset.suggestUrl;
  const DEBOUNCE_MS = 250;
  const MIN_CHARS = 2;

  let debounceTimer = null;
  let abortController = null;
  let results = [];
  let activeIndex = -1;

  function peso(value) {
    const n = parseFloat(value);
    return '₱' + (isNaN(n) ? value : n.toFixed(2));
  }

  function closeDropdown() {
    dropdown.hidden = true;
    dropdown.innerHTML = '';
    input.setAttribute('aria-expanded', 'false');
    activeIndex = -1;
  }

  function openDropdown() {
    dropdown.hidden = false;
    input.setAttribute('aria-expanded', 'true');
  }

  function render() {
    if (!results.length) {
      dropdown.innerHTML = '<div class="search-empty">No products found.</div>';
      openDropdown();
      return;
    }

    dropdown.innerHTML = results.map((item, i) => `
      <a href="${item.url}"
         class="search-result${i === activeIndex ? ' is-active' : ''}"
         role="option"
         data-index="${i}">
        <span class="search-result-name">${escapeHtml(item.name)}</span>
        <span class="search-result-meta">
          ${item.category ? `<span class="search-result-category">${escapeHtml(item.category)}</span>` : ''}
          <span class="search-result-price">${peso(item.price)}</span>
        </span>
      </a>
    `).join('');
    openDropdown();
  }

  function escapeHtml(str) {
    const div = document.createElement('div');
    div.textContent = str;
    return div.innerHTML;
  }

  async function fetchSuggestions(query) {
    if (abortController) abortController.abort();
    abortController = new AbortController();

    try {
      const res = await fetch(`${suggestUrl}?q=${encodeURIComponent(query)}`, {
        signal: abortController.signal,
        headers: { 'X-Requested-With': 'XMLHttpRequest' },
      });
      if (!res.ok) throw new Error('search request failed');
      const data = await res.json();
      results = data.results || [];
      activeIndex = -1;
      render();
    } catch (err) {
      if (err.name !== 'AbortError') closeDropdown();
    }
  }

  input.addEventListener('input', () => {
    const query = input.value.trim();
    clearTimeout(debounceTimer);

    if (query.length < MIN_CHARS) {
      closeDropdown();
      return;
    }

    debounceTimer = setTimeout(() => fetchSuggestions(query), DEBOUNCE_MS);
  });

  input.addEventListener('keydown', (e) => {
    if (dropdown.hidden || !results.length) return;

    if (e.key === 'ArrowDown') {
      e.preventDefault();
      activeIndex = Math.min(activeIndex + 1, results.length - 1);
      render();
    } else if (e.key === 'ArrowUp') {
      e.preventDefault();
      activeIndex = Math.max(activeIndex - 1, 0);
      render();
    } else if (e.key === 'Enter' && activeIndex >= 0) {
      e.preventDefault();
      window.location.href = results[activeIndex].url;
    } else if (e.key === 'Escape') {
      closeDropdown();
    }
  });

  document.addEventListener('click', (e) => {
    if (!e.target.closest('.search-box')) closeDropdown();
  });

  input.addEventListener('focus', () => {
    if (results.length && input.value.trim().length >= MIN_CHARS) openDropdown();
  });
})();

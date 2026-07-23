// Real-time, in-page filter for the products catalog. No round-trip: it hides
// the already-rendered cards as the user types. No build step, no deps.
(function () {
  const form = document.querySelector('form[data-catalog-filter]');
  const input = document.getElementById('catalog-search');
  const grid = document.getElementById('pgrid');
  const counter = document.getElementById('result-count');
  const noMatches = document.getElementById('no-matches');
  const noMatchesQuery = document.getElementById('no-matches-query');
  if (!form || !input || !grid) return;

  const cards = Array.from(grid.querySelectorAll('.pcard'));
  const total = cards.length;

  // The category <select> still submits to the server; the text box no longer
  // needs to, so don't reload the page when the user hits Enter.
  form.addEventListener('submit', (e) => e.preventDefault());

  function updateCount(visible) {
    if (!counter) return;
    counter.textContent = visible + ' item' + (visible === 1 ? '' : 's');
  }

  function filter() {
    const query = input.value.trim().toLowerCase();
    let visible = 0;

    for (const card of cards) {
      const match = !query || card.dataset.name.includes(query);
      card.hidden = !match;
      if (match) visible++;
    }

    if (noMatches) {
      const empty = total > 0 && visible === 0;
      noMatches.hidden = !empty;
      if (empty && noMatchesQuery) noMatchesQuery.textContent = input.value.trim();
    }

    updateCount(visible);
  }

  input.addEventListener('input', filter);

  // Reflect any pre-filled query (e.g. arriving from the header search).
  if (input.value.trim()) filter();
})();

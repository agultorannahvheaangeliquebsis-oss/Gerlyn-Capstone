// Grid / list layout toggle for the Products catalog.
//
// Purely a viewing preference: it changes no results and touches no query
// string, so it never round-trips to the server. The choice is remembered in
// localStorage so a shopper who prefers the list does not have to re-pick it
// on every visit.
//
// Progressive enhancement: with this script blocked the grid renders in its
// default layout and the two buttons simply do nothing visible — no results
// are hidden and nothing is unreachable.
(function () {
  var STORAGE_KEY = 'gvs:catalog-view';
  var toggle = document.querySelector('[data-view-toggle]');
  var grid = document.getElementById('pgrid');
  if (!toggle || !grid) return;

  var buttons = toggle.querySelectorAll('.view-btn');

  function apply(view) {
    grid.classList.toggle('is-list', view === 'list');
    for (var i = 0; i < buttons.length; i++) {
      var isOn = buttons[i].getAttribute('data-view') === view;
      buttons[i].classList.toggle('active', isOn);
      // aria-pressed, not just a class: the buttons carry an icon and an
      // aria-label, so without this a screen reader can name them but cannot
      // say which layout is currently on.
      buttons[i].setAttribute('aria-pressed', isOn ? 'true' : 'false');
    }
  }

  // Wrapped: a browser set to block site data throws on read rather than
  // returning null, and a catalog that will not render because of a layout
  // preference is a far worse failure than forgetting the preference.
  var stored = null;
  try {
    stored = localStorage.getItem(STORAGE_KEY);
  } catch (err) {
    stored = null;
  }
  if (stored === 'list' || stored === 'grid') apply(stored);

  toggle.addEventListener('click', function (e) {
    var btn = e.target.closest('.view-btn');
    if (!btn) return;
    var view = btn.getAttribute('data-view');
    apply(view);
    try {
      localStorage.setItem(STORAGE_KEY, view);
    } catch (err) {
      /* preference just won't persist; the toggle still worked */
    }
  });
})();

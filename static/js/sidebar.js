// Internal-system sidebar: icon-rail <-> full-panel collapse, expandable nav
// groups (the "file tree"), and the header account menu. Icons are
// Lucide, rendered client-side from the <i data-lucide="..."> markup in
// internal_base.html — this file also fires that render pass.
(function () {
  if (window.lucide) lucide.createIcons();

  var app = document.querySelector('.app');
  if (!app) return;
  var groupToggles = document.querySelectorAll('.sb-tree-toggle[data-target]');

  /* Sidebar dropdowns: remember which categories the user opened across page
     loads. The group holding the active page is always forced open (server
     renders `open` + `aria-expanded="true"`), so this only restores the
     *extra* groups a user chose to expand. Pure progressive enhancement —
     with JS off, the server-rendered state still expands the active group,
     it just won't animate or persist. */
  var OPEN_KEY = 'gv-nav-open';
  var openGroups;
  try { openGroups = JSON.parse(localStorage.getItem(OPEN_KEY)) || {}; } catch (e) { openGroups = {}; }

  function setGroupOpen(btn, open) {
    var branch = document.getElementById(btn.getAttribute('data-target'));
    if (!branch) return;
    btn.setAttribute('aria-expanded', open ? 'true' : 'false');
    branch.classList.toggle('open', open);
  }
  function persistOpen() {
    try { localStorage.setItem(OPEN_KEY, JSON.stringify(openGroups)); } catch (e) {}
  }

  groupToggles.forEach(function (btn) {
    var id = btn.getAttribute('data-target');
    if (openGroups[id] === true) setGroupOpen(btn, true);   // restore user's choice
  });

  /* Sidebar scroll position: every nav click is a normal <a href> that fully
     reloads the page (this is server-rendered, not a SPA), so without this
     the .sb-nav list snaps back to the top on every click even though the
     open/collapsed state above survives. sessionStorage (not localStorage)
     because this is per-browsing-session scroll, not a lasting preference.
     Restored after the open-groups pass above, since expanding a group
     changes .sb-nav's scrollHeight and could otherwise clamp the restore. */
  var SCROLL_KEY = 'gv-nav-scroll';
  var sbNav = document.querySelector('.sb-nav');
  if (sbNav) {
    var savedScroll = 0;
    try { savedScroll = parseInt(sessionStorage.getItem(SCROLL_KEY), 10) || 0; } catch (e) {}
    if (savedScroll) sbNav.scrollTop = savedScroll;

    var scrollSaveTimer;
    sbNav.addEventListener('scroll', function () {
      clearTimeout(scrollSaveTimer);
      scrollSaveTimer = setTimeout(function () {
        try { sessionStorage.setItem(SCROLL_KEY, String(sbNav.scrollTop)); } catch (e) {}
      }, 100);
    });
    window.addEventListener('pagehide', function () {
      try { sessionStorage.setItem(SCROLL_KEY, String(sbNav.scrollTop)); } catch (e) {}
    });
  }

  /* Sidebar collapse: remembers the collapsed/expanded state across page
     loads. A collapsed rail has nowhere to show a group's sub-items, so
     clicking its icon expands the sidebar back out (smooth width transition,
     see gerlyn.css) and opens *only* that group, instead of a flyout. */
  var COLLAPSE_KEY = 'gv-nav-collapsed';
  var toggle = document.getElementById('sidebarToggle');
  var collapsed = false;
  try { collapsed = localStorage.getItem(COLLAPSE_KEY) === '1'; } catch (e) {}
  applyCollapsed(collapsed);

  if (toggle) {
    toggle.addEventListener('click', function () {
      applyCollapsed(!app.classList.contains('sidebar-collapsed'));
      persistCollapsed();
    });
  }

  groupToggles.forEach(function (btn) {
    btn.addEventListener('click', function () {
      if (app.classList.contains('sidebar-collapsed')) {
        applyCollapsed(false);
        persistCollapsed();
        groupToggles.forEach(function (other) {
          var open = (other === btn);
          setGroupOpen(other, open);
          openGroups[other.getAttribute('data-target')] = open;
        });
        persistOpen();
        return;
      }
      var open = btn.getAttribute('aria-expanded') !== 'true';
      setGroupOpen(btn, open);
      openGroups[btn.getAttribute('data-target')] = open;
      persistOpen();
    });
  });

  function applyCollapsed(isCollapsed) {
    app.classList.toggle('sidebar-collapsed', isCollapsed);
    if (toggle) {
      toggle.setAttribute('aria-expanded', isCollapsed ? 'false' : 'true');
      toggle.setAttribute('aria-label', isCollapsed ? 'Expand sidebar' : 'Collapse sidebar');
      toggle.setAttribute('title', isCollapsed ? 'Expand sidebar' : 'Collapse sidebar');
    }
  }
  function persistCollapsed() {
    try { localStorage.setItem(COLLAPSE_KEY, app.classList.contains('sidebar-collapsed') ? '1' : '0'); } catch (e) {}
  }

  /* The bottom-nav "More" menu: close it when the user taps/clicks anywhere
     outside, since <details> only closes natively on re-toggle. The header
     avatar used to be handled here too; it is a plain link to My Profile now
     and has nothing to close. */
  document.addEventListener('click', function (e) {
    document.querySelectorAll('.more-menu[open]').forEach(function (d) {
      if (!d.contains(e.target)) d.open = false;
    });
  });
})();

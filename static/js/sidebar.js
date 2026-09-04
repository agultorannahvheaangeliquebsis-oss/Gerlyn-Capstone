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

  /* Sliding nav highlight ("magic move"): one absolutely-positioned element
     inside .sb-nav that slides and resizes to sit behind whichever row is
     hovered or keyboard-focused, and rests behind the active/current-page row
     when nothing is. It replaces the per-row hover wash, which popped in and
     out on each row on its own. The motion is a CSS transform/size transition
     (.sb-highlight in gerlyn.css); the global prefers-reduced-motion rule there
     already flattens every transition to ~0, so nothing extra is needed here.

     Because the highlight is an absolute child of the scrolling .sb-nav, it
     scrolls with the rows for free and offsetTop stays right — the only times
     it must re-measure are when a layout-changing transition finishes (sidebar
     collapse/expand, a group opening) or the window resizes. */
  var sidebarEl = document.getElementById('sidebar');
  if (sbNav) {
    var highlight = document.createElement('div');
    highlight.className = 'sb-highlight';
    highlight.setAttribute('aria-hidden', 'true');
    sbNav.insertBefore(highlight, sbNav.firstChild);

    var HL_SEL = '.sb-item, .sb-tree-toggle, .sb-tree-file';
    var hoverTarget = null;

    function placeHighlight(el, visible) {
      if (!el) return;
      highlight.style.transform =
        'translate(' + el.offsetLeft + 'px, ' + el.offsetTop + 'px)';
      highlight.style.width = el.offsetWidth + 'px';
      highlight.style.height = el.offsetHeight + 'px';
      highlight.style.opacity = visible ? '1' : '0';
    }

    function activeRow() {
      var active = sbNav.querySelector('.sb-item--active, .sb-tree-file--active');
      if (!active) return null;
      /* Collapsed: a group's files are display:none, so a selected file has no
         box to rest behind — use its group's icon, which is the row that
         carries the active tint at this width. */
      if (app.classList.contains('sidebar-collapsed') &&
          active.classList.contains('sb-tree-file')) {
        var root = active.closest('.sb-tree-root');
        return root ? root.querySelector('.sb-tree-toggle') : null;
      }
      return active;
    }

    /* Rest state: behind the hovered row if any, else the active row. With
       neither, park (invisibly) on the first row so the first hover slides in
       from a sane spot instead of growing out of the corner. */
    function settle() {
      var target = hoverTarget || activeRow();
      placeHighlight(target || sbNav.querySelector(HL_SEL), !!target);
    }

    sbNav.addEventListener('mouseover', function (e) {
      var el = e.target.closest(HL_SEL);
      if (el && el !== hoverTarget && sbNav.contains(el)) {
        hoverTarget = el;
        placeHighlight(el, true);
      }
    });
    sbNav.addEventListener('mouseleave', function () {
      hoverTarget = null;
      settle();
    });
    sbNav.addEventListener('focusin', function (e) {
      var el = e.target.closest(HL_SEL);
      if (el) { hoverTarget = el; placeHighlight(el, true); }
    });
    sbNav.addEventListener('focusout', function (e) {
      if (!sbNav.contains(e.relatedTarget)) { hoverTarget = null; settle(); }
    });

    if (sidebarEl) {
      sidebarEl.addEventListener('transitionend', function (e) {
        if (e.target === sidebarEl && e.propertyName === 'width') settle();
      });
    }
    sbNav.addEventListener('transitionend', function (e) {
      if (e.target.classList.contains('sb-tree') &&
          e.propertyName === 'grid-template-rows') settle();
    });

    var hlResizeTimer;
    window.addEventListener('resize', function () {
      clearTimeout(hlResizeTimer);
      hlResizeTimer = setTimeout(settle, 100);
    });

    requestAnimationFrame(settle);
    window.addEventListener('load', settle);
  }
})();

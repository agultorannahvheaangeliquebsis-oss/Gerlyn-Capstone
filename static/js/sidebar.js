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

  /* A closed branch is clipped to nothing by CSS, but its links stay in the
     layout and in the tab order, stacked under the group header - i.e. on top
     of the next group's row. Tabbing into one focused a link nobody could see
     and dragged the sliding highlight onto that hidden box, painting a green
     card across an unrelated row. `inert` takes the whole closed branch out of
     the tab order and out of hit-testing, and it flips the instant the group
     closes rather than after the collapse animation, so nothing can land there
     mid-transition either. The rows still animate shut normally - inert changes
     what they respond to, not how they are drawn. */
  function setGroupOpen(btn, open) {
    var branch = document.getElementById(btn.getAttribute('data-target'));
    if (!branch) return;
    btn.setAttribute('aria-expanded', open ? 'true' : 'false');
    branch.classList.toggle('open', open);
    branch.inert = !open;
  }
  function persistOpen() {
    try { localStorage.setItem(OPEN_KEY, JSON.stringify(openGroups)); } catch (e) {}
  }

  groupToggles.forEach(function (btn) {
    var id = btn.getAttribute('data-target');
    if (openGroups[id] === true) setGroupOpen(btn, true);   // restore user's choice
  });
  /* Seal every branch the server rendered closed - setGroupOpen above only ran
     for the ones being re-opened - so a page starts with no focusable links
     buried inside a collapsed group. */
  document.querySelectorAll('.sb-tree').forEach(function (branch) {
    branch.inert = !branch.classList.contains('open');
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

    /* The card is drawn 8px narrower than the row on its RIGHT side only, so
       its hairline and drop shadow have room to sit inside .sb-nav, which clips
       overflow-x (a full-bleed card's shadow gets shaved flush at the rail
       edge). The left is flush on purpose: the selected row's fill is painted
       on the row itself and runs the row's full width, so an inset hover card
       would start 8px right of it and leave a gap between the accent block and
       whichever card it is supposed to be touching. Sharing one left edge is
       what lets a single block meet both states (see .sb-item--active::before
       in gerlyn.css). It costs the card's left hairline on top-level rows,
       which sit flush against the .sb-nav clip — but that is the edge the
       block paints over, so there is nothing visible to lose. Skipped when
       collapsed — there the highlight is a bare 44px circle with no shadow
       (see .app.sidebar-collapsed .sb-highlight in gerlyn.css). */
    var HL_INSET_X = 8;

    /* A row inside a closed group has an offset box even though nothing is
       drawn there: the box sits under its group header, on top of the next
       group's row. Sizing the card from one puts a green block across a row
       it does not belong to - the bug this guard exists for. A closed branch is
       `inert` (see setGroupOpen), so nothing can hover or focus one any more
       either; this still catches a SELECTED file whose group is rendered
       closed, which no pointer or key has to touch. */
    function rowVisible(el) {
      var tree = el.closest('.sb-tree');
      return !tree || tree.classList.contains('open');
    }

    function placeHighlight(el, visible) {
      if (!el || !rowVisible(el)) { highlight.style.opacity = '0'; return; }
      var inset = app.classList.contains('sidebar-collapsed') ? 0 : HL_INSET_X;
      highlight.style.transform =
        'translate(' + el.offsetLeft + 'px, ' + el.offsetTop + 'px)';
      highlight.style.width = (el.offsetWidth - inset) + 'px';
      highlight.style.height = el.offsetHeight + 'px';
      highlight.style.opacity = visible ? '1' : '0';
    }

    function activeRow() {
      var active = sbNav.querySelector('.sb-item--active, .sb-tree-file--active');
      if (!active) return null;
      /* Collapsed: a group's files are display:none, so a selected file has no
         box to rest behind — use its group's icon, which is the row that
         carries the active tint at this width. */
      /* Same when the group is merely closed: the file is there but drawn
         nowhere, so the header that contains it is the row that can carry
         "you are here". */
      if (active.classList.contains('sb-tree-file') &&
          (app.classList.contains('sidebar-collapsed') || !rowVisible(active))) {
        var root = active.closest('.sb-tree-root');
        return root ? root.querySelector('.sb-tree-toggle') : null;
      }
      return active;
    }

    /* Rest state: behind the hovered row if any, else the active row. With
       neither, park (invisibly) on the first row so the first hover slides in
       from a sane spot instead of growing out of the corner.

       .sb-highlight--rest goes on whenever the card is over the current-page
       row (resting on it, or hovering it). It used to swap in a heavier shadow
       there; it now strips the shadow instead — the selected row is flat, and
       this class is what makes it flat, so it is still load-bearing. Because
       it is applied only when the target IS the active row, that flattening
       cannot leak onto an ordinary hovered row — see gerlyn.css. */
    function restToggle(target) {
      highlight.classList.toggle('sb-highlight--rest', !!target && target === activeRow());
    }
    function settle() {
      var target = hoverTarget || activeRow();
      restToggle(target);
      placeHighlight(target || sbNav.querySelector(HL_SEL), !!target);
    }

    sbNav.addEventListener('mouseover', function (e) {
      var el = e.target.closest(HL_SEL);
      if (el && el !== hoverTarget && sbNav.contains(el)) {
        hoverTarget = el;
        restToggle(el);
        placeHighlight(el, true);
      }
    });
    sbNav.addEventListener('mouseleave', function () {
      hoverTarget = null;
      settle();
    });
    /* Collapsing a group can pull the hovered row out from under the pointer
       (keyboard-activating the header while the mouse rests on one of its
       files does exactly that). No mouseover follows - the pointer never
       moved - so without this the card keeps a target that is no longer
       drawn. The header's own click listener is on the button and so has
       already flipped .open by the time this delegated one runs. */
    sbNav.addEventListener('click', function (e) {
      var btn = e.target.closest('.sb-tree-toggle');
      if (btn && hoverTarget && !rowVisible(hoverTarget)) {
        hoverTarget = btn;
        restToggle(btn);
        placeHighlight(btn, true);
      }
    });
    sbNav.addEventListener('focusin', function (e) {
      var el = e.target.closest(HL_SEL);
      if (el) { hoverTarget = el; restToggle(el); placeHighlight(el, true); }
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
    /* Back/forward restores from bfcache skip 'load' entirely and replay the
       page exactly as it was frozen — including the highlight's inline
       transform/opacity from whatever row the mouse was over when the user
       navigated away. Without this, that stale hover position sits behind a
       row forever, with the cursor nowhere near the sidebar, until the user
       happens to hover the nav again. */
    window.addEventListener('pageshow', function (e) {
      if (e.persisted) { hoverTarget = null; settle(); }
    });
  }
})();

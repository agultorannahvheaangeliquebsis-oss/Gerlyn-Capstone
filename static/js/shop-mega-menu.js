/* Shop mega-menu in the storefront's white nav row.
 *
 * Two panes: departments down the left, the highlighted department's shelves
 * filling the panel to the right.
 *
 * Opens on hover for a mouse — the reference opens as the pointer lands on
 * "Shop", no click needed. Click still opens it too, which is what keeps the
 * menu reachable on a touch screen (hover does not exist there) and from the
 * keyboard (Enter/Space on the focused button fires a click). Every hover
 * listener below is guarded to non-touch pointers so a tap's synthetic
 * "hover" never fights the click handler.
 *
 * The close on mouse-out is debounced. The trigger and the panel are separate,
 * non-overlapping elements (the panel is a sibling of the nav link row, not a
 * child of the trigger — .shop-links is an overflow scroller and would clip
 * it), so the instant the pointer crosses the seam between them it reads as
 * leaving one before entering the other. A short delay absorbs that gap
 * without making the menu feel sticky.
 *
 * Degrades to nothing. With this file absent the trigger is an inert button
 * and the panes stay `hidden`, which is why public_base.html keeps a real
 * <a> to the catalog on every department that has no shelves, and why each
 * pane leads with a real link to its own department.
 */
(function () {
  var menu = document.getElementById('shop-menu');
  if (!menu) return;

  var trigger = menu.querySelector('.shop-menu-trigger');
  var panel = document.getElementById('shop-menu-panel');
  if (!trigger || !panel) return;

  function inside(node) {
    return !!node && (menu.contains(node) || panel.contains(node));
  }

  var depts = Array.prototype.slice.call(panel.querySelectorAll('[data-mega-dept]'));
  var panes = Array.prototype.slice.call(panel.querySelectorAll('[data-mega-pane]'));

  /* Which department's pane is showing. Held rather than read back off the
   * DOM so that closing and reopening the menu returns to where the shopper
   * was, instead of snapping back to the first department every time. */
  var openDept = null;

  function showDept(id) {
    openDept = id;
    depts.forEach(function (d) {
      var on = d.getAttribute('data-mega-dept') === id;
      d.classList.toggle('is-active', on);
      /* Only the disclosure buttons carry aria-expanded; a department with no
       * shelves is a plain link and setting it there would be a lie. */
      if (d.hasAttribute('aria-expanded')) {
        d.setAttribute('aria-expanded', on ? 'true' : 'false');
      }
    });
    panes.forEach(function (p) {
      p.hidden = p.getAttribute('data-mega-pane') !== id;
    });
  }

  function firstDeptId() {
    return depts.length ? depts[0].getAttribute('data-mega-dept') : null;
  }

  function setOpen(open) {
    panel.classList.toggle('is-open', open);
    trigger.setAttribute('aria-expanded', open ? 'true' : 'false');
    if (open) {
      /* Open onto a pane rather than a blank slab beside the department
       * list — the reference lands on its first department highlighted. */
      showDept(openDept || firstDeptId());
    } else {
      /* Panes go back to hidden on close so their links leave the tab order.
       * The highlight state is deliberately left alone: openDept is what
       * brings the shopper back to the same pane next time. */
      panes.forEach(function (p) { p.hidden = true; });
      depts.forEach(function (d) {
        if (d.hasAttribute('aria-expanded')) d.setAttribute('aria-expanded', 'false');
      });
    }
  }

  function isOpen() {
    return panel.classList.contains('is-open');
  }

  /* ----------------------------------------------------------- hover to open
   * Mouse only. The 160ms close delay bridges the gap between the trigger and
   * the panel below it — see the file header. A pointer re-entering either
   * one cancels a pending close, so sliding from "Shop" down into the panel
   * (or back up) never flickers it shut. */
  var closeTimer = null;

  function cancelClose() {
    if (closeTimer) { clearTimeout(closeTimer); closeTimer = null; }
  }

  function scheduleClose() {
    cancelClose();
    closeTimer = setTimeout(function () { setOpen(false); }, 160);
  }

  [menu, panel].forEach(function (root) {
    root.addEventListener('pointerenter', function (e) {
      if (e.pointerType === 'touch') return;
      cancelClose();
      setOpen(true);
    });
    root.addEventListener('pointerleave', function (e) {
      if (e.pointerType === 'touch') return;
      scheduleClose();
    });
  });

  /* Click stays as the touch/keyboard path. On a mouse it also acts as an
   * explicit toggle — clicking "Shop" while it is hover-open closes it. */
  trigger.addEventListener('click', function (e) {
    e.stopPropagation();
    cancelClose();
    setOpen(!isOpen());
  });

  depts.forEach(function (d) {
    var id = d.getAttribute('data-mega-dept');

    /* A department button only ever switches panes — it never navigates and
     * never closes the menu, so the click that opens "Fresh" on a phone is
     * not also the click that leaves the page. The links inside the pane are
     * what navigate. */
    if (d.tagName === 'BUTTON') {
      d.addEventListener('click', function (e) {
        e.preventDefault();
        e.stopPropagation();
        showDept(id);
      });
    }

    /* Mouse preview, and keyboard parity: arrowing/tabbing onto a department
     * shows its pane for exactly the same reason hovering does. Guarded to
     * pointer devices so a touch "hover" (which fires on tap) does not race
     * the click handler above. */
    d.addEventListener('pointerenter', function (e) {
      if (e.pointerType === 'touch') return;
      showDept(id);
    });
    d.addEventListener('focus', function () {
      if (isOpen()) showDept(id);
    });
  });

  document.addEventListener('click', function (e) {
    if (!inside(e.target)) setOpen(false);
  });

  /* Escape closes and returns focus to the trigger — what keeps the menu
   * escapable from the keyboard alone rather than trapping a shopper who
   * opened it by mistake. */
  document.addEventListener('keydown', function (e) {
    if (e.key === 'Escape' && isOpen()) {
      setOpen(false);
      trigger.focus();
    }
  });

  /* Tabbing out of the last link in the menu closes it. Without this the
   * panel stays open over the page while focus has moved on to the cart,
   * which reads as the menu having stuck. focusout fires before the new
   * element takes focus, hence the deferral to relatedTarget. */
  [menu, panel].forEach(function (root) {
    root.addEventListener('focusout', function (e) {
      if (isOpen() && !inside(e.relatedTarget)) setOpen(false);
    });
  });
})();

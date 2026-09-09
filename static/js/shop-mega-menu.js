/* Shop mega-menu in the storefront's white nav row.
 *
 * Two panes: departments down the left, the highlighted department's shelves
 * filling the panel to the right.
 *
 * Click to open, not hover. A hover-only menu is unreachable on a touch
 * screen, and this row is the storefront's only path to a category on a
 * phone. Hover is layered on top for a mouse — moving across the department
 * list previews each pane the way the reference does — but nothing depends
 * on it: every pane is reachable by click and by keyboard alone.
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
  /* The panel is a SIBLING of the nav link row rather than a child of the
     trigger's wrapper — .shop-links is an overflow scroller and would clip
     it. So "inside the menu" is two elements, not one, and every containment
     test below has to ask both. */
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

  trigger.addEventListener('click', function (e) {
    e.stopPropagation();
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

/* Shop dropdown in the storefront's white nav row.
 *
 * One flat list of categories under "Shop" — no panes to switch between.
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
 * and the panel stays closed, which is why it renders as a real <a> list
 * that works perfectly well un-opened via "Shop" falling back to a plain
 * link when there are no categories at all.
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

  function setOpen(open) {
    panel.classList.toggle('is-open', open);
    trigger.setAttribute('aria-expanded', open ? 'true' : 'false');
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

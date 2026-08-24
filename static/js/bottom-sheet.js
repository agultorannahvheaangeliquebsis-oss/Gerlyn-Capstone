// Drives the bottom message sheet internal_base.html renders for Django
// messages tagged 'sheet' (.bottom-sheet[data-autoshow]) — slide-up entrance,
// tap-backdrop / [data-sheet-dismiss] / Escape / swipe-down-to-dismiss exit,
// each ending in .remove() so a stray hidden sheet never eats clicks.
(function () {
  var sheets = document.querySelectorAll('.bottom-sheet');
  if (!sheets.length) return;

  sheets.forEach(function (sheet) {
    var panel = sheet.querySelector('.bottom-sheet-panel');
    if (!panel) return;

    function open() {
      // rAF so the initial (closed) state paints first — without it the
      // browser can coalesce the class add into the same frame and skip
      // the transition entirely.
      requestAnimationFrame(function () {
        requestAnimationFrame(function () { sheet.classList.add('is-open'); });
      });
    }
    function close() {
      if (sheet.dataset.closing) return;
      sheet.dataset.closing = '1';
      sheet.classList.remove('is-open');
      panel.addEventListener('transitionend', function () { sheet.remove(); }, { once: true });
      // Belt-and-suspenders: reduced-motion / no-transition environments
      // never fire transitionend, so the sheet would otherwise linger
      // (invisible but still catching clicks) forever.
      setTimeout(function () { if (sheet.isConnected) sheet.remove(); }, 400);
    }

    if (sheet.dataset.autoshow) open();

    sheet.querySelectorAll('[data-sheet-dismiss]').forEach(function (btn) {
      btn.addEventListener('click', close);
    });
    // Tapping the dimmed backdrop (not the panel itself) also dismisses.
    sheet.addEventListener('click', function (e) {
      if (e.target === sheet) close();
    });
    document.addEventListener('keydown', function onKey(e) {
      if (e.key === 'Escape' && sheet.isConnected) close();
      if (!sheet.isConnected) document.removeEventListener('keydown', onKey);
    });

    // Swipe-down-to-dismiss: track the drag distance and either commit to
    // closing (past a threshold) or snap back.
    var startY = null, dragY = 0, dragging = false;
    panel.addEventListener('touchstart', function (e) {
      startY = e.touches[0].clientY;
      dragging = true;
      panel.style.transition = 'none';
    }, { passive: true });
    panel.addEventListener('touchmove', function (e) {
      if (!dragging) return;
      dragY = Math.max(0, e.touches[0].clientY - startY);
      panel.style.transform = 'translateY(' + dragY + 'px)';
    }, { passive: true });
    panel.addEventListener('touchend', function () {
      if (!dragging) return;
      dragging = false;
      panel.style.transition = '';
      panel.style.transform = '';
      if (dragY > 90) close();
      dragY = 0;
    });
  });
})();

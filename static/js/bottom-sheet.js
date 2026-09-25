// Drives the message modal partials/_flash_modal.html renders for every Django
// message and for a rejected form's errors (.bottom-sheet[data-autoshow]) —
// slide-up entrance, tap-backdrop / [data-sheet-dismiss] / Escape /
// swipe-down-to-dismiss exit, each ending in .remove() so a stray hidden sheet
// never eats clicks.
//
// It is a real modal: focus moves into it on open, Tab cannot leave it (the
// button is its only stop), the page behind cannot scroll, and focus goes back
// to where it was on close.
//
// window.showFlashSheet(message) raises the same success modal from script, for
// an action that finishes in place (fetch) and so has no page render to carry a
// Django message.
(function () {
  function bind(sheet) {
    var panel = sheet.querySelector('.bottom-sheet-panel');
    if (!panel) return;
    var lastFocused = document.activeElement;
    var confirmBtn = sheet.querySelector('[data-sheet-dismiss]');

    function open() {
      document.body.style.overflow = 'hidden';
      // rAF so the initial (closed) state paints first — without it the
      // browser can coalesce the class add into the same frame and skip
      // the transition entirely.
      requestAnimationFrame(function () {
        requestAnimationFrame(function () {
          sheet.classList.add('is-open');
          (confirmBtn || panel).focus({ preventScroll: true });
        });
      });
    }
    function close() {
      if (sheet.dataset.closing) return;
      sheet.dataset.closing = '1';
      sheet.classList.remove('is-open');
      // A dialog underneath (the notification View modal) keeps its own scroll lock.
      if (!document.querySelector('.modal-overlay.is-open')) document.body.style.overflow = '';
      if (lastFocused && lastFocused.focus && lastFocused.isConnected) lastFocused.focus();
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
      if (!sheet.isConnected) { document.removeEventListener('keydown', onKey); return; }
      if (e.key === 'Escape') close();
      // One focusable control, so "keep Tab inside" is just "keep focus on it".
      else if (e.key === 'Tab') { e.preventDefault(); (confirmBtn || panel).focus(); }
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
  }

  document.querySelectorAll('.bottom-sheet').forEach(bind);

  // Same markup as the success branch of partials/_flash_modal.html (title and
  // button copy match flash_summary's "success").
  window.showFlashSheet = function (message) {
    var sheet = document.createElement('div');
    sheet.className = 'bottom-sheet bottom-sheet--success';
    sheet.dataset.autoshow = '1';
    sheet.innerHTML =
      '<div class="bottom-sheet-panel" role="dialog" aria-modal="true" aria-label="Success" tabindex="-1">' +
        '<span class="bottom-sheet-icon" aria-hidden="true">' +
          '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><circle cx="12" cy="12" r="9"/><path d="m8.5 12.5 2.5 2.5 4.5-5"/></svg>' +
        '</span>' +
        '<h2 class="bottom-sheet-title">Success</h2>' +
        '<p></p>' +
        '<button type="button" class="btn btn-primary" data-sheet-dismiss>Done</button>' +
      '</div>';
    sheet.querySelector('p').textContent = message;
    document.body.appendChild(sheet);
    bind(sheet);
  };
})();

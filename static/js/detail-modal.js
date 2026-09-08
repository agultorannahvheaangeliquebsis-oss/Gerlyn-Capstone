/* The "see" action, as an overlay instead of a page.

   Every list in the internal system ends its rows with the same eye button —
   a withdrawal slip, a purchase order, an order, a billing record, a
   supplier. Each one used to navigate away: you left the list you were
   reading, looked at one record, then pressed Back and landed at the top of
   the list again, having lost the tab, the filter and the scroll position
   you got there with. Reading one row should not cost the page you were
   reading it from, so the record now opens on top of the list and closing it
   puts you back exactly where you were.

   One shell (#detail-modal, in internal_base.html) serves every module, and
   the record inside it is the detail page's own content block, fetched from
   the detail URL with ?modal=1 — see templates/partials/_modal_base.html.
   That is deliberately NOT a second copy of each record's markup: a field
   added to a detail page shows up in the modal because it is the same
   template, rendered by the same view, guarded by the same branch checks.

   The trigger is normally the eye button, but a bare click anywhere on its
   row opens the same record: an internal-system row carries one "see" action
   and nothing else worth clicking, so the whole strip of cells is a fair
   target. A click that lands on its own control — another link, a button, a
   form field — or one that finishes a text selection is left alone, and the
   eye button stays in place for keyboard users and as the visible affordance.

   Progressive enhancement, so nothing here is load-bearing:
     - the trigger stays a real <a href> to the full page. No JS (or a fetch
       that fails) means the click navigates, exactly as it did before.
     - ctrl/cmd/shift/middle clicks are left alone, so "open in new tab"
       still opens the page.
     - a redirect in the response — which is what the detail views do when
       the record belongs to another branch — is followed as a normal
       navigation, so the refusal message is actually seen instead of being
       swallowed into the overlay. */
(function () {
  'use strict';

  var overlay = null, card, titleEl, bodyEl, closeBtn;
  var lastFocused = null;
  var wired = false;

  var SKELETON =
    '<div class="detail-modal-loading" role="status">' +
      '<span class="detail-modal-spinner" aria-hidden="true"></span>' +
      '<span>Loading…</span>' +
    '</div>';

  function shell() {
    if (wired) return overlay;
    overlay = document.getElementById('detail-modal');
    if (!overlay) return null;
    card = overlay.querySelector('.modal-card');
    titleEl = document.getElementById('detail-modal-title');
    bodyEl = document.getElementById('detail-modal-body');
    closeBtn = document.getElementById('detail-modal-close');

    closeBtn.addEventListener('click', close);
    overlay.addEventListener('click', function (e) {
      if (e.target === overlay) close();
    });
    document.addEventListener('keydown', function (e) {
      if (e.key === 'Escape' && overlay.classList.contains('is-open')) close();
    });
    wired = true;
    return overlay;
  }

  function open(title) {
    // Only when the overlay is being raised from the page — not on a
    // modal-to-modal hop (Print PO from an already-open detail modal), where
    // document.activeElement is a control inside .modal-body that the next
    // line is about to destroy, leaving close() nothing real to focus.
    if (!overlay.classList.contains('is-open')) lastFocused = document.activeElement;
    titleEl.textContent = title || 'Details';
    bodyEl.innerHTML = SKELETON;
    overlay.classList.add('is-open');
    overlay.setAttribute('aria-hidden', 'false');
    document.body.style.overflow = 'hidden';
    // A hook for a fragment that needs to know it is being read inside the
    // overlay rather than on a page of its own — the printable purchase
    // order uses it to drop the list behind the overlay from window.print().
    document.body.classList.add('detail-modal-open');
    // The dialog itself takes focus, not the close button: Escape and Tab
    // both need focus inside the overlay, but landing it on Close draws a
    // focus ring around the one control whose job is to undo the click the
    // user just made. The card is tabindex="-1" for exactly this.
    card.focus();
  }

  function close() {
    overlay.classList.remove('is-open');
    overlay.setAttribute('aria-hidden', 'true');
    document.body.style.overflow = '';
    document.body.classList.remove('detail-modal-open');
    // Emptied on close, not on open: a stale record must never flash behind
    // the spinner the next time the overlay is raised.
    bodyEl.innerHTML = '';
    if (lastFocused && lastFocused.focus) lastFocused.focus();
    lastFocused = null;
  }

  function modalUrl(href) {
    return href + (href.indexOf('?') === -1 ? '?' : '&') + 'modal=1';
  }

  /* The fragment was written to be a page, so two things in it are addressed
     to a reader who navigated: the back link, which has no meaning in a
     dialog that closes, and the <h1>, which is the record's identity and
     belongs in the modal's own head rather than repeated under it. Both are
     handled here instead of behind an {% if %} in five templates — the page
     rendering stays the one canonical version. */
  function dress(fallbackTitle) {
    var head = bodyEl.querySelector('.page-head');
    var h1 = (head || bodyEl).querySelector('h1');
    var titleWrap = h1 ? h1.parentNode : null;
    if (h1) {
      titleEl.innerHTML = h1.innerHTML;
      h1.parentNode.removeChild(h1);
    } else {
      titleEl.textContent = fallbackTitle || 'Details';
    }

    var back = bodyEl.querySelector('.back-link');
    if (back) back.parentNode.removeChild(back);

    // The div that wrapped the back-link and <h1> is empty now that both are
    // gone. Left in place it still counts as a flex item in .page-head's
    // space-between, so a lone surviving control (Print PO) gets shoved to
    // the far edge with a dead gap where the title used to sit, instead of
    // reading as the one thing left in the head.
    if (titleWrap && titleWrap !== head && !titleWrap.children.length && !titleWrap.textContent.trim()) {
      titleWrap.parentNode.removeChild(titleWrap);
    }

    // Some page heads carry more than the title — Purchase Orders keeps its
    // Print PO button there, and that is still worth having in the modal. So
    // the head goes only once nothing actionable is left in it.
    if (head && !head.querySelector('a, button, form, input, select')) {
      head.parentNode.removeChild(head);
    }

    if (window.lucide && typeof window.lucide.createIcons === 'function') {
      window.lucide.createIcons();
    }
  }

  /* From a click, the eye button it should stand in for: the button itself
     when that is what was hit, otherwise the one in the surrounding row —
     unless the click was really meant for something else in that row. */
  function resolveTrigger(e) {
    if (!e.target.closest) return null;

    var direct = e.target.closest('a[data-detail-modal]');
    if (direct) return direct;

    var row = e.target.closest('tr');
    if (!row) return null;
    var rowLink = row.querySelector('a[data-detail-modal][href]');
    if (!rowLink) return null;

    // A control in the row that owns this click — another link, a button, a
    // form or a form field — keeps it. Only bare cell space falls through to
    // the row.
    var own = e.target.closest('a, button, input, select, textarea, label, form, [role="button"], [onclick]');
    if (own && own !== rowLink) return null;

    // A click that ended a drag-select of text in the row was after the text,
    // not the record.
    var sel = window.getSelection && window.getSelection();
    if (sel && sel.type === 'Range' && String(sel).trim()) return null;

    return rowLink;
  }

  // Point the cursor on rows a bare click will open — the hover tint the
  // list already carries is the other half of the hint. Left off when JS is
  // absent, which is right: the row does nothing then.
  function markRows() {
    var links = document.querySelectorAll('tr a[data-detail-modal][href]');
    for (var i = 0; i < links.length; i++) {
      var row = links[i].closest('tr');
      if (row) row.style.cursor = 'pointer';
    }
  }
  markRows();

  document.addEventListener('click', function (e) {
    var link = resolveTrigger(e);
    if (!link) return;
    // Anything but a plain left click is the user asking the browser for a
    // page — a new tab, a new window, a saved link. Let them have it.
    if (e.defaultPrevented || e.button !== 0 || e.metaKey || e.ctrlKey || e.shiftKey || e.altKey) return;
    if (!shell()) return;

    e.preventDefault();
    var href = link.getAttribute('href');
    var fallbackTitle = link.getAttribute('data-detail-modal') || link.getAttribute('title');
    open(fallbackTitle);

    fetch(modalUrl(href), {
      credentials: 'same-origin',
      headers: { 'X-Requested-With': 'XMLHttpRequest' }
    }).then(function (res) {
      // A redirect means the view refused (wrong branch, expired session) and
      // queued a message. Only the full page can show it.
      if (!res.ok || res.redirected) {
        window.location.href = href;
        return null;
      }
      return res.text();
    }).then(function (html) {
      if (html === null) return;
      bodyEl.innerHTML = html;
      dress(fallbackTitle);
      bodyEl.scrollTop = 0;
    }).catch(function () {
      // Offline, or the fetch was blocked. The link still points at a real
      // page, so fall back to being a link.
      close();
      window.location.href = href;
    });
  });
})();

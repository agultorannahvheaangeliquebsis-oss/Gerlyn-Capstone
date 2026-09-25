/* The confirmation step in front of a high-impact order status change —
   Mark Completed, Cancel Order, Void Sale on the Order Detail page.

   confirm-dialog.js is a one-question stop with no form fields of its own;
   this one has to show the order, say what the step will do to stock and
   payment, and collect a reason. So the dialog is rendered by the server
   (templates/partials/_order_status_confirm.html, filled from
   sales.views._status_confirmations) INSIDE the status form, hidden by CSS,
   and this script only opens and closes it. Because it sits inside the
   form, the reason field is an ordinary form control: nothing is copied and
   nothing can drift.

   Opt-in per form: form[data-status-confirm] that contains a
   [data-status-modal]. The trigger stays a real submit button; the first
   submit is stopped to open the dialog, and the dialog's own submit button
   lets the second one through.

   Loaded from internal_base.html and delegated from the document, not
   loaded by the page: Order Detail is also opened inside the shared
   detail-modal overlay (dashboard, billing, reports), and a script tag in a
   fetched fragment is never executed.

   No-JS behaviour: the dialog stays hidden and the form submits as it always
   did. That is safe by construction — transaction_set_status validates the
   transition, the payment gate and the cancellation reason itself, and a
   Cancelled request with no reason is refused with a message. This layer is
   about intent, not authorisation. */
(function () {
  'use strict';

  var active = null; // { form, overlay, trigger, bodyOverflow }

  function focusables(overlay) {
    return Array.prototype.filter.call(
      overlay.querySelectorAll('textarea, button'),
      function (el) { return !el.disabled && el.offsetParent !== null; }
    );
  }

  function open(form, overlay, trigger) {
    active = {
      form: form,
      overlay: overlay,
      trigger: trigger || null,
      // The dialog can be raised over the detail-modal overlay, which has
      // already locked page scroll — restore what was there, not ''.
      bodyOverflow: document.body.style.overflow
    };
    overlay.classList.add('is-open');
    overlay.setAttribute('aria-hidden', 'false');
    document.body.style.overflow = 'hidden';

    /* A required reason is the first thing to do, so the caret goes there.
       Otherwise focus lands on the dismiss button, not the confirming one:
       Enter is the key most likely to be already travelling when a dialog
       appears, and it should not be able to complete the step unread. */
    var reason = overlay.querySelector('textarea[data-reason-required]');
    (reason || overlay.querySelector('[data-status-cancel]')).focus();
  }

  function close() {
    if (!active) return;
    var overlay = active.overlay;
    overlay.classList.remove('is-open');
    overlay.setAttribute('aria-hidden', 'true');
    document.body.style.overflow = active.bodyOverflow;
    var trigger = active.trigger;
    active = null;
    if (trigger && trigger.focus) trigger.focus();
  }

  function showReasonError(overlay, message) {
    var box = overlay.querySelector('[data-reason-error]');
    var field = overlay.querySelector('textarea');
    if (box) { box.textContent = message; box.hidden = false; }
    if (field) { field.setAttribute('aria-invalid', 'true'); field.focus(); }
  }

  function clearReasonError(overlay) {
    var box = overlay.querySelector('[data-reason-error]');
    var field = overlay.querySelector('textarea');
    if (box) box.hidden = true;
    if (field) field.removeAttribute('aria-invalid');
  }

  /* First submit: stop it and draw the dialog. The dialog's own submit
     button sets form._statusConfirmed in its click handler below, which runs
     before this event, so the second submit passes straight through. */
  document.addEventListener('submit', function (e) {
    var form = e.target;
    if (!form.matches || !form.matches('form[data-status-confirm]')) return;
    if (form._statusConfirmed) { form._statusConfirmed = false; return; }
    var overlay = form.querySelector('[data-status-modal]');
    if (!overlay) return;

    e.preventDefault();
    open(form, overlay, e.submitter);
  }, true);

  document.addEventListener('click', function (e) {
    var t = e.target;
    if (!t.closest) return;

    if (t.closest('[data-status-cancel], [data-status-close]')) {
      e.preventDefault();
      close();
      return;
    }

    var ok = t.closest('[data-status-ok]');
    if (ok && active) {
      var overlay = active.overlay;
      var reason = overlay.querySelector('textarea');
      if (reason && reason.hasAttribute('data-reason-required') && !reason.value.trim()) {
        // The server refuses it too; this just says so before the round trip.
        e.preventDefault();
        showReasonError(overlay, 'Enter a reason before continuing.');
        return;
      }
      /* Let the submit through. Cleared on the next tick so a submit that
         never happens (validation, a blocked navigation) cannot leave the
         form waving the next trigger click past the dialog. */
      var form = active.form;
      form._statusConfirmed = true;
      setTimeout(function () { form._statusConfirmed = false; }, 0);
      return;
    }

    // A click on the dimmed backdrop itself, not on the card.
    if (active && t === active.overlay) close();
  });

  document.addEventListener('input', function (e) {
    if (active && e.target.matches && e.target.matches('textarea[data-reason-required]')) {
      clearReasonError(active.overlay);
    }
  });

  /* Capture phase + stopImmediatePropagation: when this dialog is raised over
     the detail-modal overlay, Escape must close only the dialog. Left to
     bubble, detail-modal.js's own Escape handler would close the order
     underneath it in the same keystroke. */
  document.addEventListener('keydown', function (e) {
    if (!active) return;
    if (e.key === 'Escape') {
      e.preventDefault();
      e.stopImmediatePropagation();
      close();
      return;
    }
    if (e.key !== 'Tab') return;

    var stops = focusables(active.overlay);
    if (!stops.length) return;
    var i = stops.indexOf(document.activeElement);
    e.preventDefault();
    var next = e.shiftKey
      ? (i <= 0 ? stops.length - 1 : i - 1)
      : (i === -1 || i === stops.length - 1 ? 0 : i + 1);
    stops[next].focus();
  }, true);
})();

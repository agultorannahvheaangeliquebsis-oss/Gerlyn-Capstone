/* The confirmation step for actions that cannot be taken back.

   Replaces window.confirm(), which five templates were calling directly. That
   worked, but it renders the browser's own dialog — a different typeface, a
   different button order per OS, no way to say WHICH record is about to go,
   and on mobile it prints the page origin above the question. This draws the
   same question in the design system instead.

   Opt-in per form, never global. A form is intercepted only if it carries
   data-confirm, so the ordinary save/add/edit paths keep submitting on the
   first click — a confirmation on a reversible action is just a second click
   with no information in it.

     data-confirm         the question body. REQUIRED — its presence is the
                          opt-in, and it should name the record.
     data-confirm-title   heading. Default "Are you sure?".
     data-confirm-variant "danger" (record is destroyed) or "warn" (state
                          change that cannot be undone here). Default danger.
     data-confirm-ok      label on the confirming button. Default "Confirm".
                          Write the verb — "Delete address" beats "OK",
                          because it is the last thing read before committing.
     data-confirm-dismiss label on the dismissing button. Default "Cancel".
                          Needed wherever the ACTION is itself a cancellation:
                          a dialog with "Cancel" next to "Cancel order" has
                          two buttons that read as the same instruction and
                          opposite outcomes. Those say "Keep order" instead.
     data-confirm-when    optional "field=value" guard. The dialog appears
                          only when the named field in that form currently
                          holds that value, so one <select> + Update button
                          can stop on Cancelled and stay quiet on Ordered.

   No-JS behaviour: the listener never attaches, the form submits as it did
   before. That is deliberate rather than a hole — every wired action is also
   guarded server-side (role checks, and the blocker counts on the four
   delete views), so this layer is about intent, not authorisation. */
(function () {
  'use strict';

  var ICONS = {
    danger: '<path d="M3 6h18M8 6V4a1 1 0 0 1 1-1h6a1 1 0 0 1 1 1v2m3 0v14a1 1 0 0 1-1 1H6a1 1 0 0 1-1-1V6"/><path d="M10 11v6M14 11v6"/>',
    warn: '<circle cx="12" cy="12" r="9"/><path d="M12 7v6M12 16.5v.01"/>'
  };

  var overlay, card, iconEl, titleEl, textEl, okBtn, cancelBtn, closeBtn;
  var pendingForm = null;
  var lastFocused = null;

  function build() {
    overlay = document.createElement('div');
    overlay.className = 'modal-overlay';
    overlay.innerHTML =
      '<div class="modal-card confirm-card" role="alertdialog" aria-modal="true"' +
      ' aria-labelledby="confirm-dialog-title" aria-describedby="confirm-dialog-text">' +
        '<div class="modal-head" style="border-bottom:none; padding-bottom:0; justify-content:flex-end;">' +
          '<button type="button" class="modal-close" data-confirm-close aria-label="Close">' +
            '<svg width="18" height="18" viewBox="0 0 24 24" fill="none" stroke="currentColor"' +
            ' stroke-width="2" stroke-linecap="round"><line x1="18" y1="6" x2="6" y2="18"/>' +
            '<line x1="6" y1="6" x2="18" y2="18"/></svg>' +
          '</button>' +
        '</div>' +
        '<div class="confirm-body">' +
          '<span class="confirm-icon" data-confirm-icon aria-hidden="true"></span>' +
          '<h2 class="confirm-title" id="confirm-dialog-title"></h2>' +
          '<p class="confirm-text" id="confirm-dialog-text"></p>' +
        '</div>' +
        '<div class="confirm-foot">' +
          '<button type="button" class="btn" data-confirm-cancel>Cancel</button>' +
          '<button type="button" class="btn" data-confirm-ok></button>' +
        '</div>' +
      '</div>';
    document.body.appendChild(overlay);

    card = overlay.querySelector('.confirm-card');
    iconEl = overlay.querySelector('[data-confirm-icon]');
    titleEl = overlay.querySelector('#confirm-dialog-title');
    textEl = overlay.querySelector('#confirm-dialog-text');
    okBtn = overlay.querySelector('[data-confirm-ok]');
    cancelBtn = overlay.querySelector('[data-confirm-cancel]');
    closeBtn = overlay.querySelector('[data-confirm-close]');

    cancelBtn.addEventListener('click', close);
    closeBtn.addEventListener('click', close);
    okBtn.addEventListener('click', commit);
    overlay.addEventListener('click', function (e) { if (e.target === overlay) close(); });
    document.addEventListener('keydown', onKeydown);
  }

  /* Tab must not walk out of an alertdialog and land on the page underneath,
     where the row's own Delete button is still sitting. Three stops only, so
     the cycle is cheap to compute by hand rather than by querying. */
  function onKeydown(e) {
    if (!overlay || !overlay.classList.contains('is-open')) return;
    if (e.key === 'Escape') { close(); return; }
    if (e.key !== 'Tab') return;

    var stops = [closeBtn, cancelBtn, okBtn];
    var i = stops.indexOf(document.activeElement);
    e.preventDefault();
    var next = e.shiftKey
      ? (i <= 0 ? stops.length - 1 : i - 1)
      : (i === -1 || i === stops.length - 1 ? 0 : i + 1);
    stops[next].focus();
  }

  function open(form, submitter) {
    pendingForm = form;
    pendingForm._confirmSubmitter = submitter || null;
    lastFocused = document.activeElement;

    var variant = form.dataset.confirmVariant === 'warn' ? 'warn' : 'danger';
    card.classList.remove('confirm-danger', 'confirm-warn');
    card.classList.add('confirm-' + variant);
    iconEl.innerHTML =
      '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"' +
      ' stroke-linecap="round" stroke-linejoin="round">' + ICONS[variant] + '</svg>';

    titleEl.textContent = form.dataset.confirmTitle || 'Are you sure?';
    textEl.textContent = form.dataset.confirm || '';
    okBtn.textContent = form.dataset.confirmOk || 'Confirm';
    okBtn.className = 'btn ' + (variant === 'danger' ? 'btn-danger-solid' : 'btn-primary');
    cancelBtn.textContent = form.dataset.confirmDismiss || 'Cancel';

    overlay.classList.add('is-open');
    document.body.style.overflow = 'hidden';
    /* Cancel takes focus, not Confirm. Enter is the key most likely to be
       already travelling when a dialog appears, and it should not be able to
       complete a deletion the user has not read yet. */
    cancelBtn.focus();
  }

  function close() {
    if (!overlay) return;
    overlay.classList.remove('is-open');
    document.body.style.overflow = '';
    pendingForm = null;
    if (lastFocused && lastFocused.focus) lastFocused.focus();
    lastFocused = null;
  }

  function commit() {
    var form = pendingForm;
    if (!form) return;
    var submitter = form._confirmSubmitter;

    close();
    form._confirmed = true;

    /* form.submit() drops the submitter's name/value, which a view reading
       request.POST.get('<button name>') would never see. None of the wired
       forms use a named submit button today, but carrying it costs one
       hidden input and stops this from breaking the first one that does. */
    if (submitter && submitter.name) {
      var relay = document.createElement('input');
      relay.type = 'hidden';
      relay.name = submitter.name;
      relay.value = submitter.value;
      form.appendChild(relay);
    }

    if (typeof form.requestSubmit === 'function' && !submitter) form.requestSubmit();
    else form.submit();
  }

  /* Guard for a form whose destructiveness depends on what is selected —
     procurement's single <select> covers Ordered, Received and Cancelled, and
     only Cancelled deserves a stop. */
  function shouldConfirm(form) {
    var when = form.dataset.confirmWhen;
    if (!when) return true;
    var split = when.indexOf('=');
    if (split === -1) return true;
    var field = form.elements[when.slice(0, split)];
    return !!field && field.value === when.slice(split + 1);
  }

  document.addEventListener('submit', function (e) {
    var form = e.target;
    if (!form.dataset || !form.dataset.confirm) return;
    if (form._confirmed) { form._confirmed = false; return; }
    if (!shouldConfirm(form)) return;

    e.preventDefault();
    if (!overlay) build();
    open(form, e.submitter);
  }, true);
})();

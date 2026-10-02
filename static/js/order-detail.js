/* Two controls on the staff Order Detail page: the status dropdown and the
   Record Payment dialog.

   Loaded from internal_base.html and delegated from the document, not loaded
   by the page: Order Detail is also opened inside the shared detail-modal
   overlay (dashboard, billing, reports), and a script tag in a fetched
   fragment is never executed. Same reason order-status-confirm.js works this
   way.

   Neither control is authoritative. transaction_set_status re-validates the
   transition and the payment gate; billing_create re-validates the method,
   reference, proof and that the record is still Pending. This only decides
   what is drawn and saves a round trip. */
(function () {
  'use strict';

  /* ---------------------------------------------------------- status picker
     The dropdown lists the steps the order may take; each option's value is
     the id of that step's own <form> (its own transaction_set_status URL, its
     own confirmation dialog). The Update button is a submit button whose
     `form` attribute is pointed at the picked one, so the submit event fires
     on that form and order-status-confirm.js takes it from there. */
  document.addEventListener('change', function (e) {
    var select = e.target;
    if (!select.matches || !select.matches('[data-status-select]')) return;
    var apply = select.parentNode.querySelector('[data-status-apply]');
    if (!apply) return;
    if (select.value) {
      apply.setAttribute('form', select.value);
      apply.disabled = false;
    } else {
      apply.removeAttribute('form');
      apply.disabled = true;
    }
  });

  /* ------------------------------------------------------ Record Payment */

  var active = null; // { modal, trigger, bodyOverflow }

  function list(modal, attr) {
    var raw = modal.getAttribute(attr) || '';
    return raw ? raw.split('|') : [];
  }

  /* Show the reference / proof fields only for the methods that need them,
     and require exactly then. A proof already on the record satisfies the
     rule, so it is not demanded again. style.display rather than el.hidden:
     gerlyn.css sets .field { display: flex }, which outranks [hidden]. */
  function syncFields(modal) {
    var method = modal.querySelector('[data-payment-method]');
    if (!method) return;
    var needsRef = list(modal, 'data-reference-methods').indexOf(method.value) !== -1;
    var needsProof = list(modal, 'data-proof-methods').indexOf(method.value) !== -1;
    var refField = modal.querySelector('[data-payment-reference-field]');
    var proofField = modal.querySelector('[data-payment-proof-field]');
    refField.style.display = needsRef ? '' : 'none';
    var refInput = refField.querySelector('input');
    refInput.required = needsRef;
    // GCash's 13-digit rule, with the inline warning (gcash-reference.js).
    if (window.GerlynGCashRef) window.GerlynGCashRef.sync(refInput, method.value === 'GCash');
    proofField.style.display = needsProof ? '' : 'none';
    proofField.querySelector('input').required =
      needsProof && !modal.getAttribute('data-proof-on-file');
  }

  function focusables(modal) {
    return Array.prototype.filter.call(
      modal.querySelectorAll('select, input, button, a[href]'),
      function (el) { return !el.disabled && el.offsetParent !== null; }
    );
  }

  /* Save starts disabled and only unlocks once every required field the
     current method demands is actually filled — the native Constraint
     Validation API already knows this, since syncFields keeps `required`
     in step with what is shown. */
  function syncSaveState(modal) {
    var form = modal.querySelector('[data-payment-form]');
    var save = modal.querySelector('[data-payment-save]');
    if (form && save) save.disabled = !form.checkValidity();
  }

  function open(modal, trigger) {
    if (active) {
      if (active.modal === modal) return;
      // Opened from inside another dialog (Billing's View proof -> Replace
      // proof): swap one for the other, and hand focus back to whatever
      // opened the first dialog once this one closes.
      var outer = active.trigger;
      close();
      trigger = outer || trigger;
    }
    active = { modal: modal, trigger: trigger || null, bodyOverflow: document.body.style.overflow };
    syncFields(modal);
    syncSaveState(modal);
    modal.classList.add('is-open');
    modal.setAttribute('aria-hidden', 'false');
    document.body.style.overflow = 'hidden';
    // A dialog with no form (View proof) starts on its close button.
    var first = modal.querySelector('[data-payment-method]') || modal.querySelector('[data-payment-close]');
    if (first) first.focus();
  }

  function close() {
    if (!active) return;
    var modal = active.modal;
    modal.classList.remove('is-open');
    modal.setAttribute('aria-hidden', 'true');
    document.body.style.overflow = active.bodyOverflow;
    var trigger = active.trigger;
    active = null;
    if (trigger && trigger.focus) trigger.focus();
  }

  /* Same rule as billing._validate_proof_of_payment, so a wrong file is
     caught before the upload rather than after a page reload. */
  var PROOF_TYPES = ['image/jpeg', 'image/png', 'image/webp'];

  /* The thumbnail is a local preview only — nothing is uploaded until Save.
     One object URL is ever live at a time (one modal, one proof field), so
     it is revoked before the next one replaces it rather than leaked. */
  var thumbUrl = null;

  function updateThumb(modal, file, ok) {
    var thumb = modal.querySelector('[data-payment-thumb]');
    if (!thumb) return;
    if (thumbUrl) { URL.revokeObjectURL(thumbUrl); thumbUrl = null; }
    if (file && ok) {
      thumbUrl = URL.createObjectURL(file);
      thumb.src = thumbUrl;
      thumb.hidden = false;
    } else {
      thumb.removeAttribute('src');
      thumb.hidden = true;
    }
  }

  function checkProof(input) {
    var modal = input.closest('[data-payment-modal]');
    var box = modal.querySelector('[data-payment-file-error]');
    var file = input.files && input.files[0];
    var maxMb = parseFloat(modal.getAttribute('data-proof-max-mb')) || 5;
    var problem = '';
    if (file) {
      if (PROOF_TYPES.indexOf(file.type) === -1) {
        problem = 'Only JPEG, PNG, and WEBP images are allowed.';
      } else if (file.size > maxMb * 1024 * 1024) {
        problem = 'File must be under ' + maxMb + ' MB.';
      }
    }
    input.setCustomValidity(problem);
    box.textContent = problem;
    box.hidden = !problem;
    updateThumb(modal, file, !problem);
    var nameEl = modal.querySelector('.dropzone-filename');
    if (nameEl) nameEl.textContent = (file && !problem) ? file.name : '';
  }

  document.addEventListener('click', function (e) {
    var t = e.target;
    if (!t.closest) return;

    var opener = t.closest('[data-payment-open]');
    if (opener) {
      // A modified click keeps its normal meaning (open the form in a new tab).
      if (e.ctrlKey || e.metaKey || e.shiftKey || e.button === 1) return;
      var modal = document.getElementById(opener.getAttribute('data-payment-open'));
      if (!modal) return; // no dialog on the page: follow the link to the full form
      e.preventDefault();
      open(modal, opener);
      return;
    }

    if (t.closest('[data-payment-close]')) {
      e.preventDefault();
      close();
      return;
    }

    // A click on the dimmed backdrop itself, not on the card.
    if (active && t === active.modal) close();
  });

  document.addEventListener('change', function (e) {
    var el = e.target;
    if (!el.matches) return;
    var modal = el.closest('[data-payment-modal]');
    if (!modal) return;
    if (el.matches('[data-payment-method]')) syncFields(modal);
    else if (el.matches('input[type=file]')) checkProof(el);
    syncSaveState(modal);
  });

  /* Reference number, and any other typed field, unlock Save as soon as
     they're valid rather than only on blur. */
  document.addEventListener('input', function (e) {
    var modal = e.target.closest && e.target.closest('[data-payment-modal]');
    if (modal) syncSaveState(modal);
  });

  /* Drag-and-drop onto the proof dropzone, scoped to the payment modal so it
     doesn't interfere with Product/Category's own separate dropzones. Same
     three-listener shape as product_form.html's, lifted to delegated
     listeners since this dialog can be included on more than one page. */
  ['dragenter', 'dragover'].forEach(function (evt) {
    document.addEventListener(evt, function (e) {
      var zone = e.target.closest && e.target.closest('[data-payment-modal] .dropzone');
      if (!zone) return;
      e.preventDefault();
      zone.classList.add('is-dragover');
    });
  });
  ['dragleave', 'drop'].forEach(function (evt) {
    document.addEventListener(evt, function (e) {
      var zone = e.target.closest && e.target.closest('[data-payment-modal] .dropzone');
      if (!zone) return;
      e.preventDefault();
      zone.classList.remove('is-dragover');
    });
  });
  document.addEventListener('drop', function (e) {
    var zone = e.target.closest && e.target.closest('[data-payment-modal] .dropzone');
    if (!zone) return;
    var modal = zone.closest('[data-payment-modal]');
    var input = modal && modal.querySelector('input[type=file]');
    var files = e.dataTransfer && e.dataTransfer.files;
    if (input && files && files.length) {
      input.files = files;
      checkProof(input);
      syncSaveState(modal);
    }
  });

  /* One click, one save: the button locks once the browser has accepted the
     form, so a double-click cannot post the same payment twice. Deferred a
     tick so the submit itself is not cancelled by the disable. */
  document.addEventListener('submit', function (e) {
    var form = e.target;
    if (!form.matches || !form.matches('[data-payment-form]')) return;
    var save = form.querySelector('[data-payment-save]');
    if (save) setTimeout(function () { save.disabled = true; }, 0);
  });

  /* Capture phase + stopImmediatePropagation, like order-status-confirm.js:
     when raised over the detail-modal overlay, Escape closes only this
     dialog, not the order underneath it in the same keystroke. */
  document.addEventListener('keydown', function (e) {
    if (!active) return;
    if (e.key === 'Escape') {
      e.preventDefault();
      e.stopImmediatePropagation();
      close();
      return;
    }
    if (e.key !== 'Tab') return;
    var stops = focusables(active.modal);
    if (!stops.length) return;
    var i = stops.indexOf(document.activeElement);
    var next = e.shiftKey
      ? (i <= 0 ? stops.length - 1 : i - 1)
      : (i === -1 || i === stops.length - 1 ? 0 : i + 1);
    e.preventDefault();
    stops[next].focus();
  }, true);

  /* billing_create sends a refused save back here with ?record_payment=1, so
     the dialog reopens over the message. The flag is dropped from the URL so
     a refresh does not reopen it. */
  function reopenAfterRefusal() {
    var params = new URLSearchParams(window.location.search);
    if (params.get('record_payment') !== '1') return;
    var modal = document.getElementById('record-payment-modal');
    if (modal) open(modal, null);
    params.delete('record_payment');
    var query = params.toString();
    window.history.replaceState(
      null, '', window.location.pathname + (query ? '?' + query : '') + window.location.hash
    );
  }

  if (document.readyState === 'loading') {
    document.addEventListener('DOMContentLoaded', reopenAfterRefusal);
  } else {
    reopenAfterRefusal();
  }
})();

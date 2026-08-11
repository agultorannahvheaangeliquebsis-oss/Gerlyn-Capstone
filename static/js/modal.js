/* Shared wiring for a modal: open/close triggers, overlay-click and Escape
   to dismiss, auto-open on load, and an optional dropzone with drag & drop
   plus a filename readout.

   Lifted out of product_list.html when Add Category moved into a modal too —
   the function was already fully parameterised (every dropzone option is
   optional), so both pages call the same copy rather than each carrying one.

   Loaded WITHOUT defer in internal_base.html, deliberately. Pages call this
   from an inline <script> in {% block extra_js %} at the end of <body>, and an
   inline script runs during parsing while a deferred one waits for the whole
   document — so deferring this would have it define initModal after the pages
   that call it have already run. It only declares a function at load time and
   touches no DOM, so blocking on it costs nothing. */
function initModal(opts) {
  var modal = document.getElementById(opts.modalId);
  if (!modal) return;

  var openers = (opts.openerIds || []).map(function (id) { return document.getElementById(id); });
  var closers = (opts.closerIds || []).map(function (id) { return document.getElementById(id); });

  function openModal() {
    modal.classList.add('is-open');
    document.body.style.overflow = 'hidden';
  }
  function closeModal() {
    modal.classList.remove('is-open');
    document.body.style.overflow = '';
  }

  openers.forEach(function (btn) {
    if (btn) btn.addEventListener('click', openModal);
  });
  closers.forEach(function (btn) {
    if (btn) btn.addEventListener('click', closeModal);
  });
  modal.addEventListener('click', function (e) {
    if (e.target === modal) closeModal();
  });
  document.addEventListener('keydown', function (e) {
    if (e.key === 'Escape' && modal.classList.contains('is-open')) closeModal();
  });

  // Auto-open: either the server flagged a failed submit (data-autoopen),
  // or the user landed on the add/edit URL directly and got redirected here
  // with ?add=1 / ?edit=<pk>.
  var params = new URLSearchParams(window.location.search);
  if (modal.dataset.autoopen === '1' || (opts.autoopenParam && params.get(opts.autoopenParam) === '1')) {
    openModal();
  }

  // Dropzone: click-to-browse is free via the <label for>, this layers on
  // real drag & drop plus a filename readout.
  var dropzone = document.getElementById(opts.dropzoneId);
  var fileInput = document.getElementById(opts.fileInputId);
  var filenameEl = document.getElementById(opts.filenameId);

  function showFilenames(files) {
    if (!filenameEl) return;
    if (!files || !files.length) { filenameEl.textContent = ''; return; }
    filenameEl.textContent = files.length === 1
      ? files[0].name
      : files.length + ' files selected';
  }

  if (dropzone && fileInput) {
    fileInput.addEventListener('change', function () {
      showFilenames(fileInput.files);
    });
    ['dragenter', 'dragover'].forEach(function (evt) {
      dropzone.addEventListener(evt, function (e) {
        e.preventDefault();
        dropzone.classList.add('is-dragover');
      });
    });
    ['dragleave', 'drop'].forEach(function (evt) {
      dropzone.addEventListener(evt, function (e) {
        e.preventDefault();
        dropzone.classList.remove('is-dragover');
      });
    });
    dropzone.addEventListener('drop', function (e) {
      var files = e.dataTransfer.files;
      if (files && files.length) {
        fileInput.files = files;
        showFilenames(files);
      }
    });
  }
}

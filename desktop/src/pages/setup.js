(function () {
  'use strict';
  var $ = function (id) { return document.getElementById(id); };
  var input = $('url');
  var errorEl = $('error');
  var saveBtn = $('save');
  var forceBtn = $('force');
  var cancelBtn = $('cancel');
  var lockedEl = $('locked');

  function showError(text) {
    errorEl.textContent = text || '';
    errorEl.hidden = !text;
  }

  window.gerlynSetup.info().then(function (info) {
    if (info.origin) {
      input.value = info.origin;
      cancelBtn.hidden = false; // there is already a working server to go back to
    }
    if (info.locked) {
      input.disabled = true;
      saveBtn.disabled = true;
      lockedEl.textContent = 'This address is set by the GERLYN_SERVER_URL environment variable '
        + 'and cannot be changed here.';
      lockedEl.hidden = false;
    }
  });

  function submit(force) {
    showError('');
    saveBtn.disabled = true;
    forceBtn.disabled = true;
    window.gerlynSetup.save(input.value, force).then(function (result) {
      if (result && result.ok) return; // main process navigates away
      showError((result && result.error) || 'That address could not be used.');
      forceBtn.hidden = !(result && result.unreachable);
    }).catch(function () {
      showError('Something went wrong while saving. Please try again.');
    }).then(function () {
      saveBtn.disabled = false;
      forceBtn.disabled = false;
    });
  }

  $('form').addEventListener('submit', function (ev) { ev.preventDefault(); submit(false); });
  forceBtn.addEventListener('click', function () { submit(true); });
  cancelBtn.addEventListener('click', function () { window.gerlynSetup.cancel(); });
  input.addEventListener('input', function () { forceBtn.hidden = true; });
})();

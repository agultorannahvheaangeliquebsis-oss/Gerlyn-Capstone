// Reusable barcode scanning, shared by every scan-enabled field in the app
// (POS quick-add, Inventory/Products search, and the Barcode field on the
// Add Product / Stock Adjustment forms).
//
// Hardware USB/Bluetooth scanners need no special code at all — they're
// keyboard-emulating (HID) devices that "type" the scanned value followed
// by Enter into whatever field has focus, so any plain text input already
// accepts them; this module just listens for that Enter as "a scan (or a
// manual entry) is complete."
//
// In-app camera scanning uses html5-qrcode (https://github.com/mebjas/html5-qrcode,
// MIT licensed, loaded via <script> in internal_base.html — see that file
// for the CDN tag) rather than the browser's native BarcodeDetector API,
// specifically because BarcodeDetector has no support on iOS Safari, and
// this needs to work on Android and iOS alike.
window.GerlynBarcodeScanner = (function () {
  function supportsCamera() {
    return typeof Html5Qrcode !== 'undefined'
      && !!(navigator.mediaDevices && navigator.mediaDevices.getUserMedia);
  }

  // Wires `input`: pressing Enter calls onScan(value) and clears the field
  // — used where the field's job is "add one item, then scan the next"
  // (POS, list-page search boxes). If `cameraBtn` is given and the browser
  // supports it, clicking it opens a live camera overlay and calls
  // onScan(code) on the first decode.
  function attach(input, { onScan, cameraBtn } = {}) {
    if (!input || typeof onScan !== 'function') return;

    input.addEventListener('keydown', function (e) {
      if (e.key !== 'Enter') return;
      e.preventDefault();
      const value = input.value.trim();
      if (!value) return;
      input.value = '';
      onScan(value);
    });

    wireCameraButton(cameraBtn, onScan);
  }

  // Wires a standalone "Barcode" form field (Add Product / Stock
  // Adjustment): typing/scanning a code and pressing Enter — or a
  // successful camera scan — triggers a server lookup against existing
  // products, reports the outcome via `statusEl`, and calls onFound(data)
  // or onNotFound(code). Unlike attach(), the field's value is NOT
  // cleared afterward — it's a real value the form submits.
  //
  // No debounce, no per-keystroke validation: a plain `keydown` listener
  // for Enter is the only handler, so rapid scanner input (a whole code
  // arriving in a handful of milliseconds) is never delayed or dropped.
  function attachBarcodeField(input, { lookupUrl, cameraBtn, statusEl, onFound, onNotFound, notFoundMessage } = {}) {
    if (!input || !lookupUrl) return;
    notFoundMessage = notFoundMessage || 'No existing product matches this code.';

    let keydownTimestamps = [];
    let inFlight = null;

    function setStatus(text, kind) {
      if (!statusEl) return;
      statusEl.textContent = text;
      statusEl.className = 'barcode-status' + (kind ? ' barcode-status-' + kind : '');
    }

    function lookup(code, { isScan } = {}) {
      if (!code) return;
      if (inFlight) inFlight.abort();
      const controller = new AbortController();
      inFlight = controller;

      setStatus('Looking up…', 'pending');

      fetch(lookupUrl + '?code=' + encodeURIComponent(code), {
        signal: controller.signal,
        headers: { 'X-Requested-With': 'XMLHttpRequest' },
      })
        .then(function (res) { return res.json(); })
        .then(function (data) {
          if (data.found) {
            setStatus('✓ Match found: ' + data.product_name, isScan ? 'success-scan' : 'success');
            if (typeof onFound === 'function') onFound(data);
          } else {
            setStatus(notFoundMessage, 'info');
            if (typeof onNotFound === 'function') onNotFound(code);
          }
        })
        .catch(function (err) {
          if (err.name === 'AbortError') return;
          setStatus('Could not look up this code — check your connection and try again.', 'error');
        });
    }

    // Enter = "scan complete" (hardware scanner or manual typing + Enter).
    // Fast-keystroke detection: scanners "type" a whole code in a few
    // milliseconds, humans take 100ms+ per character — used only for a
    // distinct success flash, never to block or slow down input.
    input.addEventListener('keydown', function (e) {
      if (e.key === 'Enter') {
        e.preventDefault();
        const value = input.value.trim();
        if (!value) return;
        const elapsed = keydownTimestamps.length > 1
          ? keydownTimestamps[keydownTimestamps.length - 1] - keydownTimestamps[0]
          : Infinity;
        const isScan = value.length > 1 && (elapsed / value.length) < 35;
        keydownTimestamps = [];
        lookup(value, { isScan: isScan });
        return;
      }
      keydownTimestamps.push(performance.now());
    });

    wireCameraButton(cameraBtn, function (code) {
      input.value = code;
      lookup(code, { isScan: true });
    });
  }

  function wireCameraButton(cameraBtn, onCode) {
    if (!cameraBtn) return;

    if (!supportsCamera()) {
      cameraBtn.disabled = true;
      cameraBtn.title = "Camera scanning isn't supported in this browser — use a hardware scanner or type the code instead.";
      return;
    }

    cameraBtn.addEventListener('click', function () {
      openCameraScanner(onCode);
    });
  }

  function openCameraScanner(onDecoded) {
    const overlay = document.createElement('div');
    overlay.className = 'barcode-scan-overlay';
    overlay.innerHTML =
      '<div class="barcode-scan-panel">' +
        '<div class="barcode-scan-head">' +
          '<span>Scan a barcode</span>' +
          '<button type="button" class="barcode-scan-close" aria-label="Close">&times;</button>' +
        '</div>' +
        '<div id="barcode-scan-reader"></div>' +
        '<p class="barcode-scan-hint">Point the camera at a barcode or QR code.</p>' +
      '</div>';
    document.body.appendChild(overlay);

    const hint = overlay.querySelector('.barcode-scan-hint');
    // formatsToSupport is a constructor-time option, not a start() option —
    // passing it to start() is silently ignored by html5-qrcode, so the
    // scanner would otherwise fall back to decoding every supported format.
    const html5QrCode = new Html5Qrcode('barcode-scan-reader', {
      formatsToSupport: [
        Html5QrcodeSupportedFormats.QR_CODE,
        Html5QrcodeSupportedFormats.UPC_A,
        Html5QrcodeSupportedFormats.UPC_E,
        Html5QrcodeSupportedFormats.EAN_13,
        Html5QrcodeSupportedFormats.EAN_8,
        Html5QrcodeSupportedFormats.CODE_128,
      ],
      // Where available (Chrome/Android), decode via the browser's native
      // BarcodeDetector instead of the bundled zxing-js fallback — zxing-js
      // alone was failing to lock onto real 1D product barcodes (UPC/EAN)
      // even with a clear, well-lit camera feed. Harmless on iOS Safari
      // (no BarcodeDetector support): html5-qrcode silently falls back to
      // zxing-js there, same as before.
      experimentalFeatures: { useBarCodeDetectorIfSupported: true },
    });
    let stopped = false;

    // html5-qrcode's stop() can reject — or even throw synchronously — if
    // called before start() has actually finished initializing (e.g. the
    // user closes the overlay while the camera permission prompt is still
    // up). The overlay must disappear immediately either way, so it's
    // removed synchronously here and camera teardown happens separately
    // (see `starting` below), never gating the UI on stop()'s outcome.
    let starting = true;

    function safeStopCamera() {
      try {
        html5QrCode.stop().catch(function () {});
      } catch (e) { /* scanner was never fully running — nothing to release */ }
    }

    function stop() {
      if (stopped) return;
      stopped = true;
      overlay.remove();
      if (!starting) safeStopCamera();
      // else: onStartSettled below calls safeStopCamera() once start()
      // finishes, since it can't be stopped mid-initialization.
    }

    overlay.querySelector('.barcode-scan-close').addEventListener('click', stop);
    overlay.addEventListener('click', function (e) { if (e.target === overlay) stop(); });
    document.addEventListener('keydown', function esc(e) {
      if (e.key === 'Escape') { stop(); document.removeEventListener('keydown', esc); }
    });

    const config = {
      fps: 10,
      // A square box suits QR codes but clips wide 1D barcodes (UPC/EAN/
      // Code128) at the edges, which is why real product barcodes weren't
      // decoding even with a steady, well-lit camera feed. Size the box
      // off the actual viewfinder instead of a fixed square, and make it
      // noticeably wider than tall so a whole UPC/EAN fits inside it.
      qrbox: function (viewfinderWidth, viewfinderHeight) {
        const minEdge = Math.min(viewfinderWidth, viewfinderHeight);
        const boxWidth = Math.floor(Math.min(viewfinderWidth * 0.85, minEdge * 1.4));
        const boxHeight = Math.floor(boxWidth * 0.55);
        return { width: boxWidth, height: boxHeight };
      },
    };

    html5QrCode
      .start(
        { facingMode: 'environment' },
        config,
        function onScanSuccess(decodedText) {
          if (stopped) return;
          stopped = true;
          overlay.remove();
          onDecoded(decodedText);
          safeStopCamera();
        },
        function onScanFailure() { /* fires continuously while no code is in frame — expected, not an error */ }
      )
      .then(function () {
        starting = false;
        if (stopped) safeStopCamera();  // closed while the camera was still starting up
      })
      .catch(function () {
        starting = false;
        if (!stopped) hint.textContent = 'Could not access the camera — check permissions, or use a hardware scanner / type the code instead.';
      });
  }

  return { attach: attach, attachBarcodeField: attachBarcodeField, supportsCamera: supportsCamera };
})();

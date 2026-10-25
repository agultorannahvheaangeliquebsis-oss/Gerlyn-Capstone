/* A drawn-signature field: a canvas the person signs on with a finger, stylus
   or mouse, whose PNG ends up in a hidden input the form posts.

   Markup (templates/sales/receipt_confirm_form.html):
     <div data-signature>
       <canvas data-signature-canvas></canvas>
       <input type="hidden" name="signature_data" data-signature-input>
       <button type="button" data-signature-clear>Clear</button>
     </div>

   The hidden input stays empty until something is drawn, and goes back to
   empty on Clear, so an untouched pad posts "no signature". A value already in
   the input when the page loads (the form came back with an error) is drawn
   onto the pad, so the signer does not have to sign again.

   Nothing here is trusted by the server: sales.receipts.decode_signature
   re-checks that it is a real, small PNG with ink on it. No-JS behaviour: the
   canvas is inert and the form simply posts without a signature, which is
   optional.

   Pointer events cover touch, pen and mouse in one code path. The canvas has
   touch-action: none in CSS, otherwise a finger stroke scrolls the page
   instead of drawing. */
(function () {
  'use strict';

  var INK = '#111111';
  var LINE_WIDTH = 2.5;

  function init(root) {
    var canvas = root.querySelector('[data-signature-canvas]');
    var input = root.querySelector('[data-signature-input]');
    var clear = root.querySelector('[data-signature-clear]');
    if (!canvas || !input || !canvas.getContext) return;

    var ctx = canvas.getContext('2d');
    var ratio = window.devicePixelRatio || 1;
    var box = canvas.getBoundingClientRect();
    // Backing store at device resolution so the stroke is sharp on a phone;
    // CSS keeps the displayed size. Pointer positions are scaled back below.
    canvas.width = Math.max(1, Math.round(box.width * ratio));
    canvas.height = Math.max(1, Math.round(box.height * ratio));
    ctx.lineWidth = LINE_WIDTH * ratio;
    ctx.lineCap = 'round';
    ctx.lineJoin = 'round';
    ctx.strokeStyle = INK;

    var drawing = false;
    var inked = false;
    var last = null;

    function point(e) {
      var rect = canvas.getBoundingClientRect();
      return {
        x: (e.clientX - rect.left) * (canvas.width / rect.width),
        y: (e.clientY - rect.top) * (canvas.height / rect.height)
      };
    }

    function save() {
      input.value = inked ? canvas.toDataURL('image/png') : '';
    }

    function start(e) {
      drawing = true;
      last = point(e);
      if (canvas.setPointerCapture) {
        try { canvas.setPointerCapture(e.pointerId); } catch (err) { /* synthetic event */ }
      }
      // A tap leaves a dot, not nothing.
      ctx.beginPath();
      ctx.arc(last.x, last.y, ctx.lineWidth / 2, 0, Math.PI * 2);
      ctx.fillStyle = INK;
      ctx.fill();
      inked = true;
      e.preventDefault();
    }

    function move(e) {
      if (!drawing) return;
      var p = point(e);
      ctx.beginPath();
      ctx.moveTo(last.x, last.y);
      ctx.lineTo(p.x, p.y);
      ctx.stroke();
      last = p;
      e.preventDefault();
    }

    function end() {
      if (!drawing) return;
      drawing = false;
      last = null;
      save();
    }

    canvas.addEventListener('pointerdown', start);
    canvas.addEventListener('pointermove', move);
    canvas.addEventListener('pointerup', end);
    canvas.addEventListener('pointercancel', end);
    canvas.addEventListener('pointerleave', end);

    if (clear) {
      clear.addEventListener('click', function () {
        ctx.clearRect(0, 0, canvas.width, canvas.height);
        inked = false;
        input.value = '';
      });
    }

    // Come back with a signature already drawn (server-side error).
    if (input.value) {
      var img = new Image();
      img.onload = function () {
        ctx.drawImage(img, 0, 0, canvas.width, canvas.height);
        inked = true;
      };
      img.src = input.value;
    }
  }

  document.addEventListener('DOMContentLoaded', function () {
    Array.prototype.forEach.call(document.querySelectorAll('[data-signature]'), init);
  });
})();

// Storefront toast notifications. public_base.html renders each Django message
// into #toast-stack; this slides them in, auto-dismisses them, and wires the
// "Undo" button (still used by any server-rendered undoable message) to the
// undo endpoint. It also handles Add-to-Cart forms over fetch so the page
// never reloads and never jumps back to the top.
//
// A SUCCESSFUL add no longer raises a toast: it is confirmed on the button
// itself, which turns to a tick in solid dark green for two seconds and then
// turns back into the cart glyph — see markAdded() below. A FAILED one still
// toasts, because a refusal has a reason and the button has no room to give
// one. No build, no deps.
(function () {
  const stack = document.getElementById('toast-stack');
  if (!stack) return;

  const VISIBLE_MS = 4500;   // how long a toast lingers before it leaves
  const ADDED_MS = 2000;     // how long the tick stays before the cart returns
  // Per-button timers, keyed by the element: a grid can have several buttons
  // counting down at once, and a WeakMap lets a card the catalog swaps out
  // mid-countdown be collected instead of pinned by its own timer entry.
  const addedTimers = new WeakMap();
  const csrfToken = document
    .querySelector('meta[name="csrf-token"]')
    ?.getAttribute('content') || '';

  function dismiss(toast) {
    if (toast.dataset.leaving) return;       // guard against double-dismiss
    toast.dataset.leaving = '1';
    toast.classList.remove('is-visible');
    toast.classList.add('is-leaving');
    toast.addEventListener('transitionend', () => toast.remove(), { once: true });
  }

  function updateCartBadge(count) {
    const cartBtn = document.querySelector('.cart-btn');
    if (!cartBtn) return;
    let badge = cartBtn.querySelector('.shop-icon-badge');
    if (count > 0) {
      if (!badge) {
        badge = document.createElement('span');
        badge.className = 'shop-icon-badge';
        cartBtn.appendChild(badge);
      }
      badge.textContent = count;
    } else if (badge) {
      badge.remove();
    }
  }

  // Confirm the add ON the button: the cart glyph becomes a tick and the
  // button goes solid dark green (see .btn-cart--add.is-added in
  // storefront.css). This replaces the success toast, which said the same
  // thing in a panel that covered the corner of the page and then took it
  // away again — the state now lives on the control that caused it.
  //
  // And it hands the button back after ADDED_MS. The tick is a receipt for
  // one press, not a record of what the cart holds: left up, a grid a shopper
  // has been through reads as a wall of ticks with no cart glyph left to
  // press, and the mark stops meaning "that worked" the moment it means "this
  // is in your cart". The header badge carries what the cart holds, and it
  // does not expire.
  function markAdded(btn) {
    if (!btn) return;
    btn.classList.add('is-added');
    // A repeat press restarts the two seconds rather than stacking a second
    // timer that would clear the tick early, mid-way through the new one.
    clearTimeout(addedTimers.get(btn));
    addedTimers.set(btn, setTimeout(() => {
      btn.classList.remove('is-added');
      addedTimers.delete(btn);
    }, ADDED_MS));

    // Restart the pulse even if the button is already in the added state, so
    // a second add on the same card is visibly a second add.
    btn.classList.remove('is-added-now');
    void btn.offsetWidth;                      // force a reflow to re-trigger
    btn.classList.add('is-added-now');
    btn.addEventListener('animationend', () => btn.classList.remove('is-added-now'), { once: true });
  }

  // The tick is the whole confirmation and the tick is purely visual, so the
  // add has to be said out loud somewhere: this polite live region is what a
  // screen reader gets in place of the toast that used to be announced.
  //
  // It is also why the button's aria-label stays "Add <product> to cart"
  // throughout. Renaming the control to "Added" for two seconds would tell a
  // reader what happened in the one place that is supposed to say what
  // pressing it does — and then rename it back under their cursor.
  let liveRegion;
  function announce(message) {
    if (!liveRegion) {
      liveRegion = document.createElement('p');
      liveRegion.className = 'sr-only';
      liveRegion.setAttribute('aria-live', 'polite');
      document.body.appendChild(liveRegion);
    }
    // Clear first, on a tick of its own: assigning the same string twice is
    // not a change, and adding the same product again would announce nothing.
    liveRegion.textContent = '';
    setTimeout(() => { liveRegion.textContent = message; }, 60);
  }

  async function undo(toast) {
    const url = toast.dataset.undoUrl;
    if (!url) return;
    try {
      const res = await fetch(url, {
        method: 'POST',
        headers: {
          'X-Requested-With': 'XMLHttpRequest',
          'X-CSRFToken': csrfToken,
        },
      });
      if (!res.ok) throw new Error('undo request failed');
      const data = await res.json();
      updateCartBadge(data.cart_count);
      dismiss(toast);
    } catch (err) {
      // If the fetch can't complete, fall back to a normal navigation so the
      // customer still lands on a correct page with the item removed.
      window.location.href = url;
    }
  }

  // Attach entrance, auto-dismiss, hover-pause, and button handlers to a toast
  // node — used for both the server-rendered messages and ones raised live.
  function wireToast(toast, i) {
    let timer;
    const startTimer = () => { timer = setTimeout(() => dismiss(toast), VISIBLE_MS); };
    const stopTimer = () => clearTimeout(timer);

    // Stagger the entrance so stacked toasts cascade instead of popping at once.
    setTimeout(() => toast.classList.add('is-visible'), 60 + i * 90);
    startTimer();

    // Pause the countdown while the pointer is hovering the toast.
    toast.addEventListener('mouseenter', stopTimer);
    toast.addEventListener('mouseleave', startTimer);

    toast.querySelector('[data-toast-close]')
      ?.addEventListener('click', () => { stopTimer(); dismiss(toast); });
    toast.querySelector('[data-undo]')
      ?.addEventListener('click', () => { stopTimer(); undo(toast); });
  }

  // Build a toast node matching public_base.html's markup, append it, wire it.
  //
  // Live toasts are refusals now, nothing else, so this builds a plain one
  // with no actions in it: "Undo" and "View cart" were the success toast's
  // buttons, and the success toast is gone — an add that worked is confirmed
  // on the button by markAdded(). undo() above is still reached, but only
  // from the undoable message public_base.html renders when the add went
  // through a normal form POST because JavaScript never ran.
  //
  // role="alert", not the "status" the server-rendered messages use: this
  // one always says why something the shopper just asked for did NOT happen
  // ("Only 3 left at this branch"), and it takes itself away again after
  // VISIBLE_MS whether or not a polite announcement has had its turn.
  function showToast({ message, level = 'error' }) {
    const toast = document.createElement('div');
    toast.className = `toast toast-${level}`;
    toast.setAttribute('role', 'alert');
    toast.dataset.toast = '';
    toast.innerHTML =
      '<span class="toast-dot" aria-hidden="true"></span>' +
      '<div class="toast-body"><p class="toast-msg"></p></div>' +
      '<button type="button" class="toast-close" data-toast-close aria-label="Dismiss">&times;</button>';
    // textContent, not innerHTML, so a product name can never inject markup.
    toast.querySelector('.toast-msg').textContent = message;

    stack.appendChild(toast);
    wireToast(toast, 0);
    return toast;
  }

  // Wire the toasts Django rendered on this page load.
  stack.querySelectorAll('[data-toast]').forEach(wireToast);

  // Intercept Add-to-Cart submissions: POST over fetch and toast in place so
  // the page keeps its scroll position instead of reloading to the top.
  // Delegated on `document` (not queried once at load) so cards the catalog
  // page swaps in later via live search still get the same behavior.
  document.addEventListener('submit', async (e) => {
    const form = e.target.closest('form[data-add-to-cart]');
    if (!form) return;
    // "Buy Now" shares this form but wants the server's redirect to the cart,
    // so it opts out of the fetch path and submits normally.
    if (e.submitter && e.submitter.hasAttribute('data-skip-ajax')) return;
    e.preventDefault();
    const btn = form.querySelector('button[type="submit"]');
    if (btn) btn.disabled = true;
    try {
      const res = await fetch(form.action, {
        method: 'POST',
        headers: {
          'X-Requested-With': 'XMLHttpRequest',
          'X-CSRFToken': csrfToken,
        },
        body: new FormData(form),
      });
      const data = await res.json();
      if (!res.ok || !data.ok) {
        // Errors still toast. "Only 3 left at this branch" is a REASON, and
        // there is nowhere on a card to put one — the button state below can
        // say that something happened, never why something didn't.
        showToast({ message: data.error || 'Could not add to cart.', level: 'error' });
        return;
      }
      updateCartBadge(data.cart_count);
      markAdded(btn);
      announce(data.message || 'Added to your cart.');
    } catch (err) {
      // Network/JSON failure — fall back to a normal form submit so the add
      // still goes through (accepting the reload) rather than silently dropping.
      form.submit();
      return;
    } finally {
      if (btn) btn.disabled = false;
    }
  });
})();

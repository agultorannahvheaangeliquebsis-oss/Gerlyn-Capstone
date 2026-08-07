// Storefront toast notifications. public_base.html renders each Django message
// into #toast-stack; this slides them in, auto-dismisses them, and wires the
// "Undo" button on the Add-to-Cart toast to the undo endpoint. It also handles
// Add-to-Cart forms over fetch so the page never reloads (and never jumps back
// to the top) — raising the confirmation toast in place. No build, no deps.
(function () {
  const stack = document.getElementById('toast-stack');
  if (!stack) return;

  const VISIBLE_MS = 4500;   // how long a toast lingers before it leaves
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
    let badge = cartBtn.querySelector('.cart-badge');
    if (count > 0) {
      if (!badge) {
        badge = document.createElement('span');
        badge.className = 'cart-badge';
        cartBtn.appendChild(badge);
      }
      badge.textContent = count;
    } else if (badge) {
      badge.remove();
    }
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
  function showToast({ message, level = 'success', undoUrl = '' }) {
    const toast = document.createElement('div');
    toast.className = `toast toast-${level}`;
    toast.setAttribute('role', 'status');
    toast.dataset.toast = '';
    if (undoUrl) toast.dataset.undoUrl = undoUrl;

    const undoBtn = undoUrl
      ? '<button type="button" class="toast-undo" data-undo>Undo</button>'
      : '';
    toast.innerHTML =
      '<span class="toast-dot" aria-hidden="true"></span>' +
      '<div class="toast-body"><p class="toast-msg"></p>' + undoBtn + '</div>' +
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
        showToast({ message: data.error || 'Could not add to cart.', level: 'error' });
        return;
      }
      updateCartBadge(data.cart_count);
      showToast({ message: data.message, undoUrl: data.undo_url });
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

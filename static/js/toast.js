// Storefront toast notifications. public_base.html renders each Django message
// into #toast-stack; this slides them in, auto-dismisses them, and wires the
// "Undo" button on the Add-to-Cart toast to the undo endpoint. No build, no deps.
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

  stack.querySelectorAll('[data-toast]').forEach((toast, i) => {
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
  });
})();

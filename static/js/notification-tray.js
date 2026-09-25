/*
 * notification-tray.js — the floating notification tray.
 *
 * Progressive enhancement over a plain link: #notifBell's href already goes
 * to a full notifications page (core/views.py:notification_center_view for
 * the internal Owner/Manager/Staff tray, procurement/views.py:
 * supplier_notifications for the supplier portal's own). This script
 * intercepts the click and opens #notifTray in place instead, fetching the
 * same URL with X-Requested-With so the view returns a JSON fragment
 * (list_html + counts) rather than a full page — same convention
 * static/js/toast.js and orders/customer_views.py's live catalog search
 * already use. If the fetch fails, the click falls through to the normal
 * link.
 *
 * Shared by two different DOM shapes: internal_base.html's tray has
 * category-pills/mark-all, the supplier portal's is just bell+list+dismiss —
 * every optional element below is null-checked so either can omit pieces it
 * has no use for.
 *
 * Search lives only on the full page (core/notification_center.html) now —
 * the tray is a quick glance, not a place to type. See that page's own
 * inline script for its search+category filtering.
 *
 * Runs on every page that includes it (internal_base.html loads it
 * unconditionally; templates/procurement/supplier_portal.html does too), so
 * it re-initializes fresh on each server-rendered navigation rather than
 * persisting tray state across pages — this is a multi-page app, not an SPA.
 */
(function () {
  var bell = document.getElementById('notifBell');
  var tray = document.getElementById('notifTray');
  if (!bell || !tray) return;

  var list = document.getElementById('notifList');
  var badge = document.getElementById('notifBadge');
  var pills = document.getElementById('notifPills');
  var markAllBtn = document.getElementById('notifMarkAll');
  var scrim = document.getElementById('notifScrim');
  var anchor = bell.closest('.notif-tray-anchor');

  var csrfToken = document
    .querySelector('meta[name="csrf-token"]')
    ?.getAttribute('content') || '';

  var activeFilter = 'all';
  var loaded = false;

  function setBadge(count) {
    if (!badge) {
      if (count > 0) {
        badge = document.createElement('span');
        badge.className = 'nav-badge';
        badge.id = 'notifBadge';
        bell.appendChild(badge);
      } else {
        return;
      }
    }
    badge.textContent = String(count);
    badge.classList.remove('pulse');
    void badge.offsetWidth; // restart the animation
    badge.classList.add('pulse');
    if (count <= 0) {
      badge.remove();
      badge = null;
    }
  }

  function applyFilters() {
    var cards = list.querySelectorAll('.tray-ncard');
    var visible = 0;
    cards.forEach(function (card) {
      var cat = card.dataset.cat || '';
      var show = activeFilter === 'all' || cat.indexOf(activeFilter) !== -1;
      card.hidden = !show;
      if (card.classList.contains('tray-ncard-group')) {
        var children = card.nextElementSibling;
        if (children && children.classList.contains('tray-group-children')) {
          children.hidden = !show;
        }
      }
      if (show) visible++;
    });
    var filterEmpty = list.querySelector('.tray-filter-empty');
    var hadCards = cards.length > 0;
    if (filterEmpty) filterEmpty.hidden = !hadCards || visible > 0;
  }

  function openTray() {
    tray.classList.add('open');
    tray.setAttribute('aria-hidden', 'false');
    bell.setAttribute('aria-expanded', 'true');
    fetchList();
  }

  function closeTray() {
    tray.classList.remove('open');
    tray.setAttribute('aria-hidden', 'true');
    bell.setAttribute('aria-expanded', 'false');
  }

  function isOpen() {
    return tray.classList.contains('open');
  }

  function fetchList() {
    // Re-fetched on every open (not cached across opens) — the list is
    // small (see feedback-gerlyn-conventions: no pagination anywhere yet at
    // current data volume) and this guarantees what's shown is never stale
    // from an action taken on another page since the tray last opened.
    fetch(bell.href, {
      headers: { 'X-Requested-With': 'XMLHttpRequest' },
    })
      .then(function (res) {
        if (!res.ok) throw new Error('notification fetch failed');
        return res.json();
      })
      .then(function (data) {
        list.innerHTML = data.list_html;
        loaded = true;
        activeFilter = 'all';
        if (pills) {
          pills.querySelectorAll('.filter-tab').forEach(function (p) {
            p.classList.toggle('active', p.dataset.filter === 'all');
          });
        }
        applyFilters();
        setBadge(data.needs_action_count);
      })
      .catch(function () {
        list.innerHTML = '<div class="tray-empty">Couldn\'t load notifications. <a href="' + bell.href + '">Open the full list</a>.</div>';
      });
  }

  bell.addEventListener('click', function (e) {
    e.preventDefault();
    if (isOpen()) {
      closeTray();
    } else {
      openTray();
    }
  });

  document.addEventListener('click', function (e) {
    if (isOpen() && !anchor.contains(e.target)) closeTray();
  });
  document.addEventListener('keydown', function (e) {
    if (e.key === 'Escape' && isOpen()) closeTray();
  });
  if (scrim) {
    scrim.addEventListener('click', closeTray);
  }

  if (pills) {
    pills.addEventListener('click', function (e) {
      var pill = e.target.closest('.filter-tab');
      if (!pill) return;
      pills.querySelectorAll('.filter-tab').forEach(function (p) { p.classList.remove('active'); });
      pill.classList.add('active');
      activeFilter = pill.dataset.filter;
      applyFilters();
    });
  }

  // Grouped Low-priority row: expand/collapse, no fetch (children are
  // already server-rendered alongside the row — see
  // templates/core/_notification_tray_list.html). Delegated on `document`,
  // not `list`, so this works both for the tray's dynamically-fetched
  // content and for the full page's server-rendered #notifFeed.
  document.addEventListener('click', function (e) {
    var group = e.target.closest('.tray-ncard-group');
    if (group) group.classList.toggle('expanded');
  });

  // Both actions leave a notification read (resolving also reads it), so the
  // row's status becomes Read and its action forms are spent. Announced as an
  // event so the page's View modal and tab tints can follow.
  function settleRow(row) {
    row.classList.add('is-read');
    var status = row.querySelector('td[data-label="Status"]');
    if (status) status.innerHTML = '<span class="pill pill-ok">Read</span>';
    row.querySelectorAll('.tray-resolve-form, .tray-read-form').forEach(function (f) { f.remove(); });
    document.dispatchEvent(new CustomEvent('notif:row-updated', { detail: { row: row } }));
  }

  // Resolve/Dismiss: real <form> posts (see the partial) intercepted here so
  // they update in place instead of reloading the page. Delegated on
  // `document`, covering the tray and the full page with one listener —
  // matches the toast.js/catalog form-intercept convention (fetch + JSON,
  // X-Requested-With) elsewhere in this codebase.
  document.addEventListener('submit', function (e) {
    var form = e.target;
    var isResolve = form.classList.contains('tray-resolve-form');
    var isRead = form.classList.contains('tray-read-form');
    if (!isResolve && !isRead) return;
    // confirm-dialog.js (capture phase) has already stopped this submit to ask
    // first; it re-submits on Confirm, and that second event is the one to act on.
    if (e.defaultPrevented) return;

    e.preventDefault();
    var card = form.closest('.tray-ncard');
    var btn = form.querySelector('button[type="submit"]');
    if (btn) btn.disabled = true;

    fetch(form.action, {
      method: 'POST',
      headers: {
        'X-Requested-With': 'XMLHttpRequest',
        'X-CSRFToken': csrfToken,
      },
      body: new FormData(form),
    })
      .then(function (res) { return res.json(); })
      .then(function (data) {
        setBadge(data.needs_action_count);
        if (!card) return;
        if (card.tagName === 'TR') {
          // Full-page table row: stay on the page and show the new state in
          // place — vanishing would disagree with what a reload shows.
          settleRow(card);
          // Resolving happens in place, so nothing else says it worked:
          // acknowledge it with the success modal.
          if (isResolve && window.showFlashSheet) {
            window.showFlashSheet(form.dataset.resolvedMessage || 'Marked as resolved.');
          }
          return;
        }
        if (isResolve) {
          // Resolved: the card's job here is done, shrink it out.
          card.style.transition = 'opacity .18s ease, max-height .2s ease, margin .2s ease, padding .2s ease';
          card.style.maxHeight = card.offsetHeight + 'px';
          requestAnimationFrame(function () {
            card.style.opacity = '0';
            card.style.maxHeight = '0px';
            card.style.paddingTop = '0';
            card.style.paddingBottom = '0';
            card.style.overflow = 'hidden';
          });
          setTimeout(function () { card.remove(); }, 220);
        } else {
          // Dismissed (marked read only): stays visible, fades to the read state.
          card.classList.add('is-read');
        }
      })
      .catch(function () {
        // Fetch failed — fall back to a normal navigation so the action
        // still happens, same fallback pattern as toast.js's undo button.
        form.submit();
      });
  });

  if (markAllBtn) {
    markAllBtn.addEventListener('click', function () {
      fetch(markAllBtn.dataset.url, {
        method: 'POST',
        headers: {
          'X-Requested-With': 'XMLHttpRequest',
          'X-CSRFToken': csrfToken,
        },
      })
        .then(function (res) { return res.json(); })
        .then(function (data) {
          list.querySelectorAll('.tray-ncard').forEach(function (c) { c.classList.add('is-read'); });
          setBadge(data.needs_action_count);
        });
    });
  }
})();

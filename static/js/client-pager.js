// Client-side pagination for lists that are filtered in the browser (the
// Notification Center's category tabs + search, the Alerts Center's panels).
// Server-paginated lists use templates/partials/_pager.html instead; this
// draws the same markup/classes (.gv-pager, from gerlyn.css) so both look
// alike.
//
// A row is "matching" when the page's own filter has NOT set `row.hidden`.
// The pager never touches `hidden`; it hides rows outside the current page
// with [data-page-hidden], so a filter and the pager cannot undo each other.
// After the filter runs, call refresh(true) to go back to page 1.
//
//   var pager = GerlynClientPager.attach({
//     rows: function () { return document.querySelectorAll('#feed tr.item'); },
//     mount: document.getElementById('feed-pager'),
//     size: 15, noun: 'notification'
//   });
//   pager.refresh(true);
(function () {
  function el(tag, cls, text) {
    var node = document.createElement(tag);
    if (cls) node.className = cls;
    if (text != null) node.textContent = text;
    return node;
  }

  // 1, 2 … 5 6 [7] 8 9 … 20 — null marks a skipped run.
  function windowOf(current, total) {
    var wanted = {};
    [1, total].forEach(function (n) { wanted[n] = true; });
    for (var n = current - 2; n <= current + 2; n++) wanted[n] = true;
    var nums = Object.keys(wanted).map(Number).filter(function (n) {
      return n >= 1 && n <= total;
    }).sort(function (a, b) { return a - b; });
    var out = [];
    nums.forEach(function (n) {
      if (out.length) {
        var gap = n - out[out.length - 1];
        if (gap === 2) out.push(out[out.length - 1] + 1);   // one skipped page: show it
        else if (gap > 2) out.push(null);
      }
      out.push(n);
    });
    return out;
  }

  function attach(opts) {
    var size = opts.size || 15;
    var noun = opts.noun || 'record';
    var plural = opts.plural || (/[^aeiou]y$/.test(noun) ? noun.slice(0, -1) + 'ies' : noun + 's');
    var mount = opts.mount;
    var page = 1;

    function matching() {
      return Array.prototype.filter.call(opts.rows(), function (row) { return !row.hidden; });
    }

    function render(total, pages) {
      if (!mount) return;
      mount.textContent = '';
      if (!total) return;
      var first = (page - 1) * size + 1;
      var last = Math.min(page * size, total);

      var nav = el('nav', 'gv-pager');
      nav.setAttribute('aria-label', 'Pagination');
      nav.appendChild(el('p', 'gv-pager-count gv-meta',
        'Showing ' + first + '–' + last + ' of ' + total + ' ' + (total === 1 ? noun : plural)));
      if (pages > 1) {
        var list = el('ul', 'gv-pager-pages');
        function item(label, target, extra, current) {
          var li = el('li');
          var node = target && !current
            ? el('button', 'btn btn-sm ' + (extra || ''), label)
            : el('span', 'btn btn-sm ' + (extra || ''), label);
          if (node.tagName === 'BUTTON') {
            node.type = 'button';
            node.addEventListener('click', function () { go(target); });
          } else if (current) {
            node.setAttribute('aria-current', 'page');
          } else {
            node.setAttribute('aria-disabled', 'true');
          }
          li.appendChild(node);
          list.appendChild(li);
        }
        item('Previous', page > 1 ? page - 1 : 0);
        windowOf(page, pages).forEach(function (n) {
          if (n === null) {
            var gap = el('li', 'gv-pager-gap', '…');
            gap.setAttribute('aria-hidden', 'true');
            list.appendChild(gap);
          } else {
            item(String(n), n, 'gv-pager-num' + (n === page ? ' btn-primary' : ''), n === page);
          }
        });
        item('Next', page < pages ? page + 1 : 0);
        nav.appendChild(list);
      }
      mount.appendChild(nav);
    }

    function refresh(resetPage) {
      var rows = opts.rows();
      var shown = matching();
      var pages = Math.max(1, Math.ceil(shown.length / size));
      if (resetPage) page = 1;
      if (page > pages) page = pages;
      var from = (page - 1) * size;
      var visible = new Set(shown.slice(from, from + size));
      Array.prototype.forEach.call(rows, function (row) {
        if (visible.has(row)) row.removeAttribute('data-page-hidden');
        else row.setAttribute('data-page-hidden', '');
      });
      render(shown.length, pages);
    }

    function go(n) {
      page = n;
      refresh(false);
      if (opts.scrollTo !== false && mount && mount.scrollIntoView) {
        // Keep the top of the list in view after a page turn on a long page.
        var rect = mount.getBoundingClientRect();
        if (rect.top < 0 || rect.top > window.innerHeight) {
          mount.scrollIntoView({ block: 'center' });
        }
      }
    }

    refresh(true);
    return { refresh: refresh };
  }

  window.GerlynClientPager = { attach: attach };
})();

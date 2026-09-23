/* Dashboard KPI rail — the four cards act as tabs into the one shared graph
   panel beneath them (DESIGN.md Section 2, #2a).

   Every series, its polyline/area geometry and its display strings are
   precomputed server-side (reports/views.py::_dashboard_kpi_payload) and
   delivered as one JSON block via |json_script, so switching cards is a
   client-side redraw rather than a new request. Nothing here recomputes a
   figure: a card's headline and the panel's total for that metric are the
   same server-rendered string, which is what keeps them in agreement. */
(function () {
  'use strict';

  var dataEl = document.getElementById('dashboard-kpi-data');
  var svg = document.getElementById('kpi-graph-svg');
  if (!dataEl || !svg) return;

  var data;
  try {
    data = JSON.parse(dataEl.textContent);
  } catch (e) {
    return;
  }

  var cards = document.querySelectorAll('.stat-hero[data-kpi-key]');
  var areaEl = document.getElementById('kpi-graph-area');
  var lineEl = document.getElementById('kpi-graph-line');
  var prevLineEl = document.getElementById('kpi-graph-prev-line');
  var rangeHeaderEl = document.getElementById('kpi-graph-range-header');
  var legendEl = document.getElementById('kpi-graph-legend');
  var pointsEl = document.getElementById('kpi-graph-points');
  var axisEl = document.getElementById('kpi-graph-axis');
  var emptyEl = document.getElementById('kpi-graph-empty');
  var labelEl = document.getElementById('kpi-graph-label');
  var valueEl = document.getElementById('kpi-graph-value');
  var figureEl = document.getElementById('kpi-graph-figure');
  var peakEl = document.getElementById('kpi-graph-peak');
  var wrapEl = document.getElementById('kpi-graph-svg-wrap');
  var tooltipEl = document.getElementById('kpi-graph-tooltip');
  var guidelineEl = document.getElementById('kpi-graph-guideline');
  var hitareaEl = document.getElementById('kpi-graph-hitarea');
  var SVG_NS = 'http://www.w3.org/2000/svg';

  // .kpi-graph-axis-label's declared font-size (gerlyn.css) is stated in
  // SVG user-units — the SAME units the chart's 760-wide viewBox uses for
  // point coordinates — not real CSS pixels, even though "14px" reads like
  // one. #kpi-graph-svg is responsive (width:100%), so once it renders
  // wider than 760 real px (routine — this panel is usually 900-1100px
  // wide), the viewBox-to-viewport scale stretches that declared size
  // right along with everything else: measured live, a declared 14
  // (meant to match the Top Products legend's real 14px) was painting at
  // an effective ~19px. getComputedStyle() can't catch this — it reports
  // the declared value, never the geometric scale SVG applies on top of
  // it — which is exactly why matching the CSS rule's number to the
  // reference's number alone wasn't enough.
  //
  // renderAxisLabels (below) corrects this by computing the CURRENT scale
  // factor at draw time and setting each tick's own font-size (still in
  // user-units) to AXIS_LABEL_TARGET_PX divided by it, so the PAINTED size
  // stays pinned to a true 14 real px no matter how wide the chart actually
  // renders. Re-run on window resize too — the scale factor is a function
  // of live layout width, not a constant, so a resize alone (no card
  // switch) would otherwise leave stale, wrong-sized ticks on screen.
  var AXIS_LABEL_TARGET_PX = 14;

  function axisLabelScaleFactor() {
    var vb = svg.viewBox.baseVal;
    if (!vb || !vb.width) return 1;
    var rect = svg.getBoundingClientRect();
    return rect.width ? (rect.width / vb.width) : 1;
  }

  // The series continuous hover reads from — kept in sync by activate() on
  // every card switch, since the hitarea's mousemove listener is wired once
  // below, not re-wired per render.
  var currentEntry = null;

  // Full names for the tooltip specifically (the x-axis ticks below it come
  // pre-formatted, already-abbreviated, straight from the server — see
  // reports/views.py::_kpi_axis_labels — and never touch this list).
  var MONTH_NAMES_FULL = ['January', 'February', 'March', 'April', 'May', 'June',
                           'July', 'August', 'September', 'October', 'November', 'December'];
  var WEEKDAY_ABBR = ['Sun', 'Mon', 'Tue', 'Wed', 'Thu', 'Fri', 'Sat'];

  function formatValue(unit, value) {
    var digits = unit === 'currency' ? 2 : 0;
    var formatted = Number(value).toLocaleString('en-US', {
      minimumFractionDigits: digits,
      maximumFractionDigits: digits
    });
    return unit === 'currency' ? '₱' + formatted : formatted;
  }

  // p.date is 'YYYY-MM-DD' from the server — split by hand rather than
  // `new Date(p.date)`, which parses as UTC midnight and can print as the
  // previous day in a negative-UTC-offset timezone.
  //
  // The Total Sales panel's 'today' view is the one exception: every point
  // falls on the same (obviously-today) date, so reports/views.py labels
  // those points with a time instead ("2:00 PM" — see _dashboard_kpi_payload's
  // _hour_label) rather than a date nobody needs restated 24 times. Detected
  // by the ':' neither an ISO date nor MONTH_NAMES_FULL ever produces.
  function formatDateFull(isoDate) {
    if (isoDate.indexOf(':') !== -1) return isoDate;
    var parts = isoDate.split('-');
    var month = MONTH_NAMES_FULL[parseInt(parts[1], 10) - 1];
    return month + ' ' + parseInt(parts[2], 10);
  }

  // The two-value tooltip's own date format ("Thu, Jul 21") — only used
  // when a point has a comparable prior-period value (showTooltip below);
  // the single-value tooltip keeps formatDateFull's plain "July 21"
  // unchanged. new Date(y, m, d) — the local-time 3-argument form, not
  // `new Date(isoDate)` — for the same UTC-midnight reason formatDateFull's
  // own comment gives; passing the already-parsed numeric parts sidesteps
  // it entirely rather than re-deriving the local day from a parsed
  // timestamp. Today's hour-labelled points ("2:00 PM") pass through
  // unchanged, same guard as formatDateFull.
  function formatDateWithWeekday(isoDate) {
    if (isoDate.indexOf(':') !== -1) return isoDate;
    var parts = isoDate.split('-');
    var y = parseInt(parts[0], 10), m = parseInt(parts[1], 10) - 1, d = parseInt(parts[2], 10);
    var weekday = WEEKDAY_ABBR[new Date(y, m, d).getDay()];
    return weekday + ', ' + MONTH_NAMES_FULL[m].slice(0, 3) + ' ' + d;
  }

  // Converts one (x, y) in the SVG's own 760x220 user-space (exactly what
  // p.x/p.y already are) to real screen pixels via the SVG's current screen
  // transform — correct regardless of how the responsive width:100% layout
  // has actually scaled the viewBox right now, which a hand-rolled ratio
  // from getBoundingClientRect() would have to reimplement and could drift
  // out of sync with on a window resize.
  function svgPointToScreen(x, y) {
    var pt = svg.createSVGPoint();
    pt.x = x;
    pt.y = y;
    return pt.matrixTransform(svg.getScreenCTM());
  }

  // Styled tooltip (kpi-graph-tooltip in gerlyn.css) for the continuous-hover
  // path only (Total Sales, entry.smooth) — see the hitarea mousemove
  // handler below. The other three metrics keep their original native
  // <title> per point instead (renderPoints), never this.
  //
  // One shared guideline + hitarea, one tooltip — never a second hoverable
  // target for the dashed prior-period line. p.prevValue (set server-side,
  // reports/views.py::_kpi_graph_series) is the overlay's own value at this
  // SAME day-offset-within-period, already index-aligned with p.value the
  // same way the drawn prevLinePath is — so both numbers shown here are
  // guaranteed to be "this point" and "the equivalent point last period",
  // never a coincidentally-nearby prior-period point.
  function showTooltip(p, unit) {
    if (!tooltipEl || !wrapEl) return;
    var hasPrev = p.prevValue !== null && p.prevValue !== undefined;
    // Toggled, not just added on the multi branch — the same tooltip
    // element is reused hover to hover, including across a card switch, so
    // a stale 'multi' from the previous point/metric must be cleared on the
    // no-overlay branch or the CSS below would keep laying it out as if a
    // second row were still coming.
    tooltipEl.classList.toggle('multi', hasPrev);
    if (hasPrev) {
      tooltipEl.innerHTML =
        '<span class="ttip-date">' + formatDateWithWeekday(p.date) + '</span>' +
        '<div class="ttip-row">' +
          '<span class="ttip-left"><span class="ttip-dot ttip-dot--current"></span>This period</span>' +
          '<span class="ttip-value">' + formatValue(unit, p.value) + '</span>' +
        '</div>' +
        '<div class="ttip-row ttip-row--prior">' +
          '<span class="ttip-left"><span class="ttip-dot ttip-dot--prior"></span>Prior period</span>' +
          '<span class="ttip-value">' + formatValue(unit, p.prevValue) + '</span>' +
        '</div>';
    } else {
      // Unchanged from before this feature — no overlay, nothing to
      // disambiguate, so the plain single date/value line stays exactly
      // as it was.
      tooltipEl.innerHTML =
        '<span class="ttip-date">' + formatDateFull(p.date) + '</span> – ' +
        '<span class="ttip-amount">' + formatValue(unit, p.value) + '</span>';
    }
    var wrapRect = wrapEl.getBoundingClientRect();
    var screenPt = svgPointToScreen(p.x, p.y);
    tooltipEl.style.left = (screenPt.x - wrapRect.left) + 'px';
    tooltipEl.style.top = (screenPt.y - wrapRect.top - 8) + 'px';
    tooltipEl.classList.add('visible');
  }

  function hideTooltip() {
    if (tooltipEl) tooltipEl.classList.remove('visible');
    if (guidelineEl) guidelineEl.hidden = true;
  }

  // Finds the point in `points` (already sorted by date, i.e. by x) whose x
  // is closest to `svgX` — a plain linear scan, not a binary search: a KPI
  // series is at most a few hundred days, so the O(n) cost here is trivial
  // next to a DOM write, and it stays correct even though Avg. Transaction
  // Value's points (the only other caller of this shared component that
  // could reach it) aren't evenly spaced.
  function nearestPoint(points, svgX) {
    var nearest = points[0];
    var best = Math.abs(points[0].x - svgX);
    for (var i = 1; i < points.length; i++) {
      var d = Math.abs(points[i].x - svgX);
      if (d < best) {
        best = d;
        nearest = points[i];
      }
    }
    return nearest;
  }

  // Continuous hover for the smoothed panel: no visible dot sits on the
  // line at rest (renderPoints below draws none for entry.smooth), so this
  // is what answers "what's the value right here" — wherever the cursor is
  // over the plot, not only when it happens to land exactly on a point.
  function handleHitareaMove(evt) {
    if (!currentEntry || !currentEntry.smooth || !currentEntry.points.length) return;
    var svgPt = svg.createSVGPoint();
    svgPt.x = evt.clientX;
    svgPt.y = evt.clientY;
    var local = svgPt.matrixTransform(svg.getScreenCTM().inverse());
    var p = nearestPoint(currentEntry.points, local.x);
    if (guidelineEl) {
      guidelineEl.setAttribute('x1', p.x);
      guidelineEl.setAttribute('x2', p.x);
      guidelineEl.hidden = false;
    }
    showTooltip(p, currentEntry.unit);
  }

  if (hitareaEl) {
    hitareaEl.addEventListener('mousemove', handleHitareaMove);
    hitareaEl.addEventListener('mouseleave', hideTooltip);
  }

  // Dots are drawn only for the other three (non-smoothed) metrics, whose
  // per-point native <title> IS their only hover affordance — continuous
  // hover (above) replaces dots + per-point listeners for Total Sales,
  // which is also why "tuldok-tuldok" clutter on every real data point is
  // gone specifically (and only) there.
  function renderPoints(entry) {
    pointsEl.textContent = '';
    if (entry.smooth) return;
    entry.points.forEach(function (p) {
      var circle = document.createElementNS(SVG_NS, 'circle');
      circle.setAttribute('cx', p.x);
      circle.setAttribute('cy', p.y);
      circle.setAttribute('r', entry.pointRadius);
      circle.setAttribute('fill', entry.stroke);
      var title = document.createElementNS(SVG_NS, 'title');
      // p.date is already YYYY-MM-DD from the server — the system-wide
      // date format, so no client-side date formatting happens here.
      title.textContent = p.date + ': ' + formatValue(entry.unit, p.value);
      circle.appendChild(title);
      pointsEl.appendChild(circle);
    });
  }

  // Ticks come pre-thinned and pre-positioned from the server
  // (reports/views.py::_kpi_axis_labels) — this only draws them, the same
  // split renderPoints already uses for the dots.
  function renderAxisLabels(entry) {
    if (!axisEl) return;
    axisEl.textContent = '';
    // See AXIS_LABEL_TARGET_PX above — computed once per call, not once per
    // tick: the chart's rendered width can't change between two ticks in
    // the same draw.
    var userUnitSize = AXIS_LABEL_TARGET_PX / axisLabelScaleFactor();
    (entry.axisLabels || []).forEach(function (tick) {
      var text = document.createElementNS(SVG_NS, 'text');
      text.setAttribute('x', tick.x);
      // 254, not 214 — the viewBox grew from height 220 to 260 (see
      // dashboard.html's #kpi-graph-svg), and this must track the same
      // offset-from-baseline the template's own server-rendered ticks use.
      text.setAttribute('y', 254);
      text.setAttribute('text-anchor', tick.anchor);
      text.setAttribute('class', 'kpi-graph-axis-label');
      // Inline, not the class's own font-size: an inline style wins over
      // any stylesheet rule for that one longhand property regardless of
      // the class rule being a `font` shorthand, so family/weight/
      // line-height still come from .kpi-graph-axis-label untouched — only
      // size is overridden, per-render, to the current scale-compensated
      // value.
      text.style.fontSize = userUnitSize + 'px';
      text.textContent = tick.text;
      axisEl.appendChild(text);
    });
  }

  function activate(key) {
    var entry = data[key];
    if (!entry) return;

    // The hitarea's mousemove listener (wired once, outside activate) reads
    // this on every move — must be updated before anything else so a switch
    // mid-hover can't briefly read the old (now-replaced) series' points.
    currentEntry = entry;

    // The old points are about to be replaced out from under any tooltip
    // currently pointing at one of them.
    hideTooltip();

    Array.prototype.forEach.call(cards, function (card) {
      var isActive = card.getAttribute('data-kpi-key') === key;
      card.classList.toggle('active', isActive);
      card.setAttribute('aria-pressed', isActive ? 'true' : 'false');
    });

    // A metric can legitimately have nothing to plot — Avg. Transaction
    // Value over a range with no transactions at all omits every day rather
    // than drawing a fabricated zero line, so the panel says so instead.
    var hasSeries = entry.points.length > 0;
    // toggleAttribute, not `.hidden` — `hidden` is an HTMLElement property and
    // this is an SVGSVGElement, which does not implement it, so the assignment
    // set a stray JS property and never wrote the attribute at all.
    svg.toggleAttribute('hidden', !hasSeries);
    if (emptyEl) emptyEl.hidden = hasSeries;

    if (hasSeries) {
      areaEl.setAttribute('d', entry.area);
      // Both colours come from the payload (KPI_LINE/KPI_AREA in views.py).
      // This used to pick the fill by string-comparing the stroke against the
      // literal 'var(--brand)', which silently fell through to a grey area for
      // any other value — the reason Procurement Cost and Total Transactions
      // rendered as though they were disabled.
      areaEl.setAttribute('fill', entry.fill);
      // Primary/secondary tier (KPI_TIERS in views.py). Weight, not colour:
      // the hue stays --brand at full opacity on every metric, because fading
      // it drops the line under the 3:1 SC 1.4.11 floor and lands back on
      // "disabled". The area can fade freely — it carries no data.
      areaEl.setAttribute('fill-opacity', entry.areaOpacity);
      // 'linePath' is always populated server-side now — straight segments
      // when entry.smooth is false, a Catmull-Rom curve when true — so this
      // <path> element's 'd' never needs a different attribute for the two
      // cases the way a <polyline> vs <path> split once would have.
      lineEl.setAttribute('d', entry.linePath);
      lineEl.setAttribute('stroke', entry.stroke);
      lineEl.setAttribute('stroke-width', entry.strokeWidth);
      renderPoints(entry);
      renderAxisLabels(entry);
    } else if (axisEl) {
      axisEl.textContent = '';
    }

    // Prior-period dashed overlay (Total Sales only, and only on a bounded
    // range — see prevLinePath in reports/views.py::_kpi_graph_series).
    // toggleAttribute for the same SVGSVGElement/HTMLElement.hidden mismatch
    // svg.toggleAttribute above already documents.
    if (prevLineEl) {
      var hasPrevLine = hasSeries && !!entry.prevLinePath;
      prevLineEl.toggleAttribute('hidden', !hasPrevLine);
      if (hasPrevLine) prevLineEl.setAttribute('d', entry.prevLinePath);
    }
    // Range/overlay descriptor above the chart — text always shown, legend
    // only when this entry actually has an overlay to label (entry.showLegend,
    // same condition prevLineEl's visibility above tracks).
    if (rangeHeaderEl) rangeHeaderEl.textContent = entry.rangeHeader || '';
    if (legendEl) legendEl.hidden = !entry.showLegend;

    // The panel no longer restates the card's foot or delta pill — the card
    // is tinted .active while its panel is open, so it already says both.
    // The label/value/peak block is dropped together for the smoothed Total
    // Sales panel — the card above already states its name and value, and
    // the range/legend row is the only thing this header needs then. The
    // other three metrics keep all three. Both #kpi-graph-label and
    // #kpi-graph-figure are always present server-side (just `hidden` on
    // whichever the default card doesn't want — see dashboard.html), so
    // this only ever toggles that attribute, never creates or removes
    // anything.
    if (labelEl) {
      labelEl.hidden = entry.smooth;
      labelEl.textContent = entry.label;
    }
    if (figureEl) figureEl.hidden = entry.smooth;
    if (valueEl) valueEl.textContent = entry.valueDisplay;
    if (peakEl) peakEl.textContent = entry.peak;
  }

  Array.prototype.forEach.call(cards, function (card) {
    card.addEventListener('click', function () {
      activate(card.getAttribute('data-kpi-key'));
    });
  });

  // Seed the point markers/tooltips for the server-rendered default card,
  // and point continuous hover at it before any card is ever clicked. The
  // rest of its initial state is already in the HTML, so the panel is
  // correct and readable even if this script never runs.
  var defaultCard = document.querySelector('.stat-hero.active[data-kpi-key]');
  if (defaultCard) {
    var defaultEntry = data[defaultCard.getAttribute('data-kpi-key')];
    if (defaultEntry) {
      currentEntry = defaultEntry;
      if (defaultEntry.points.length) renderPoints(defaultEntry);
      // Re-draws the server-rendered default card's ticks with the
      // scale-compensated font-size from the moment this script can run —
      // the server has no way to know the client's actual rendered width,
      // so the markup it sent uses the plain CSS-declared size (see
      // AXIS_LABEL_TARGET_PX above) until this corrects it.
      renderAxisLabels(defaultEntry);
    }
  }

  // The axis ticks' font-family (.kpi-graph-axis-label in gerlyn.css) is
  // already correct — var(--font-sans), same as the rest of the page — but
  // Google Fonts is loaded with &display=swap (internal_base.html), so the
  // very first paint of any text on the page is in the fallback font
  // (Segoe UI/Arial) until Roboto finishes downloading, then swaps. HTML
  // text reliably repaints on that swap; SVG <text> is a known Chromium/
  // WebKit gap where it doesn't always repaint the same way, so these ticks
  // can visibly stay in the fallback font well after the rest of the page
  // has already swapped to Roboto. Re-drawing them once document.fonts.ready
  // resolves forces a fresh paint with the now-loaded font, rather than
  // waiting on a repaint that may not come on its own. A no-op re-render
  // (same data, same positions) if the font was already loaded/swapped by
  // then — this only ever costs one extra draw per page load.
  if (document.fonts && document.fonts.ready) {
    document.fonts.ready.then(function () {
      if (currentEntry) renderAxisLabels(currentEntry);
    });
  }

  // The scale-compensation factor (axisLabelScaleFactor above) is a
  // function of the chart's LIVE rendered width, not a constant — a window
  // resize alone, with no card switch and no font event to piggyback on,
  // would otherwise leave the ticks at whatever size matched the width
  // before the resize. Debounced: resize fires continuously while dragging,
  // and re-measuring/redrawing on every single event is wasted work against
  // a value that only matters once the drag settles.
  var resizeTimer = null;
  window.addEventListener('resize', function () {
    clearTimeout(resizeTimer);
    resizeTimer = setTimeout(function () {
      if (currentEntry) renderAxisLabels(currentEntry);
    }, 150);
  });
})();

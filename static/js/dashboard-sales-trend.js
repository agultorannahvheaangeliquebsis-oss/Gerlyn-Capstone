/* Sales Trend by Branch — continuous-hover tooltip/guideline and
   scale-compensated axis labels for the multi-branch line chart
   (templates/reports/dashboard.html, reports/views.py::
   _sales_trend_by_branch_chart).

   Deliberately its own small file rather than folding into
   dashboard-kpi.js: that file's activate()/currentEntry machinery exists
   to swap between four SINGLE-series panels on card click. This chart has
   no cards to swap — one static render of N branch lines at once — so
   reusing its structure would mean carrying condensed/smooth/card-switching
   concepts that don't apply here. What IS shared is copied over rather than
   re-derived: the SVG-viewBox font-size scaling fix and the point-to-screen
   tooltip positioning math are identical problems with identical fixes. */
(function () {
  'use strict';

  var dataEl = document.getElementById('sales-trend-data');
  var svg = document.getElementById('trend-svg');
  if (!dataEl || !svg) return;

  var data;
  try {
    data = JSON.parse(dataEl.textContent);
  } catch (e) {
    return;
  }
  if (!data.points || !data.points.length) return;

  var axisEl = document.getElementById('trend-axis');
  var guidelineEl = document.getElementById('trend-guideline');
  var hitareaEl = document.getElementById('trend-hitarea');
  var tooltipEl = document.getElementById('trend-tooltip');
  var wrapEl = document.getElementById('trend-svg-wrap');
  var SVG_NS = 'http://www.w3.org/2000/svg';

  // Same fix, same reasoning as dashboard-kpi.js's own AXIS_LABEL_TARGET_PX/
  // axisLabelScaleFactor — see that file's comment for the full explanation.
  // Declared font-size on an SVG <text> is in viewBox user-units, not real
  // CSS pixels, so a responsively-scaled SVG (routinely wider than its own
  // 760 viewBox here) paints a plain "14" bigger than 14 real px unless the
  // current scale factor is divided back out at render time.
  var AXIS_LABEL_TARGET_PX = 14;

  // Height, not width — the KPI rail's own version of this function (which
  // this is otherwise a straight copy of) can use either one because its
  // SVG's height:auto always keeps both axes scaled together (see
  // gerlyn.css's own comment on .kpi-graph-panel svg). This chart's card is
  // full-width rather than sharing a row, so it's the one actually wide
  // enough to hit that same rule's max-height:280px cap in practice — past
  // that point the two axes scale by DIFFERENT factors (width keeps
  // stretching with the container, height is capped), and font-size is a
  // vertical measurement, so compensating against the wrong (width) axis
  // painted labels far too small here. Height stays correct even once the
  // cap applies; width would not.
  function axisLabelScaleFactor() {
    var vb = svg.viewBox.baseVal;
    if (!vb || !vb.height) return 1;
    var rect = svg.getBoundingClientRect();
    return rect.height ? (rect.height / vb.height) : 1;
  }

  function renderAxisLabels() {
    if (!axisEl) return;
    axisEl.textContent = '';
    var userUnitSize = AXIS_LABEL_TARGET_PX / axisLabelScaleFactor();
    (data.axisLabels || []).forEach(function (tick) {
      var text = document.createElementNS(SVG_NS, 'text');
      text.setAttribute('x', tick.x);
      // 214, not 254 (dashboard-kpi.js's own value) — this chart's viewBox
      // stayed 760x220 (it still shows its own title/legend around the
      // SVG, unlike the KPI rail's condensed panel, so there was no
      // vertical space to reclaim by growing the viewBox), and 214 is the
      // same offset-from-baseline (220 - 24 pad_y - a couple px) the KPI
      // rail's own pre-260 version used.
      text.setAttribute('y', 214);
      text.setAttribute('text-anchor', tick.anchor);
      text.setAttribute('class', 'kpi-graph-axis-label');
      text.style.fontSize = userUnitSize + 'px';
      text.textContent = tick.text;
      axisEl.appendChild(text);
    });
  }
  renderAxisLabels();

  // Google Fonts loads with &display=swap — see dashboard-kpi.js's own
  // comment on why SVG <text> needs an explicit re-render on the font swap
  // rather than relying on the browser to repaint it unprompted.
  if (document.fonts && document.fonts.ready) {
    document.fonts.ready.then(renderAxisLabels);
  }

  var resizeTimer = null;
  window.addEventListener('resize', function () {
    clearTimeout(resizeTimer);
    resizeTimer = setTimeout(renderAxisLabels, 150);
  });

  function formatPeso(value) {
    return '₱' + Number(value).toLocaleString('en-US', {
      minimumFractionDigits: 2, maximumFractionDigits: 2
    });
  }

  var MONTH_NAMES_FULL = ['January', 'February', 'March', 'April', 'May', 'June',
                           'July', 'August', 'September', 'October', 'November', 'December'];
  // p.date is 'YYYY-MM-DD' from the server — split by hand rather than
  // `new Date(p.date)`, same UTC-midnight reason dashboard-kpi.js's own
  // formatDateFull documents.
  function formatDateFull(isoDate) {
    var parts = isoDate.split('-');
    return MONTH_NAMES_FULL[parseInt(parts[1], 10) - 1] + ' ' + parseInt(parts[2], 10);
  }

  function svgPointToScreen(x, y) {
    var pt = svg.createSVGPoint();
    pt.x = x;
    pt.y = y;
    return pt.matrixTransform(svg.getScreenCTM());
  }

  // Same linear-scan nearest-point search as dashboard-kpi.js's own
  // nearestPoint — this chart never has more than a few hundred days either.
  function nearestPoint(svgX) {
    var points = data.points;
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

  // One row per branch at this day — the same "everything at this x, one
  // tooltip" shape the KPI rail's own dual-value (this period/prior period)
  // tooltip uses, generalized from 2 fixed rows to N branches.
  function showTooltip(p) {
    if (!tooltipEl || !wrapEl) return;
    var rows = p.values.map(function (v) {
      return (
        '<div class="ttip-row">' +
          '<span class="ttip-left"><span class="ttip-dot" style="background:' + v.colour + ';"></span>' + v.name + '</span>' +
          '<span class="ttip-value">' + formatPeso(v.value) + '</span>' +
        '</div>'
      );
    }).join('');
    tooltipEl.innerHTML = '<span class="ttip-date">' + formatDateFull(p.date) + '</span>' + rows;
    var wrapRect = wrapEl.getBoundingClientRect();
    var screenPt = svgPointToScreen(p.x, 24);
    tooltipEl.style.left = (screenPt.x - wrapRect.left) + 'px';
    tooltipEl.style.top = (screenPt.y - wrapRect.top - 8) + 'px';
    tooltipEl.classList.add('visible');
  }

  function hideTooltip() {
    if (tooltipEl) tooltipEl.classList.remove('visible');
    // toggleAttribute, not `.hidden = true` — confirmed live that setting
    // the JS property alone left the `hidden` ATTRIBUTE in place on this
    // SVG <line> (property and attribute went out of sync: `.hidden` read
    // back `false` after being "unhidden" but `hasAttribute('hidden')`
    // stayed `true`), so the guideline never actually appeared. Same
    // property/attribute mismatch dashboard-kpi.js's own svg.toggleAttribute
    // / prevLineEl.toggleAttribute calls already exist to avoid — this file
    // just hadn't applied it to ITS guideline yet.
    if (guidelineEl) guidelineEl.toggleAttribute('hidden', true);
  }

  function handleHitareaMove(evt) {
    var svgPt = svg.createSVGPoint();
    svgPt.x = evt.clientX;
    svgPt.y = evt.clientY;
    var local = svgPt.matrixTransform(svg.getScreenCTM().inverse());
    var p = nearestPoint(local.x);
    if (guidelineEl) {
      guidelineEl.setAttribute('x1', p.x);
      guidelineEl.setAttribute('x2', p.x);
      guidelineEl.toggleAttribute('hidden', false);
    }
    showTooltip(p);
  }

  if (hitareaEl) {
    hitareaEl.addEventListener('mousemove', handleHitareaMove);
    hitareaEl.addEventListener('mouseleave', hideTooltip);
  }
})();

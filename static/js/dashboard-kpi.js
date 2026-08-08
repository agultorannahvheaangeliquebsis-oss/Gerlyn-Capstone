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
  var pointsEl = document.getElementById('kpi-graph-points');
  var emptyEl = document.getElementById('kpi-graph-empty');
  var labelEl = document.getElementById('kpi-graph-label');
  var valueEl = document.getElementById('kpi-graph-value');
  var footEl = document.getElementById('kpi-graph-foot');
  var deltaEl = document.getElementById('kpi-graph-delta');
  var SVG_NS = 'http://www.w3.org/2000/svg';

  function formatValue(unit, value) {
    var digits = unit === 'currency' ? 2 : 0;
    var formatted = Number(value).toLocaleString('en-US', {
      minimumFractionDigits: digits,
      maximumFractionDigits: digits
    });
    return unit === 'currency' ? '₱' + formatted : formatted;
  }

  function renderPoints(entry) {
    pointsEl.textContent = '';
    entry.points.forEach(function (p) {
      var circle = document.createElementNS(SVG_NS, 'circle');
      circle.setAttribute('cx', p.x);
      circle.setAttribute('cy', p.y);
      circle.setAttribute('r', entry.pointRadius);
      circle.setAttribute('fill', entry.stroke);
      var title = document.createElementNS(SVG_NS, 'title');
      // p.date is already YYYY-MM-DD from the server — the system-wide date
      // format, so no client-side date formatting happens here at all.
      title.textContent = p.date + ': ' + formatValue(entry.unit, p.value);
      circle.appendChild(title);
      pointsEl.appendChild(circle);
    });
  }

  function renderDelta(delta) {
    deltaEl.textContent = '';
    var span = document.createElement('span');
    span.className = 'pill ' + delta.pill;
    span.textContent = delta.text;
    deltaEl.appendChild(span);
  }

  function activate(key) {
    var entry = data[key];
    if (!entry) return;

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
      lineEl.setAttribute('points', entry.polyline);
      lineEl.setAttribute('stroke', entry.stroke);
      lineEl.setAttribute('stroke-width', entry.strokeWidth);
      renderPoints(entry);
    }

    labelEl.textContent = entry.label;
    valueEl.textContent = entry.valueDisplay;
    if (footEl) footEl.textContent = entry.foot;
    renderDelta(entry.delta);
  }

  Array.prototype.forEach.call(cards, function (card) {
    card.addEventListener('click', function () {
      activate(card.getAttribute('data-kpi-key'));
    });
  });

  // Seed the point markers/tooltips for the server-rendered default card.
  // The rest of its initial state is already in the HTML, so the panel is
  // correct and readable even if this script never runs.
  var defaultCard = document.querySelector('.stat-hero.active[data-kpi-key]');
  if (defaultCard) {
    var defaultEntry = data[defaultCard.getAttribute('data-kpi-key')];
    if (defaultEntry && defaultEntry.points.length) renderPoints(defaultEntry);
  }
})();

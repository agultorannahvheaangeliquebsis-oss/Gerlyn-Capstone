"""Geometry for the Profitability page's monthly trend charts.

Built on the server, like the Dashboard's other charts, so the page needs no
charting library and works with the same offline-first assets as everything
else. The template only draws what is described here.

Two panels sharing one row of months, not one chart with four series. Net sales
and cost of goods run to the hundreds of thousands while a month's net profit
is a few thousand, so on one axis the profit would be a flat line hugging zero
-- and a second y-axis is the one thing a chart must never have. Each panel
therefore has its own single axis in pesos:

  1. net sales, cost of goods sold and expenses, three columns per month;
  2. net profit, one column per month, rising from the zero baseline when it is
     a profit and hanging below it when it is a loss, with its value written at
     the tip (twelve labels at most, so none are crowded).

Marks follow the dataviz skill's specs: columns at most 24px thick, grown from
a single baseline and rounded only at the data end, a 2px surface gap between
touching columns, hairline recessive gridlines. Identity is carried by the
legend, the value labels and the monthly table beneath the charts -- never by
colour alone.
"""
import math

WIDTH = 760
LEFT, RIGHT = 58, 14
MAX_BAR = 24
BAR_GAP = 2          # surface gap between touching columns
RADIUS = 4           # rounded data end

# Panel 1: the dataviz palette's categorical slots 1-3 in order (light mode;
# the three-slot prefix validates with adjacent-pair CVD separation >= 9. Two of
# the three sit under 3:1 on white, which is why every figure is also in the
# tooltip and the monthly table).
BAR_SERIES = [
    {'key': 'net_sales', 'label': 'Net sales', 'colour': '#2a78d6'},
    {'key': 'cogs', 'label': 'Cost of goods sold', 'colour': '#eb6834'},
    {'key': 'expenses', 'label': 'Expenses', 'colour': '#1baf7a'},
]
# Panel 2 is one measure whose SIGN is the message, so it takes the app's own
# status colours (--action / --danger-dot) rather than a categorical slot.
PROFIT_COLOUR = '#1E7A4F'
LOSS_COLOUR = '#B42318'


def compact(value):
    """Axis label: 850 / 30k / 1.2M, signed."""
    sign = '-' if value < 0 else ''
    value = abs(float(value))
    if value >= 1_000_000:
        text = f'{value / 1_000_000:.1f}M'
    elif value >= 10_000 or (value >= 1_000 and value % 1000 == 0):
        text = f'{value / 1_000:.0f}k'
    elif value >= 1_000:
        text = f'{value / 1_000:.1f}k'
    else:
        text = f'{value:.0f}'
    return f'{sign}₱{text}'


def _nice_scale(low, high, target_ticks=4):
    """(axis_min, axis_max, step) -- clean 1/2/5 x 10^n steps that include 0."""
    low, high = min(low, 0.0), max(high, 0.0)
    span = (high - low) or 1.0
    raw = span / target_ticks
    magnitude = 10 ** math.floor(math.log10(raw))
    for multiple in (1, 2, 5, 10):
        step = multiple * magnitude
        if raw <= step:
            break
    return math.floor(low / step) * step, math.ceil(high / step) * step, step


def _column(x, width, baseline, top):
    """A column from the baseline to `top`, square at the baseline and rounded
    (up to RADIUS) at the data end. Handles columns that hang downward."""
    if round(top, 1) == round(baseline, 1):
        return ''
    r = min(RADIUS, width / 2, abs(baseline - top))
    sign = 1 if top < baseline else -1          # -1: the data end is below the baseline
    inner = top + sign * r
    return (f'M{x:.1f},{baseline:.1f} L{x:.1f},{inner:.1f} Q{x:.1f},{top:.1f} {x + r:.1f},{top:.1f} '
            f'L{x + width - r:.1f},{top:.1f} Q{x + width:.1f},{top:.1f} {x + width:.1f},{inner:.1f} '
            f'L{x + width:.1f},{baseline:.1f} Z')


def _money(value):
    sign = '-' if value < 0 else ''
    return f'{sign}₱{abs(value):,.2f}'


def _panel(rows, months, keys, *, height, top, bottom, colour_of, value_labels=False):
    """One panel: axis, gridlines and the columns for `keys` of every month."""
    values = [row[k] for row in rows for k in keys]
    axis_min, axis_max, step = _nice_scale(min(values), max(values))
    plot_w, plot_h = WIDTH - LEFT - RIGHT, height - top - bottom

    def y_of(value):
        return top + (axis_max - value) / (axis_max - axis_min) * plot_h

    baseline = y_of(0)
    ticks, tick = [], axis_min
    while tick <= axis_max + step / 2:
        ticks.append({'y': round(y_of(tick), 1), 'label': compact(tick), 'zero': abs(tick) < step / 1000})
        tick += step

    n = len(rows)
    band = plot_w / n
    per = len(keys)
    bar_w = max(4.0, min(MAX_BAR, (band * 0.72 - (per - 1) * BAR_GAP) / per))
    group_w = per * bar_w + (per - 1) * BAR_GAP

    columns = []
    for i, (month, row) in enumerate(zip(months, rows)):
        centre = LEFT + band * (i + 0.5)
        left = centre - group_w / 2
        bars, label = [], None
        for j, key in enumerate(keys):
            x = left + j * (bar_w + BAR_GAP)
            tip = y_of(row[key])
            bars.append({'key': key, 'colour': colour_of(key, row[key]), 'path': _column(x, bar_w, baseline, tip)})
            if value_labels:
                below = row[key] < 0
                label = {
                    'x': round(centre, 1), 'y': round(tip + (13 if below else -6), 1),
                    'text': compact(row[key]),
                }
        columns.append({
            'label': month.label, 'short': month.label.split()[0], 'partial': month.partial,
            'centre': round(centre, 1), 'bars': bars, 'value_label': label,
            # The whole month's slot, top to bottom: aim at a month, not at a bar.
            'hit': {'x': round(LEFT + band * i, 1), 'y': 0, 'w': round(band, 1), 'h': height},
        })
    return {
        'width': WIDTH, 'height': height, 'left': LEFT, 'right': WIDTH - RIGHT,
        'baseline': round(baseline, 1), 'label_y': height - 8, 'ticks': ticks, 'columns': columns,
    }


def build_trend_chart(months):
    """Description of both panels for `months` (reports.profitability.MonthRow
    list), or None when there is nothing to draw (no month has any activity)."""
    if not months:
        return None
    rows = [
        {'net_sales': float(m.net_sales), 'cogs': float(m.cogs),
         'expenses': float(m.expenses), 'net_profit': float(m.net_profit)}
        for m in months
    ]
    if not any(v for row in rows for v in row.values()):
        return None

    money = [
        {'net_sales': _money(r['net_sales']), 'cogs': _money(r['cogs']),
         'expenses': _money(r['expenses']), 'net_profit': _money(r['net_profit'])}
        for r in rows
    ]
    amounts = _panel(
        rows, months, [s['key'] for s in BAR_SERIES], height=236, top=14, bottom=30,
        colour_of=lambda key, value: next(s['colour'] for s in BAR_SERIES if s['key'] == key),
    )
    profit = _panel(
        rows, months, ['net_profit'], height=170, top=26, bottom=40,
        colour_of=lambda key, value: LOSS_COLOUR if value < 0 else PROFIT_COLOUR,
        value_labels=True,
    )
    # Everything the tooltip and the screen reader need, once per month.
    for i, row in enumerate(rows):
        for panel in (amounts, profit):
            panel['columns'][i]['values'] = money[i]
            panel['columns'][i]['profit_colour'] = LOSS_COLOUR if row['net_profit'] < 0 else PROFIT_COLOUR
    return {
        'amounts': amounts, 'profit': profit, 'series': BAR_SERIES,
        'profit_colour': PROFIT_COLOUR, 'loss_colour': LOSS_COLOUR,
        'summary': '; '.join(
            f"{m.label}: net sales {money[i]['net_sales']}, cost of goods {money[i]['cogs']}, "
            f"expenses {money[i]['expenses']}, net profit {money[i]['net_profit']}"
            for i, m in enumerate(months)
        ),
    }

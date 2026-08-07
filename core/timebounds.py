"""
core/timebounds.py — local calendar boundaries for timezone-aware filtering.

Why this exists
---------------
Django's date-part lookups on a DateTimeField (`__date`, `__month`, `__day`)
are compiled by the MySQL backend into expressions wrapped in CONVERT_TZ.
CONVERT_TZ returns NULL when the MySQL server has no zoneinfo tables loaded,
which is the case on this deployment — so those filters do not error, they
silently match ZERO rows. Measured against 30 real SalesTransaction rows:

    transaction_date__date=<their date>        -> 0
    transaction_date__month=<their month>      -> 0
    transaction_date__date__gte=<earlier date> -> 0
    transaction_date__gte=<aware datetime>     -> 30   <-- works

`__year=` happens to survive because Django compiles it to a BETWEEN range on
the raw column rather than an EXTRACT, but relying on that is a trap: pairing
it with `__month=` still yields nothing.

Plain range comparisons against aware datetimes need no server-side timezone
conversion, so they are unaffected. These helpers build those ranges from a
local calendar day or month, giving correct local-time bucketing without ever
touching CONVERT_TZ.

Usage — always half-open, [start, end):

    start, end = local_day_bounds(day)
    qs.filter(created_at__gte=start, created_at__lt=end)

Fields declared as plain DateField (e.g. Procurement.order_date) carry no
timezone component and are NOT affected; `__month=` is correct on those and
they do not need these helpers.
"""
import calendar
from datetime import date, datetime, timedelta

from django.utils import timezone


def local_day_bounds(day):
    """
    Aware [start, end) datetimes spanning one local calendar day.

    `day` is a `datetime.date`. For an inclusive from/to date range, call this
    once per endpoint and take the near edge of each:

        start, _ = local_day_bounds(date_from)   # >= 00:00 on date_from
        _, end   = local_day_bounds(date_to)     # <  00:00 the day after
                                                 #    date_to (so date_to is
                                                 #    fully included)
    """
    start = timezone.make_aware(datetime.combine(day, datetime.min.time()))
    return start, start + timedelta(days=1)


def local_month_bounds(year, month):
    """
    Aware [start, end) datetimes spanning one local calendar month.

    Raises ValueError on an out-of-range month, so callers parsing user input
    can catch it alongside their int() parse.
    """
    start = timezone.make_aware(datetime(year, month, 1))
    end_year, end_month = (year + 1, 1) if month == 12 else (year, month + 1)
    return start, timezone.make_aware(datetime(end_year, end_month, 1))


def local_range_bounds(start_day, end_day):
    """
    Aware [start, end) datetimes spanning a local calendar date range,
    inclusive of both `start_day` and `end_day`. Composes local_day_bounds
    for each endpoint — see its docstring for why plain date lookups on a
    DateTimeField don't work on this deployment.
    """
    start, _ = local_day_bounds(start_day)
    _, end = local_day_bounds(end_day)
    return start, end


# Shared across Dashboard/Reports (DESIGN.md Section 2, #2e).
DATE_RANGE_PRESETS = ('today', '7', '30', 'this_month', 'all', 'custom')


def resolve_date_range(preset, date_from=None, date_to=None, today=None):
    """
    Resolves a date-range preset to a concrete [start, end] pair of
    `datetime.date`s, inclusive of both ends.

    `preset` is one of DATE_RANGE_PRESETS. For 'custom', `date_from`/
    `date_to` must already be parsed `datetime.date` objects — this function
    does not parse request strings, callers do that first so a parse error
    is theirs to handle. Falls back to the last 30 days for any other
    preset value, including 'custom' with a missing or inverted bound.

    Returns (start, end, preset) — `preset` echoes back the preset actually
    applied, so a caller can tell when a bad 'custom' request fell back.

    'all' returns a start of None, meaning "no lower bound". Callers must skip
    their `__gte` term rather than substituting a date. Deliberately NOT a
    MIN() across the tables: nothing needs to know the earliest record just to
    show unbounded history, and that query would cost a full scan per surface.
    """
    today = today or timezone.localtime().date()

    if preset == 'today':
        return today, today, 'today'
    if preset == 'all':
        return None, today, 'all'
    if preset == '7':
        return today - timedelta(days=6), today, '7'
    if preset == 'this_month':
        return today.replace(day=1), today, 'this_month'
    if preset == 'custom' and date_from and date_to and date_from <= date_to:
        return date_from, date_to, 'custom'
    return today - timedelta(days=29), today, '30'


# The date range the whole back office shares (DESIGN.md §2e). Persisted per
# session so the selection survives navigation, the same way session['language']
# and the storefront's session['branch_id'] already work.
DATE_RANGE_SESSION_KEY = 'date_range'


def parse_iso_date(value):
    """A YYYY-MM-DD request param as a date, or None if absent/malformed."""
    if not value:
        return None
    try:
        return datetime.strptime(value, '%Y-%m-%d').date()
    except ValueError:
        return None


def month_as_range(value):
    """
    A legacy `?month=YYYY-MM` param as the (first, last) dates of that month,
    or None if absent/malformed.

    Reports' Sales tab filtered by month before the shared range control
    existed (DESIGN.md §2e), and those URLs are still in bookmarks and in the
    wild. Mapping the param onto its equivalent custom range keeps them
    landing on the same data instead of silently falling back to the default
    30 days — a wrong-figures failure that never raises.

    The equivalence is exact, not approximate: `local_range_bounds` composes
    `local_day_bounds` at each endpoint, so first-of-month 00:00 through
    first-of-next-month 00:00 — byte-identical to what `local_month_bounds`
    returns for the same month.
    """
    if not value:
        return None
    try:
        year, month = (int(part) for part in value.split('-'))
        last_day = calendar.monthrange(year, month)[1]
        return date(year, month, 1), date(year, month, last_day)
    except (ValueError, TypeError, calendar.IllegalMonthError):
        return None


def range_from_request(request, today=None, allow_all=True):
    """
    Resolve the shared date range for a request (DESIGN.md §2e).

    Precedence, deliberately in this order:
      1. URL params (`range`, plus `date_from`/`date_to` for custom) — so a
         copied link always reproduces exactly what the sender saw.
      2. Legacy `?month=YYYY-MM`, mapped onto its equivalent custom range.
      3. The session's last selection, so the choice survives navigation.
      4. resolve_date_range's own fallback (last 30 days).

    The user's selection is written back to the session on every call, so the
    next page without params continues where they left off. Note a relative
    preset is re-resolved against *today* each time rather than being pinned
    to the dates it produced, so "Last 7 days" keeps rolling.

    `allow_all=False` is for surfaces that cannot render an unbounded range —
    the Dashboard buckets one entry per day across the range and compares it
    against a previous window of equal length, neither of which is defined
    without a lower bound. Those surfaces fall back to the default window.
    The guard is here rather than in the template because hiding the button
    would still leave a hand-typed `?range=all` reaching the day-bucketing.

    Crucially the clamp does NOT overwrite what the user picked: the stored
    selection stays 'all', so moving Reports -> Dashboard -> Reports still
    comes back to All time instead of being quietly downgraded en route.

    Returns (start, end, preset) exactly like resolve_date_range; `start` is
    None for 'all', and callers must drop their lower-bound term rather than
    substituting a date.
    """
    preset = request.GET.get('range')
    date_from = parse_iso_date(request.GET.get('date_from'))
    date_to = parse_iso_date(request.GET.get('date_to'))

    if not preset:
        legacy = month_as_range(request.GET.get('month'))
        if legacy:
            preset, date_from, date_to = 'custom', legacy[0], legacy[1]

    if not preset:
        stored = request.session.get(DATE_RANGE_SESSION_KEY) or {}
        preset = stored.get('range')
        date_from = date_from or parse_iso_date(stored.get('date_from'))
        date_to = date_to or parse_iso_date(stored.get('date_to'))

    start, end, active = resolve_date_range(preset, date_from, date_to, today=today)

    request.session[DATE_RANGE_SESSION_KEY] = {
        'range': active,
        'date_from': start.isoformat() if start else '',
        'date_to': end.isoformat(),
    }

    if active == 'all' and not allow_all:
        start, end, active = resolve_date_range(None, today=today)

    return start, end, active

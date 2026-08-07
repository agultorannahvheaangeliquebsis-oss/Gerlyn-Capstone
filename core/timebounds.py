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
from datetime import datetime, timedelta

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


# Shared across Dashboard/Reports/Alerts (DESIGN.md Section 2, #2e).
DATE_RANGE_PRESETS = ('today', '7', '30', 'this_month', 'custom')


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
    """
    today = today or timezone.localtime().date()

    if preset == 'today':
        return today, today, 'today'
    if preset == '7':
        return today - timedelta(days=6), today, '7'
    if preset == 'this_month':
        return today.replace(day=1), today, 'this_month'
    if preset == 'custom' and date_from and date_to and date_from <= date_to:
        return date_from, date_to, 'custom'
    return today - timedelta(days=29), today, '30'

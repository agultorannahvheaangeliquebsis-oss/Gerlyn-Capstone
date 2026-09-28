"""Shared calendar-date validation for GerlynPLUS.

Keep policy comparisons here so server-rendered views, DRF serializers, and
domain services use the same local-calendar rules and messages.
"""

from django.core.exceptions import ValidationError
from django.utils import timezone
from django.utils.dateparse import parse_date


TODAY_OR_FUTURE_MESSAGE = "Date must be today or later."
DATE_FROM_INVALID_MESSAGE = "Enter a valid from date."
DATE_TO_INVALID_MESSAGE = "Enter a valid to date."
DATE_FROM_REQUIRED_MESSAGE = "Enter a from date."
DATE_TO_REQUIRED_MESSAGE = "Enter a to date."
DATE_RANGE_ORDER_MESSAGE = "From date cannot be after to date."


def validate_today_or_future(value, *, today=None):
    """Accept ``value`` only when it is on/after the current local date.

    ``timezone.localdate()`` is deliberate: comparing against UTC's date can
    reject the user's local "today" around midnight in Asia/Manila.
    """
    minimum = today or timezone.localdate()
    if value is not None and value < minimum:
        raise ValidationError(
            TODAY_OR_FUTURE_MESSAGE,
            code="date_in_past",
        )
    return value


def validate_date_range(date_from, date_to):
    """Reject an inverted inclusive range while allowing historical dates."""
    if date_from is not None and date_to is not None and date_from > date_to:
        raise ValidationError(
            DATE_RANGE_ORDER_MESSAGE,
            code="invalid_date_range",
        )
    return date_from, date_to


def parse_date_range(date_from_value, date_to_value, *, required=False):
    """Parse two HTML/API ISO date values and return field-level errors.

    The return shape is ``(date_from, date_to, errors)``. ``errors`` uses the
    submitted field names so templates and APIs can display the same policy
    at the relevant input instead of collapsing it into a generic error.
    """
    raw_from = (date_from_value or "").strip()
    raw_to = (date_to_value or "").strip()
    try:
        date_from = parse_date(raw_from) if raw_from else None
    except ValueError:
        date_from = None
    try:
        date_to = parse_date(raw_to) if raw_to else None
    except ValueError:
        date_to = None
    errors = {}

    if raw_from and date_from is None:
        errors["date_from"] = DATE_FROM_INVALID_MESSAGE
    elif required and date_from is None:
        errors["date_from"] = DATE_FROM_REQUIRED_MESSAGE

    if raw_to and date_to is None:
        errors["date_to"] = DATE_TO_INVALID_MESSAGE
    elif required and date_to is None:
        errors["date_to"] = DATE_TO_REQUIRED_MESSAGE

    if not errors:
        try:
            validate_date_range(date_from, date_to)
        except ValidationError as exc:
            errors["date_from"] = exc.messages[0]

    return date_from, date_to, errors

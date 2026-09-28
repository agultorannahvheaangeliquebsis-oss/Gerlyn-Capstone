from datetime import date, datetime, timedelta, timezone as dt_timezone
from types import SimpleNamespace
from unittest.mock import patch

from django.core.exceptions import ValidationError
from django.test import SimpleTestCase, override_settings
from django.utils import timezone

from core.date_validation import (
    DATE_RANGE_ORDER_MESSAGE,
    TODAY_OR_FUTURE_MESSAGE,
    parse_date_range,
    validate_today_or_future,
)
from core.timebounds import range_from_request


class TodayOrFutureValidatorTests(SimpleTestCase):
    def test_yesterday_rejected_today_and_future_accepted(self):
        today = date(2026, 9, 28)
        with patch('core.date_validation.timezone.localdate', return_value=today):
            with self.assertRaisesMessage(ValidationError, TODAY_OR_FUTURE_MESSAGE):
                validate_today_or_future(today - timedelta(days=1))
            self.assertEqual(validate_today_or_future(today), today)
            self.assertEqual(
                validate_today_or_future(today + timedelta(days=1)),
                today + timedelta(days=1),
            )

    @override_settings(USE_TZ=True, TIME_ZONE='Asia/Manila')
    def test_local_today_is_not_rejected_when_utc_is_still_yesterday(self):
        instant = datetime(2026, 9, 27, 16, 30, tzinfo=dt_timezone.utc)
        with patch('django.utils.timezone.now', return_value=instant):
            self.assertEqual(timezone.localdate(), date(2026, 9, 28))
            self.assertEqual(
                validate_today_or_future(date(2026, 9, 28)),
                date(2026, 9, 28),
            )

class DateRangeValidatorTests(SimpleTestCase):
    def test_historical_range_is_allowed(self):
        start, end, errors = parse_date_range('2020-01-01', '2020-01-31')
        self.assertEqual((start, end), (date(2020, 1, 1), date(2020, 1, 31)))
        self.assertEqual(errors, {})

    def test_inverted_range_has_field_level_error(self):
        _, _, errors = parse_date_range('2020-02-01', '2020-01-31')
        self.assertEqual(errors, {'date_from': DATE_RANGE_ORDER_MESSAGE})

    def test_report_range_rejects_inversion_and_keeps_last_valid_selection(self):
        request = SimpleNamespace(
            GET={
                'range': 'custom',
                'date_from': '2026-09-20',
                'date_to': '2026-09-01',
            },
            session={
                'date_range': {
                    'range': '7',
                    'date_from': '2026-09-22',
                    'date_to': '2026-09-28',
                }
            },
        )

        start, end, active = range_from_request(
            request, today=date(2026, 9, 28)
        )

        self.assertEqual((start, end, active), (
            date(2026, 9, 22), date(2026, 9, 28), '7'
        ))
        self.assertEqual(
            request.date_range_errors,
            {'date_from': DATE_RANGE_ORDER_MESSAGE},
        )

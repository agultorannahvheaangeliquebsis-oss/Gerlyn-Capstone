"""core.limits — the one definition of what a numeric input may be."""
from decimal import Decimal

from django.core.exceptions import ValidationError
from django.test import SimpleTestCase

from core.limits import (
    MAX_AMOUNT, MAX_PRICE, MAX_QUANTITY, parse_money, parse_quantity, parse_text,
    peso, price_validators, validate_image_uploads,
)


class ParseMoneyTests(SimpleTestCase):
    def test_the_price_ceiling_is_250000_pesos(self):
        self.assertEqual(MAX_PRICE, Decimal('250000.00'))

    def test_the_ceiling_itself_is_accepted(self):
        self.assertEqual(parse_money('250000', 'Price'), (Decimal('250000.00'), None))
        self.assertEqual(parse_money('250,000.00', 'Price'), (Decimal('250000.00'), None))

    def test_one_centavo_over_the_ceiling_is_refused(self):
        value, error = parse_money('250000.01', 'Retail price')
        self.assertIsNone(value)
        self.assertEqual(error, 'Retail price cannot be more than ₱250,000.00.')

    def test_zero_and_negative_are_refused_with_the_existing_wording(self):
        for raw in ('0', '0.00', '-5', '-0.01'):
            with self.subTest(raw=raw):
                self.assertEqual(
                    parse_money(raw, 'Retail price')[1], 'Retail price must be more than ₱0.'
                )

    def test_junk_is_not_a_valid_number(self):
        # NaN/Infinity parse as Decimals and then explode on comparison;
        # exponent, underscore and non-ASCII digits are accepted by Decimal()
        # but are not something anyone means to type into a price box.
        for raw in ('abc', 'NaN', 'Infinity', '-Infinity', '1e3', '1_000', '٣', '12abc', '1.2.3'):
            with self.subTest(raw=raw):
                self.assertEqual(
                    parse_money(raw, 'Price'), (None, 'Price must be a valid number.')
                )

    def test_a_third_decimal_place_is_refused_rather_than_silently_rounded(self):
        self.assertEqual(
            parse_money('12.505', 'Price')[1], 'Price can have at most 2 decimal places.'
        )
        # Trailing zeros are not a third place.
        self.assertEqual(parse_money('12.500', 'Price')[0], Decimal('12.50'))

    def test_required_and_optional_blank(self):
        self.assertEqual(parse_money('', 'Price'), (None, 'Price is required.'))
        self.assertEqual(parse_money('   ', 'Price', required=False), (None, None))
        self.assertEqual(parse_money(None, 'Price', required=False), (None, None))

    def test_a_larger_ceiling_can_be_passed_for_non_price_amounts(self):
        self.assertEqual(parse_money('9000000', 'Amount', maximum=MAX_AMOUNT)[1], None)
        self.assertIn('cannot be more than', parse_money('10000000.01', 'Amount', maximum=MAX_AMOUNT)[1])

    def test_absurdly_large_input_is_refused_not_a_crash(self):
        self.assertIn('cannot be more than', parse_money('9' * 60, 'Price')[1])


class ParseQuantityTests(SimpleTestCase):
    def test_bounds(self):
        self.assertEqual(parse_quantity('0', 'Qty'), (0, None))
        self.assertEqual(parse_quantity(str(MAX_QUANTITY), 'Qty'), (MAX_QUANTITY, None))
        self.assertEqual(
            parse_quantity(str(MAX_QUANTITY + 1), 'Qty')[1],
            f'Qty cannot be more than {MAX_QUANTITY:,}.',
        )

    def test_minimum(self):
        self.assertEqual(parse_quantity('0', 'Qty', minimum=1)[1], 'Qty must be at least 1.')

    def test_only_plain_whole_numbers(self):
        self.assertEqual(parse_quantity('-3', 'Qty')[1], 'Qty cannot be negative.')
        for raw in ('12.0', '1e3', 'abc', '٣', '1_0'):
            with self.subTest(raw=raw):
                self.assertEqual(parse_quantity(raw, 'Qty')[1], 'Qty must be a whole number.')

    def test_blank(self):
        self.assertEqual(parse_quantity('', 'Qty')[1], 'Qty is required.')
        self.assertEqual(parse_quantity('', 'Qty', required=False), (None, None))

    def test_a_huge_digit_string_is_refused_not_a_crash(self):
        # int() of a 5,000-digit string raises ValueError on its own.
        self.assertIn('cannot be more than', parse_quantity('9' * 5000, 'Qty')[1])


class ParseTextTests(SimpleTestCase):
    def test_trims_and_limits(self):
        self.assertEqual(parse_text('  hi  ', 'Name', max_length=5), ('hi', None))
        self.assertEqual(
            parse_text('x' * 6, 'Name', max_length=5)[1],
            'Name cannot be longer than 5 characters.',
        )

    def test_required(self):
        self.assertEqual(parse_text('  ', 'Name', max_length=5, required=True)[1], 'Name is required.')
        self.assertEqual(parse_text('  ', 'Name', max_length=5), ('', None))


class ModelValidatorTests(SimpleTestCase):
    def test_price_validators_enforce_both_ends(self):
        validators = price_validators()
        for value, ok in ((Decimal('0.01'), True), (Decimal('250000'), True),
                          (Decimal('0'), False), (Decimal('250000.01'), False)):
            with self.subTest(value=value):
                try:
                    for v in validators:
                        v(value)
                except ValidationError:
                    self.assertFalse(ok)
                else:
                    self.assertTrue(ok)

    def test_peso_format(self):
        self.assertEqual(peso(Decimal('250000')), '₱250,000.00')


class ImageUploadTests(SimpleTestCase):
    class _File:
        def __init__(self, name, content_type, size):
            self.name, self.content_type, self.size = name, content_type, size

    def test_good_image_passes(self):
        self.assertEqual(validate_image_uploads([self._File('a.png', 'image/png', 1024)]), [])

    def test_wrong_type_and_oversize_are_named(self):
        errors = validate_image_uploads([
            self._File('a.html', 'text/html', 10),
            self._File('b.png', 'image/png', 6 * 1024 * 1024),
        ])
        self.assertEqual(len(errors), 2)
        self.assertIn('a.html', errors[0])
        self.assertIn('larger than 5 MB', errors[1])

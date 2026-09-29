"""The one rule for what a payment reference number may be.

billing_create (web), BillingPaymentSerializer (API) and online checkout all
call clean_reference_number, and BillingRecord.reference_number carries
validate_reference_number, so the three entry points and the admin/model
layer cannot drift apart.
"""
import re
import unicodedata

from django.core.exceptions import ValidationError

# Mirrors BillingRecord.reference_number's max_length. The column is
# VARCHAR(100): anything longer is a DataError on strict MySQL and a silent
# truncation (a different reference) on non-strict.
REFERENCE_NUMBER_MAX_LENGTH = 100

# GCash and bank references are letters, digits and a little punctuation
# ("GC-2026-0001", "BT/2026/17", "ref #123"). Nothing else has a reason to be
# there, and markup or control characters only ever end up in printed
# receipts and exports. \Z, not $, which would let a trailing newline through.
_ALLOWED = re.compile(r'[A-Za-z0-9][A-Za-z0-9 ._/#:-]*\Z')

_TOO_LONG = (
    f'The reference number can be at most {REFERENCE_NUMBER_MAX_LENGTH} '
    f'characters.'
)
_BAD_CHARACTERS = (
    'The reference number can only use letters, numbers, spaces and the '
    'characters . _ / # : - and must start with a letter or number.'
)


# PaymentMethod.GCASH's value, spelled out because billing.models imports this
# module.
GCASH_METHOD = 'GCash'
GCASH_REFERENCE_LENGTH = 13
_GCASH_DIGITS_ONLY = (
    'A GCash reference number is digits only — no letters or symbols.'
)
_GCASH_LENGTH = (
    f'A GCash reference number is exactly {GCASH_REFERENCE_LENGTH} digits '
    f'(for example 1234 567 890123).'
)


def validate_reference_number(value):
    """Model-field validator. Blank is allowed here — whether a blank is
    acceptable depends on the payment method, which is
    BillingRecord.missing_payment_evidence's job."""
    if not value:
        return
    if len(value) > REFERENCE_NUMBER_MAX_LENGTH:
        raise ValidationError(_TOO_LONG, code='max_length')
    if not _ALLOWED.match(value):
        raise ValidationError(_BAD_CHARACTERS, code='invalid')


def clean_gcash_reference(raw):
    """A GCash reference is exactly 13 digits — the app shows it as
    "1234 567 890123". Spaces and dashes are dropped so the stored value is
    the bare 13 digits and two spellings of the same transfer are one string.
    Blank comes back as '' (missing_payment_evidence's job).

    static/js/gcash-reference.js mirrors this rule in the browser."""
    text = unicodedata.normalize('NFKC', raw or '')
    if not text.strip():
        return ''
    digits = re.sub(r'[\s-]', '', text)
    if not (digits.isascii() and digits.isdigit()):
        raise ValidationError(_GCASH_DIGITS_ONLY, code='invalid')
    if len(digits) != GCASH_REFERENCE_LENGTH:
        raise ValidationError(_GCASH_LENGTH, code='invalid')
    return digits


def clean_reference_number(raw, method=None):
    """Return `raw` in the form it is stored, or raise ValidationError.

    Compatibility-normalised (full-width digits become ASCII), trimmed, and
    every run of whitespace collapsed to one space — so "BT-1   2" and
    "BT-1 2", the same transfer, are one string that search can find. Blank
    or None comes back as '' untouched.

    `method` is the payment method's value. GCash has one fixed reference
    format (clean_gcash_reference); every other method keeps the general rule.
    """
    if method == GCASH_METHOD:
        return clean_gcash_reference(raw)
    text = unicodedata.normalize('NFKC', raw or '')
    text = ' '.join(text.split())
    validate_reference_number(text)
    return text

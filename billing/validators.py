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


def clean_reference_number(raw):
    """Return `raw` in the form it is stored, or raise ValidationError.

    Compatibility-normalised (full-width digits become ASCII), trimmed, and
    every run of whitespace collapsed to one space — so "GC-1   2" and
    "GC-1 2", the same transfer, are one string that search can find. Blank
    or None comes back as '' untouched.
    """
    text = unicodedata.normalize('NFKC', raw or '')
    text = ' '.join(text.split())
    validate_reference_number(text)
    return text

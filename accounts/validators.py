import re

from django.core.exceptions import ValidationError
from django.utils.translation import gettext as _


class StrongPasswordValidator:
    """Requires at least one uppercase letter, one digit, and one special character."""

    SPECIAL_CHARACTERS = "!@#$%^&*()_+-=[]{}|;:'\",.<>/?`~\\"

    def validate(self, password, user=None):
        errors = []
        if not password or len(password) > 128:
            raise ValidationError('Password must be between 8 and 128 characters.')
        if not re.search(r'[A-Z]', password):
            errors.append(_("Password must contain at least one uppercase letter."))
        if not re.search(r'[0-9]', password):
            errors.append(_("Password must contain at least one number."))
        if not any(char in self.SPECIAL_CHARACTERS for char in password):
            errors.append(_("Password must contain at least one special character (e.g. ! @ # $ % * -)."))
        if errors:
            raise ValidationError(errors)

    def get_help_text(self):
        return _(
            "Your password must contain at least one uppercase letter, one number, "
            "and one special character (e.g. Juan2000-)."
        )


# 11-digit local form (09XXXXXXXXX) or +63 international form
# (+639XXXXXXXXX) — separators are stripped before matching, so
# "0917-123-4567" and "+63 917 123 4567" validate the same as "09171234567".
_PH_MOBILE_RE = re.compile(r'^(?:\+63|0)9[0-9]{9}$')


def validate_ph_mobile_number(value):
    if len(value or '') > 20:
        raise ValidationError('Contact number cannot be longer than 20 characters.')
    stripped = re.sub(r'[\s-]', '', value or '')
    if not _PH_MOBILE_RE.match(stripped):
        raise ValidationError(
            'Enter a valid PH mobile number, e.g. 0917 123 4567 or +63 917 123 4567.',
            code='invalid_ph_mobile',
        )

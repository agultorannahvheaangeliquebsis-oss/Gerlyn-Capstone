import re

from django.core.exceptions import ValidationError
from django.utils.translation import gettext as _


class StrongPasswordValidator:
    """Requires at least one uppercase letter, one digit, and one special character."""

    SPECIAL_CHARACTERS = "!@#$%^&*()_+-=[]{}|;:'\",.<>/?`~\\"

    def validate(self, password, user=None):
        errors = []
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

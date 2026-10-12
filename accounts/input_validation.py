"""Shared input rules for HTML, REST and administrative account writes."""
import unicodedata

from django import forms
from django.core.exceptions import ValidationError
from django.core.validators import RegexValidator
from django.db.models import Q

from .validators import validate_ph_mobile_number


def validate_person_name(value):
    if value and (not any(c.isalpha() for c in value) or any(
        not (c.isalpha() or unicodedata.category(c).startswith('M') or c in " -'’.")
        for c in value
    )):
        raise ValidationError("Use letters, spaces, hyphens, apostrophes or periods for names.")


def account_inputs(data, *, user=None, customer=False, required=False):
    """Return normalized values and field errors, before any persistent writes.

    Only supplied fields are checked on partial edits. Customer name columns
    are 50 characters; internal display names are 100 including the separator.
    """
    from .models import User
    from .services import contact_number_taken

    fields = {
        'username': forms.CharField(min_length=1, max_length=150, validators=[User.username_validator]),
        'email': forms.EmailField(max_length=254, required=required or customer),
        'first_name': forms.CharField(max_length=50 if customer else 100, required=required or customer, validators=[validate_person_name]),
        'last_name': forms.CharField(max_length=50 if customer else 100, required=required or customer, validators=[validate_person_name]),
        'contact_person': forms.CharField(max_length=100, required=False, validators=[validate_person_name]),
        'fullName': forms.CharField(max_length=100, required=False, validators=[validate_person_name]),
        'supplier_name': forms.CharField(max_length=100, required=True),
        'contact_number': forms.CharField(max_length=20, required=False, validators=[validate_ph_mobile_number]),
        'address': forms.CharField(max_length=500, required=required),
        'text': forms.CharField(max_length=500),
        'street_detail': forms.CharField(max_length=255, required=False),
        'region': forms.CharField(max_length=10, required=False),
        'province': forms.CharField(max_length=10, required=False),
        'city': forms.CharField(max_length=10, required=False),
        'barangay': forms.CharField(max_length=15, required=False),
        'business_registration_id': forms.CharField(max_length=100, required=False, validators=[RegexValidator(
            r'\A[A-Za-z0-9][A-Za-z0-9 ./_-]*\Z',
            'Use letters, numbers, spaces, periods, slashes, underscores or hyphens for the business/tax identifier.',
        )]),
    }
    values, errors = {}, {}
    for key, field in fields.items():
        if key not in data and not (required and key in ('username', 'email', 'first_name', 'last_name', 'address')):
            continue
        try:
            raw = data.get(key, '')
            if key == 'username' and isinstance(raw, str):
                raw = User.normalize_username(raw)
            values[key] = field.clean(raw)
        except ValidationError as exc:
            errors[key] = ' '.join(exc.messages)
    if not customer and len(f"{values.get('first_name', '')} {values.get('last_name', '')}".strip()) > 100:
        errors['last_name'] = 'First and last name together cannot be longer than 100 characters.'
    others = User.objects.all()
    if user and user.pk:
        others = others.exclude(pk=user.pk)
    if values.get('username') and others.filter(username__iexact=values['username']).exists():
        errors['username'] = 'That username is already taken.'
    if values.get('email'):
        values['email'] = values['email'].lower()
        if others.filter(Q(email__iexact=values['email']) | Q(pending_email__iexact=values['email'])).exists():
            errors['email'] = 'That email is already associated with another account.'
    if values.get('contact_number') and contact_number_taken(values['contact_number'], exclude_user=user):
        errors['contact_number'] = 'That contact number is already in use.'
    identifier = values.get('business_registration_id')
    if identifier:
        values['business_registration_id'] = identifier.upper()
        if others.filter(supplier_profile__business_registration_id__iexact=identifier).exists():
            errors['business_registration_id'] = 'That business/tax registration identifier already belongs to another supplier.'
    return values, errors

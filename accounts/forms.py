from django import forms

from .models import (
    BranchManagerProfile,
    CustomerProfile,
    OwnerProfile,
    StaffProfile,
    SupplierProfile,
    User,
)


class SupplierSelfProfileForm(forms.Form):
    """The explicit set of profile fields a supplier may self-manage.

    This is deliberately a ``Form`` rather than a broad ``ModelForm``. The
    supplier's category, branch, account state, permissions, and product
    offers are owner-controlled and therefore cannot be made writable by a
    forged POST field.
    """

    supplier_name = forms.CharField(label="Supplier / company name", max_length=100)
    contact_person = forms.CharField(label="Contact person", max_length=100)
    email = forms.EmailField(max_length=254)
    contact_number = forms.CharField(label="Phone number", max_length=20)
    address = forms.CharField(label="Business address", widget=forms.Textarea)

    def __init__(self, *args, user, **kwargs):
        self.user = user
        profile = getattr(user, "supplier_profile", None)
        kwargs.setdefault(
            "initial",
            {
                "supplier_name": getattr(profile, "supplier_name", ""),
                "contact_person": getattr(profile, "contact_person", ""),
                "email": user.email,
                "contact_number": getattr(profile, "contact_number", ""),
                "address": getattr(profile, "address", ""),
            },
        )
        super().__init__(*args, **kwargs)

    def clean_email(self):
        email = self.cleaned_data["email"].strip().lower()
        if (
            User.objects.exclude(pk=self.user.pk).filter(email__iexact=email).exists()
            or User.objects.exclude(pk=self.user.pk).filter(
                pending_email__iexact=email
            ).exists()
        ):
            raise forms.ValidationError(
                "That email is already associated with another account."
            )
        return email

    def clean_contact_number(self):
        number = self.cleaned_data["contact_number"].strip()
        profile_models = (
            BranchManagerProfile,
            StaffProfile,
            CustomerProfile,
            OwnerProfile,
            SupplierProfile,
        )
        if any(
            model.objects.exclude(user=self.user)
            .filter(contact_number=number)
            .exists()
            for model in profile_models
        ):
            raise forms.ValidationError("That contact number is already in use.")
        return number

    def save(self):
        profile = self.user.get_or_create_role_profile()
        if not isinstance(profile, SupplierProfile):
            raise ValueError("SupplierSelfProfileForm can only save a supplier profile.")

        for field in (
            "supplier_name",
            "contact_person",
            "contact_number",
            "address",
        ):
            setattr(profile, field, self.cleaned_data[field])
        profile.save(
            update_fields=[
                "supplier_name",
                "contact_person",
                "contact_number",
                "address",
            ]
        )
        return profile

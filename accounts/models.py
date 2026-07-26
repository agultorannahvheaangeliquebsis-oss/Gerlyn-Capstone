# accounts/models.py
from django.contrib.auth.models import AbstractUser
from django.db import models
from django.utils import timezone
import uuid


def avatar_upload_path(instance, filename):
    ext = filename.rsplit('.', 1)[-1].lower()
    return f"avatars/{instance.pk}/avatar.{ext}"


class User(AbstractUser):
    class Role(models.TextChoices):
        OWNER          = 'OWNER', 'Owner'
        BRANCH_MANAGER = 'BRANCH_MANAGER', 'Branch Manager'
        STAFF          = 'STAFF', 'Store Staff'
        SUPPLIER       = 'SUPPLIER', 'Supplier'
        CUSTOMER       = 'CUSTOMER', 'Customer'

    role = models.CharField(
        max_length=20, choices=Role.choices, default=Role.CUSTOMER
    )

    avatar = models.ImageField(
        upload_to=avatar_upload_path,
        null=True, blank=True,
        help_text="Profile picture. JPEG/PNG/WEBP, max 2 MB."
    )
    is_email_verified = models.BooleanField(default=False)
    pending_email     = models.EmailField(null=True, blank=True)
    two_factor_enabled = models.BooleanField(default=False)

    def __str__(self):
        return f"{self.username} ({self.get_role_display()})"

    def get_initials(self):
        """Returns 'JA' for 'Joshua Agulto', or first letter of username."""
        name = self.get_full_name()
        parts = name.split()
        if len(parts) >= 2:
            return (parts[0][0] + parts[-1][0]).upper()
        if parts:
            return parts[0][0].upper()
        return self.username[0].upper()

    def get_role_profile(self):
        """Returns whichever role-specific profile this user has, or None."""
        for attr in ('owner_profile', 'manager_profile', 'staff_profile',
                     'supplier_profile', 'customer_profile'):
            profile = getattr(self, attr, None)
            if profile is not None:
                return profile
        return None


class VerificationToken(models.Model):
    """Single-use, time-limited tokens for email verification."""
    PURPOSE_EMAIL_VERIFY    = 'EMAIL_VERIFY'
    PURPOSE_EMAIL_CHANGE    = 'EMAIL_CHANGE'
    PURPOSE_PASSWORD_CHANGE = 'PASSWORD_CHANGE'
    PURPOSE_CHOICES = [
        (PURPOSE_EMAIL_VERIFY, 'Initial email verification'),
        (PURPOSE_EMAIL_CHANGE, 'Email change verification'),
        (PURPOSE_PASSWORD_CHANGE, 'Password change verification'),
    ]
    EXPIRY_HOURS = 24
    MAX_ATTEMPTS = 3

    user       = models.ForeignKey(
        User, on_delete=models.CASCADE, related_name='tokens'
    )
    token      = models.UUIDField(default=uuid.uuid4, unique=True, editable=False)
    purpose    = models.CharField(max_length=20, choices=PURPOSE_CHOICES)
    is_used    = models.BooleanField(default=False)
    attempts   = models.PositiveSmallIntegerField(default=0)
    created_at = models.DateTimeField(auto_now_add=True)
    expires_at = models.DateTimeField()

    def save(self, *args, **kwargs):
        if not self.pk:
            self.expires_at = timezone.now() + timezone.timedelta(hours=self.EXPIRY_HOURS)
        super().save(*args, **kwargs)

    @property
    def is_valid(self):
        return not self.is_used and timezone.now() < self.expires_at

    def consume(self):
        self.is_used = True
        self.save(update_fields=['is_used'])

    def register_failed_attempt(self):
        """Records a failed attempt; blocks (consumes) the token once MAX_ATTEMPTS is hit.
        Returns True if the token is now blocked."""
        self.attempts += 1
        if self.attempts >= self.MAX_ATTEMPTS:
            self.is_used = True
        self.save(update_fields=['attempts', 'is_used'])
        return self.is_used

    def __str__(self):
        return f"{self.purpose} for {self.user.username}"


# --- All existing profile models below — completely unchanged ---

class OwnerProfile(models.Model):
    user = models.OneToOneField(
        User, on_delete=models.CASCADE, related_name='owner_profile'
    )
    owner_name     = models.CharField(max_length=100)
    contact_number = models.CharField(max_length=20)

    def __str__(self):
        return self.owner_name


class BranchManagerProfile(models.Model):
    user = models.OneToOneField(
        User, on_delete=models.CASCADE, related_name='manager_profile'
    )
    branch = models.ForeignKey(
        'branches.Branch', on_delete=models.SET_NULL,
        null=True, blank=True, related_name='managers'
    )
    manager_name   = models.CharField(max_length=100)
    contact_number = models.CharField(max_length=20)

    def __str__(self):
        return self.manager_name


class StaffProfile(models.Model):
    user = models.OneToOneField(
        User, on_delete=models.CASCADE, related_name='staff_profile'
    )
    branch = models.ForeignKey(
        'branches.Branch', on_delete=models.SET_NULL,
        null=True, blank=True, related_name='staff'
    )
    staff_name     = models.CharField(max_length=100)
    contact_number = models.CharField(max_length=20)
    date_hired     = models.DateField(null=True, blank=True)
    shift_start    = models.TimeField(null=True, blank=True)
    shift_end      = models.TimeField(null=True, blank=True)

    def __str__(self):
        return self.staff_name


class CustomerProfile(models.Model):
    user = models.OneToOneField(
        User, on_delete=models.CASCADE, related_name='customer_profile'
    )
    first_name     = models.CharField(max_length=50)
    last_name      = models.CharField(max_length=50)
    contact_number = models.CharField(max_length=20)
    address        = models.TextField(blank=True)

    def __str__(self):
        return f"{self.first_name} {self.last_name}"


class SupplierProfile(models.Model):
    user = models.OneToOneField(
        User, on_delete=models.CASCADE, related_name='supplier_profile'
    )
    supplier_name     = models.CharField(max_length=100)
    contact_number    = models.CharField(max_length=20)
    address           = models.TextField(blank=True)
    supplier_category = models.CharField(max_length=100, blank=True)
    contact_person    = models.CharField(max_length=100, blank=True)

    def __str__(self):
        return self.supplier_name
    
    
class Notification(models.Model):
    user = models.ForeignKey(
        User, on_delete=models.CASCADE,
        related_name='notifications'
    )
    title = models.CharField(max_length=255)
    message = models.TextField()
    is_read = models.BooleanField(default=False)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ['-created_at']

    def __str__(self):
        return f"Notification for {self.user.username}: {self.title}"
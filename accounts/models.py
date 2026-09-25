# accounts/models.py
from django.contrib.auth.models import AbstractUser
from django.db import models
from django.utils import timezone
import uuid

from core.module_permissions import default_allowed_modules
from .validators import validate_ph_mobile_number


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

    class Language(models.TextChoices):
        ENGLISH  = 'en', 'English'
        FILIPINO = 'fil', 'Filipino'

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
    language = models.CharField(
        max_length=8, choices=Language.choices, default=Language.ENGLISH
    )

    # Per-user restriction on top of role (core.module_permissions). Only
    # meaningful for STAFF/BRANCH_MANAGER — ignored entirely for OWNER,
    # SUPPLIER, CUSTOMER. Defaults to every module so a Branch Manager's
    # existing full access is preserved; a Staff account's real default is
    # set explicitly wherever one is created (this field default is just the
    # safe fallback for anything that bypasses that, e.g. a fixture/shell).
    allowed_modules = models.JSONField(
        default=default_allowed_modules,
        blank=True,
        help_text="Which internal sections this account can see, on top of "
                  "its role. Owner is exempt and always sees everything."
    )

    # Set only by accounts.services.hard_delete_personal_data — a deliberate,
    # Owner-triggered erasure of this account's identity fields in response
    # to an actual request (received outside the system, by email), never by
    # ordinary self-service deactivation (is_active alone covers that, and
    # is fully reversible). The row itself is never deleted and this FK is
    # never cleared, so every table that references this user by id — sales,
    # stock movements, billing confirmations — keeps working exactly as
    # before; only the identity fields on THIS row go blank. This pair is
    # the accountability record itself: who erased it and when, which is
    # all a request like this needs to prove after the fact.
    deleted_at = models.DateTimeField(null=True, blank=True)
    deleted_by = models.ForeignKey(
        'self', null=True, blank=True,
        on_delete=models.SET_NULL, related_name='personal_data_deletions_performed',
        help_text="The Owner who erased this account's personal data, if it has been."
    )

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

    def get_or_create_role_profile(self):
        """Like get_role_profile(), but creates the profile (with the same
        defaults accounts.signals.create_user_profile uses) if it's missing.
        Covers legacy/seed accounts created before that signal existed —
        without this, code that writes to the role profile (e.g. saving a
        contact number) silently no-ops for those accounts instead of
        erroring or succeeding. Reads that don't need to write should keep
        using get_role_profile() instead, since this can create a row."""
        profile = self.get_role_profile()
        if profile is not None:
            return profile
        if self.role == self.Role.OWNER:
            return OwnerProfile.objects.create(user=self, owner_name=self.username)
        if self.role == self.Role.BRANCH_MANAGER:
            return BranchManagerProfile.objects.create(user=self, manager_name=self.username)
        if self.role == self.Role.STAFF:
            return StaffProfile.objects.create(user=self, staff_name=self.username)
        if self.role == self.Role.SUPPLIER:
            return SupplierProfile.objects.create(user=self, supplier_name=self.username)
        if self.role == self.Role.CUSTOMER:
            return CustomerProfile.objects.create(user=self, first_name=self.username, last_name='')
        return None


class VerificationToken(models.Model):
    """Single-use, time-limited tokens for email verification."""
    PURPOSE_EMAIL_VERIFY    = 'EMAIL_VERIFY'
    PURPOSE_EMAIL_CHANGE    = 'EMAIL_CHANGE'
    PURPOSE_PASSWORD_CHANGE = 'PASSWORD_CHANGE'
    PURPOSE_ACCOUNT_DEACTIVATE = 'ACCOUNT_DEACTIVATE'
    PURPOSE_STAFF_INVITE    = 'STAFF_INVITE'
    PURPOSE_CHOICES = [
        (PURPOSE_STAFF_INVITE, 'Internal user invite'),
        (PURPOSE_EMAIL_VERIFY, 'Initial email verification'),
        (PURPOSE_EMAIL_CHANGE, 'Email change verification'),
        (PURPOSE_PASSWORD_CHANGE, 'Password change verification'),
        (PURPOSE_ACCOUNT_DEACTIVATE, 'Account deactivation verification'),
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
    contact_number = models.CharField(max_length=20, blank=True, validators=[validate_ph_mobile_number])

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
    contact_number = models.CharField(max_length=20, blank=True, validators=[validate_ph_mobile_number])

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
    contact_number = models.CharField(max_length=20, blank=True, validators=[validate_ph_mobile_number])
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
    contact_number = models.CharField(max_length=20, blank=True, validators=[validate_ph_mobile_number])
    address        = models.TextField(blank=True)

    def __str__(self):
        return f"{self.first_name} {self.last_name}"


class Address(models.Model):
    """A saved delivery address. Exactly one per user is ever marked default
    (enforced in Address.objects.set_default, not at the DB level)."""
    class Type(models.TextChoices):
        HOME  = 'HOME', 'Home'
        WORK  = 'WORK', 'Work'
        OTHER = 'OTHER', 'Other'

    user       = models.ForeignKey(
        User, on_delete=models.CASCADE, related_name='addresses'
    )
    type       = models.CharField(max_length=10, choices=Type.choices, default=Type.HOME)
    text       = models.TextField()
    is_default = models.BooleanField(default=False)
    created_at = models.DateTimeField(auto_now_add=True)

    # Structured location, filled in from the Region/Province/City/Barangay
    # dropdowns. PSGC codes rather than names — names are looked up on demand
    # via accounts.geo, which keeps these in sync with the bundled dataset
    # instead of freezing a copy. `text` (above) is the source of truth for
    # the human-readable address shown everywhere.
    region        = models.CharField(max_length=10, blank=True)
    province      = models.CharField(max_length=10, blank=True)
    city          = models.CharField(max_length=10, blank=True)
    barangay      = models.CharField(max_length=15, blank=True)
    street_detail = models.CharField(max_length=255, blank=True)

    # Legacy pin coordinates. No longer captured — the map picker was
    # dropped as out of scope — but kept nullable so existing rows are
    # untouched. Nothing reads these.
    latitude  = models.DecimalField(max_digits=9, decimal_places=6, null=True, blank=True)
    longitude = models.DecimalField(max_digits=9, decimal_places=6, null=True, blank=True)

    class Meta:
        ordering = ['-is_default', '-created_at']

    def __str__(self):
        return f"{self.get_type_display()} — {self.text[:40]}"

    def set_as_default(self):
        """Makes this the user's one default address, unsetting any other."""
        Address.objects.filter(user=self.user, is_default=True).exclude(pk=self.pk).update(is_default=False)
        if not self.is_default:
            self.is_default = True
            self.save(update_fields=['is_default'])

    def compose_text(self):
        """Builds a human-readable address from the structured PSGC fields —
        the fallback used when the picker's text field is left untouched."""
        from . import geo
        parts = [self.street_detail]
        if self.barangay:
            parts.append(f"Brgy. {geo.barangay_name(self.barangay)}")
        if self.city:
            parts.append(geo.city_name(self.city))
        if self.province:
            parts.append(geo.province_name(self.province))
        return ", ".join(p for p in parts if p)


class NotificationPreference(models.Model):
    user               = models.OneToOneField(
        User, on_delete=models.CASCADE, related_name='notification_preference'
    )
    order_updates      = models.BooleanField(default=True)
    promotions         = models.BooleanField(default=True)
    delivery_reminders = models.BooleanField(default=True)
    newsletter         = models.BooleanField(default=False)

    def __str__(self):
        return f"Notification preferences for {self.user.username}"


class UserSession(models.Model):
    """Tracks one row per active login (per Django session), so Settings >
    Sessions can list and individually revoke them. Django's own session
    store has no user->sessions index, so this is what makes that
    queryable without scanning every session in the table.

    Rows are created on login and deleted on logout (both regular and the
    idle-timeout logout), and pruned lazily on read against whichever
    django_session rows are still unexpired — no separate cleanup job.
    """
    user         = models.ForeignKey(User, on_delete=models.CASCADE, related_name='sessions')
    session_key  = models.CharField(max_length=40, unique=True)
    ip_address   = models.GenericIPAddressField(null=True, blank=True)
    user_agent   = models.CharField(max_length=255, blank=True)
    created_at   = models.DateTimeField(auto_now_add=True)
    last_active  = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ['-last_active']

    def __str__(self):
        return f"Session for {self.user.username} ({self.session_key[:8]}…)"

    def device_label(self):
        from .device_info import describe_user_agent
        return describe_user_agent(self.user_agent)


class SupplierCategory(models.Model):
    """What kind of goods a supplier sells — a managed list the Owner
    maintains, replacing the free-text field SupplierProfile used to carry
    (which produced "Canned Goods" / "canned goods" / "Cans" as three
    different categories).

    product_categories optionally maps this supplier category onto the
    storefront's ProductCategory rows; the supplier's own "add a product"
    picker starts narrowed to those so a canned-goods supplier isn't scrolling
    the whole catalog. It is a convenience filter, never a permission —
    what a supplier may be ordered from is decided by its approved offers."""

    name = models.CharField(max_length=100, unique=True)
    product_categories = models.ManyToManyField(
        'inventory.ProductCategory', blank=True, related_name='supplier_categories',
    )
    is_active = models.BooleanField(
        default=True,
        help_text="Inactive categories stay on existing suppliers but can't "
                  "be picked for new ones.",
    )

    class Meta:
        verbose_name_plural = 'Supplier categories'
        ordering = ['name']

    def __str__(self):
        return self.name


class SupplierProfile(models.Model):
    user = models.OneToOneField(
        User, on_delete=models.CASCADE, related_name='supplier_profile'
    )
    branch = models.ForeignKey(
        'branches.Branch', on_delete=models.SET_NULL,
        null=True, blank=True, related_name='suppliers',
        help_text="Which branch this supplier serves. Left blank, the "
                  "supplier is treated as available to every branch — "
                  "assign one to scope it to just that branch's dropdowns."
    )
    supplier_name     = models.CharField(max_length=100)
    contact_number    = models.CharField(max_length=20, blank=True, validators=[validate_ph_mobile_number])
    address           = models.TextField(blank=True)
    category          = models.ForeignKey(
        SupplierCategory, on_delete=models.SET_NULL,
        null=True, blank=True, related_name='suppliers',
    )
    contact_person    = models.CharField(max_length=100, blank=True)

    def __str__(self):
        return self.supplier_name
    
    
class Notification(models.Model):
    """
    One notification, one row, one recipient — a customer's order update and
    an Owner's "PO delayed" alert are the same model. Fan-out to several
    recipients (e.g. the Owner + a Branch Manager) means several rows, not a
    many-to-many: each reads/resolves independently.

    `category`/`priority`/`branch`/`action_*`/`requires_action` back the
    internal Owner Notification Center (core.notifications.notify(), see
    core/views.py:notification_center_view). They're blank/default on a
    plain customer notification (order updates etc., created directly with
    Notification.objects.create() as before this feature) — that's what
    keeps this one model serving both audiences without a second table.
    """

    class Category(models.TextChoices):
        STAFF = 'staff', 'Staff'
        PROCUREMENT = 'procurement', 'Procurement & Inventory'
        SALES = 'sales', 'Sales & Financial'
        SYSTEM = 'system', 'System & Security'

    class Priority(models.TextChoices):
        CRITICAL = 'critical', 'Critical'
        HIGH = 'high', 'High'
        MEDIUM = 'medium', 'Medium'
        LOW = 'low', 'Low'

    class EventType(models.TextChoices):
        """Stable business-event codes used by the notification matrix.

        ``GENERAL`` keeps customer notices and historical rows created before
        the matrix valid.  Operational alerts should use one of the named
        events so the database records *why* a row exists, independently of
        wording that may later change.
        """

        GENERAL = '', 'General'
        LOW_STOCK = 'low_stock', 'Low stock'
        NEAR_EXPIRY = 'near_expiry', 'Near expiry'
        PO_APPROVED = 'po_approved', 'Purchase order approved'
        INCOMING_DELIVERY = 'incoming_delivery', 'Incoming delivery'
        ORDER_DELIVERED = 'order_delivered', 'Order delivered'
        PAYMENT_VERIFIED = 'payment_verified', 'Payment verified'
        PAYMENT_REJECTED = 'payment_rejected', 'Payment rejected'
        EXPIRY_WRITE_OFF = 'expiry_write_off', 'Expiry write-off'

    user = models.ForeignKey(
        User, on_delete=models.CASCADE,
        related_name='notifications'
    )
    title = models.CharField(max_length=255)
    message = models.TextField()
    is_read = models.BooleanField(default=False)
    created_at = models.DateTimeField(auto_now_add=True)
    read_at = models.DateTimeField(
        null=True, blank=True,
        help_text="When the recipient first marked this notification read.",
    )

    # Stable audit metadata.  The human title/message can change without
    # losing which event produced the row or which business record it was
    # about. recipient_role is deliberately a snapshot: a later role change
    # must not rewrite who received the notice at the time it was sent.
    event_type = models.CharField(
        max_length=40, choices=EventType.choices, blank=True, default='',
        db_index=True,
    )
    recipient_role = models.CharField(
        max_length=20, choices=User.Role.choices, blank=True, default='',
    )
    reference_type = models.CharField(max_length=40, blank=True, default='')
    reference_id = models.CharField(max_length=64, blank=True, default='')

    # --- Owner Notification Center fields ------------------------------
    category = models.CharField(
        max_length=20, choices=Category.choices, blank=True, default='',
        help_text="Blank for a plain customer notification (order updates, "
                  "etc). Set for anything raised by core.notifications.notify()."
    )
    priority = models.CharField(
        max_length=10, choices=Priority.choices, default=Priority.LOW,
    )
    branch = models.ForeignKey(
        'branches.Branch', on_delete=models.SET_NULL,
        null=True, blank=True, related_name='notifications',
    )
    action_url = models.CharField(max_length=255, blank=True)
    action_label = models.CharField(max_length=40, blank=True)
    requires_action = models.BooleanField(
        default=False,
        help_text="High/Critical items that stay pinned as unresolved until "
                  "acted on, rather than clearing on read alone.",
    )
    resolved_at = models.DateTimeField(null=True, blank=True)

    class Meta:
        ordering = ['-created_at']
        indexes = [
            models.Index(fields=['user', 'category', '-created_at']),
        ]

    def __str__(self):
        return f"Notification for {self.user.username}: {self.title}"

    @property
    def is_resolved(self):
        return not self.requires_action or self.resolved_at is not None

    def resolve(self):
        self.is_read = True
        now = timezone.now()
        self.read_at = self.read_at or now
        self.resolved_at = now
        self.save(update_fields=['is_read', 'read_at', 'resolved_at'])

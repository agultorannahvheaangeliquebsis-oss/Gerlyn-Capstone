from django.db import models
from sales.models import SalesTransaction
from accounts.models import User


class BillingRecord(models.Model):
    class PaymentStatus(models.TextChoices):
        PENDING = 'Pending', 'Pending'
        PAID = 'Paid', 'Paid'
        REJECTED = 'Rejected', 'Rejected'
        # The order behind this billing record was cancelled or voided —
        # distinct from REJECTED, which means a submitted payment proof was
        # specifically turned down, not that the order itself fell through.
        CANCELLED = 'Cancelled', 'Cancelled'

    class PaymentMethod(models.TextChoices):
        CASH = 'Cash', 'Cash'
        GCASH = 'GCash', 'GCash'
        CARD = 'Card', 'Card'
        BANK_TRANSFER = 'Bank Transfer', 'Bank Transfer'
        # Recorded only — funds move outside the system (Chapter 1, Limitation 1).
        COD = 'Cash on Delivery', 'Cash on Delivery'
        PAY_AT_STORE = 'Pay at Store', 'Pay at Store'

    # The only payment-status changes billing_confirm / BillingConfirmSerializer
    # will apply. Without this map both entry points accepted any target that
    # was merely one of the four valid strings, so a record could be flipped
    # Paid -> Rejected -> Paid indefinitely, each pass silently overwriting
    # confirmed_by / date_confirmed with no history.
    #
    #   Pending  -> Paid | Rejected
    #   Rejected -> Pending          (re-open for another payment attempt)
    #   Paid     -> (terminal here)
    #   Cancelled-> (terminal here)
    #
    # CANCELLED is reached only by the order itself being cancelled or voided
    # (sales.views.transaction_set_status writes it straight onto the model),
    # never through the confirm form, so it has no outbound entry.
    ALLOWED_STATUS_TRANSITIONS = {
        PaymentStatus.PENDING: {PaymentStatus.PAID, PaymentStatus.REJECTED},
        PaymentStatus.REJECTED: {PaymentStatus.PENDING},
        PaymentStatus.PAID: set(),
        PaymentStatus.CANCELLED: set(),
    }

    def can_transition_to(self, new_status):
        """True when billing_confirm may move this record to `new_status`."""
        return new_status in self.ALLOWED_STATUS_TRANSITIONS.get(self.status, set())

    transaction = models.OneToOneField(
        SalesTransaction, on_delete=models.CASCADE,
        related_name='billing_record'
    )
    amount_due = models.DecimalField(max_digits=12, decimal_places=2)
    payment_method = models.CharField(
        max_length=20, choices=PaymentMethod.choices,
        default=PaymentMethod.CASH
    )
    reference_number = models.CharField(
        max_length=100, blank=True,
        help_text="GCash/bank reference number, if applicable"
    )
    proof_of_payment = models.ImageField(
        upload_to='payment_proofs/', null=True, blank=True,
        help_text="Optional uploaded screenshot/photo of payment"
    )
    status = models.CharField(
        max_length=10, choices=PaymentStatus.choices,
        default=PaymentStatus.PENDING
    )
    confirmed_by = models.ForeignKey(
        User, on_delete=models.SET_NULL, null=True, blank=True,
        limit_choices_to={'role__in': ['OWNER', 'BRANCH_MANAGER', 'STAFF']},
        related_name='confirmed_billings'
    )
    date_recorded = models.DateTimeField(auto_now_add=True)
    date_confirmed = models.DateTimeField(null=True, blank=True)

    def __str__(self):
        return f"Billing #{self.id} - Transaction #{self.transaction_id} - {self.status}"

    class Meta:
        verbose_name_plural = "Billing Records"
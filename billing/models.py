from django.db import models
from sales.models import SalesTransaction
from accounts.models import User
from .storage import PaymentProofStorage


payment_proof_storage = PaymentProofStorage()


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
    #   Pending  -> Paid | Rejected | Cancelled
    #   Rejected -> Pending | Cancelled     (re-open for another payment attempt)
    #   Paid     -> Cancelled               (terminal to confirm/reject either way)
    #   Cancelled-> (terminal here)
    #
    # B-1 — CANCELLED is reachable from every state, but ONLY the order's own
    # cancellation may actually take that route: sales.views.transaction_set_
    # status is the one caller that ever passes CANCELLED here (through
    # can_transition_to, same as every other caller of this map, rather than
    # writing the status straight onto the field). billing_confirm and
    # BillingConfirmSerializer never offer CANCELLED as a choice a human can
    # pick — their own `offered`/ChoiceField sets stop at Paid/Rejected/
    # Pending — so adding it here does not open a new control anywhere; it
    # closes the gap where this map claimed Paid was terminal while a
    # completely different module quietly moved it anyway.
    ALLOWED_STATUS_TRANSITIONS = {
        PaymentStatus.PENDING: {PaymentStatus.PAID, PaymentStatus.REJECTED, PaymentStatus.CANCELLED},
        PaymentStatus.REJECTED: {PaymentStatus.PENDING, PaymentStatus.CANCELLED},
        PaymentStatus.PAID: {PaymentStatus.CANCELLED},
        PaymentStatus.CANCELLED: set(),
    }

    def can_transition_to(self, new_status):
        """True when billing_confirm may move this record to `new_status`."""
        return new_status in self.ALLOWED_STATUS_TRANSITIONS.get(self.status, set())

    def is_self_confirmation(self, user):
        """True when `user` recorded this sale and must not confirm its payment.

        A-1 maker/checker separation. A Staff member who rang up a walk-in
        sale cannot be the one to confirm the payment for it — someone else
        has to check it. Owner and Branch Manager are trusted to confirm a
        sale they recorded themselves: the role test is simply false for
        them, so they never reach the identity comparison — the role
        condition IS the override, there is no separate branch for it.

        A sale with no recorded staff actor — an Owner/Manager-rung walk-in,
        or any online order, where SalesTransaction.staff is None — can
        never equal a Staff user's id, so this returns False on its own with
        no null-check needed. See Finding A-3.

        Both entry points (billing_confirm and BillingConfirmSerializer) call
        this — one definition, same rule as can_transition_to.
        """
        return (
            user.role == User.Role.STAFF
            and self.transaction.staff_id == user.id
        )

    def sync_payment_method_to_sale(self):
        """X-2 — keep the sale's own mode_of_payment in step with what was
        just recorded on its bill. billing_create (web) and
        BillingPaymentSerializer.update (API) both only ever touch this
        BillingRecord, so without this the Order Console could go on
        showing a payment method the bill itself had already moved past.

        Bank Transfer has no SalesTransaction.ModeOfPayment equivalent (it
        is a billing-only method, recorded for wholesale/office accounts
        that settle by transfer rather than at the counter), so there is
        nothing to sync in that one case — the sale keeps whatever method
        it already had.

        Both entry points call this — one definition, same rule as
        can_transition_to / is_self_confirmation above.
        """
        txn = self.transaction
        if (
            self.payment_method in SalesTransaction.ModeOfPayment.values
            and txn.mode_of_payment != self.payment_method
        ):
            txn.mode_of_payment = self.payment_method
            txn.save(update_fields=['mode_of_payment'])

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
        upload_to='payment_proofs/', storage=payment_proof_storage,
        null=True, blank=True,
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

    # Voiding a completed sale (or cancelling an earlier-stage order) moves
    # this record to CANCELLED from sales.views.transaction_set_status. That
    # is a different actor at a different moment from the original payment
    # confirmation, so it gets its own trail: confirmed_by / date_confirmed
    # stay pointing at whoever cleared the payment, and these record who
    # voided the record and when. Either can be null — a still-Pending order
    # that is cancelled was never confirmed, and a void done before this
    # field existed has neither.
    voided_by = models.ForeignKey(
        User, on_delete=models.SET_NULL, null=True, blank=True,
        limit_choices_to={'role__in': ['OWNER', 'BRANCH_MANAGER', 'STAFF']},
        related_name='voided_billings'
    )
    date_voided = models.DateTimeField(null=True, blank=True)

    def __str__(self):
        return f"Billing #{self.id} - Transaction #{self.transaction_id} - {self.status}"

    class Meta:
        verbose_name_plural = "Billing Records"

from django.core.exceptions import ValidationError
from django.db import models, transaction as db_transaction
from sales.models import SalesTransaction
from accounts.models import User
from .storage import PaymentProofStorage
from .validators import REFERENCE_NUMBER_MAX_LENGTH, validate_reference_number


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
        # sales.views.transaction_refund is the only writer of either of
        # these — set once a Refund exists against a Paid record, so Billing
        # (list, filters, report) can show which orders were refunded
        # without opening each one in Sales & Orders. PARTIALLY_REFUNDED
        # while sales.views._refundable_items still finds unrefunded
        # quantity on the order; REFUNDED once none remains.
        PARTIALLY_REFUNDED = 'Partially Refunded', 'Partially Refunded'
        REFUNDED = 'Refunded', 'Refunded'

    class PaymentMethod(models.TextChoices):
        CASH = 'Cash', 'Cash'
        GCASH = 'GCash', 'GCash'
        CARD = 'Card', 'Card'
        MAYA = 'Maya', 'Maya'
        QR_PH = 'QR Ph', 'QR Ph'
        CHARGE_SALE = 'Charge Sale', 'Charge Sale'
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
    #   Pending            -> Paid | Rejected | Cancelled
    #   Rejected           -> Pending | Cancelled           (re-open for another payment attempt)
    #   Paid               -> Cancelled | Partially Refunded | Refunded
    #   Partially Refunded -> Refunded | Cancelled          (more of the order gets refunded, or it's voided)
    #   Refunded           -> (terminal here — nothing left to refund)
    #   Cancelled          -> (terminal here)
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
    #
    # PARTIALLY_REFUNDED / REFUNDED follow the same shape — only
    # sales.views.transaction_refund ever passes either here (also through
    # can_transition_to, never written straight onto the field), so a human
    # still has no direct control to flip a record to either from Billing.
    ALLOWED_STATUS_TRANSITIONS = {
        PaymentStatus.PENDING: {PaymentStatus.PAID, PaymentStatus.REJECTED, PaymentStatus.CANCELLED},
        PaymentStatus.REJECTED: {PaymentStatus.PENDING, PaymentStatus.CANCELLED},
        PaymentStatus.PAID: {
            PaymentStatus.CANCELLED, PaymentStatus.PARTIALLY_REFUNDED, PaymentStatus.REFUNDED,
        },
        PaymentStatus.PARTIALLY_REFUNDED: {PaymentStatus.REFUNDED, PaymentStatus.CANCELLED},
        PaymentStatus.REFUNDED: set(),
        PaymentStatus.CANCELLED: set(),
    }

    # The only targets a human may ever pick directly — billing_confirm's own
    # Mark as Paid / Reject / Re-open buttons, and BillingConfirmSerializer's
    # `status` field. CANCELLED, PARTIALLY_REFUNDED and REFUNDED stay in
    # ALLOWED_STATUS_TRANSITIONS above only so sales.views
    # (transaction_set_status / transaction_refund) can route through
    # can_transition_to like every other caller — never through this set.
    # Before this existed, BillingConfirmSerializer.status was a bare
    # ChoiceField over all six PaymentStatus values with nothing narrowing it
    # back down, so any ordinary Billing-access API caller could PATCH a
    # Pending record straight to Cancelled, or a Paid one to Refunded —
    # bypassing the order's own cancellation/refund workflow (stock reversal,
    # RefundEvent, the Owner/Branch-Manager-only refund approval) entirely.
    HUMAN_CONFIRMABLE_STATUSES = {PaymentStatus.PENDING, PaymentStatus.PAID, PaymentStatus.REJECTED}

    # What each method has to leave behind before it can be saved or verified.
    # No payment gateway is integrated, so for a remote settlement the
    # reference number and the screenshot ARE the record that it happened:
    # GCash and bank transfers need both, a card payment needs the slip/
    # screenshot (a card has no reference the counter can key in), and Cash on
    # Delivery needs the rider's receipt photo — the money changes hands at
    # the door, so with no gateway that photo is the only evidence it did.
    # Cash and Pay at Store are counter-settled and leave nothing to attach.
    # One definition — the web form, the API serializer, billing_confirm and
    # checkout all read these, so they cannot drift apart.
    #
    # COD is a proof-at-VERIFY rule, not proof-at-checkout: the customer has
    # nothing to show when they place the order, so checkout never asks for it
    # (orders.customer_views only gates GCash) and the order stays Pending
    # until staff upload the photo and mark it Paid.
    REFERENCE_REQUIRED_METHODS = {PaymentMethod.GCASH, PaymentMethod.BANK_TRANSFER}
    PROOF_REQUIRED_METHODS = {
        PaymentMethod.GCASH, PaymentMethod.BANK_TRANSFER, PaymentMethod.CARD,
        PaymentMethod.COD,
    }

    @classmethod
    def missing_payment_evidence(cls, method, reference_number, has_proof):
        """Names of what `method` still needs that isn't there — empty when
        the record is complete. Callers word their own message."""
        missing = []
        if method in cls.REFERENCE_REQUIRED_METHODS and not (reference_number or '').strip():
            missing.append('reference number')
        if method in cls.PROOF_REQUIRED_METHODS and not has_proof:
            missing.append('proof of payment')
        return missing

    def can_transition_to(self, new_status):
        """True when billing_confirm may move this record to `new_status`."""
        return new_status in self.ALLOWED_STATUS_TRANSITIONS.get(self.status, set())

    def pwd_review_block_message(self):
        """Why this record may not be marked Paid yet, or '' when nothing is
        in the way.

        An online order carrying a PWD discount request has an amount due that
        is not final until staff have looked at the ID at pick-up. Marking it
        Paid first would settle the full price and leave the request with
        nothing to apply against (a Paid bill can no longer be re-priced), so
        the customer would lose a discount they were owed. billing_confirm and
        BillingConfirmSerializer both ask this — one definition, like
        can_transition_to. Only Paid is gated: rejecting a payment or
        re-opening one settles nothing.
        """
        txn = self.transaction
        if txn.has_pending_pwd_request:
            return (
                f'{txn.order_number} has a PWD discount request waiting for '
                f'verification — approve or reject it on the order before '
                f'confirming payment, so the amount due is final.'
            )
        return ''

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

    def record_payment_details(self, payment_method, reference_number, proof_of_payment=None):
        """Write the payment method / reference / proof onto this Pending
        record — the one write path billing_create (web) and
        BillingPaymentSerializer.update (API) share.

        Both used to decide "still Pending" from a read taken before the
        save and then write every column of that stale copy, so a
        confirmation landing in between was rolled back to Pending while the
        reference number was written over a verified record. The row is now
        locked and its status re-read inside one transaction, only the
        payment columns are written, and the sale's own method is synced in
        the same transaction so the two can't disagree after a failure.

        `reference_number` must already have been through
        validators.clean_reference_number. Raises ValidationError when the
        record is no longer Pending.
        """
        with db_transaction.atomic():
            locked = BillingRecord.objects.select_for_update().get(pk=self.pk)
            if locked.status != self.PaymentStatus.PENDING:
                raise ValidationError('This billing record has already been processed.')

            fields = ['payment_method', 'reference_number']
            locked.payment_method = payment_method
            locked.reference_number = reference_number
            if proof_of_payment:
                locked.proof_of_payment = proof_of_payment
                fields.append('proof_of_payment')
            locked.save(update_fields=fields)

            # Leave the caller's instance showing what is now on the row.
            for field in fields:
                setattr(self, field, getattr(locked, field))
            # X-2 — one definition, see sync_payment_method_to_sale.
            self.sync_payment_method_to_sale()

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
        max_length=REFERENCE_NUMBER_MAX_LENGTH, blank=True,
        validators=[validate_reference_number],
        help_text="GCash/bank reference number, if applicable"
    )
    # Why a Rejected payment was turned down — required by billing_confirm /
    # BillingConfirmSerializer only when moving to Rejected, and cleared on
    # re-open to Pending, symmetric with confirmed_by / date_confirmed below.
    reject_reason = models.CharField(max_length=255, blank=True, default='')
    proof_of_payment = models.ImageField(
        upload_to='payment_proofs/', storage=payment_proof_storage,
        null=True, blank=True,
        help_text="Optional uploaded screenshot/photo of payment"
    )
    status = models.CharField(
        max_length=18, choices=PaymentStatus.choices,
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

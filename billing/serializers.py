# billing/serializers.py

from rest_framework import serializers
from django.core.exceptions import ValidationError as DjangoValidationError
from django.urls import reverse
from .models import BillingRecord
from .validators import clean_reference_number

class BillingRecordSerializer(serializers.ModelSerializer):
    transactionId = serializers.PrimaryKeyRelatedField(source='transaction', read_only=True)
    orderNumber = serializers.CharField(source='transaction.order_number', read_only=True)
    branch = serializers.CharField(source='transaction.branch.branch_name', read_only=True, allow_null=True)
    amountDue = serializers.DecimalField(source='amount_due', max_digits=12, decimal_places=2, read_only=True)
    paymentMethod = serializers.CharField(source='payment_method', read_only=True)
    referenceNumber = serializers.CharField(source='reference_number', read_only=True)
    # F-06 — never the raw /media/payment_proofs/ URL (that path is no longer
    # served). Point at an auth-gated payment-proof view instead; absolute if
    # the serializer was given the request in context, relative otherwise.
    proofOfPayment = serializers.SerializerMethodField()
    confirmedBy = serializers.SerializerMethodField()
    dateRecorded = serializers.DateTimeField(source='date_recorded', read_only=True)
    dateConfirmed = serializers.DateTimeField(source='date_confirmed', read_only=True)

    class Meta:
        model = BillingRecord
        fields = [
            'id', 'transactionId', 'orderNumber', 'branch', 'amountDue',
            'paymentMethod', 'referenceNumber', 'proofOfPayment', 'status',
            'confirmedBy', 'dateRecorded', 'dateConfirmed',
        ]

    def get_confirmedBy(self, obj):
        if not obj.confirmed_by:
            return None
        return obj.confirmed_by.get_full_name() or obj.confirmed_by.username

    def get_proofOfPayment(self, obj):
        if not obj.proof_of_payment:
            return None
        url = reverse('billing_proof', kwargs={'pk': obj.pk})
        request = self.context.get('request')
        return request.build_absolute_uri(url) if request else url


class BillingPaymentSerializer(serializers.Serializer):
    """
    Handles PATCH /api/billing/<pk>/pay/ — mirrors billing_create: fills in
    the payment details on the Pending record the signal already created.
    File upload, so the view must use MultiPartParser/FormParser.
    """
    paymentMethod = serializers.ChoiceField(choices=BillingRecord.PaymentMethod.choices)
    referenceNumber = serializers.CharField(required=False, allow_blank=True)
    proofOfPayment = serializers.ImageField(required=False)

    def validate(self, data):
        # X-2 — what each method needs is BillingRecord.missing_payment_
        # evidence, the same rule billing_create enforces on the web form.
        # A proof already on the record counts. Anything not required is
        # cleared, unchecked — same as the web form. What is kept goes
        # through the length/character rule billing_create and checkout
        # share; an over-long value used to reach the VARCHAR(100) column
        # and 500.
        method = data.get('paymentMethod')
        reference = ''
        if method in BillingRecord.REFERENCE_REQUIRED_METHODS:
            try:
                reference = clean_reference_number(
                    data.get('referenceNumber', ''), method
                )
            except DjangoValidationError as e:
                raise serializers.ValidationError({'referenceNumber': e.messages})
        has_proof = bool(
            data.get('proofOfPayment')
            or (self.instance is not None and self.instance.proof_of_payment)
        )
        missing = BillingRecord.missing_payment_evidence(method, reference, has_proof)
        if missing:
            raise serializers.ValidationError(
                f'A {method} payment needs its {" and ".join(missing)} — we '
                f'cannot match the payment to this order without it.'
            )
        data['referenceNumber'] = reference
        return data

    def update(self, instance, validated_data):
        # One locked, Pending-only write shared with billing.views.
        # billing_create, which also syncs the sale's payment method — see
        # BillingRecord.record_payment_details. Raises ValidationError if the
        # record was confirmed since this serializer read it.
        instance.record_payment_details(
            validated_data['paymentMethod'],
            validated_data.get('referenceNumber', ''),
            validated_data.get('proofOfPayment'),
        )
        return instance


class BillingConfirmSerializer(serializers.Serializer):
    """Handles PATCH /api/billing/<pk>/confirm/ — mirrors billing_confirm."""
    status = serializers.ChoiceField(choices=BillingRecord.PaymentStatus.choices)
    reason = serializers.CharField(required=False, allow_blank=True, max_length=255)

    def validate(self, data):
        # Object-level: a rejection needs a reason on file, the same rule
        # billing_confirm enforces. Field-level validate_status below still
        # runs first (DRF calls it before this), so the maker/checker and
        # transition checks are already settled by the time this runs.
        if data.get('status') == BillingRecord.PaymentStatus.REJECTED:
            reason = data.get('reason', '').strip()
            if not reason:
                raise serializers.ValidationError(
                    {'reason': 'A reason is required to reject this payment.'}
                )
            data['reason'] = reason[:255]
        return data

    def validate_status(self, value):
        record = self.instance

        # A-1 — maker/checker separation, the same guard billing_confirm
        # applies and checked first for the same reason: whether this user
        # may act on the record is prior to what they're moving it to. Only
        # Paid/Rejected record a confirmer; the Rejected -> Pending re-open
        # does not, so it is not gated.
        if (
            record is not None
            and value != BillingRecord.PaymentStatus.PENDING
            and record.is_self_confirmation(self.context['request'].user)
        ):
            raise serializers.ValidationError(
                "You can't confirm a payment you recorded yourself — ask "
                "another staff member, a manager, or the owner."
            )

        # Same state machine billing_confirm enforces — see
        # BillingRecord.ALLOWED_STATUS_TRANSITIONS.
        if record is not None and not record.can_transition_to(value):
            raise serializers.ValidationError(
                f'A {record.status} payment cannot be changed to {value}.'
            )

        # Same evidence gate billing_confirm enforces: Paid is the one
        # irreversible status change (ALLOWED_STATUS_TRANSITIONS has no
        # outbound edge from it), so what the method requires —
        # BillingRecord.missing_payment_evidence — has to be on record first.
        if record is not None and value == BillingRecord.PaymentStatus.PAID:
            # P15 — same PWD-review gate billing_confirm applies.
            blocked = record.pwd_review_block_message()
            if blocked:
                raise serializers.ValidationError(blocked)

            missing = BillingRecord.missing_payment_evidence(
                record.payment_method, record.reference_number,
                bool(record.proof_of_payment),
            )
            if missing:
                raise serializers.ValidationError(
                    f'A {record.payment_method} payment needs its '
                    f'{" and ".join(missing)} before it can be marked Paid.'
                )
        return value

    def update(self, instance, validated_data):
        from django.utils import timezone
        new_status = validated_data['status']
        instance.status = new_status
        if new_status == BillingRecord.PaymentStatus.REJECTED:
            instance.reject_reason = validated_data.get('reason', '')
        if new_status == BillingRecord.PaymentStatus.PENDING:
            instance.confirmed_by = None
            instance.date_confirmed = None
            instance.reject_reason = ''
        else:
            instance.confirmed_by = self.context['request'].user
            instance.date_confirmed = timezone.now()
        instance.save()
        if new_status in {
            BillingRecord.PaymentStatus.PAID,
            BillingRecord.PaymentStatus.REJECTED,
        }:
            from .notifications import notify_payment_decision

            notify_payment_decision(instance, self.context['request'].user)
        return instance

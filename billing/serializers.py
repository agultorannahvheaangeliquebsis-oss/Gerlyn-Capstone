# billing/serializers.py

from rest_framework import serializers
from django.urls import reverse
from .models import BillingRecord


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
        url = reverse('payment_proof_download', kwargs={'pk': obj.pk})
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

    def update(self, instance, validated_data):
        instance.payment_method = validated_data['paymentMethod']
        instance.reference_number = validated_data.get('referenceNumber', '')
        if validated_data.get('proofOfPayment'):
            instance.proof_of_payment = validated_data['proofOfPayment']
        instance.save()
        return instance


class BillingConfirmSerializer(serializers.Serializer):
    """Handles PATCH /api/billing/<pk>/confirm/ — mirrors billing_confirm."""
    status = serializers.ChoiceField(choices=BillingRecord.PaymentStatus.choices)

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

        # Same proof gate billing_confirm enforces: a non-Cash method has no
        # counter moment where handing over money and confirming it are the
        # same event, so Paid needs a reference number or an uploaded proof
        # of payment on record — otherwise this is the one irreversible
        # status change (ALLOWED_STATUS_TRANSITIONS has no outbound edge
        # from Paid) with zero evidence behind it.
        if (
            record is not None
            and value == BillingRecord.PaymentStatus.PAID
            and record.payment_method != BillingRecord.PaymentMethod.CASH
            and not record.reference_number
            and not record.proof_of_payment
        ):
            raise serializers.ValidationError(
                f'A {record.payment_method} payment needs a reference number '
                f'or an uploaded proof of payment before it can be marked Paid.'
            )
        return value

    def update(self, instance, validated_data):
        from django.utils import timezone
        new_status = validated_data['status']
        instance.status = new_status
        if new_status == BillingRecord.PaymentStatus.PENDING:
            instance.confirmed_by = None
            instance.date_confirmed = None
        else:
            instance.confirmed_by = self.context['request'].user
            instance.date_confirmed = timezone.now()
        instance.save()
        return instance

# billing/serializers.py

from rest_framework import serializers
from .models import BillingRecord


class BillingRecordSerializer(serializers.ModelSerializer):
    transactionId = serializers.PrimaryKeyRelatedField(source='transaction', read_only=True)
    orderNumber = serializers.CharField(source='transaction.order_number', read_only=True)
    branch = serializers.CharField(source='transaction.branch.branch_name', read_only=True, allow_null=True)
    amountDue = serializers.DecimalField(source='amount_due', max_digits=12, decimal_places=2, read_only=True)
    paymentMethod = serializers.CharField(source='payment_method', read_only=True)
    referenceNumber = serializers.CharField(source='reference_number', read_only=True)
    proofOfPayment = serializers.ImageField(source='proof_of_payment', read_only=True, allow_null=True)
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
        # Same state machine billing_confirm enforces — see
        # BillingRecord.ALLOWED_STATUS_TRANSITIONS.
        record = self.instance
        if record is not None and not record.can_transition_to(value):
            raise serializers.ValidationError(
                f'A {record.status} payment cannot be changed to {value}.'
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

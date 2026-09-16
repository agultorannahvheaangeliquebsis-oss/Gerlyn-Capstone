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

    def update(self, instance, validated_data):
        from django.utils import timezone
        instance.status = validated_data['status']
        instance.confirmed_by = self.context['request'].user
        instance.date_confirmed = timezone.now()
        instance.save()
        return instance

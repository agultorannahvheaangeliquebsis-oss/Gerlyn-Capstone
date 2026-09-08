# billing/api_views.py

from rest_framework import status
from rest_framework.views import APIView
from rest_framework.response import Response
from rest_framework.permissions import IsAuthenticated
from rest_framework.parsers import MultiPartParser, FormParser
from django.db import transaction
from django.shortcuts import get_object_or_404

from .models import BillingRecord
from .serializers import (
    BillingRecordSerializer,
    BillingPaymentSerializer,
    BillingConfirmSerializer,
)
from accounts.models import User


# Branch scoping lives in core.scoping so the API and the template views can
# never drift apart — this is security-critical logic, it gets ONE definition.
from core.scoping import branch_for_user as get_user_branch  # noqa: E402


def user_can_access_billing_record(user, record):
    """
    Same scoping rule as BillingRecordListAPIView.get, applied to a single
    record: Owner sees everything, Manager/Staff are scoped to their own
    branch, Customers are scoped to their own transactions. Without this,
    any authenticated user could view or pay toward any billing record just
    by guessing its pk — harmless while only internal staff had accounts,
    but a real hole once customers can self-register.
    """
    if user.role == User.Role.OWNER:
        return True
    if user.role in [User.Role.BRANCH_MANAGER, User.Role.STAFF]:
        user_branch = get_user_branch(user)
        return user_branch is not None and record.transaction.branch_id == user_branch.id
    if user.role == User.Role.CUSTOMER:
        return record.transaction.customer_id == user.id
    return False


class BillingRecordListAPIView(APIView):
    """GET /api/billing/ — scoped like billing_list."""
    permission_classes = [IsAuthenticated]

    def get(self, request):
        qs = BillingRecord.objects.select_related('transaction', 'transaction__branch').all()

        if request.user.role in [User.Role.BRANCH_MANAGER, User.Role.STAFF]:
            user_branch = get_user_branch(request.user)
            qs = qs.filter(transaction__branch=user_branch) if user_branch else qs.none()
        elif request.user.role == User.Role.CUSTOMER:
            qs = qs.filter(transaction__customer=request.user)
        elif request.user.role == User.Role.SUPPLIER:
            # Suppliers have no business in the store's billing at all.
            qs = qs.none()

        serializer = BillingRecordSerializer(qs.order_by('-date_recorded'), many=True)
        return Response(serializer.data)


class BillingRecordDetailAPIView(APIView):
    """GET /api/billing/<pk>/"""
    permission_classes = [IsAuthenticated]

    def get(self, request, pk):
        record = get_object_or_404(
            BillingRecord.objects.select_related('transaction', 'transaction__branch'), pk=pk
        )
        if not user_can_access_billing_record(request.user, record):
            return Response(status=status.HTTP_404_NOT_FOUND)
        return Response(BillingRecordSerializer(record).data)


class BillingPaymentAPIView(APIView):
    """
    PATCH /api/billing/<pk>/pay/ — mirrors billing_create: fills in payment
    method/reference/proof on the auto-created Pending record.
    """
    permission_classes = [IsAuthenticated]
    parser_classes = [MultiPartParser, FormParser]

    def patch(self, request, pk):
        record = get_object_or_404(BillingRecord, pk=pk)
        if not user_can_access_billing_record(request.user, record):
            return Response(status=status.HTTP_404_NOT_FOUND)

        if record.status != BillingRecord.PaymentStatus.PENDING:
            return Response(
                {'detail': 'This billing record has already been processed.'},
                status=status.HTTP_400_BAD_REQUEST
            )

        serializer = BillingPaymentSerializer(record, data=request.data)
        if serializer.is_valid():
            updated = serializer.save()
            return Response(BillingRecordSerializer(updated).data)
        return Response(serializer.errors, status=status.HTTP_400_BAD_REQUEST)


class BillingConfirmAPIView(APIView):
    """PATCH /api/billing/<pk>/confirm/ — mirrors billing_confirm."""
    permission_classes = [IsAuthenticated]

    def patch(self, request, pk):
        if request.user.role not in [User.Role.OWNER, User.Role.BRANCH_MANAGER, User.Role.STAFF]:
            return Response(
                {'detail': 'You do not have permission to confirm payments.'},
                status=status.HTTP_403_FORBIDDEN
            )

        record = get_object_or_404(
            BillingRecord.objects.select_related('transaction'), pk=pk
        )
        # F-02 — this was the one confirm/reject path that skipped branch
        # scoping entirely: a Staff/Manager account could PATCH a record
        # belonging to another branch just by knowing its pk, while the web
        # view (billing_confirm) already refused the same thing. 404, not
        # 403 — same convention as every other API view in this file: don't
        # confirm to a caller outside their scope that the record exists at
        # all.
        if not user_can_access_billing_record(request.user, record):
            return Response(status=status.HTTP_404_NOT_FOUND)

        with transaction.atomic():
            # Re-fetch and lock so two confirmations racing the same record
            # (double-click, retry) can't both pass BillingConfirmSerializer's
            # transition/self-confirmation checks against the same stale,
            # pre-lock status — the same protection billing_confirm's own
            # select_for_update already gives the web path.
            locked = BillingRecord.objects.select_for_update().select_related(
                'transaction'
            ).get(pk=pk)
            serializer = BillingConfirmSerializer(
                locked, data=request.data, context={'request': request}
            )
            if serializer.is_valid():
                updated = serializer.save()
                return Response(BillingRecordSerializer(updated).data)
            return Response(serializer.errors, status=status.HTTP_400_BAD_REQUEST)

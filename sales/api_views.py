# sales/api_views.py

from rest_framework import status
from rest_framework.views import APIView
from rest_framework.response import Response
from rest_framework.permissions import IsAuthenticated
from core.permissions import HasModuleAccess
from django.shortcuts import get_object_or_404

from . import refunds as refund_service
from .models import Refund, SalesTransaction
from .serializers import (
    RefundDecisionWriteSerializer, RefundRequestWriteSerializer, RefundSerializer,
    SalesTransactionSerializer, SalesTransactionWriteSerializer,
    TRANSACTION_READ_PREFETCH,
)
from accounts.models import User


# Branch scoping lives in core.scoping so the API and the template views can
# never drift apart — this is security-critical logic, it gets ONE definition.
from core.scoping import (  # noqa: E402
    branch_for_user as get_user_branch, scope_queryset, user_can_access_branch,
)


def _can_access_transaction(user, txn):
    """Owner sees everything, Manager/Staff their own branch, Customer their
    own orders. Suppliers have no business in sales data at all."""
    if user.role == User.Role.CUSTOMER:
        return txn.customer_id == user.id
    if user.role == User.Role.SUPPLIER:
        return False
    return user_can_access_branch(user, txn.branch_id)


class SalesTransactionListCreateAPIView(APIView):
    """
    GET  /api/sales/   → list, scoped like transaction_list
    POST /api/sales/   → create, mirrors transaction_add
    """
    permission_classes = [IsAuthenticated, HasModuleAccess]

    def get(self, request):
        qs = SalesTransaction.objects.select_related(
            'branch', 'staff', 'customer', 'billing_record'
        ).prefetch_related(*TRANSACTION_READ_PREFETCH).all()

        if request.user.role in [User.Role.BRANCH_MANAGER, User.Role.STAFF]:
            # X-4 — the shared, fail-closed helper, not a fourth hand-rolled
            # copy of this same branch check (Order Console, billing_list
            # and the billing API's own scoping are the other three).
            qs = scope_queryset(qs, request.user, 'branch')
        elif request.user.role == User.Role.CUSTOMER:
            qs = qs.filter(customer=request.user)
        elif request.user.role == User.Role.SUPPLIER:
            # Suppliers have no business in the store's sales data at all.
            qs = qs.none()

        serializer = SalesTransactionSerializer(
            qs.order_by('-transaction_date'), many=True, context={'request': request}
        )
        return Response(serializer.data)

    def post(self, request):
        if request.user.role not in [
            User.Role.OWNER, User.Role.BRANCH_MANAGER, User.Role.STAFF, User.Role.CUSTOMER
        ]:
            return Response(
                {'detail': 'You do not have permission to record sales.'},
                status=status.HTTP_403_FORBIDDEN
            )

        user_branch = get_user_branch(request.user)
        serializer = SalesTransactionWriteSerializer(
            data=request.data,
            context={'request': request, 'user_branch_id': user_branch.id if user_branch else None},
        )
        if serializer.is_valid():
            txn = serializer.save()
            return Response(
                SalesTransactionSerializer(txn, context={'request': request}).data,
                status=status.HTTP_201_CREATED,
            )
        return Response(serializer.errors, status=status.HTTP_400_BAD_REQUEST)


class SalesTransactionDetailAPIView(APIView):
    """GET /api/sales/<pk>/"""
    permission_classes = [IsAuthenticated, HasModuleAccess]

    def get(self, request, pk):
        txn = get_object_or_404(
            SalesTransaction.objects.select_related(
                'branch', 'staff', 'customer', 'billing_record'
            ).prefetch_related(*TRANSACTION_READ_PREFETCH),
            pk=pk
        )
        if not _can_access_transaction(request.user, txn):
            return Response(status=status.HTTP_404_NOT_FOUND)
        return Response(SalesTransactionSerializer(txn, context={'request': request}).data)


# =============================================================================
# REFUNDS (P14) — the DRF twin of sales.views.transaction_refund / _approve /
# _reject. Every rule (roles, branch scope, quantities, locking, stock and
# money effects) is sales.refunds; these only translate its errors to HTTP.
# =============================================================================

INTERNAL_ROLES = (User.Role.OWNER, User.Role.BRANCH_MANAGER, User.Role.STAFF)


def _refund_queryset():
    return Refund.objects.select_related(
        'transaction', 'requested_by', 'refunded_by'
    ).prefetch_related('items__sales_item__product', 'events__actor')


def _refund_error_response(error):
    code = (
        status.HTTP_403_FORBIDDEN
        if isinstance(error, refund_service.RefundForbidden)
        else status.HTTP_400_BAD_REQUEST
    )
    return Response({'detail': str(error)}, status=code)


def _refund_payload(refund_id, outcome=None):
    data = RefundSerializer(_refund_queryset().get(pk=refund_id)).data
    if outcome is not None and outcome.unrestockable:
        data['unrestockableUnits'] = outcome.unrestockable
    return data


class SalesRefundListCreateAPIView(APIView):
    """
    GET  /api/sales/<pk>/refunds/  → the order's refunds, each with its lines
                                     and event log. Internal roles only.
    POST /api/sales/<pk>/refunds/  → request a refund (mirrors
                                     transaction_refund). An Owner/Branch
                                     Manager's own request is approved as it
                                     is sent; Staff's waits for a decision.
    """
    permission_classes = [IsAuthenticated, HasModuleAccess]

    def _order(self, request, pk):
        if request.user.role not in INTERNAL_ROLES:
            return None, Response(
                {'detail': 'Refunds are managed by staff only.'},
                status=status.HTTP_403_FORBIDDEN,
            )
        txn = get_object_or_404(SalesTransaction, pk=pk)
        if not user_can_access_branch(request.user, txn.branch_id):
            return None, Response(status=status.HTTP_404_NOT_FOUND)
        return txn, None

    def get(self, request, pk):
        txn, error = self._order(request, pk)
        if error:
            return error
        return Response(RefundSerializer(_refund_queryset().filter(transaction=txn), many=True).data)

    def post(self, request, pk):
        txn, error = self._order(request, pk)
        if error:
            return error
        serializer = RefundRequestWriteSerializer(data=request.data)
        if not serializer.is_valid():
            return Response(serializer.errors, status=status.HTTP_400_BAD_REQUEST)
        data = serializer.validated_data
        try:
            outcome = refund_service.request_refund(
                txn.pk, request.user,
                reason_code=data['reasonCode'],
                reason=data.get('reason', ''),
                refund_method=data['refundMethod'],
                lines=[
                    {'item_id': line['itemId'], 'quantity': line['quantity'],
                     'condition': line['condition']}
                    for line in data.get('items', [])
                ],
                refund_all=data.get('fullRefund', False),
                default_condition=data.get('condition'),
            )
        except refund_service.RefundError as e:
            return _refund_error_response(e)
        return Response(
            _refund_payload(outcome.refund.pk, outcome), status=status.HTTP_201_CREATED
        )


class SalesRefundDecisionAPIView(APIView):
    """
    POST /api/sales/refunds/<pk>/approve/  → approve a Pending refund
    POST /api/sales/refunds/<pk>/reject/   → reject it (`note` required)

    Owner/Branch Manager only, and only for their own branch. `approve` is set
    per route in api_urls.py.
    """
    permission_classes = [IsAuthenticated, HasModuleAccess]
    approve = None

    def post(self, request, pk):
        if request.user.role not in refund_service.DECIDER_ROLES:
            return Response(
                {'detail': 'Only the Owner or a Branch Manager can decide a refund.'},
                status=status.HTTP_403_FORBIDDEN,
            )
        refund = get_object_or_404(Refund.objects.select_related('transaction'), pk=pk)
        if not user_can_access_branch(request.user, refund.transaction.branch_id):
            return Response(status=status.HTTP_404_NOT_FOUND)

        serializer = RefundDecisionWriteSerializer(data=request.data)
        if not serializer.is_valid():
            return Response(serializer.errors, status=status.HTTP_400_BAD_REQUEST)
        note = serializer.validated_data.get('note', '')
        try:
            if self.approve:
                outcome = refund_service.approve_refund(refund.pk, request.user, note)
            else:
                outcome = refund_service.reject_refund(refund.pk, request.user, note)
        except refund_service.RefundError as e:
            return _refund_error_response(e)
        return Response(_refund_payload(refund.pk, outcome))

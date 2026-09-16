# sales/api_views.py

from rest_framework import status
from rest_framework.views import APIView
from rest_framework.response import Response
from rest_framework.permissions import IsAuthenticated
from django.shortcuts import get_object_or_404

from .models import SalesTransaction
from .serializers import SalesTransactionSerializer, SalesTransactionWriteSerializer
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
    permission_classes = [IsAuthenticated]

    def get(self, request):
        qs = SalesTransaction.objects.select_related(
            'branch', 'staff', 'customer'
        ).prefetch_related('items__product').all()

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

        serializer = SalesTransactionSerializer(qs.order_by('-transaction_date'), many=True)
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
            return Response(SalesTransactionSerializer(txn).data, status=status.HTTP_201_CREATED)
        return Response(serializer.errors, status=status.HTTP_400_BAD_REQUEST)


class SalesTransactionDetailAPIView(APIView):
    """GET /api/sales/<pk>/"""
    permission_classes = [IsAuthenticated]

    def get(self, request, pk):
        txn = get_object_or_404(
            SalesTransaction.objects.select_related(
                'branch', 'staff', 'customer'
            ).prefetch_related('items__product'),
            pk=pk
        )
        if not _can_access_transaction(request.user, txn):
            return Response(status=status.HTTP_404_NOT_FOUND)
        return Response(SalesTransactionSerializer(txn).data)

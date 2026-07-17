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
from core.scoping import branch_for_user as get_user_branch  # noqa: E402


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
            user_branch = get_user_branch(request.user)
            qs = qs.filter(branch=user_branch) if user_branch else qs.none()
        elif request.user.role == User.Role.CUSTOMER:
            qs = qs.filter(customer=request.user)

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
        return Response(SalesTransactionSerializer(txn).data)

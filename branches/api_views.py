from rest_framework import viewsets, status
from rest_framework.response import Response
from rest_framework.permissions import IsAuthenticated, AllowAny
from .models import Branch
from .serializers import BranchSerializer, PublicBranchSerializer
from accounts.models import User
from .services import BranchDeletionBlocked, delete_branch_if_safe


class BranchViewSet(viewsets.ModelViewSet):
    """
    /api/branches/
    GET → public (the storefront's branch picker needs this before login,
          plus every internal role's branch-picker).
    POST/PUT/PATCH/DELETE → Owner only.
    """
    queryset = Branch.objects.select_related('owner').all()
    serializer_class = BranchSerializer
    permission_classes = [IsAuthenticated]

    def get_serializer_class(self):
        if self.action in ('list', 'retrieve') and (
            not self.request.user.is_authenticated
            or self.request.user.role != User.Role.OWNER
        ):
            return PublicBranchSerializer
        return BranchSerializer

    def get_queryset(self):
        queryset = super().get_queryset()
        user = self.request.user
        if not user.is_authenticated or user.role != User.Role.OWNER:
            queryset = queryset.filter(is_active=True)
        return queryset

    def get_permissions(self):
        if self.action in ['list', 'retrieve']:
            return [AllowAny()]
        return super().get_permissions()

    def check_write_permission(self, request):
        if request.user.role != User.Role.OWNER:
            return Response(
                {'detail': 'Only the Owner can modify branches.'},
                status=status.HTTP_403_FORBIDDEN
            )
        return None

    def perform_create(self, serializer):
        serializer.save(owner=self.request.user)

    def create(self, request, *args, **kwargs):
        denied = self.check_write_permission(request)
        if denied:
            return denied
        return super().create(request, *args, **kwargs)

    def update(self, request, *args, **kwargs):
        denied = self.check_write_permission(request)
        if denied:
            return denied
        return super().update(request, *args, **kwargs)

    def destroy(self, request, *args, **kwargs):
        denied = self.check_write_permission(request)
        if denied:
            return denied
        branch = self.get_object()
        try:
            delete_branch_if_safe(branch.pk)
        except BranchDeletionBlocked as exc:
            return Response(
                {'detail': str(exc), 'blockers': exc.blockers},
                status=status.HTTP_400_BAD_REQUEST,
            )
        return Response(status=status.HTTP_204_NO_CONTENT)

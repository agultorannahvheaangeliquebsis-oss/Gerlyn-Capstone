from rest_framework import viewsets, status
from rest_framework.response import Response
from rest_framework.permissions import IsAuthenticated
from .models import Branch
from .serializers import BranchSerializer
from accounts.models import User


class BranchViewSet(viewsets.ModelViewSet):
    """
    /api/branches/
    GET → any authenticated user (a React owner dashboard, and every other
          role's branch-picker, needs to read this list).
    POST/PUT/PATCH/DELETE → Owner only.
    """
    queryset = Branch.objects.select_related('owner').all()
    serializer_class = BranchSerializer
    permission_classes = [IsAuthenticated]

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
        return super().destroy(request, *args, **kwargs)

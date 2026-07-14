from rest_framework import serializers
from .models import User


class UserSerializer(serializers.ModelSerializer):
    """
    Returned after login and on /api/auth/me/ requests.
    React uses role to decide which dashboard to render.
    """
    role = serializers.CharField(source='get_role_display')
    roleCode = serializers.CharField(source='role')
    fullName = serializers.SerializerMethodField()
    branch = serializers.SerializerMethodField()

    class Meta:
        model = User
        fields = [
            'id',
            'username',
            'email',
            'role',
            'roleCode',
            'fullName',
            'branch',
        ]

    def get_fullName(self, obj):
        """
        Return the full name from the correct profile model
        based on the user's role.
        """
        try:
            if obj.role == User.Role.OWNER:
                return obj.owner_profile.owner_name
            elif obj.role == User.Role.BRANCH_MANAGER:
                return obj.manager_profile.manager_name
            elif obj.role == User.Role.STAFF:
                return obj.staff_profile.staff_name
            elif obj.role == User.Role.CUSTOMER:
                return obj.customer_profile.customer_name
            elif obj.role == User.Role.SUPPLIER:
                return obj.supplier_profile.supplier_name
        except Exception:
            pass
        return obj.username

    def get_branch(self, obj):
        """
        Return the branch name for Staff and Managers.
        Owners return 'All Branches'. Customers and Suppliers return None.
        """
        try:
            if obj.role == User.Role.OWNER:
                return 'All Branches'
            elif obj.role == User.Role.BRANCH_MANAGER:
                return obj.manager_profile.branch.branch_name
            elif obj.role == User.Role.STAFF:
                return obj.staff_profile.branch.branch_name
        except Exception:
            pass
        return None
from django.contrib.auth import password_validation
from django.core.exceptions import ValidationError as DjangoValidationError
from rest_framework import serializers
from .models import User
from .role_policy import USER_MANAGEMENT_ROLE_CHOICES


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
                profile = obj.customer_profile
                return f"{profile.first_name} {profile.last_name}".strip() or obj.username
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


# =============================================================================
# ADMINISTRATION — user management (Owner only)
# =============================================================================

def _profile_for(user):
    """Return (profile, name_field) for the user's role-specific profile."""
    if user.role == User.Role.OWNER:
        return user.owner_profile, 'owner_name'
    if user.role == User.Role.BRANCH_MANAGER:
        return user.manager_profile, 'manager_name'
    if user.role == User.Role.STAFF:
        return user.staff_profile, 'staff_name'
    if user.role == User.Role.SUPPLIER:
        return user.supplier_profile, 'supplier_name'
    return None, None


class UserAdminSerializer(UserSerializer):
    """Extends UserSerializer with the extra fields the Administration
    screen needs: contact info, branch id (not just name), active status."""
    branchId = serializers.SerializerMethodField()
    contactNumber = serializers.SerializerMethodField()
    isActive = serializers.BooleanField(source='is_active', read_only=True)
    dateJoined = serializers.DateTimeField(source='date_joined', read_only=True)

    class Meta(UserSerializer.Meta):
        fields = UserSerializer.Meta.fields + ['branchId', 'contactNumber', 'isActive', 'dateJoined']

    def get_branchId(self, obj):
        try:
            if obj.role == User.Role.BRANCH_MANAGER:
                return obj.manager_profile.branch_id
            elif obj.role == User.Role.STAFF:
                return obj.staff_profile.branch_id
        except Exception:
            pass
        return None

    def get_contactNumber(self, obj):
        try:
            if obj.role == User.Role.CUSTOMER:
                return obj.customer_profile.contact_number
            profile, _ = _profile_for(obj)
            return profile.contact_number if profile else None
        except Exception:
            return None


class UserCreateSerializer(serializers.Serializer):
    """Handles POST /api/auth/users/ — creates a User plus its role-specific
    profile in one call. The post_save signal (accounts/signals.py) creates
    an empty profile first; this fills it in with the submitted details."""
    username = serializers.CharField()
    email = serializers.EmailField(required=False, allow_blank=True)
    password = serializers.CharField(write_only=True)
    role = serializers.ChoiceField(choices=USER_MANAGEMENT_ROLE_CHOICES)
    fullName = serializers.CharField(required=False, allow_blank=True)
    contactNumber = serializers.CharField(required=False, allow_blank=True)
    branchId = serializers.IntegerField(required=False, allow_null=True)
    address = serializers.CharField(required=False, allow_blank=True)

    def validate_username(self, value):
        if User.objects.filter(username=value).exists():
            raise serializers.ValidationError('Username already taken.')
        return value

    def validate_password(self, value):
        # Same AUTH_PASSWORD_VALIDATORS enforced on self-service registration —
        # an Owner-created account (including elevated roles) shouldn't be
        # able to end up with a weaker password than a customer's.
        try:
            password_validation.validate_password(
                value, user=User(username=self.initial_data.get('username', ''))
            )
        except DjangoValidationError as e:
            raise serializers.ValidationError(e.messages)
        return value

    def create(self, validated_data):
        role = validated_data['role']
        user = User.objects.create_user(
            username=validated_data['username'],
            email=validated_data.get('email', ''),
            password=validated_data['password'],
            role=role,
        )

        full_name = validated_data.get('fullName', '').strip()
        contact_number = validated_data.get('contactNumber', '')
        branch_id = validated_data.get('branchId')

        profile, name_field = _profile_for(user)
        if profile:
            setattr(profile, name_field, full_name or user.username)
            profile.contact_number = contact_number
            if role in (User.Role.BRANCH_MANAGER, User.Role.STAFF) and branch_id:
                profile.branch_id = branch_id
            profile.save()

        return user


class UserUpdateSerializer(serializers.Serializer):
    """Handles PATCH /api/auth/users/<pk>/ — profile fields + active status.
    Role and username are intentionally not editable here: changing role
    would require migrating between profile models, which is out of scope."""
    fullName = serializers.CharField(required=False, allow_blank=True)
    contactNumber = serializers.CharField(required=False, allow_blank=True)
    branchId = serializers.IntegerField(required=False, allow_null=True)
    address = serializers.CharField(required=False, allow_blank=True)
    isActive = serializers.BooleanField(required=False)

    def update(self, instance, validated_data):
        if 'isActive' in validated_data:
            instance.is_active = validated_data['isActive']
            instance.save(update_fields=['is_active'])

        full_name = validated_data.get('fullName')
        contact_number = validated_data.get('contactNumber')

        try:
            if instance.role == User.Role.CUSTOMER:
                profile = instance.customer_profile
                if full_name is not None:
                    parts = full_name.strip().split(' ', 1)
                    profile.first_name = parts[0] if parts and parts[0] else instance.username
                    profile.last_name = parts[1] if len(parts) > 1 else ''
                if contact_number is not None:
                    profile.contact_number = contact_number
                if 'address' in validated_data:
                    profile.address = validated_data['address']
                profile.save()
            else:
                profile, name_field = _profile_for(instance)
                if profile:
                    if full_name is not None:
                        setattr(profile, name_field, full_name or instance.username)
                    if contact_number is not None:
                        profile.contact_number = contact_number
                    if instance.role in (User.Role.BRANCH_MANAGER, User.Role.STAFF) and 'branchId' in validated_data:
                        profile.branch_id = validated_data['branchId']
                    profile.save()
        except Exception:
            pass

        return instance

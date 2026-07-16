from rest_framework.views import APIView
from rest_framework.response import Response
from rest_framework import status
from rest_framework.permissions import IsAuthenticated, AllowAny
from django.contrib.auth import authenticate, login, logout
from django.shortcuts import get_object_or_404
from .serializers import UserSerializer, UserAdminSerializer, UserCreateSerializer, UserUpdateSerializer
from .models import User


class LoginView(APIView):
    """
    POST /api/auth/login/
    Body: { "username": "...", "password": "..." }
    Returns: user data JSON + sets session cookie
    React uses roleCode to decide which dashboard to render.
    """
    permission_classes = [AllowAny]

    def post(self, request):
        username = request.data.get('username')
        password = request.data.get('password')

        if not username or not password:
            return Response(
                {'detail': 'Username and password are required.'},
                status=status.HTTP_400_BAD_REQUEST
            )

        user = authenticate(request, username=username, password=password)

        if user is None:
            return Response(
                {'detail': 'Invalid username or password.'},
                status=status.HTTP_401_UNAUTHORIZED
            )

        login(request, user)
        serializer = UserSerializer(user)
        return Response({
            'user': serializer.data,
            'detail': 'Login successful.',
        }, status=status.HTTP_200_OK)


class LogoutView(APIView):
    """
    POST /api/auth/logout/
    Clears the Django session. React redirects to login after.
    """
    permission_classes = [IsAuthenticated]

    def post(self, request):
        logout(request)
        return Response(
            {'detail': 'Logged out successfully.'},
            status=status.HTTP_200_OK
        )


class MeView(APIView):
    """
    GET /api/auth/me/
    React calls this on every page load to check if
    the user is still logged in and get their current data.
    Returns 403 if not authenticated — React redirects to login.
    """
    permission_classes = [IsAuthenticated]

    def get(self, request):
        serializer = UserSerializer(request.user)
        return Response(serializer.data)


class RegisterCustomerView(APIView):
    """
    POST /api/auth/register/
    Public endpoint — creates a new Customer account.
    React calls this from the customer registration form.
    Body: {
        "username": "...",
        "email": "...",
        "password": "...",
        "fullName": "...",
        "contactNumber": "...",
        "address": "..."
    }
    """
    permission_classes = [AllowAny]

    def post(self, request):
        username = request.data.get('username')
        email = request.data.get('email')
        password = request.data.get('password')
        full_name = request.data.get('fullName', '')
        contact_number = request.data.get('contactNumber', '')
        address = request.data.get('address', '')

        # Validation
        if not username or not email or not password:
            return Response(
                {'detail': 'Username, email, and password are required.'},
                status=status.HTTP_400_BAD_REQUEST
            )

        if User.objects.filter(username=username).exists():
            return Response(
                {'detail': 'Username already taken.'},
                status=status.HTTP_400_BAD_REQUEST
            )

        if User.objects.filter(email=email).exists():
            return Response(
                {'detail': 'Email already registered.'},
                status=status.HTTP_400_BAD_REQUEST
            )

        # Create user — signal will auto-create CustomerProfile
        user = User.objects.create_user(
            username=username,
            email=email,
            password=password,
            role=User.Role.CUSTOMER,
        )

        # Update the auto-created profile with the submitted data
        try:
            profile = user.customer_profile
            name_parts = full_name.strip().split(' ', 1)
            profile.first_name = name_parts[0] if name_parts else ''
            profile.last_name = name_parts[1] if len(name_parts) > 1 else ''
            profile.contact_number = contact_number
            profile.address = address
            profile.save()
        except Exception:
            pass

        # Log them in immediately after registration
        login(request, user)
        serializer = UserSerializer(user)
        return Response({
            'user': serializer.data,
            'detail': 'Account created successfully.',
        }, status=status.HTTP_201_CREATED)


# =============================================================================
# ADMINISTRATION — user management (Owner only)
# =============================================================================

def check_owner(request):
    if request.user.role != User.Role.OWNER:
        return Response(
            {'detail': 'Only the Owner can manage user accounts.'},
            status=status.HTTP_403_FORBIDDEN
        )
    return None


class UserListCreateAPIView(APIView):
    """
    GET  /api/auth/users/   → list every account, all roles
    POST /api/auth/users/   → create a new account + role-specific profile
    """
    permission_classes = [IsAuthenticated]

    def get(self, request):
        denied = check_owner(request)
        if denied:
            return denied
        users = User.objects.all().order_by('role', 'username')
        return Response(UserAdminSerializer(users, many=True).data)

    def post(self, request):
        denied = check_owner(request)
        if denied:
            return denied
        serializer = UserCreateSerializer(data=request.data)
        if serializer.is_valid():
            user = serializer.save()
            return Response(UserAdminSerializer(user).data, status=status.HTTP_201_CREATED)
        return Response(serializer.errors, status=status.HTTP_400_BAD_REQUEST)


class UserDetailAPIView(APIView):
    """
    PATCH /api/auth/users/<pk>/   → update profile fields / active status
    No DELETE — accounts are deactivated (isActive: false), not removed,
    since they're referenced by historical sales/procurement/transfer records.
    """
    permission_classes = [IsAuthenticated]

    def patch(self, request, pk):
        denied = check_owner(request)
        if denied:
            return denied
        user = get_object_or_404(User, pk=pk)
        serializer = UserUpdateSerializer(user, data=request.data, partial=True)
        if serializer.is_valid():
            updated = serializer.save()
            return Response(UserAdminSerializer(updated).data)
        return Response(serializer.errors, status=status.HTTP_400_BAD_REQUEST)
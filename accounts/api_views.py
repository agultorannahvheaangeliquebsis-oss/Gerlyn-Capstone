from rest_framework.views import APIView
from rest_framework.response import Response
from rest_framework import status
from rest_framework.permissions import IsAuthenticated, AllowAny
from django.contrib.auth import authenticate, login, logout, password_validation
from django.core.exceptions import ValidationError as DjangoValidationError
from django.db import transaction
from django.shortcuts import get_object_or_404
from .serializers import UserSerializer, UserAdminSerializer, UserCreateSerializer, UserUpdateSerializer
from .models import User
from .rate_limit import clear_failed_logins, is_login_locked_out, register_failed_login
from .two_factor import (
    cancel_login_otp, is_login_otp_locked_out, start_login_otp, verify_login_otp,
)


class LoginView(APIView):
    """
    POST /api/auth/login/
    Body: { "username": "...", "password": "..." }
    Returns: user data JSON + sets session cookie
    React uses roleCode to decide which dashboard to render.

    When the account has Two-Factor Authentication on, a correct password
    does not log the session in yet — it returns two_factor_required: true
    instead, the same gap this closes on the template login flow
    (accounts.views.login_view). The caller must then POST the emailed code
    to VerifyTwoFactorView below before a session actually exists.
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

        if is_login_locked_out(username):
            return Response(
                {'detail': 'Too many failed attempts. Try again in a few minutes.'},
                status=status.HTTP_429_TOO_MANY_REQUESTS
            )

        user = authenticate(request, username=username, password=password)

        if user is None:
            register_failed_login(username)
            return Response(
                {'detail': 'Invalid username or password.'},
                status=status.HTTP_401_UNAUTHORIZED
            )

        clear_failed_logins(username)

        if user.two_factor_enabled:
            if not start_login_otp(user, request):
                # The cache the code lives in is unreachable — nothing was
                # emailed, so telling the caller a code is pending would be
                # a screen that can never succeed. The password is not
                # consumed; the caller can just retry.
                return Response(
                    {'detail': 'Could not start email verification right now. Please try again in a moment.'},
                    status=status.HTTP_503_SERVICE_UNAVAILABLE
                )
            request.session['pending_2fa_user_id'] = user.pk
            return Response({
                'two_factor_required': True,
                'detail': f'A login code was sent to {user.email}.',
            }, status=status.HTTP_200_OK)

        login(request, user)
        serializer = UserSerializer(user)
        return Response({
            'user': serializer.data,
            'detail': 'Login successful.',
        }, status=status.HTTP_200_OK)


class VerifyTwoFactorView(APIView):
    """
    POST /api/auth/verify-2fa/
    Body: { "code": "123456" } or { "resend": true }
    Completes a login LoginView started when two_factor_enabled is on —
    the session's pending_2fa_user_id (set by that call) is the only way
    this endpoint knows which account is mid-login, so it can't be used to
    probe or complete anyone else's login.
    """
    permission_classes = [AllowAny]

    def post(self, request):
        pending_user_id = request.session.get('pending_2fa_user_id')
        if not pending_user_id:
            return Response(
                {'detail': 'No login is pending verification.'},
                status=status.HTTP_400_BAD_REQUEST
            )

        user = User.objects.filter(pk=pending_user_id).first()
        if user is None or not user.two_factor_enabled:
            request.session.pop('pending_2fa_user_id', None)
            return Response(
                {'detail': 'No login is pending verification.'},
                status=status.HTTP_400_BAD_REQUEST
            )

        if request.data.get('resend'):
            if start_login_otp(user, request):
                return Response(
                    {'detail': f'A new code was sent to {user.email}.'},
                    status=status.HTTP_200_OK
                )
            return Response(
                {'detail': "Couldn't resend right now. Please try again shortly."},
                status=status.HTTP_503_SERVICE_UNAVAILABLE
            )

        if is_login_otp_locked_out(user):
            request.session.pop('pending_2fa_user_id', None)
            cancel_login_otp(user)
            return Response(
                {'detail': 'Too many incorrect codes. Please log in again.'},
                status=status.HTTP_429_TOO_MANY_REQUESTS
            )

        if not verify_login_otp(user, request.data.get('code', '')):
            return Response({'detail': 'Incorrect code.'}, status=status.HTTP_401_UNAUTHORIZED)

        request.session.pop('pending_2fa_user_id', None)
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
        candidate = User(
            username=username,
            email=email,
            role=User.Role.CUSTOMER,
        )
        try:
            password_validation.validate_password(password, user=candidate)
        except DjangoValidationError as exc:
            return Response(
                {'password': exc.messages},
                status=status.HTTP_400_BAD_REQUEST,
            )

        # Keep account and signal-created profile updates all-or-nothing.
        with transaction.atomic():
            user = User.objects.create_user(
                username=username,
                email=email,
                password=password,
                role=User.Role.CUSTOMER,
            )
            profile = user.customer_profile
            name_parts = full_name.strip().split(' ', 1)
            profile.first_name = name_parts[0] if name_parts else ''
            profile.last_name = name_parts[1] if len(name_parts) > 1 else ''
            profile.contact_number = contact_number
            profile.address = address
            profile.save()

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

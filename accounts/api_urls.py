from django.urls import path
from .api_views import (
    LoginView,
    LogoutView,
    MeView,
    RegisterCustomerView,
    UserListCreateAPIView,
    UserDetailAPIView,
    VerifyTwoFactorView,
)

urlpatterns = [
    path('login/', LoginView.as_view(), name='api-login'),
    path('verify-2fa/', VerifyTwoFactorView.as_view(), name='api-verify-2fa'),
    path('logout/', LogoutView.as_view(), name='api-logout'),
    path('me/', MeView.as_view(), name='api-me'),
    path('register/', RegisterCustomerView.as_view(), name='api-register'),
    path('users/', UserListCreateAPIView.as_view(), name='api-users-list'),
    path('users/<int:pk>/', UserDetailAPIView.as_view(), name='api-users-detail'),
]

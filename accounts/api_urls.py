from django.urls import path
from .api_views import LoginView, LogoutView, MeView, RegisterCustomerView

urlpatterns = [
    path('login/', LoginView.as_view(), name='api-login'),
    path('logout/', LogoutView.as_view(), name='api-logout'),
    path('me/', MeView.as_view(), name='api-me'),
    path('register/', RegisterCustomerView.as_view(), name='api-register'),
]
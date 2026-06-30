from django.urls import path
from . import views

app_name = 'orders'
urlpatterns = [
    path('branch/', views.select_branch, name='select_branch'),
    path('', views.product_catalog, name='catalog'),
    path('add/<int:product_id>/', views.add_to_cart, name='add_to_cart'),
    path('cart/', views.view_cart, name='cart'),
    path('checkout/', views.checkout, name='checkout'),
    path('confirmation/<int:transaction_id>/', views.order_confirmation, name='confirmation'),
]
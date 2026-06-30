from django.urls import path
from . import customer_views

app_name = 'orders'
urlpatterns = [
    path('branch/', customer_views.select_branch, name='select_branch'),
    path('', customer_views.product_catalog, name='catalog'),
    path('add/<int:product_id>/', customer_views.add_to_cart, name='add_to_cart'),
    path('cart/', customer_views.view_cart, name='cart'),
    path('checkout/', customer_views.checkout, name='checkout'),
    path('confirmation/<int:transaction_id>/', customer_views.order_confirmation, name='confirmation'),
]
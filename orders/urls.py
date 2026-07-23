from django.urls import path

from . import customer_views

app_name = 'orders'
urlpatterns = [
    # Browsing (open to guests)
    path('', customer_views.product_catalog, name='catalog'),
    path('search/suggest/', customer_views.search_suggestions, name='search_suggestions'),
    path('product/<int:product_id>/', customer_views.product_detail, name='product_detail'),
    path('branch/', customer_views.select_branch, name='select_branch'),
    path('branch/<int:branch_id>/', customer_views.set_branch, name='set_branch'),
    path('branch/clear/', customer_views.clear_branch, name='clear_branch'),

    # Cart + checkout (customers only)
    path('add/<int:product_id>/', customer_views.add_to_cart, name='add_to_cart'),
    path('undo/', customer_views.undo_add, name='undo_add'),
    path('cart/', customer_views.view_cart, name='cart'),
    path('cart/update/<int:product_id>/', customer_views.update_cart, name='update_cart'),
    path('cart/remove/<int:product_id>/', customer_views.remove_from_cart, name='remove_from_cart'),
    path('checkout/', customer_views.checkout, name='checkout'),
    path('confirmation/<int:transaction_id>/', customer_views.order_confirmation, name='confirmation'),

    # My orders / order status
    path('orders/', customer_views.my_orders, name='my_orders'),
    path('orders/<int:transaction_id>/', customer_views.track_order, name='track_order'),
]

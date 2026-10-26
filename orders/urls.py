from django.urls import path

from . import customer_views, damage_views, receipt_views

app_name = 'orders'
urlpatterns = [
    # Browsing (open to guests)
    path('', customer_views.product_catalog, name='catalog'),
    path('search/suggest/', customer_views.search_suggestions, name='search_suggestions'),
    path('product/<int:product_id>/', customer_views.product_detail, name='product_detail'),
    path('branch/', customer_views.select_branch, name='select_branch'),

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

    # Confirming receipt of a delivery / pickup, and the receipt itself
    path('orders/<int:transaction_id>/received/', receipt_views.confirm_received, name='confirm_received'),
    path('orders/<int:transaction_id>/receipt/', receipt_views.order_receipt, name='receipt'),
    path('orders/<int:transaction_id>/receipt/pdf/', receipt_views.order_receipt_pdf, name='receipt_pdf'),
    path('orders/<int:transaction_id>/receipt/evidence/<str:kind>/',
         receipt_views.order_receipt_evidence, name='receipt_evidence'),

    # Reporting a problem (damaged goods) with a received order, and the
    # photos of the customer's own reports
    path('orders/<int:transaction_id>/report-problem/',
         damage_views.report_problem, name='report_problem'),
    path('orders/<int:transaction_id>/problems/<int:report_id>/photos/<int:photo_id>/',
         damage_views.problem_photo, name='problem_photo'),
]

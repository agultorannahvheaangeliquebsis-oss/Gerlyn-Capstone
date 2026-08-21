# sales/serializers.py

from rest_framework import serializers
from django.db import transaction

from .models import SalesTransaction, SalesItem
from core.scoping import user_can_access_branch
from inventory.models import Product, Inventory, Batch
from branches.models import Branch


# =============================================================================
# SALES ITEM
# =============================================================================

class SalesItemSerializer(serializers.ModelSerializer):
    productId = serializers.PrimaryKeyRelatedField(source='product', read_only=True)
    productName = serializers.CharField(source='product.product_name', read_only=True)
    productSku = serializers.CharField(source='product.sku_code', read_only=True)
    quantitySold = serializers.IntegerField(source='quantity_sold', read_only=True)
    unitPrice = serializers.DecimalField(
        source='unit_price', max_digits=10, decimal_places=2, read_only=True
    )
    subtotal = serializers.DecimalField(max_digits=12, decimal_places=2, read_only=True)

    class Meta:
        model = SalesItem
        fields = ['id', 'productId', 'productName', 'productSku', 'quantitySold', 'unitPrice', 'subtotal']


# =============================================================================
# SALES TRANSACTION (READ)
# =============================================================================

class SalesTransactionSerializer(serializers.ModelSerializer):
    orderNumber = serializers.CharField(source='order_number', read_only=True)
    branch = serializers.CharField(source='branch.branch_name', read_only=True, allow_null=True)
    staff = serializers.SerializerMethodField()
    customer = serializers.SerializerMethodField()
    transactionDate = serializers.DateTimeField(source='transaction_date', read_only=True)
    totalAmount = serializers.DecimalField(source='total_amount', max_digits=12, decimal_places=2, read_only=True)
    transactionType = serializers.CharField(source='transaction_type', read_only=True)
    modeOfPayment = serializers.CharField(source='mode_of_payment', read_only=True)
    orderType = serializers.CharField(source='order_type', read_only=True)
    orderStatus = serializers.CharField(source='order_status', read_only=True)
    deliveryAddress = serializers.CharField(source='delivery_address', read_only=True)
    items = SalesItemSerializer(many=True, read_only=True)

    class Meta:
        model = SalesTransaction
        fields = [
            'id', 'orderNumber', 'branch', 'staff', 'customer',
            'transactionDate', 'totalAmount', 'transactionType', 'modeOfPayment',
            'orderType', 'orderStatus', 'deliveryAddress', 'items',
        ]

    def get_staff(self, obj):
        if obj.staff:
            try:
                return obj.staff.staff_profile.staff_name
            except Exception:
                return obj.staff.get_full_name() or obj.staff.username
        return None

    def get_customer(self, obj):
        if obj.customer:
            try:
                return obj.customer.customer_profile.first_name
            except Exception:
                return obj.customer.get_full_name() or obj.customer.username
        return None


# =============================================================================
# SALES TRANSACTION (WRITE)
# Handles POST /api/sales/ — mirrors sales/views.py:transaction_add, plus
# picking unit_price vs wholesale_price based on orderType (the field the
# model added specifically for this: "Retail uses unit_price; Wholesale
# uses wholesale_price").
# =============================================================================

class SalesItemWriteSerializer(serializers.Serializer):
    productId = serializers.IntegerField()
    quantity = serializers.IntegerField(min_value=1)


class SalesTransactionWriteSerializer(serializers.Serializer):
    branchId = serializers.IntegerField(required=False)
    transactionType = serializers.ChoiceField(choices=SalesTransaction.TransactionType.choices)
    modeOfPayment = serializers.ChoiceField(choices=SalesTransaction.ModeOfPayment.choices)
    orderType = serializers.ChoiceField(
        choices=SalesTransaction.OrderType.choices, default=SalesTransaction.OrderType.RETAIL
    )
    deliveryAddress = serializers.CharField(required=False, allow_blank=True)
    items = SalesItemWriteSerializer(many=True, min_length=1)

    def validate_items(self, value):
        product_ids = [item['productId'] for item in value]
        existing = Product.objects.filter(id__in=product_ids).count()
        if existing != len(set(product_ids)):
            raise serializers.ValidationError("One or more products not found.")
        return value

    def validate(self, data):
        if data.get('transactionType') == SalesTransaction.TransactionType.ONLINE \
                and not data.get('deliveryAddress', '').strip():
            raise serializers.ValidationError(
                {'deliveryAddress': 'Delivery address is required for online orders.'}
            )
        return data

    def create(self, validated_data):
        request = self.context['request']
        user = request.user
        items_data = validated_data.pop('items')

        branch_id = validated_data.get('branchId') or self.context.get('user_branch_id')
        if not branch_id:
            raise serializers.ValidationError({'branchId': 'No branch assigned to your account.'})

        # Staff/Manager are pinned to their own branch everywhere else in the
        # app — a client-supplied branchId must not let them write a sale
        # (and deduct stock) for a different one. Owner is unrestricted, and
        # a Customer has no "home" branch to be pinned to in the first place.
        if (user.role in (user.Role.STAFF, user.Role.BRANCH_MANAGER)
                and not user_can_access_branch(user, branch_id)):
            raise serializers.ValidationError(
                {'branchId': 'You do not have access to that branch.'}
            )

        order_type = validated_data['orderType']

        with transaction.atomic():
            # Lock stock and validate availability before writing anything, so
            # total_amount can be set at creation time (billing.signals reads
            # it then — creating with 0 and updating afterward left the
            # billing record's amount_due permanently stuck at 0) and an
            # oversold cart is rejected outright instead of being silently
            # short-filled (fine when staff are watching; not fine for
            # unattended customer checkout).
            resolved_items = []
            total = 0
            for item in items_data:
                product = Product.objects.get(id=item['productId'])
                qty = item['quantity']
                unit_price = (
                    product.wholesale_price
                    if order_type == SalesTransaction.OrderType.WHOLESALE and product.wholesale_price
                    else product.unit_price
                )
                inv = Inventory.objects.select_for_update().filter(
                    product=product, branch_id=branch_id
                ).first()
                if not inv or inv.quantity_on_hand < qty:
                    raise serializers.ValidationError(
                        {'items': f'Not enough stock for {product.product_name}.'}
                    )
                resolved_items.append((product, qty, unit_price, inv))
                total += unit_price * qty

            txn = SalesTransaction.objects.create(
                branch_id=branch_id,
                staff=user if user.role == user.Role.STAFF else None,
                customer=user if user.role == user.Role.CUSTOMER else None,
                transaction_type=validated_data['transactionType'],
                mode_of_payment=validated_data['modeOfPayment'],
                order_type=order_type,
                delivery_address=validated_data.get('deliveryAddress', ''),
                total_amount=total,
            )

            for product, qty, unit_price, inv in resolved_items:
                SalesItem.objects.create(
                    transaction=txn,
                    product=product,
                    quantity_sold=qty,
                    unit_price=unit_price,
                )
                inv.quantity_on_hand -= qty
                inv.save()
                # FEFO: drain whatever batches exist for this product/branch,
                # nearest-expiry first — mirrors sales/views.py:_deduct_stock.
                Batch.consume_fefo(product, inv.branch, qty)

        return txn

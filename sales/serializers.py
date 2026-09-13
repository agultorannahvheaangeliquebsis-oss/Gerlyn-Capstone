# sales/serializers.py

from rest_framework import serializers
from django.db import transaction

from .models import SalesTransaction, SalesItem
from .pricing import price_cart
from core.scoping import user_can_access_branch
from inventory.models import Product, Inventory, Batch, StockMovement
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

        # A client asking for WHOLESALE is a request, not a decision. The tier
        # is resolved from the account: only a customer the Owner or a Branch
        # Manager has marked wholesale-eligible actually gets wholesale
        # prices, and anyone else is priced at RETAIL no matter what the
        # payload said. Until this check existed, any authenticated CUSTOMER
        # could post orderType=Wholesale and be billed at wholesale_price.
        #
        # This is the same refusal the PWD/Senior claim already gets a few
        # lines down: an unverified assertion over the wire does not move
        # money.
        order_type = validated_data['orderType']
        if order_type == SalesTransaction.OrderType.WHOLESALE:
            profile = getattr(user, 'customer_profile', None)
            if user.role == user.Role.CUSTOMER and not (
                profile and profile.is_wholesale_eligible
            ):
                order_type = SalesTransaction.OrderType.RETAIL

        with transaction.atomic():
            # Lock stock and validate availability before writing anything, so
            # total_amount can be set at creation time (billing.signals reads
            # it then — creating with 0 and updating afterward left the
            # billing record's amount_due permanently stuck at 0) and an
            # oversold cart is rejected outright instead of being silently
            # short-filled (fine when staff are watching; not fine for
            # unattended customer checkout).
            #
            # Availability is checked on EVERY channel; stock is only DEDUCTED
            # on Physical ones — see the deduction loop below.
            cart_lines = []
            inventories = []
            for item in items_data:
                product = Product.objects.get(id=item['productId'])
                qty = item['quantity']
                unit_price = (
                    product.wholesale_price
                    if order_type == SalesTransaction.OrderType.WHOLESALE and product.wholesale_price
                    else product.unit_price
                )
                # available_qty nets out anything past its expiry_date — a
                # POS or online sale must never draw down expired stock.
                inv = Inventory.objects.select_for_update().sellable().filter(
                    product=product, branch_id=branch_id
                ).first()
                if not inv or inv.available_qty < qty:
                    raise serializers.ValidationError(
                        {'items': f'Not enough stock for {product.product_name}.'}
                    )
                cart_lines.append((product, qty, unit_price))
                inventories.append(inv)

            # Same pricing engine as the POS, so a VAT-exempt product is
            # exempt on both paths and reports don't undercount VAT on
            # API-created sales.
            #
            # No discount is ever claimed here: this endpoint serves online
            # and customer-facing checkout, where nobody has presented a PWD
            # or OSCA card to a cashier. Accepting a claim over the wire would
            # mean granting a statutory discount on an unverified assertion.
            priced = price_cart(cart_lines, discount_type=None)
            total = priced['total_amount']

            txn = SalesTransaction.objects.create(
                branch_id=branch_id,
                staff=user if user.role == user.Role.STAFF else None,
                customer=user if user.role == user.Role.CUSTOMER else None,
                transaction_type=validated_data['transactionType'],
                mode_of_payment=validated_data['modeOfPayment'],
                order_type=order_type,
                # This endpoint's validate() already refuses an Online order
                # with a blank deliveryAddress, so an Online sale here is
                # always a Delivery and a Physical one is always handed over
                # at the counter. Set explicitly rather than left on the
                # model default: fulfillment_type gates who may apply the
                # statutory discount (F-04), and defaulting an addressed
                # online order to Pickup would open exactly the in-person
                # verification hole that gate exists to close.
                fulfillment_type=(
                    SalesTransaction.FulfillmentType.DELIVERY
                    if validated_data['transactionType']
                    == SalesTransaction.TransactionType.ONLINE
                    else SalesTransaction.FulfillmentType.PICKUP
                ),
                delivery_address=validated_data.get('deliveryAddress', ''),
                total_amount=total,
                total_vat_amount=priced['total_vat_amount'],
            )

            # An Online order has not left the shelf yet — it is Pending
            # until the Order Console walks it to Completed, and
            # sales.views.transaction_set_status deducts it there
            # (_deduct_stock). Deducting here as well took the stock twice for
            # one sale: 5 units at creation, 5 more at completion. Availability
            # was still validated above, exactly like the storefront checkout
            # (orders.customer_views.checkout) does — reserve nothing, promise
            # nothing that isn't on the shelf, and let completion be the one
            # place stock moves.
            #
            # A Physical sale over this endpoint IS the moment of sale (there
            # is no console step to wait for, the way there is none for the
            # walk-in POS), so it still deducts here.
            deducts_now = (
                validated_data['transactionType']
                == SalesTransaction.TransactionType.PHYSICAL
            )

            for line, inv in zip(priced['lines'], inventories):
                product, qty = line['product'], line['quantity']
                SalesItem.objects.create(
                    transaction=txn,
                    product=product,
                    quantity_sold=qty,
                    unit_price=line['unit_price'],
                    is_vat_exempt_line=line['is_vat_exempt_line'],
                    vat_amount=line['vat_amount'],
                )
                if not deducts_now:
                    continue
                before = inv.quantity_on_hand
                inv.quantity_on_hand -= qty
                inv.save()
                # FEFO: drain the nearest-expiry floor batches for this
                # product/branch — mirrors sales/views.py:_deduct_stock.
                Batch.consume_fefo(product, inv.branch, Batch.Location.FLOOR, qty)
                # ...and so does the audit row. Stock that moves without a
                # StockMovement is stock nobody can trace: the Stock Movement
                # log, and every report reading it, silently missed every sale
                # written through this endpoint.
                StockMovement.objects.create(
                    inventory=inv,
                    location_type=StockMovement.LocationType.FLOOR,
                    movement_type=StockMovement.MovementType.SALE,
                    quantity_change=inv.quantity_on_hand - before,
                    quantity_before=before,
                    quantity_after=inv.quantity_on_hand,
                    reference_id=txn.pk,
                    notes=f'Order {txn.order_number}',
                    created_by=user,
                )

            # billing.signals already created the record at the right
            # amount_due (total_amount was set on create() above, not
            # patched in afterward). The walk-in POS and the storefront
            # checkout both copy the real payment method onto it too; this
            # endpoint used to skip that step entirely, so an API-created
            # GCash or Card sale silently kept the signal's Cash default
            # with no reference number — the billing record existed (the
            # 1:1 guarantee held) but its contents disagreed with the sale.
            billing = getattr(txn, 'billing_record', None)
            if billing:
                billing.payment_method = validated_data['modeOfPayment']
                billing.save()

        return txn

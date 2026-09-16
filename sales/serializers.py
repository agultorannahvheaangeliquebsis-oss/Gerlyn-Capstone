# sales/serializers.py

from rest_framework import serializers
from django.db import transaction

from .models import SalesTransaction, SalesItem, SalesItemBatchConsumption
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
    orderStatus = serializers.CharField(source='order_status', read_only=True)
    deliveryAddress = serializers.CharField(source='delivery_address', read_only=True)
    items = SalesItemSerializer(many=True, read_only=True)

    class Meta:
        model = SalesTransaction
        fields = [
            'id', 'orderNumber', 'branch', 'staff', 'customer',
            'transactionDate', 'totalAmount', 'transactionType', 'modeOfPayment',
            'orderStatus', 'deliveryAddress', 'items',
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
# Handles POST /api/sales/ — mirrors sales/views.py:transaction_add, including
# resolving each line's price from the quantity ordered against the product's
# bulk tiers.
# =============================================================================

class SalesItemWriteSerializer(serializers.Serializer):
    productId = serializers.IntegerField()
    quantity = serializers.IntegerField(min_value=1)


class SalesTransactionWriteSerializer(serializers.Serializer):
    branchId = serializers.IntegerField(required=False)
    transactionType = serializers.ChoiceField(choices=SalesTransaction.TransactionType.choices)
    modeOfPayment = serializers.ChoiceField(choices=SalesTransaction.ModeOfPayment.choices)
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

        # The price tier is never taken from the payload — there is no field
        # for it. It is derived below from the quantity actually ordered, the
        # same rule the counter and the storefront apply, so a client cannot
        # ask to be billed at a bulk rate it has not bought its way into.
        #
        # Quantities are totalled per product first: a payload that splits one
        # product across two item entries must earn the same break as one that
        # sends it as a single line.
        qty_by_product = {}
        for item in items_data:
            qty_by_product[item['productId']] = (
                qty_by_product.get(item['productId'], 0) + item['quantity']
            )

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
                product = Product.objects.prefetch_related('price_tiers').get(
                    id=item['productId']
                )
                qty = item['quantity']
                unit_price = product.price_at(qty_by_product[item['productId']])
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

            # X-3 — whoever actually authenticated to create this sale,
            # regardless of role, mirroring sales.views.transaction_add.
            # Null for a Customer (an online order has no staff-side
            # operator) or a Supplier (has no business creating sales at
            # all — permission-checked in the view before this is reached).
            recorded_by = user if user.role in (
                user.Role.OWNER, user.Role.BRANCH_MANAGER, user.Role.STAFF
            ) else None

            txn = SalesTransaction.objects.create(
                branch_id=branch_id,
                staff=user if user.role == user.Role.STAFF else None,
                recorded_by=recorded_by,
                customer=user if user.role == user.Role.CUSTOMER else None,
                transaction_type=validated_data['transactionType'],
                mode_of_payment=validated_data['modeOfPayment'],
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
                item = SalesItem.objects.create(
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
                consumed = Batch.consume_fefo(product, inv.branch, Batch.Location.FLOOR, qty)
                # O-1 — same batch-consumption record sales.views._deduct_stock
                # writes, so a Physical sale created through this endpoint can
                # be voided and reversed against the SAME batches later too,
                # not just the aggregate Inventory row.
                SalesItemBatchConsumption.objects.bulk_create([
                    SalesItemBatchConsumption(sales_item=item, batch=batch, quantity_taken=taken)
                    for batch, taken in consumed
                ])
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

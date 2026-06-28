from django.db import models
from branches.models import Branch
from inventory.models import Product
from accounts.models import User


class SalesTransaction(models.Model):
    class TransactionType(models.TextChoices):
        ONLINE = 'Online', 'Online'
        PHYSICAL = 'Physical', 'Physical'

    class ModeOfPayment(models.TextChoices):
        CASH = 'Cash', 'Cash'
        GCASH = 'GCash', 'GCash'
        CARD = 'Card', 'Card'

    branch = models.ForeignKey(
        Branch, on_delete=models.SET_NULL,
        null=True, related_name='transactions'
    )
    staff = models.ForeignKey(
        User, on_delete=models.SET_NULL, null=True,
        limit_choices_to={'role': 'STAFF'},
        related_name='processed_transactions'
    )
    customer = models.ForeignKey(
        User, on_delete=models.SET_NULL, null=True, blank=True,
        limit_choices_to={'role': 'CUSTOMER'},
        related_name='purchases'
    )
    transaction_date = models.DateTimeField(auto_now_add=True)
    total_amount = models.DecimalField(max_digits=12, decimal_places=2, default=0)
    transaction_type = models.CharField(
        max_length=10, choices=TransactionType.choices,
        default=TransactionType.PHYSICAL
    )
    mode_of_payment = models.CharField(
        max_length=10, choices=ModeOfPayment.choices,
        default=ModeOfPayment.CASH
    )

    def __str__(self):
        return f"Transaction #{self.id} - {self.branch} - {self.transaction_date.strftime('%Y-%m-%d')}"


class SalesItem(models.Model):
    transaction = models.ForeignKey(
        SalesTransaction, on_delete=models.CASCADE,
        related_name='items'
    )
    product = models.ForeignKey(
        Product, on_delete=models.SET_NULL,
        null=True, related_name='sales_items'
    )
    quantity_sold = models.PositiveIntegerField()
    unit_price = models.DecimalField(max_digits=10, decimal_places=2)
    subtotal = models.DecimalField(max_digits=12, decimal_places=2, editable=False)

    def save(self, *args, **kwargs):
        # Auto-calculate subtotal
        self.subtotal = self.quantity_sold * self.unit_price
        super().save(*args, **kwargs)

    def __str__(self):
        return f"{self.product.product_name} x{self.quantity_sold}"
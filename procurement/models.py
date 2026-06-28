from django.db import models
from branches.models import Branch
from inventory.models import Product
from accounts.models import User


class Procurement(models.Model):
    class Status(models.TextChoices):
        ORDERED = 'Ordered', 'Ordered'
        RECEIVED = 'Received', 'Received'
        CANCELLED = 'Cancelled', 'Cancelled'

    branch = models.ForeignKey(
        Branch, on_delete=models.SET_NULL,
        null=True, related_name='procurements'
    )
    supplier = models.ForeignKey(
        User, on_delete=models.SET_NULL, null=True,
        limit_choices_to={'role': 'SUPPLIER'},
        related_name='fulfilled_orders'
    )
    owner = models.ForeignKey(
        User, on_delete=models.SET_NULL, null=True,
        limit_choices_to={'role': 'OWNER'},
        related_name='authorized_procurements'
    )
    order_date = models.DateField()
    delivery_date = models.DateField(null=True, blank=True)
    total_cost = models.DecimalField(
        max_digits=12, decimal_places=2, default=0
    )
    status = models.CharField(
        max_length=15, choices=Status.choices,
        default=Status.ORDERED
    )

    def __str__(self):
        return f"Procurement #{self.id} - {self.branch} - {self.status}"


class ProcurementItem(models.Model):
    procurement = models.ForeignKey(
        Procurement, on_delete=models.CASCADE,
        related_name='items'
    )
    product = models.ForeignKey(
        Product, on_delete=models.SET_NULL,
        null=True, related_name='procurement_items'
    )
    quantity_ordered = models.PositiveIntegerField()
    quantity_received = models.PositiveIntegerField(default=0)
    cost_price = models.DecimalField(max_digits=10, decimal_places=2)

    def __str__(self):
        return f"{self.product.product_name} x{self.quantity_ordered}"
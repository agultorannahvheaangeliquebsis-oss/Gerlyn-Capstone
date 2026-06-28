from django.db import models
from branches.models import Branch


class ProductCategory(models.Model):
    category_name = models.CharField(max_length=100)
    description = models.TextField(blank=True)

    def __str__(self):
        return self.category_name

    class Meta:
        verbose_name_plural = "Product Categories"


class Product(models.Model):
    class UnitOfMeasure(models.TextChoices):
        SACK = 'Sack', 'Sack'
        KILO = 'Kilo', 'Kilo'
        PIECE = 'Piece', 'Piece'

    category = models.ForeignKey(
        ProductCategory, on_delete=models.SET_NULL,
        null=True, related_name='products'
    )
    sku_code = models.CharField(max_length=50, unique=True)
    product_name = models.CharField(max_length=150)
    unit_of_measure = models.CharField(
        max_length=20, choices=UnitOfMeasure.choices, default=UnitOfMeasure.PIECE
    )
    unit_price = models.DecimalField(max_digits=10, decimal_places=2)

    def __str__(self):
        return f"{self.product_name} ({self.sku_code})"


class Inventory(models.Model):
    product = models.ForeignKey(
        Product, on_delete=models.CASCADE, related_name='inventory_records'
    )
    branch = models.ForeignKey(
        Branch, on_delete=models.CASCADE, related_name='inventory'
    )
    quantity_on_hand = models.PositiveIntegerField(default=0)
    reorder_level = models.PositiveIntegerField(default=0)
    restock = models.BooleanField(default=False)
    last_stock_update = models.DateTimeField(auto_now=True)

    def __str__(self):
        return f"{self.product.product_name} - {self.branch.branch_name}"

    def save(self, *args, **kwargs):
        # Auto-flag restock if quantity is at or below reorder level
        self.restock = self.quantity_on_hand <= self.reorder_level
        super().save(*args, **kwargs)

    class Meta:
        verbose_name_plural = "Inventory"
        unique_together = ('product', 'branch')
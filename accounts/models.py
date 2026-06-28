from django.contrib.auth.models import AbstractUser
from django.db import models

class User(AbstractUser):
    class Role(models.TextChoices):
        ADMIN = 'ADMIN', 'Admin'
        BRANCH_MANAGER = 'BRANCH_MANAGER', 'Branch Manager'
        STAFF = 'STAFF', 'Store Staff'
        SUPPLIER = 'SUPPLIER', 'Supplier'
        CUSTOMER = 'CUSTOMER', 'Customer'

    role = models.CharField(max_length=20, choices=Role.choices, default=Role.CUSTOMER)

    def __str__(self):
        return f"{self.username} ({self.get_role_display()})"
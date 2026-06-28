from django.contrib.auth.models import AbstractUser
from django.db import models


class User(AbstractUser):
    class Role(models.TextChoices):
        OWNER = 'OWNER', 'Owner'
        BRANCH_MANAGER = 'BRANCH_MANAGER', 'Branch Manager'
        STAFF = 'STAFF', 'Store Staff'
        SUPPLIER = 'SUPPLIER', 'Supplier'
        CUSTOMER = 'CUSTOMER', 'Customer'

    role = models.CharField(
        max_length=20, choices=Role.choices, default=Role.CUSTOMER
    )

    def __str__(self):
        return f"{self.username} ({self.get_role_display()})"


class OwnerProfile(models.Model):
    user = models.OneToOneField(
        User, on_delete=models.CASCADE, related_name='owner_profile'
    )
    owner_name = models.CharField(max_length=100)
    contact_number = models.CharField(max_length=20)

    def __str__(self):
        return self.owner_name


class BranchManagerProfile(models.Model):
    user = models.OneToOneField(
        User, on_delete=models.CASCADE, related_name='manager_profile'
    )
    branch = models.ForeignKey(
        'branches.Branch', on_delete=models.SET_NULL,
        null=True, blank=True, related_name='managers'
    )
    manager_name = models.CharField(max_length=100)
    contact_number = models.CharField(max_length=20)

    def __str__(self):
        return self.manager_name


class StaffProfile(models.Model):
    user = models.OneToOneField(
        User, on_delete=models.CASCADE, related_name='staff_profile'
    )
    branch = models.ForeignKey(
        'branches.Branch', on_delete=models.SET_NULL,
        null=True, blank=True, related_name='staff'
    )
    staff_name = models.CharField(max_length=100)
    contact_number = models.CharField(max_length=20)
    date_hired = models.DateField(null=True, blank=True)
    shift_start = models.TimeField(null=True, blank=True)
    shift_end = models.TimeField(null=True, blank=True)

    def __str__(self):
        return self.staff_name

class CustomerProfile(models.Model):
    user = models.OneToOneField(
        User, on_delete=models.CASCADE, related_name='customer_profile'
    )
    customer_name = models.CharField(max_length=100)
    contact_number = models.CharField(max_length=20)
    address = models.TextField(blank=True)

    def __str__(self):
        return self.customer_name


class SupplierProfile(models.Model):
    user = models.OneToOneField(
        User, on_delete=models.CASCADE, related_name='supplier_profile'
    )
    supplier_name = models.CharField(max_length=100)
    contact_number = models.CharField(max_length=20)
    address = models.TextField(blank=True)

    def __str__(self):
        return self.supplier_name
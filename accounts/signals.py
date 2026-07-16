from django.db.models.signals import post_save
from django.dispatch import receiver
from .models import (
    User, OwnerProfile, BranchManagerProfile,
    StaffProfile, CustomerProfile, SupplierProfile
)


@receiver(post_save, sender=User)
def create_user_profile(sender, instance, created, **kwargs):
    if not created:
        return  # only run when a NEW user is created, not on every save

    role = instance.role

    if role == User.Role.OWNER and not hasattr(instance, 'owner_profile'):
        OwnerProfile.objects.create(user=instance, owner_name=instance.username)

    elif role == User.Role.BRANCH_MANAGER and not hasattr(instance, 'manager_profile'):
        BranchManagerProfile.objects.create(user=instance, manager_name=instance.username)

    elif role == User.Role.STAFF and not hasattr(instance, 'staff_profile'):
        StaffProfile.objects.create(user=instance, staff_name=instance.username)

    elif role == User.Role.SUPPLIER and not hasattr(instance, 'supplier_profile'):
        SupplierProfile.objects.create(user=instance, supplier_name=instance.username)

    elif role == User.Role.CUSTOMER and not hasattr(instance, 'customer_profile'):
        CustomerProfile.objects.create(user=instance, first_name=instance.username, last_name='')
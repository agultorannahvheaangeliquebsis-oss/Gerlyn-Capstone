from django.db.models.signals import post_save
from django.dispatch import receiver

from .models import Branch


@receiver(post_save, sender=Branch)
def create_branch_warehouse(sender, instance, created, **kwargs):
    """Every branch needs a Warehouse row — transfer_create() looks one up
    by branch and treats a missing one as a hard error ("No warehouse found
    for your branch"). Nothing used to create this row anywhere, so branches
    added through the normal Add Branch form silently had no warehouse."""
    if not created:
        return

    from inventory.models import Warehouse

    if not hasattr(instance, 'warehouse'):
        Warehouse.objects.create(
            branch=instance,
            name=f'{instance.branch_name} Warehouse',
        )

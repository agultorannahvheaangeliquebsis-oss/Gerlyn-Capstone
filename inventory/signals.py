from django.db.models.signals import post_save
from django.dispatch import receiver

from .models import Batch


@receiver(post_save, sender=Batch)
def notify_batch_near_expiry(sender, instance, created, **kwargs):
    """Alert when a new or edited lot is already inside the 30-day window."""
    update_fields = kwargs.get('update_fields')
    if (
        not created
        and update_fields is not None
        and not {'expiry_date', 'quantity'}.intersection(update_fields)
    ):
        return
    from .alerts import notify_near_expiry

    notify_near_expiry(instance)

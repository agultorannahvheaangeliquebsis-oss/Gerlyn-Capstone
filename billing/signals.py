from django.db.models.signals import post_save
from django.dispatch import receiver
from sales.models import SalesTransaction
from .models import BillingRecord


@receiver(post_save, sender=SalesTransaction)
def auto_create_billing_record(sender, instance, created, **kwargs):
    """
    Automatically create a BillingRecord whenever a new SalesTransaction
    is saved — whether it comes from an online customer checkout or a
    staff-recorded in-store sale via /sales/add/.
    """
    if created and not hasattr(instance, 'billing_record'):
        BillingRecord.objects.create(
            transaction=instance,
            amount_due=instance.total_amount,
            # Default to Cash for in-store sales; customer-facing checkout
            # can update this via billing_create if a different method is chosen.
            payment_method=BillingRecord.PaymentMethod.CASH,
            status=BillingRecord.PaymentStatus.PENDING,
        )
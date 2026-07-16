from django.conf import settings
from django.db import models


class TimeStampedModel(models.Model):
    """Abstract base: adds created/updated timestamps. Use on NEW models."""

    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        abstract = True


class AuditStampedModel(TimeStampedModel):
    """
    Abstract base giving a digital audit trail (a documented objective:
    every write is attributable). Adds created_by / updated_by on top of
    timestamps. Apply to NEW models; existing models that already carry
    their own audit fields (StockMovement, StockTransfer, Procurement,
    BillingRecord) are left as-is to avoid churn this week.
    """

    created_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="+",
    )
    updated_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="+",
    )

    class Meta:
        abstract = True

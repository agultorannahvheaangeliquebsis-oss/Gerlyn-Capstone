from django.conf import settings
from django.db import models


class OfflineOperation(models.Model):
    """The server's ledger of changes a browser queued while it was offline.

    One row per client-generated operation id. It exists for exactly one
    reason: so replaying the same queued change twice (a dropped response, two
    tabs, a retry button) can never apply it twice. Only APPLIED is sticky --
    a REJECTED or CONFLICT row changed nothing, so the same operation may be
    sent again once whatever blocked it has been fixed.

    The business effect itself lives in the normal tables (sales, stock,
    procurement) exactly as if the user had submitted the form online; this
    row only points at it.
    """

    class Status(models.TextChoices):
        PENDING = 'PENDING', 'Pending'
        APPLIED = 'APPLIED', 'Applied'
        REJECTED = 'REJECTED', 'Rejected'
        CONFLICT = 'CONFLICT', 'Conflict'

    user = models.ForeignKey(
        settings.AUTH_USER_MODEL, on_delete=models.CASCADE,
        related_name='offline_operations',
    )
    client_op_id = models.CharField(max_length=64)
    op_type = models.CharField(max_length=40)
    target_pk = models.PositiveIntegerField(null=True, blank=True)
    # sha256 of the submitted fields. An id re-sent with different content is a
    # client bug (or tampering) and is refused rather than silently accepted.
    fingerprint = models.CharField(max_length=64)
    status = models.CharField(
        max_length=10, choices=Status.choices, default=Status.PENDING,
    )
    message = models.TextField(blank=True)
    # {'label': 'SO-2026-0042', 'url': '/sales/42/'} -- what the client shows
    # once the change is confirmed.
    result = models.JSONField(default=dict, blank=True)
    attempts = models.PositiveIntegerField(default=0)
    queued_at = models.DateTimeField(null=True, blank=True)
    received_at = models.DateTimeField(auto_now_add=True)
    applied_at = models.DateTimeField(null=True, blank=True)

    class Meta:
        ordering = ['-received_at', '-pk']
        constraints = [
            models.UniqueConstraint(
                fields=['user', 'client_op_id'], name='offline_op_unique_per_user',
            ),
        ]
        indexes = [
            models.Index(fields=['status', '-received_at'], name='offline_op_status_idx'),
        ]

    def __str__(self):
        return f'{self.op_type} {self.client_op_id} [{self.status}]'

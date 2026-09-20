from django.db import models
from django.utils import timezone


class AppendOnlyError(Exception):
    """Raised on any attempt to edit or delete an audit row."""


class AuditLogQuerySet(models.QuerySet):
    """No bulk edit or delete either — the row-level guards on AuditLog alone
    would leave `.update()` / `.delete()` on a queryset wide open."""

    def update(self, **kwargs):
        raise AppendOnlyError('The audit log is append-only.')

    def bulk_update(self, objs, fields, batch_size=None):
        raise AppendOnlyError('The audit log is append-only.')

    def delete(self):
        raise AppendOnlyError('The audit log is append-only.')

    def _raw_delete(self, using):
        raise AppendOnlyError('The audit log is append-only.')


class AuditLog(models.Model):
    """One recorded data change: who did what to which record, and when.

    Append-only by design: rows are only ever inserted. That is enforced in
    the model and queryset (below), in the Django admin (read-only), and — where
    the database user allows it — by MySQL triggers (audit/triggers.py).

    The actor and the branch are stored as plain integers, not foreign keys, on
    purpose: an FK with SET_NULL would UPDATE audit rows whenever a user or
    branch is removed, and the log has to outlive both. A hard-deleted account
    (accounts.services.hard_delete_personal_data) therefore stays traceable by
    its id without this table holding anything personal about it.

    Personal data never lands in before/after: see audit/registry.py, where
    each model names the fields whose values are hidden (the fact that they
    changed is still recorded).
    """

    class Action(models.TextChoices):
        CREATE = 'create', 'Created'
        UPDATE = 'update', 'Updated'
        DELETE = 'delete', 'Deleted'

    class Source(models.TextChoices):
        REQUEST = 'request', 'Web request'
        SYSTEM = 'system', 'System'

    timestamp = models.DateTimeField(default=timezone.now, db_index=True, editable=False)

    actor_id = models.PositiveBigIntegerField(null=True, blank=True, db_index=True)
    actor_role = models.CharField(max_length=20, blank=True)
    source = models.CharField(max_length=10, choices=Source.choices, default=Source.SYSTEM)

    action = models.CharField(max_length=10, choices=Action.choices)
    # "app_label.ModelName", e.g. "sales.SalesTransaction".
    entity = models.CharField(max_length=80, db_index=True)
    record_id = models.CharField(max_length=64)
    # A name for the record as it read at the time (an order number, a product
    # name) — never anything personal; see registry.AuditConfig.repr_field.
    record_repr = models.CharField(max_length=255, blank=True)
    branch_id = models.PositiveIntegerField(null=True, blank=True, db_index=True)

    summary = models.CharField(max_length=500, blank=True)
    # Only the fields that changed (update), the full row (create → after,
    # delete → before). Masked values read "[personal data]" / "[file]".
    before = models.JSONField(null=True, blank=True)
    after = models.JSONField(null=True, blank=True)

    ip_address = models.GenericIPAddressField(null=True, blank=True)
    # Groups the rows one request produced (an order and its items, say).
    request_id = models.CharField(max_length=32, blank=True, db_index=True)

    objects = AuditLogQuerySet.as_manager()

    class Meta:
        ordering = ['-timestamp', '-id']
        indexes = [
            models.Index(fields=['entity', 'record_id'], name='audit_entity_record_idx'),
            models.Index(fields=['actor_id', 'timestamp'], name='audit_actor_time_idx'),
        ]

    def __str__(self):
        return f'{self.get_action_display()} {self.entity} #{self.record_id}'

    def save(self, *args, **kwargs):
        if not self._state.adding:
            raise AppendOnlyError('The audit log is append-only: rows cannot be edited.')
        super().save(*args, **kwargs)

    def delete(self, *args, **kwargs):
        raise AppendOnlyError('The audit log is append-only: rows cannot be deleted.')

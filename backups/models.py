from django.conf import settings
from django.db import models
from django.utils import timezone


class BackupRun(models.Model):
    """One backed-up artifact from one backup run.

    A run produces several: the database dump, the uploaded-files archive, the
    list of erased accounts, and a manifest tying them together. They share a
    run_id. Only what the backup itself knows is recorded here — the status page
    reads this, since the write-only Backblaze key can't list the bucket.
    """

    class Kind(models.TextChoices):
        DATABASE = 'database', 'Database'
        MEDIA = 'media', 'Uploaded files'
        ERASURES = 'erasures', 'Erased-accounts list'
        MANIFEST = 'manifest', 'Manifest'

    class Status(models.TextChoices):
        RUNNING = 'running', 'Running'
        SUCCESS = 'success', 'Succeeded'
        FAILED = 'failed', 'Failed'

    run_id = models.CharField(max_length=32, db_index=True)
    kind = models.CharField(max_length=10, choices=Kind.choices)
    status = models.CharField(max_length=10, choices=Status.choices, default=Status.RUNNING)
    trigger = models.CharField(max_length=10, blank=True)
    started_at = models.DateTimeField(default=timezone.now, db_index=True)
    finished_at = models.DateTimeField(null=True, blank=True)
    size_bytes = models.PositiveBigIntegerField(null=True, blank=True)
    sha256 = models.CharField(max_length=64, blank=True)
    remote_keys = models.JSONField(default=list, blank=True)
    detail = models.JSONField(default=dict, blank=True)
    error = models.TextField(blank=True)

    class Meta:
        ordering = ['-started_at', '-id']
        indexes = [models.Index(fields=['kind', 'status', 'finished_at'], name='backup_kind_status_idx')]

    def __str__(self):
        return f'{self.get_kind_display()} backup {self.started_at:%Y-%m-%d %H:%M} ({self.status})'

    @property
    def duration_seconds(self):
        if self.finished_at is None:
            return None
        return int((self.finished_at - self.started_at).total_seconds())


class RestoreTestRecord(models.Model):
    """A restore test somebody carried out, entered by hand on the status page.

    A backup that has never been restored is a guess. This is the running proof
    that it has been, and when.
    """

    class Outcome(models.TextChoices):
        PASSED = 'passed', 'Passed'
        FAILED = 'failed', 'Failed'

    tested_on = models.DateField()
    backup_used = models.CharField(max_length=200, help_text='Which backup was restored, e.g. its date.')
    outcome = models.CharField(max_length=10, choices=Outcome.choices)
    notes = models.TextField(blank=True)
    recorded_by = models.ForeignKey(
        settings.AUTH_USER_MODEL, null=True, blank=True, on_delete=models.SET_NULL, related_name='+',
    )
    recorded_at = models.DateTimeField(default=timezone.now)

    class Meta:
        ordering = ['-tested_on', '-id']

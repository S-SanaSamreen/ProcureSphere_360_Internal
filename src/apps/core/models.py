import uuid

from django.conf import settings
from django.db import models


class TimeStampedModel(models.Model):
    """
    Abstract base model providing self-updating created_at and updated_at fields.
    """

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    created_at = models.DateTimeField(auto_now_add=True, db_index=True)
    updated_at = models.DateTimeField(auto_now=True)
    created_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.PROTECT,
        null=True,
        blank=True,
        related_name="%(class)s_created",
    )

    class Meta:
        abstract = True


class SoftDeleteModel(TimeStampedModel):
    """
    Abstract base model for entities requiring non-destructive soft-deletion.
    """

    is_deleted = models.BooleanField(default=False, db_index=True)
    deleted_at = models.DateTimeField(null=True, blank=True)
    deleted_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.PROTECT,
        null=True,
        blank=True,
        related_name="%(class)s_deleted",
    )

    class Meta:
        abstract = True


class JobRun(models.Model):
    """Execution record for a scheduled (Celery beat) task. Platform-level, not business data."""

    STATUS_SUCCESS = "SUCCESS"
    STATUS_FAILED = "FAILED"
    STATUS_CHOICES = [(STATUS_SUCCESS, "Success"), (STATUS_FAILED, "Failed")]

    task_name = models.CharField(max_length=200, db_index=True)
    started_at = models.DateTimeField(db_index=True)
    finished_at = models.DateTimeField(null=True, blank=True)
    status = models.CharField(max_length=10, choices=STATUS_CHOICES)
    detail = models.TextField(blank=True)
    organization = models.ForeignKey(
        "organization.Organization",
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="job_runs",
    )

    class Meta:
        ordering = ["-started_at"]

    def __str__(self):
        return f"{self.task_name} {self.status} @ {self.started_at:%Y-%m-%d %H:%M}"


class BackupRecord(models.Model):
    """Append-only evidence that a backup ran or a restore was tested (platform-level)."""

    KIND_BACKUP = "BACKUP"
    KIND_RESTORE_TEST = "RESTORE_TEST"
    KIND_CHOICES = [(KIND_BACKUP, "Backup"), (KIND_RESTORE_TEST, "Restore test")]
    STATUS_SUCCESS = "SUCCESS"
    STATUS_FAILED = "FAILED"
    STATUS_CHOICES = [(STATUS_SUCCESS, "Success"), (STATUS_FAILED, "Failed")]

    kind = models.CharField(max_length=20, choices=KIND_CHOICES, db_index=True)
    status = models.CharField(max_length=10, choices=STATUS_CHOICES)
    reference = models.CharField(max_length=500, help_text="Backup location / job reference")
    size_bytes = models.BigIntegerField(null=True, blank=True)
    checksum = models.CharField(max_length=128, blank=True)
    performed_by = models.CharField(max_length=200, blank=True)
    notes = models.TextField(blank=True)
    created_at = models.DateTimeField(auto_now_add=True, db_index=True)

    class Meta:
        ordering = ["-created_at"]

    def __str__(self):
        return f"{self.kind} {self.status} @ {self.created_at:%Y-%m-%d %H:%M}"

    def save(self, *args, **kwargs):
        if self.pk and not self._state.adding:
            raise ValueError("BackupRecord is append-only.")
        super().save(*args, **kwargs)

    def delete(self, *args, **kwargs):
        raise ValueError("BackupRecord is append-only.")


class MaintenanceState(models.Model):
    """Singleton (pk=1) toggled by the platform superuser. Blocks non-superusers when enabled."""

    enabled = models.BooleanField(default=False)
    message = models.CharField(
        max_length=300, blank=True, default="The system is undergoing scheduled maintenance."
    )
    updated_at = models.DateTimeField(auto_now=True)
    updated_by = models.CharField(max_length=200, blank=True)

    def __str__(self):
        return f"Maintenance mode: {'ON' if self.enabled else 'off'}"

    def save(self, *args, **kwargs):
        self.pk = 1
        super().save(*args, **kwargs)
        from django.core.cache import cache

        cache.delete("maintenance-state")

    @classmethod
    def current(cls):
        from django.conf import settings
        from django.core.cache import cache

        state = cache.get("maintenance-state")
        if state is None:
            obj = cls.objects.filter(pk=1).first()
            state = (bool(obj and obj.enabled), obj.message if obj else "")
            cache.set("maintenance-state", state, 30)
        enabled, message = state
        if getattr(settings, "MAINTENANCE_MODE", False):
            enabled = True
        return enabled, message or "The system is undergoing scheduled maintenance."

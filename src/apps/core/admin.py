from django.contrib import admin

from apps.core.admin_site import register_model
from apps.core.models import BackupRecord, JobRun, MaintenanceState


class JobRunAdmin(admin.ModelAdmin):
    """Read-only scheduled-task run log, visible to the platform superuser only."""

    list_display = ("task_name", "status", "started_at", "finished_at", "organization")
    list_filter = ("status", "task_name")
    search_fields = ("task_name", "detail")
    readonly_fields = [f.name for f in JobRun._meta.fields]

    def _platform(self, request):
        return request.user.is_active and request.user.is_superuser

    def has_module_permission(self, request):
        return self._platform(request)

    def has_view_permission(self, request, obj=None):
        return self._platform(request)

    def has_add_permission(self, request):
        return False

    def has_change_permission(self, request, obj=None):
        return False

    def has_delete_permission(self, request, obj=None):
        return False


register_model(JobRun, JobRunAdmin)


class _PlatformOnlyAdmin(admin.ModelAdmin):
    def _platform(self, request):
        return request.user.is_active and request.user.is_superuser

    def has_module_permission(self, request):
        return self._platform(request)

    def has_view_permission(self, request, obj=None):
        return self._platform(request)

    def has_delete_permission(self, request, obj=None):
        return False


class BackupRecordAdmin(_PlatformOnlyAdmin):
    """Append-only: platform superuser may add evidence, never edit or delete it."""

    list_display = ("kind", "status", "reference", "size_bytes", "performed_by", "created_at")
    list_filter = ("kind", "status")
    search_fields = ("reference", "performed_by", "notes")

    def has_add_permission(self, request):
        return self._platform(request)

    def has_change_permission(self, request, obj=None):
        return False


class MaintenanceStateAdmin(_PlatformOnlyAdmin):
    """Platform superuser toggles maintenance mode here."""

    list_display = ("__str__", "message", "updated_at", "updated_by")

    def has_add_permission(self, request):
        return self._platform(request) and not MaintenanceState.objects.exists()

    def has_change_permission(self, request, obj=None):
        return self._platform(request)

    def save_model(self, request, obj, form, change):
        obj.updated_by = request.user.email
        super().save_model(request, obj, form, change)


register_model(BackupRecord, BackupRecordAdmin)
register_model(MaintenanceState, MaintenanceStateAdmin)

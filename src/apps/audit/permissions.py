from rest_framework.permissions import SAFE_METHODS, BasePermission

from apps.accounts.models import Role


class IsAuditorOrSuperAdmin(BasePermission):
    """
    Grants access exclusively to Compliance Auditor or Super Admin users.
    """

    def has_permission(self, request, view):
        if not request.user or not request.user.is_authenticated:
            return False
        if request.user.is_superuser:
            return True
        role_code = getattr(request.user, "role_code", None) or (
            request.user.role.code if getattr(request.user, "role", None) else None
        )
        return role_code in [Role.AUDITOR, Role.SUPER_ADMIN]


class AuditorReadOnlyPermission(BasePermission):
    """
    Enforces strict read-only access (GET, HEAD, OPTIONS) for Auditor roles.
    Prevents any state mutations, guaranteeing immutable audit integrity.
    """

    def has_permission(self, request, view):
        if not request.user or not request.user.is_authenticated:
            return False
        role_code = getattr(request.user, "role_code", None) or (
            request.user.role.code if getattr(request.user, "role", None) else None
        )
        # Audit logs are immutable for everyone; Super Admin may read and export them (PRD 3.1).
        if request.user.is_superuser or role_code in (Role.SUPER_ADMIN, Role.AUDITOR):
            action = getattr(view, "action", None)
            if action in ["export_data", "export"] or getattr(request, "path", "").endswith(
                "/export/"
            ):
                return True
            return request.method in SAFE_METHODS
        return True

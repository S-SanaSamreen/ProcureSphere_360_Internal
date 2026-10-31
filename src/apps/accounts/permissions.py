from rest_framework.permissions import SAFE_METHODS, BasePermission

from .models import Role


def is_non_transacting_actor(user) -> bool:
    """
    PRD 3.6: the org Super Admin configures and oversees, and the platform superuser administers
    organizations; neither is a business actor, so neither may create/approve/reject/return/issue
    /pay/post anything. Read access to the org's records is unaffected.
    """
    return bool(
        getattr(user, "is_superuser", False) or getattr(user, "role_code", None) == Role.SUPER_ADMIN
    )


def forbid_non_transacting_actor(user, action: str = "perform this business action"):
    """Service-layer guard: raises ValidationError when the actor is a non-transacting admin."""
    if is_non_transacting_actor(user):
        from django.core.exceptions import ValidationError

        raise ValidationError(
            f"Super Admin and platform administrators cannot {action}; "
            "this is a business action reserved for the responsible business role."
        )


class HasRole(BasePermission):
    """
    Custom permission class verifying user's assigned role against allowed roles list.
    Usage in views: permission_classes = [HasRole([Role.PROC_EXEC, Role.PROC_MGR])]
    """

    allowed_roles = []

    def __init__(self, roles=None):
        if roles:
            self.allowed_roles = roles

    def has_permission(self, request, view):
        if not request.user or not request.user.is_authenticated:
            return False
        # PRD 3.6: neither the Super Admin nor the platform superuser is a business actor.
        if request.user.is_superuser:
            return False
        user_role = request.user.role_code
        return user_role in self.allowed_roles


class IsSuperAdmin(BasePermission):
    def has_permission(self, request, view):
        return bool(
            request.user
            and request.user.is_authenticated
            and (request.user.is_superuser or request.user.role_code == Role.SUPER_ADMIN)
        )


class IsNotVendorUser(BasePermission):
    """
    Blocks external vendor users from accessing internal resources and directories.
    """

    def has_permission(self, request, view):
        if not request.user or not request.user.is_authenticated:
            return False
        return not (
            getattr(request.user, "is_vendor", False)
            or getattr(request.user, "role_code", None) == Role.VENDOR_USER
        )


class IsRequester(BasePermission):
    def has_permission(self, request, view):
        return bool(
            request.user
            and request.user.is_authenticated
            and (request.user.role_code == Role.REQUESTER)
        )


class IsDeptApprover(BasePermission):
    def has_permission(self, request, view):
        return bool(
            request.user
            and request.user.is_authenticated
            and (request.user.role_code == Role.DEPT_APPROVER)
        )


class IsProcurementExec(BasePermission):
    def has_permission(self, request, view):
        return bool(
            request.user
            and request.user.is_authenticated
            and (request.user.role_code in [Role.PROC_EXEC, Role.PROC_MGR])
        )


class IsProcurementManager(BasePermission):
    def has_permission(self, request, view):
        # Tenant isolation: an internal user without an organisation sees nothing.
        if (
            request.user
            and request.user.is_authenticated
            and not request.user.is_superuser
            and getattr(request.user, "effective_organization", None) is None
        ):
            return False
        return bool(
            request.user
            and request.user.is_authenticated
            and (
                request.user.role_code == Role.PROC_MGR
                # Super Admin may read the manager's org-scoped views, never act (PRD 3.6)
                or (request.user.role_code == Role.SUPER_ADMIN and request.method in SAFE_METHODS)
            )
        )


class IsFinanceAP(BasePermission):
    def has_permission(self, request, view):
        return bool(
            request.user
            and request.user.is_authenticated
            and (request.user.role_code == Role.FINANCE_AP)
        )


class IsStoresReceiver(BasePermission):
    def has_permission(self, request, view):
        return bool(
            request.user
            and request.user.is_authenticated
            and (request.user.role_code == Role.STORES_RECEIVER)
        )


class IsLegalManager(BasePermission):
    def has_permission(self, request, view):
        return bool(
            request.user
            and request.user.is_authenticated
            and (request.user.role_code == Role.LEGAL_MGR)
        )


class IsAuditorReadOnly(BasePermission):
    """
    Auditor has global read-only access (GET, HEAD, OPTIONS). State changes return 403.
    """

    def has_permission(self, request, view):
        if not request.user or not request.user.is_authenticated:
            return False
        if request.user.role_code == Role.AUDITOR:
            return request.method in SAFE_METHODS
        return True


class IsVendorUser(BasePermission):
    def has_permission(self, request, view):
        return bool(
            request.user
            and request.user.is_authenticated
            and (request.user.role_code == Role.VENDOR_USER)
        )


class IsAuditorOrSuperAdmin(BasePermission):
    """
    Allows read-only access to Auditor and to Super Admin (user admin writes use IsSuperAdmin).
    """

    def has_permission(self, request, view):
        if not request.user or not request.user.is_authenticated:
            return False
        if request.user.is_superuser:
            return True
        role_code = getattr(request.user, "role_code", None) or (
            request.user.role.code if getattr(request.user, "role", None) else None
        )
        if role_code == Role.SUPER_ADMIN:
            return True
        if role_code == Role.AUDITOR and request.method in SAFE_METHODS:
            return True
        return False

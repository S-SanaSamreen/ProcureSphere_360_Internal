"""
Keeps the Django admin (the platform administrator's console) to platform concerns.

The platform superuser creates organizations and their Super Admins; tenant business records
(requisitions, sourcing, orders, vendors, contracts, budgets, approvals, notifications) are not
exposed there. Organization, user and audit administration stay available.
"""

from django.conf import settings

BUSINESS_APP_LABELS = frozenset(
    {
        "approvals",
        "budgets",
        "contracts",
        "invoices",
        "notifications",
        "orders",
        "receipts",
        "reports",
        "requisitions",
        "sourcing",
        "vendors",
    }
)


def _deny(*args, **kwargs):
    return False


def lock_business_models(site):
    if not getattr(settings, "PLATFORM_ADMIN_HIDE_BUSINESS_DATA", True):
        return
    for model, model_admin in site._registry.items():
        if model._meta.app_label in BUSINESS_APP_LABELS:
            for perm in (
                "has_module_permission",
                "has_view_permission",
                "has_add_permission",
                "has_change_permission",
                "has_delete_permission",
            ):
                setattr(model_admin, perm, _deny)

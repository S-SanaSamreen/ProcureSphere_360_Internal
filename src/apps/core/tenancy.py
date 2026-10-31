"""Shared tenant-isolation helpers.

Rules: only the single platform superuser spans organisations; every internal user is limited to
their own organisation and sees nothing when they have none; vendor users are limited to their
own vendor's records (independent of organisations).
"""

from django.db.models import Q

from apps.accounts.models import Role


def user_role_code(user):
    return getattr(user, "role_code", None) or (
        user.role.code if getattr(user, "role", None) else None
    )


def is_vendor_user(user):
    return bool(getattr(user, "is_vendor", False)) or user_role_code(user) == Role.VENDOR_USER


def user_org(user):
    return getattr(user, "effective_organization", None)


def scope_to_org(qs, user, *org_paths, vendor_path=None):
    """Restrict ``qs`` to rows reachable from the user's organisation via any of ``org_paths``.

    ``vendor_path`` (a lookup that points at a Vendor) lets vendor users see their own rows;
    without it vendor users see nothing.
    """
    if not user or not getattr(user, "is_authenticated", False):
        return qs.none()
    if user.is_superuser:
        return qs
    if is_vendor_user(user):
        vendor_id = getattr(user, "vendor_id", None)
        if vendor_path and vendor_id:
            return qs.filter(**{f"{vendor_path}_id": vendor_id})
        return qs.none()
    org = user_org(user)
    if org is None or not org_paths:
        return qs.none()
    cond = Q()
    for path in org_paths:
        cond |= Q(**{path: org})
    return qs.filter(cond)


def scope_users(qs, user):
    """Users visible to ``user``: same organisation only."""
    return scope_to_org(qs, user, "organization", "department__organization")


def scope_contracts(qs, user):
    return scope_to_org(
        qs,
        user,
        "contract_owner__organization",
        "contract_owner__department__organization",
        "po__cost_center__department__organization",
        "sourcing_event__requisition__department__organization",
        "vendor__organization",
        vendor_path="vendor",
    )


def scope_sourcing_events(qs, user):
    return scope_to_org(
        qs,
        user,
        "requisition__department__organization",
        "created_by__organization",
        "created_by__department__organization",
    )


def scope_scorecards(qs, user):
    return scope_to_org(
        qs,
        user,
        "evaluated_by__organization",
        "evaluated_by__department__organization",
        "vendor__organization",
        vendor_path="vendor",
    )


def scope_audit_logs(qs, user):
    return scope_to_org(qs, user, "actor__organization", "actor__department__organization")


def scope_match_exceptions(qs, user):
    from apps.invoices.models import SupplierInvoice
    from apps.invoices.selectors import scope_invoices_queryset

    ids = scope_invoices_queryset(SupplierInvoice.objects.all(), user).values("pk")
    return qs.filter(invoice_id__in=ids)


def scope_approval_requests(qs, user):
    return scope_to_org(
        qs,
        user,
        "department__organization",
        "cost_center__department__organization",
        "created_by__organization",
        "created_by__department__organization",
    )


def scope_approval_actions(qs, user):
    return scope_to_org(
        qs,
        user,
        "actor__organization",
        "actor__department__organization",
        "approval_request__department__organization",
        "approval_request__cost_center__department__organization",
    )


def scope_approval_policies(qs, user):
    """Policies of the user's organisation plus the organisation-agnostic defaults."""
    if user and getattr(user, "is_authenticated", False) and user.is_superuser:
        return qs
    if not user or not getattr(user, "is_authenticated", False) or is_vendor_user(user):
        return qs.none()
    org = user_org(user)
    if org is None:
        return qs.none()
    return qs.filter(
        Q(department__organization=org)
        | Q(cost_center__department__organization=org)
        | Q(department__isnull=True, cost_center__isnull=True)
    )

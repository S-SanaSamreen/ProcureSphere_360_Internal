"""Service-entry / milestone confirmation for service lines on purchase orders."""

from decimal import Decimal

from django.core.exceptions import PermissionDenied, ValidationError
from django.db import models, transaction
from django.utils import timezone

from apps.accounts.models import Role
from apps.audit.models import AuditLog
from apps.audit.services import create_audit_log_service
from apps.sourcing.controls import po_organization

from .models import POLine, PurchaseOrder, ServiceEntry

OPEN_PO_STATUSES = (
    PurchaseOrder.STATUS_ISSUED,
    PurchaseOrder.STATUS_ACKNOWLEDGED,
    PurchaseOrder.STATUS_PARTIAL_RECEIPT,
)


def _same_org(user, po) -> bool:
    user_org = getattr(user, "effective_organization", None)
    org = po_organization(po)
    return user_org is not None and org is not None and user_org.pk == org.pk


def confirmed_percent(po_line) -> Decimal:
    total = ServiceEntry.objects.filter(
        po_line=po_line, status=ServiceEntry.STATUS_CONFIRMED
    ).aggregate(t=models.Sum("percent_complete"))["t"]
    return total or Decimal("0.00")


def confirmed_service_quantity(po_line) -> Decimal:
    """Accepted quantity of a service line: confirmed milestone percent x line quantity."""
    if not po_line.is_service:
        return Decimal("0.00")
    return (po_line.quantity * confirmed_percent(po_line) / Decimal("100")).quantize(
        Decimal("0.01")
    )


def can_confirm_service_entry(user, entry) -> bool:
    """Business owner: the requisition's requester or a department approver of its department."""
    po = entry.po_line.po
    req = po.requisition
    if req is None or not _same_org(user, po):
        return False
    if user.pk == entry.recorded_by_id:
        return False
    if req.requester_id == user.pk:
        return True
    return (
        getattr(user, "role_code", None) == Role.DEPT_APPROVER
        and user.department_id is not None
        and user.department_id == req.department_id
    )


@transaction.atomic
def record_service_entry_service(
    *, po_line: POLine, user, milestone: str, percent_complete, entry_date=None, notes: str = ""
) -> ServiceEntry:
    """Stores Receiver records a milestone against a service line of an open PO."""
    po_line = POLine.objects.select_for_update().select_related("po").get(pk=po_line.pk)
    po = po_line.po
    if getattr(user, "role_code", None) != Role.STORES_RECEIVER or not _same_org(user, po):
        raise PermissionDenied("Only Stores of the PO's organization can record service entries.")
    if not po_line.is_service:
        raise ValidationError(f"'{po_line.item_description}' is not a service line.")
    if po.status not in OPEN_PO_STATUSES:
        raise ValidationError(f"Cannot record service entries on a PO in status '{po.status}'.")
    if not (milestone or "").strip():
        raise ValidationError("A milestone description is required.")
    try:
        pct = Decimal(str(percent_complete)).quantize(Decimal("0.01"))
    except Exception:
        raise ValidationError("Percent complete must be a number.")
    if pct <= 0 or pct > 100:
        raise ValidationError("Percent complete must be greater than 0 and at most 100.")
    pending = ServiceEntry.objects.filter(
        po_line=po_line, status=ServiceEntry.STATUS_RECORDED
    ).aggregate(t=models.Sum("percent_complete"))["t"] or Decimal("0.00")
    if confirmed_percent(po_line) + pending + pct > Decimal("100"):
        raise ValidationError(
            f"Entries for '{po_line.item_description}' would exceed 100% "
            f"(confirmed {confirmed_percent(po_line)}%, awaiting {pending}%)."
        )
    entry = ServiceEntry.objects.create(
        po_line=po_line,
        milestone=milestone.strip(),
        percent_complete=pct,
        entry_date=entry_date or timezone.localdate(),
        notes=notes or "",
        recorded_by=user,
        created_by=user,
    )
    create_audit_log_service(
        actor=user,
        action=AuditLog.ACTION_CREATE,
        target_model="ServiceEntry",
        target_object_id=entry.id,
        new_state={
            "po_number": po.po_number,
            "line": po_line.item_description,
            "milestone": entry.milestone,
            "percent_complete": str(pct),
        },
    )
    if po.requisition and po.requisition.requester:
        from apps.notifications.models import Notification
        from apps.notifications.services import notify_users

        notify_users(
            users=[po.requisition.requester],
            notification_type=Notification.TYPE_APPROVAL_REQUIRED,
            title=f"Confirm service milestone: PO {po.po_number}",
            message=f"{entry.milestone} ({pct}% of '{po_line.item_description}') awaits your confirmation.",
            target_url=f"/purchase-orders/{po.id}/service-entries/",
        )
    return entry


@transaction.atomic
def decide_service_entry_service(
    *, entry: ServiceEntry, user, confirm: bool, comments: str = ""
) -> ServiceEntry:
    entry = ServiceEntry.objects.select_for_update().select_related("po_line__po").get(pk=entry.pk)
    if entry.status != ServiceEntry.STATUS_RECORDED:
        raise ValidationError(f"This service entry is already {entry.status.lower()}.")
    if not can_confirm_service_entry(user, entry):
        raise PermissionDenied(
            "Only the business owner (requester or department approver) of this PO, "
            "other than whoever recorded the entry, can confirm it."
        )
    if not confirm and not (comments or "").strip():
        raise ValidationError("A reason is required to reject a service entry.")
    entry.status = ServiceEntry.STATUS_CONFIRMED if confirm else ServiceEntry.STATUS_REJECTED
    entry.confirmed_by = user
    entry.confirmed_at = timezone.now()
    entry.decision_comments = (comments or "").strip()
    entry.save()
    create_audit_log_service(
        actor=user,
        action=AuditLog.ACTION_APPROVE if confirm else AuditLog.ACTION_REJECT,
        target_model="ServiceEntry",
        target_object_id=entry.id,
        previous_state={"status": ServiceEntry.STATUS_RECORDED},
        new_state={
            "status": entry.status,
            "milestone": entry.milestone,
            "percent_complete": str(entry.percent_complete),
        },
    )
    if confirm:
        # Newly accepted service quantity may release invoices held until receipt is posted.
        from apps.invoices.services import rematch_held_exceptions_for_po

        rematch_held_exceptions_for_po(entry.po_line.po, user=user)
    return entry

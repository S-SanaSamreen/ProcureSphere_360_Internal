import hashlib
import json
import logging
import os
from decimal import Decimal
from typing import Optional

from django.conf import settings
from django.db import models, transaction
from django.utils import timezone

from apps.accounts.models import Role, User
from apps.audit.models import AuditLog
from apps.notifications.email_service import send_grn_acknowledgement_email
from apps.notifications.models import Notification
from apps.receipts.documents import generate_grn_acknowledgement_pdf
from apps.receipts.models import (
    GoodsReceipt,
    GRNAcknowledgement,
)

logger = logging.getLogger(__name__)


def compute_inspection_fingerprint(receipt: GoodsReceipt) -> str:
    """
    Computes a deterministic SHA-256 fingerprint representing the current
    line-by-line quality inspection results of a Goods Receipt.
    Used for deduplication and revision tracking.
    """
    lines_data = []
    lines = (
        receipt.lines.select_related("po_line")
        .prefetch_related("inspection", "rejections")
        .order_by("id")
    )
    for line in lines:
        insp = getattr(line, "inspection", None)
        rej = line.latest_rejection
        lines_data.append(
            {
                "line_id": str(line.id),
                "qty_recv": str(line.quantity_received),
                "qty_acc": str(line.quantity_accepted),
                "qty_rej": str(line.quantity_rejected),
                "insp_passed": insp.passed if insp else None,
                "insp_notes": str(insp.inspection_notes).strip() if insp else "",
                "rej_reason": str(rej.rejection_reason).strip() if rej else "",
            }
        )
    raw_str = json.dumps(lines_data, sort_keys=True)
    return hashlib.sha256(raw_str.encode("utf-8")).hexdigest()


def resolve_grn_acknowledgement_recipients(receipt: GoodsReceipt) -> dict:
    """
    Resolves authoritative internal and external recipients for a GRN acknowledgement.
    Enforces tenant/organization isolation and verified contact records.

    Returns a dictionary containing:
        - finance_users: list of User
        - procurement_execs: list of User
        - procurement_managers: list of User
        - vendor_contact: Optional VendorContact
        - vendor_email: Optional str
        - vendor_name: str
        - vendor_users: list of User
    """
    po = receipt.po
    vendor = po.vendor

    # Resolve organization context for tenant isolation
    org = None
    if po.cost_center and hasattr(po.cost_center, "department") and po.cost_center.department:
        org = po.cost_center.department.organization
    elif po.requisition and hasattr(po.requisition, "department") and po.requisition.department:
        org = po.requisition.department.organization
    elif vendor and getattr(vendor, "organization", None):
        org = vendor.organization

    # 1. Finance / AP Specialists
    finance_qs = User.objects.filter(role__code=Role.FINANCE_AP, is_active=True)
    if org:
        finance_qs = finance_qs.filter(
            models.Q(organization=org) | models.Q(organization__isnull=True)
        )
    finance_users = list(finance_qs.distinct())

    # 2. Procurement Executives
    proc_exec_qs = User.objects.filter(role__code=Role.PROC_EXEC, is_active=True)
    if org:
        proc_exec_qs = proc_exec_qs.filter(
            models.Q(organization=org) | models.Q(organization__isnull=True)
        )
    proc_exec_list = list(proc_exec_qs.distinct())

    if (
        po.created_by
        and po.created_by.is_active
        and po.created_by not in proc_exec_list
        and (
            getattr(po.created_by, "role_code", None) == Role.PROC_EXEC
            or (hasattr(po.created_by, "role") and po.created_by.role and po.created_by.role.code == Role.PROC_EXEC)
        )
    ):
        proc_exec_list.append(po.created_by)

    # 3. Procurement Managers
    proc_mgr_qs = User.objects.filter(role__code=Role.PROC_MGR, is_active=True)
    if org:
        proc_mgr_qs = proc_mgr_qs.filter(
            models.Q(organization=org) | models.Q(organization__isnull=True)
        )
    proc_mgr_list = list(proc_mgr_qs.distinct())

    # 4. Vendor / Supplier Contact Resolution
    primary_contact = None
    vendor_email = None
    if vendor:
        primary_contact = vendor.contacts.filter(is_primary=True).first() or vendor.contacts.first()
        if primary_contact and primary_contact.email:
            vendor_email = primary_contact.email.strip()
        elif vendor.email:
            vendor_email = vendor.email.strip()

    # Vendor portal users
    vendor_users = list(User.objects.filter(vendor=vendor, is_active=True)) if vendor else []

    return {
        "finance_users": finance_users,
        "procurement_execs": proc_exec_list,
        "procurement_managers": proc_mgr_list,
        "vendor_contact": primary_contact,
        "vendor_email": vendor_email,
        "vendor_name": vendor.legal_name if vendor else "Supplier",
        "vendor_users": vendor_users,
    }


def determine_inspection_outcome(receipt: GoodsReceipt) -> Optional[str]:
    """
    Inspects line records to evaluate whether the receipt is:
    - FULL_ACCEPTANCE
    - PARTIAL_ACCEPTANCE
    - REJECTION
    - None (Inspection incomplete / pending)
    """
    lines = list(receipt.lines.prefetch_related("inspection").all())
    if not lines:
        return None

    # Check if all lines have been inspected
    for line in lines:
        if not hasattr(line, "inspection") or line.inspection is None:
            return None

    total_accepted = sum(line.quantity_accepted for line in lines)
    total_rejected = sum(line.quantity_rejected for line in lines)

    if total_rejected == Decimal("0.00") and all(
        getattr(line, "inspection", None) and line.inspection.passed for line in lines
    ):
        return GRNAcknowledgement.OUTCOME_FULL

    if total_accepted == Decimal("0.00") and total_rejected > Decimal("0.00"):
        return GRNAcknowledgement.OUTCOME_REJECTION

    if total_accepted > Decimal("0.00") and total_rejected > Decimal("0.00"):
        return GRNAcknowledgement.OUTCOME_PARTIAL

    # Fallback to rejected if any line marked failed
    return GRNAcknowledgement.OUTCOME_REJECTION


@transaction.atomic
def generate_and_distribute_grn_acknowledgement_service(
    *,
    receipt: GoodsReceipt,
    triggered_by: User,
    force_retry: bool = False,
    notes: str = "",
) -> Optional[GRNAcknowledgement]:
    """
    Generates and distributes an authoritative, traceable GRN Acknowledgement following quality inspection.
    Enforces:
    - Complete inspection guard (incomplete inspections produce no final acknowledgement).
    - Status-aware outcomes (Full, Partial, Rejection).
    - Idempotency & deduplication: Identical inspection events return the existing acknowledgement
      without duplicate notification spam.
    - Versioned revisions: If inspection quantities or results are revised, a new version is created
      preserving previous versions in history.
    - Observability: Records separate status for in-app notifications and email delivery.
    - Graceful handling of missing vendor contacts.
    - Full immutable AuditLog tracking.
    """
    receipt = GoodsReceipt.objects.select_for_update().get(id=receipt.id)

    outcome = determine_inspection_outcome(receipt)
    if not outcome:
        logger.info(
            "Inspection for GRN %s is pending or incomplete. Final acknowledgement not generated.",
            receipt.grn_number,
        )
        return None

    fingerprint = compute_inspection_fingerprint(receipt)
    latest_ack = receipt.acknowledgements.order_by("-version").first()

    # Deduplication & Idempotency Check
    if latest_ack and latest_ack.inspection_fingerprint == fingerprint and not force_retry:
        logger.info(
            "GRN %s acknowledgement %s already matches current inspection fingerprint. Skipping duplicate generation.",
            receipt.grn_number,
            latest_ack.acknowledgement_number,
        )
        return latest_ack

    # Determine Versioning
    if latest_ack and latest_ack.inspection_fingerprint != fingerprint:
        version = latest_ack.version + 1
        ack_number = f"ACK-{receipt.grn_number}-V{version}"
        prior_ack = latest_ack
        is_revision = True
    elif latest_ack and force_retry:
        # Re-attempt delivery on existing version
        acknowledgement = latest_ack
        version = acknowledgement.version
        ack_number = acknowledgement.acknowledgement_number
        prior_ack = acknowledgement.prior_acknowledgement
        is_revision = False
    else:
        version = 1
        ack_number = f"ACK-{receipt.grn_number}-V1"
        prior_ack = None
        is_revision = False

    lines = list(
        receipt.lines.select_related("po_line")
        .prefetch_related("inspection", "rejections")
        .all()
    )
    total_recv = sum(line.quantity_received for line in lines)
    total_acc = sum(line.quantity_accepted for line in lines)
    total_rej = sum(line.quantity_rejected for line in lines)

    idempotency_key = f"{receipt.grn_number}:v{version}:{fingerprint}"

    if not (latest_ack and force_retry):
        acknowledgement = GRNAcknowledgement.objects.create(
            receipt=receipt,
            acknowledgement_number=ack_number,
            version=version,
            outcome=outcome,
            inspection_fingerprint=fingerprint,
            idempotency_key=idempotency_key,
            generated_by=triggered_by,
            generated_at=timezone.now(),
            total_received_qty=total_recv,
            total_accepted_qty=total_acc,
            total_rejected_qty=total_rej,
            document_notes=notes or receipt.remarks,
            prior_acknowledgement=prior_ack,
        )

    # 1. Generate Official PDF Document
    pdf_filename = f"{acknowledgement.acknowledgement_number}.pdf"
    pdf_dir = os.path.join(settings.MEDIA_ROOT, "grn_acknowledgements")
    os.makedirs(pdf_dir, exist_ok=True)
    pdf_path = os.path.join(pdf_dir, pdf_filename)

    try:
        pdf_bytes = generate_grn_acknowledgement_pdf(acknowledgement, output_path=pdf_path)
        acknowledgement.pdf_file_path = f"grn_acknowledgements/{pdf_filename}"
        acknowledgement.save(update_fields=["pdf_file_path", "updated_at"])
    except Exception as pdf_err:
        logger.error(
            "Failed to generate PDF for acknowledgement %s: %s",
            acknowledgement.acknowledgement_number,
            pdf_err,
        )
        pdf_bytes = None

    # 2. Recipient Resolution
    recipients = resolve_grn_acknowledgement_recipients(receipt)
    vendor_email = recipients["vendor_email"]
    vendor_name = recipients["vendor_name"]
    po = receipt.po

    recipient_summary = {
        "finance_recipients": [u.email for u in recipients["finance_users"]],
        "procurement_execs": [u.email for u in recipients["procurement_execs"]],
        "procurement_managers": [u.email for u in recipients["procurement_managers"]],
        "vendor_contact_name": (
            f"{recipients['vendor_contact'].first_name} {recipients['vendor_contact'].last_name}"
            if recipients["vendor_contact"]
            else None
        ),
        "vendor_email": vendor_email,
        "vendor_users": [u.email for u in recipients["vendor_users"]],
    }

    # 3. In-App Notification Distribution (Internal & Vendor Portal Users)
    in_app_targets = set()
    notif_type = (
        Notification.TYPE_GOODS_RECEIVED
        if outcome == GRNAcknowledgement.OUTCOME_FULL
        else Notification.TYPE_EXCEPTION_RAISED
    )

    if outcome == GRNAcknowledgement.OUTCOME_FULL:
        subject_title = f"GRN Acknowledgement (Full Acceptance): {receipt.grn_number}"
        msg_body = (
            f"Goods Receipt Note {receipt.grn_number} (PO {po.po_number}) quality inspection has passed. "
            f"All {total_acc} units accepted. Acknowledgement {acknowledgement.acknowledgement_number} issued."
        )
    elif outcome == GRNAcknowledgement.OUTCOME_PARTIAL:
        subject_title = f"GRN Acknowledgement (Partial Acceptance): {receipt.grn_number}"
        msg_body = (
            f"Goods Receipt Note {receipt.grn_number} (PO {po.po_number}) partially accepted: "
            f"{total_acc} units accepted, {total_rej} units rejected. Acknowledgement {acknowledgement.acknowledgement_number} issued."
        )
    else:
        subject_title = f"GRN Quality Rejection Notice: {receipt.grn_number}"
        msg_body = (
            f"Goods Receipt Note {receipt.grn_number} (PO {po.po_number}) inspection failed: "
            f"All {total_rej} units rejected. Rejection acknowledgement {acknowledgement.acknowledgement_number} issued."
        )

    # Finance users get notified for payment readiness or exception
    for u in recipients["finance_users"]:
        in_app_targets.add(u)

    # Procurement Executive gets notified
    for u in recipients["procurement_execs"]:
        in_app_targets.add(u)

    # Procurement Manager gets notified (always on partial/rejection, also on full)
    for u in recipients["procurement_managers"]:
        in_app_targets.add(u)

    # Vendor portal users get notified
    for u in recipients["vendor_users"]:
        in_app_targets.add(u)

    created_notifs = 0
    target_url = f"/receipts/{receipt.id}/"
    for user_target in in_app_targets:
        # Check if already notified for this exact acknowledgement version
        if not Notification.objects.filter(
            recipient=user_target,
            target_url=target_url,
            title__contains=acknowledgement.acknowledgement_number,
        ).exists():
            Notification.objects.create(
                recipient=user_target,
                notification_type=notif_type,
                title=f"{subject_title} [{acknowledgement.acknowledgement_number}]",
                message=msg_body,
                target_url=target_url,
            )
            created_notifs += 1

    acknowledgement.in_app_notified_count = created_notifs

    # 4. External Email Distribution
    line_summary_items = []
    for line in lines:
        rej = line.latest_rejection
        line_summary_items.append(
            {
                "description": line.po_line.item_description,
                "ordered_qty": line.po_line.quantity,
                "received_qty": line.quantity_received,
                "accepted_qty": line.quantity_accepted,
                "rejected_qty": line.quantity_rejected,
                "rejection_reason": rej.rejection_reason if rej else "",
            }
        )

    email_context = {
        "acknowledgement": acknowledgement,
        "receipt": receipt,
        "po": po,
        "vendor_name": vendor_name,
        "line_items": line_summary_items,
        "subject": subject_title,
    }

    if not vendor_email:
        acknowledgement.delivery_status = GRNAcknowledgement.STATUS_NO_EMAIL
        acknowledgement.delivery_error = (
            "Vendor has no configured primary contact or email address on file. "
            "In-app notifications were dispatched to portal users."
        )
        acknowledgement.email_sent_count = 0
    else:
        email_status, email_err, sent_list, failed_list = send_grn_acknowledgement_email(
            to_emails=[vendor_email],
            subject=subject_title,
            context=email_context,
            pdf_content=pdf_bytes,
            attachment_filename=pdf_filename,
        )
        acknowledgement.delivery_status = email_status
        acknowledgement.delivery_error = email_err or ""
        acknowledgement.email_sent_count = len(sent_list)
        if email_status in [
            GRNAcknowledgement.STATUS_SENT,
            GRNAcknowledgement.STATUS_PARTIAL,
        ]:
            acknowledgement.delivered_at = timezone.now()

    acknowledgement.recipient_summary = recipient_summary
    acknowledgement.save(
        update_fields=[
            "delivery_status",
            "delivery_error",
            "delivered_at",
            "recipient_summary",
            "in_app_notified_count",
            "email_sent_count",
            "updated_at",
        ]
    )

    # 5. Immutable AuditLog Entry
    action_type = (
        AuditLog.ACTION_CREATE
        if version == 1 and not force_retry
        else AuditLog.ACTION_UPDATE
    )
    AuditLog.objects.create(
        actor=triggered_by,
        action=action_type,
        target_model="GRNAcknowledgement",
        target_object_id=str(acknowledgement.id),
        previous_state=(
            {
                "acknowledgement_number": prior_ack.acknowledgement_number,
                "version": prior_ack.version,
                "outcome": prior_ack.outcome,
                "delivery_status": prior_ack.delivery_status,
            }
            if prior_ack
            else None
        ),
        new_state={
            "acknowledgement_number": acknowledgement.acknowledgement_number,
            "version": acknowledgement.version,
            "outcome": acknowledgement.outcome,
            "grn_number": receipt.grn_number,
            "po_number": po.po_number,
            "vendor": vendor_name,
            "vendor_email": vendor_email,
            "delivery_status": acknowledgement.delivery_status,
            "delivery_error": acknowledgement.delivery_error,
            "is_revision": is_revision,
            "is_retry": force_retry,
            "in_app_notified_count": acknowledgement.in_app_notified_count,
            "email_sent_count": acknowledgement.email_sent_count,
        },
    )

    return acknowledgement

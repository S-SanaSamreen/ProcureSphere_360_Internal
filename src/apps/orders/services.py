from decimal import Decimal

from django.core.exceptions import ValidationError
from django.db import transaction
from django.utils import timezone

from apps.accounts.models import Role, User
from apps.approvals import engine as approval_engine
from apps.approvals.models import ApprovalPolicy, ApprovalRequest
from apps.audit.models import AuditLog
from apps.budgets.models import SpendLedger
from apps.budgets.services import (
    budget_headroom,
    commit_po_budget_service,
    get_po_budget,
    po_budget_shortfall,
    po_net_commitment,
    post_ledger_entry_service,
)
from apps.notifications.models import Notification
from apps.organization.models import CostCenter
from apps.vendors.models import Vendor

from .models import POAmendment, POLine, PurchaseOrder


def _po_exception_reason(po: PurchaseOrder) -> str:
    """A PO priced above its requisition estimate is an exception and gets the extra route."""
    if po.requisition_id and po.subtotal > po.requisition.total_amount:
        return (
            f"PO value ${po.subtotal:,.2f} exceeds requisition estimate "
            f"${po.requisition.total_amount:,.2f}"
        )
    return ""


def _open_po_request(po: PurchaseOrder, maker, *, required=False):
    return approval_engine.open_approval_request(
        module=ApprovalPolicy.MODULE_PO,
        target=po,
        amount=po.total_amount,
        maker=maker,
        department=po.cost_center.department if po.cost_center_id else None,
        cost_center=po.cost_center,
        exception_reason=_po_exception_reason(po),
        required=required,
    )


def _notify_request_approvers(request, *, notification_type, title, message, target_url):
    """Tell whoever can decide the active step; if nobody can, make a Super Admin aware."""
    from apps.notifications.services import notify_role_users, notify_users

    approvers = approval_engine.step_approvers(request)
    if approvers:
        notify_users(
            users=approvers,
            notification_type=notification_type,
            title=title,
            message=message,
            target_url=target_url,
        )
    else:
        notify_role_users(
            role_codes=[Role.SUPER_ADMIN],
            notification_type=notification_type,
            title=f"No eligible approver: {title}",
            message=message + " No user currently holds the authority (role / limit) for this step.",
            target_url=target_url,
        )


def _assert_legacy_po_approver(user: User):
    """No PO policy requires approval: the historical rule (Procurement Manager / Super Admin)."""
    role_code = getattr(user, "role_code", None) or (user.role.code if getattr(user, "role", None) else None)
    if not (getattr(user, "is_superuser", False) or role_code in (Role.PROC_MGR, Role.SUPER_ADMIN)):
        raise ValidationError(
            "Permission Denied: Only Procurement Managers or Super Admin can approve purchase orders."
        )


def _refresh_requisition_status(po: PurchaseOrder, actor=None):
    """Moves the PO's requisition to PO_ISSUED / back to SOURCING / APPROVED as appropriate."""
    if po.requisition_id:
        from apps.requisitions.services import refresh_requisition_fulfilment_status

        refresh_requisition_fulfilment_status(po.requisition, actor=actor)


@transaction.atomic
def generate_purchase_order_service(
    *,
    vendor: Vendor,
    cost_center: CostCenter,
    line_items: list,
    created_by_user: User,
    requisition=None,
    sourcing_event=None,
    terms_and_conditions: str = "Standard HPE Enterprise Procurement Terms & Conditions",
    tax_rate: Decimal = None,
    initial_status: str = None,
) -> PurchaseOrder:
    """
    Generates a new Purchase Order from requisition/award.

    ``initial_status=None`` (default) lets the approval engine decide: if an active PO policy
    matches (amount / department / cost center) the PO is created in APPROVAL with a pending
    approval request; if no policy requires approval it is issued immediately. An explicit
    status (e.g. DRAFT) is honoured.

    When ISSUED, the requisition's reservation is converted into this PO's commitment and the
    commitment is refused if it would overspend the budget (see commit_po_budget_service).
    ``tax_rate`` defaults to settings.DEFAULT_PO_TAX_RATE and is stored on the PO.
    """
    from apps.orders.models import default_po_tax_rate

    tax_rate = default_po_tax_rate() if tax_rate is None else Decimal(str(tax_rate))
    if tax_rate < Decimal("0") or tax_rate > Decimal("1"):
        raise ValidationError("Tax rate must be a fraction between 0 and 1 (e.g. 0.10 for 10%).")

    from apps.core.services import generate_document_number

    if vendor.status != Vendor.STATUS_ACTIVE:
        raise ValidationError(
            f"Vendor '{vendor.legal_name}' is in status '{vendor.get_status_display()}' and cannot be issued Purchase Orders. Vendor must be ACTIVE."
        )

    po_number = generate_document_number(PurchaseOrder, "po_number", "PO")

    po = PurchaseOrder.objects.create(
        po_number=po_number,
        version=1,
        vendor=vendor,
        requisition=requisition,
        sourcing_event=sourcing_event,
        cost_center=cost_center,
        status=PurchaseOrder.STATUS_DRAFT if initial_status is None else initial_status,
        created_by=created_by_user,
        tax_rate=tax_rate,
        terms_and_conditions=terms_and_conditions,
        subtotal=Decimal("0.00"),
        tax_amount=Decimal("0.00"),
        total_amount=Decimal("0.00"),
    )

    subtotal = Decimal("0.00")
    for item in line_items:
        qty = Decimal(str(item["quantity"]))
        unit_price = Decimal(str(item["unit_price"]))
        if qty <= Decimal("0.00"):
            raise ValidationError(f"Quantity for line item '{item.get('item_description', '')}' must be positive.")
        if unit_price < Decimal("0.00"):
            raise ValidationError(f"Unit price for line item '{item.get('item_description', '')}' cannot be negative.")

        line = POLine.objects.create(
            po=po,
            item_description=item["item_description"],
            quantity=qty,
            unit_price=unit_price,
            unit_of_measure=item.get("unit_of_measure", "EA"),
        )
        subtotal += line.line_total

    subtotal = subtotal.quantize(Decimal("0.01"))
    tax = (subtotal * tax_rate).quantize(Decimal("0.01"))
    total = subtotal + tax

    po.subtotal = subtotal
    po.tax_amount = tax
    po.total_amount = total
    po.save(update_fields=["subtotal", "tax_amount", "total_amount", "updated_at"])

    pending_approval = None
    if initial_status is None:
        pending_approval = _open_po_request(po, created_by_user, required=False)
        initial_status = (
            PurchaseOrder.STATUS_APPROVAL if pending_approval else PurchaseOrder.STATUS_ISSUED
        )
        po.status = initial_status
        po.save(update_fields=["status", "updated_at"])
        if pending_approval:
            _notify_request_approvers(
                pending_approval,
                notification_type=Notification.TYPE_APPROVAL_REQUIRED,
                title=f"PO Approval Required: {po.po_number}",
                message=(
                    f"Purchase Order {po.po_number} (${po.total_amount:,.2f}) for "
                    f"{vendor.legal_name} requires your approval."
                ),
                target_url=f"/orders/{po.id}/",
            )

    # Commit funds (converting the requisition reservation) only if the PO is issued now
    if initial_status == PurchaseOrder.STATUS_ISSUED:
        commit_po_budget_service(po=po, user=created_by_user)

        # Notify active vendor users
        for v_user in User.objects.filter(vendor=vendor, is_active=True):
            Notification.objects.create(
                recipient=v_user,
                notification_type=Notification.TYPE_PO_ISSUED,
                title=f"New Purchase Order Issued: {po.po_number}",
                message=f"A new Purchase Order {po.po_number} (${po.total_amount:,.2f}) has been issued to {vendor.legal_name}.",
                target_url=f"/orders/{po.id}/",
            )
        # Notify requisition requester if present
        if requisition and requisition.requester:
            Notification.objects.create(
                recipient=requisition.requester,
                notification_type=Notification.TYPE_PO_ISSUED,
                title=f"PO {po.po_number} Generated for PR {requisition.pr_number}",
                message=f"Purchase Order {po.po_number} has been generated from your approved requisition {requisition.pr_number}.",
                target_url=f"/orders/{po.id}/",
            )

    AuditLog.objects.create(
        actor=created_by_user,
        action=AuditLog.ACTION_CREATE,
        target_model="PurchaseOrder",
        target_object_id=str(po.id),
        new_state={
            "po_number": po.po_number,
            "version": po.version,
            "total_amount": str(po.total_amount),
            "vendor": vendor.legal_name,
        },
    )

    if initial_status == PurchaseOrder.STATUS_ISSUED:
        _refresh_requisition_status(po, created_by_user)
        from apps.notifications.services import notify_vendor_users

        notify_vendor_users(
            vendor=vendor,
            notification_type=Notification.TYPE_PO_ISSUED,
            title=f"Purchase Order Issued: {po.po_number}",
            message=f"Purchase Order {po.po_number} for ${po.total_amount:,.2f} has been issued and requires formal acknowledgement.",
            target_url=f"/vendor/purchase-orders/{po.id}/",
        )

    return po


@transaction.atomic
def acknowledge_purchase_order_service(
    *, po: PurchaseOrder, vendor_user: User, acknowledgement_notes: str = ""
) -> PurchaseOrder:
    """
    Vendor acknowledges an issued PO. Transitions status to ACKNOWLEDGED.
    """
    if po.status != PurchaseOrder.STATUS_ISSUED:
        raise ValidationError(
            f"Cannot acknowledge PO in status '{po.status}'. Must be ISSUED."
        )

    previous_status = po.status
    po.status = PurchaseOrder.STATUS_ACKNOWLEDGED
    po.acknowledged_at = timezone.now()
    po.acknowledged_by = vendor_user
    po.acknowledgement_notes = acknowledgement_notes or po.acknowledgement_notes
    po.save(
        update_fields=[
            "status",
            "acknowledged_at",
            "acknowledged_by",
            "acknowledgement_notes",
            "updated_at",
        ]
    )

    AuditLog.objects.create(
        actor=vendor_user,
        action=AuditLog.ACTION_UPDATE,
        target_model="PurchaseOrder",
        target_object_id=str(po.id),
        previous_state={"status": previous_status},
        new_state={
            "status": po.status,
            "acknowledged_at": str(po.acknowledged_at),
            "acknowledged_by": vendor_user.email,
            "acknowledgement_notes": po.acknowledgement_notes,
        },
    )

    from apps.accounts.models import Role
    from apps.notifications.services import notify_role_users

    if po.requisition and po.requisition.requester:
        Notification.objects.create(
            recipient=po.requisition.requester,
            notification_type=Notification.TYPE_PO_ACKNOWLEDGED,
            title=f"PO {po.po_number} Acknowledged by Vendor",
            message=f"Vendor {po.vendor.legal_name} has acknowledged Purchase Order {po.po_number}.",
            target_url=f"/orders/{po.id}/",
        )

    notify_role_users(
        role_codes=[Role.PROC_EXEC, Role.PROC_MGR, Role.SUPER_ADMIN],
        notification_type=Notification.TYPE_PO_ACKNOWLEDGED,
        title=f"PO Acknowledged: {po.po_number}",
        message=f"Purchase Order {po.po_number} has been acknowledged by {vendor_user.get_full_name() or vendor_user.email} from {po.vendor.legal_name}.",
        target_url=f"/orders/{po.id}/",
    )

    return po


def _assert_amendment_fits_budget(po: PurchaseOrder, delta: Decimal):
    budget = get_po_budget(po)
    headroom = budget_headroom(budget)
    if delta > headroom and not budget.allow_overspend:
        raise ValidationError(
            f"Insufficient budget for cost center {po.cost_center.code}. "
            f"Additional commitment required: ${delta:,.2f}, "
            f"Available: ${max(headroom, Decimal('0.00')):,.2f}."
        )
    return budget


@transaction.atomic
def request_po_amendment_service(
    *,
    po: PurchaseOrder,
    reason: str,
    updated_line_items: list,
    requested_by_user: User,
    tax_rate: Decimal = None,
) -> POAmendment:
    """
    Initiates a formal Purchase Order Change Order / Amendment request.
    - Validates vendor status and caller authorization.
    - Validates line item quantities against received goods.
    - Verifies cost-center budget availability if total amount increases.
    - Captures immutable JSON snapshot of prior PO state.
    - Sets PO status to AMENDMENT_PENDING.
    - Creates POAmendment in PENDING state awaiting managerial approval.
    """
    if not reason or not str(reason).strip():
        raise ValidationError("Amendment justification reason is required.")

    tax_rate = po.tax_rate if tax_rate is None else Decimal(str(tax_rate))

    if po.status in (PurchaseOrder.STATUS_CANCELLED, PurchaseOrder.STATUS_DRAFT, PurchaseOrder.STATUS_COMPLETED):
        raise ValidationError(
            f"Purchase orders in '{po.get_status_display()}' status cannot be amended."
        )

    if po.vendor.status != Vendor.STATUS_ACTIVE:
        raise ValidationError(
            f"Vendor '{po.vendor.legal_name}' is not active and PO cannot be amended."
        )

    if po.status == PurchaseOrder.STATUS_AMENDMENT_PENDING:
        raise ValidationError(
            f"Purchase Order {po.po_number} already has an amendment pending approval."
        )

    role_code = getattr(requested_by_user, "role_code", None) or (
        requested_by_user.role.code
        if getattr(requested_by_user, "role", None)
        else None
    )
    if not (
        getattr(requested_by_user, "is_superuser", False)
        or role_code in (Role.PROC_EXEC, Role.PROC_MGR, Role.SUPER_ADMIN)
    ):
        raise ValidationError(
            "Permission Denied: Only procurement roles or Super Admin can amend purchase orders."
        )

    if po.amendments.filter(status=POAmendment.STATUS_PENDING).exists():
        raise ValidationError(
            "A pending amendment already exists for this purchase order."
        )

    # 1. Take JSON snapshot of previous version
    snapshot = {
        "po_number": po.po_number,
        "version": po.version,
        "subtotal": str(po.subtotal),
        "tax_amount": str(po.tax_amount),
        "total_amount": str(po.total_amount),
        "status": po.status,
        "lines": [
            {
                "id": str(line.id),
                "description": line.item_description,
                "quantity": str(line.quantity),
                "quantity_received": str(line.quantity_received),
                "unit_price": str(line.unit_price),
                "line_total": str(line.line_total),
                "unit_of_measure": line.unit_of_measure,
            }
            for line in po.lines.all()
        ],
    }

    # 2. Validate proposed line items & calculate proposed totals
    existing_lines = list(po.lines.all().order_by("id"))
    matched_line_ids = set()
    subtotal = Decimal("0.00")
    validated_lines = []

    for idx, item in enumerate(updated_line_items):
        item_id = item.get("id") or item.get("line_id")
        matched_line = None
        if item_id:
            matched_line = next(
                (line for line in existing_lines if str(line.id) == str(item_id)), None
            )
        if not matched_line and idx < len(existing_lines):
            candidate = existing_lines[idx]
            if candidate.id not in matched_line_ids:
                matched_line = candidate
        if not matched_line:
            candidate = next(
                (
                    line
                    for line in existing_lines
                    if line.item_description == item.get("item_description")
                    and line.id not in matched_line_ids
                ),
                None,
            )
            if candidate:
                matched_line = candidate

        qty = Decimal(str(item["quantity"]))
        unit_price = Decimal(str(item["unit_price"]))
        desc = item.get("item_description", "").strip()
        uom = item.get("unit_of_measure", "EA")

        if matched_line:
            matched_line_ids.add(matched_line.id)
            if qty < matched_line.quantity_received:
                raise ValidationError(
                    f"Amended quantity ({qty}) for '{matched_line.item_description}' cannot be less than "
                    f"quantity already received ({matched_line.quantity_received})."
                )
            line_total = qty * unit_price
            subtotal += line_total
            validated_lines.append(
                {
                    "id": str(matched_line.id),
                    "item_description": desc or matched_line.item_description,
                    "quantity": str(qty),
                    "unit_price": str(unit_price),
                    "unit_of_measure": uom,
                    "line_total": str(line_total),
                }
            )
        else:
            line_total = qty * unit_price
            subtotal += line_total
            validated_lines.append(
                {
                    "id": None,
                    "item_description": desc or f"Line Item {idx + 1}",
                    "quantity": str(qty),
                    "unit_price": str(unit_price),
                    "unit_of_measure": uom,
                    "line_total": str(line_total),
                }
            )

    # Guard against removing lines referenced by goods receipts
    for line in existing_lines:
        if line.id not in matched_line_ids:
            has_receipts = (
                line.quantity_received > Decimal("0.00") or line.receipt_lines.exists()
            )
            if has_receipts:
                raise ValidationError(
                    f"Cannot delete line '{line.item_description}' referenced by goods receipts."
                )

    subtotal = subtotal.quantize(Decimal("0.01"))
    tax = (subtotal * tax_rate).quantize(Decimal("0.01"))
    proposed_total = subtotal + tax
    old_total = Decimal(str(snapshot["total_amount"]))
    delta = proposed_total - old_total

    # Verify Budget availability if delta > 0 (re-checked again at approval time)
    if delta > Decimal("0.00"):
        _assert_amendment_fits_budget(po, delta)

    amendment_count = po.amendments.count() + 1
    amendment = POAmendment.objects.create(
        po=po,
        amendment_number=amendment_count,
        reason=reason.strip(),
        previous_version_snapshot=snapshot,
        requested_by=requested_by_user,
        status=POAmendment.STATUS_PENDING,
        delta_amount=delta,
        proposed_changes={
            "lines": validated_lines,
            "subtotal": str(subtotal),
            "tax_amount": str(tax),
            "total_amount": str(proposed_total),
        },
    )

    # Routed by policy (cost center > department > global, amount tiers); fail closed - an
    # amendment that no policy covers is refused and nothing above is persisted.
    approval = approval_engine.open_approval_request(
        module=ApprovalPolicy.MODULE_PO_AMENDMENT,
        target=amendment,
        amount=abs(delta),
        maker=requested_by_user,
        department=po.cost_center.department if po.cost_center_id else None,
        cost_center=po.cost_center,
        required=True,
    )

    po.status = PurchaseOrder.STATUS_AMENDMENT_PENDING
    po.save(update_fields=["status", "updated_at"])

    AuditLog.objects.create(
        actor=requested_by_user,
        action=AuditLog.ACTION_UPDATE,
        target_model="PurchaseOrder",
        target_object_id=str(po.id),
        previous_state={
            "status": snapshot["status"],
            "total_amount": snapshot["total_amount"],
        },
        new_state={
            "status": po.status,
            "amendment_number": amendment.amendment_number,
            "delta_amount": str(delta),
            "proposed_total": str(proposed_total),
        },
    )

    _notify_request_approvers(
        approval,
        notification_type=Notification.TYPE_PO_AMENDMENT,
        title=f"PO Amendment #{amendment.amendment_number} Pending Approval: {po.po_number}",
        message=(
            f"PO {po.po_number} amendment #{amendment.amendment_number} "
            f"(Delta: ${delta:+,.2f}) submitted by {requested_by_user.get_full_name() or requested_by_user.email} "
            "requires formal approval."
        ),
        target_url=f"/orders/{po.id}/",
    )

    return amendment


@transaction.atomic
def approve_po_amendment_service(
    *,
    amendment: POAmendment,
    approver: User,
    comments: str = "",
) -> PurchaseOrder:
    """
    Procurement Manager governance action to approve a pending PO Amendment:
    - Enforces Separation of Duties: Requester cannot approve their own amendment.
    - Mutates POLine items to proposed state, preserving FK references and IDs.
    - Increments PO version counter (e.g. V1 -> V2).
    - Updates Budget committed_amount by delta difference and logs to SpendLedger.
    - Transitions PO to ISSUED and resets vendor acknowledgement to mandate re-acknowledgement.
    - Emits formal notification to vendor to re-acknowledge amended order.
    """
    if amendment.status != POAmendment.STATUS_PENDING:
        raise ValidationError(
            f"Amendment #{amendment.amendment_number} is in '{amendment.status}' state and cannot be approved."
        )

    # Authorisation, separation of duties, approval limits and multi-step chains are all enforced
    # by the approval engine, against the policy version the amendment was routed under.
    request = approval_engine.get_pending_request(ApprovalPolicy.MODULE_PO_AMENDMENT, amendment)
    if request is None:  # amendment requested before the engine existed
        request = approval_engine.open_approval_request(
            module=ApprovalPolicy.MODULE_PO_AMENDMENT,
            target=amendment,
            amount=abs(amendment.delta_amount),
            maker=amendment.requested_by,
            department=amendment.po.cost_center.department,
            cost_center=amendment.po.cost_center,
            required=True,
            record_submission=False,
        )
    is_final_step = request.current_step + 1 >= len(request.steps)
    request = approval_engine.approve_step(
        request=request,
        actor=approver,
        comments=comments,
        states=("AMENDMENT_PENDING", "AMENDMENT_APPROVED" if is_final_step else "AMENDMENT_PENDING"),
    )
    if request.status != ApprovalRequest.STATUS_APPROVED:
        step = request.active_step
        _notify_request_approvers(
            request,
            notification_type=Notification.TYPE_PO_AMENDMENT,
            title=f"PO Amendment #{amendment.amendment_number} needs your approval (Step {step['step_number']})",
            message=f"PO {amendment.po.po_number} amendment #{amendment.amendment_number} awaits {step['role_name']} approval.",
            target_url=f"/orders/{amendment.po_id}/",
        )
        return amendment.po  # partially approved: nothing is applied until the last step

    po = amendment.po
    proposed_changes = amendment.proposed_changes or {}
    proposed_lines = proposed_changes.get("lines", [])

    existing_lines = list(po.lines.all().order_by("id"))
    matched_line_ids = set()
    subtotal = Decimal("0.00")

    for idx, item in enumerate(proposed_lines):
        item_id = item.get("id") or item.get("line_id")
        matched_line = None
        if item_id:
            matched_line = next(
                (line for line in existing_lines if str(line.id) == str(item_id)), None
            )
        if not matched_line and idx < len(existing_lines):
            candidate = existing_lines[idx]
            if candidate.id not in matched_line_ids:
                matched_line = candidate
        if not matched_line:
            candidate = next(
                (
                    line
                    for line in existing_lines
                    if line.item_description == item.get("item_description")
                    and line.id not in matched_line_ids
                ),
                None,
            )
            if candidate:
                matched_line = candidate

        qty = Decimal(str(item["quantity"]))
        unit_price = Decimal(str(item["unit_price"]))
        desc = item.get("item_description", "").strip()
        uom = item.get("unit_of_measure", "EA")

        if matched_line:
            matched_line_ids.add(matched_line.id)
            if qty < matched_line.quantity_received:
                raise ValidationError(
                    f"Amended quantity ({qty}) for '{matched_line.item_description}' cannot be less than "
                    f"quantity already received ({matched_line.quantity_received})."
                )
            if desc:
                matched_line.item_description = desc
            matched_line.quantity = qty
            matched_line.unit_price = unit_price
            matched_line.unit_of_measure = uom
            matched_line.save()
            subtotal += matched_line.line_total
        else:
            new_line = POLine.objects.create(
                po=po,
                item_description=desc or f"Line Item {idx + 1}",
                quantity=qty,
                unit_price=unit_price,
                unit_of_measure=uom,
            )
            subtotal += new_line.line_total

    # Guard against removing lines referenced by receipts
    for line in existing_lines:
        if line.id not in matched_line_ids:
            has_receipts = (
                line.quantity_received > Decimal("0.00") or line.receipt_lines.exists()
            )
            if has_receipts:
                raise ValidationError(
                    f"Cannot delete line '{line.item_description}' referenced by goods receipts."
                )
            line.delete()

    subtotal_val = Decimal(str(proposed_changes.get("subtotal", subtotal)))
    tax_val = Decimal(
        str(proposed_changes.get("tax_amount", subtotal * po.tax_rate))
    )
    total_val = Decimal(
        str(proposed_changes.get("total_amount", subtotal_val + tax_val))
    )
    diff = amendment.delta_amount

    # Update PO version, values, and reset vendor acknowledgement
    po.version += 1
    po.subtotal = subtotal_val
    po.tax_amount = tax_val
    po.total_amount = total_val
    po.status = PurchaseOrder.STATUS_ISSUED
    po.acknowledged_at = None
    po.acknowledged_by = None
    po.acknowledgement_notes = ""
    po.save(
        update_fields=[
            "version",
            "subtotal",
            "tax_amount",
            "total_amount",
            "status",
            "acknowledged_at",
            "acknowledged_by",
            "acknowledgement_notes",
            "updated_at",
        ]
    )

    # Adjust Budget commitment with delta difference (re-validated now: the budget may have
    # been consumed since the amendment was requested)
    if diff != Decimal("0.00"):
        budget = (
            _assert_amendment_fits_budget(po, diff)
            if diff > Decimal("0.00")
            else get_po_budget(po)
        )
        post_ledger_entry_service(
            budget=budget,
            entry_type=SpendLedger.ENTRY_COMMITMENT,
            amount=diff,
            reference_number=f"{po.po_number}-AMD{amendment.amendment_number}",
            description=f"PO Amendment #{amendment.amendment_number} adjustment for {po.vendor.legal_name}",
        )

    # Mark amendment as approved
    amendment.status = POAmendment.STATUS_APPROVED
    amendment.approved_by = approver
    amendment.approved_at = timezone.now()
    amendment.save(update_fields=["status", "approved_by", "approved_at", "updated_at"])

    AuditLog.objects.create(
        actor=approver,
        action=AuditLog.ACTION_UPDATE,
        target_model="PurchaseOrder",
        target_object_id=str(po.id),
        previous_state={
            "version": amendment.previous_version_snapshot.get("version"),
            "total_amount": amendment.previous_version_snapshot.get("total_amount"),
            "status": PurchaseOrder.STATUS_AMENDMENT_PENDING,
        },
        new_state={
            "version": po.version,
            "total_amount": str(po.total_amount),
            "status": po.status,
            "amendment_number": amendment.amendment_number,
            "approved_by": approver.email,
        },
    )

    # Require vendor re-acknowledgement and notify vendor users
    from apps.notifications.services import notify_vendor_users

    notify_vendor_users(
        vendor=po.vendor,
        notification_type=Notification.TYPE_PO_AMENDMENT,
        title=f"Purchase Order Amended to V{po.version}: {po.po_number}",
        message=(
            f"Purchase Order {po.po_number} has been amended to Version {po.version} "
            f"(${po.total_amount:,.2f}). Mandatory re-acknowledgement is required."
        ),
        target_url=f"/vendor/purchase-orders/{po.id}/",
    )

    for v_user in User.objects.filter(vendor=po.vendor, is_active=True):
        Notification.objects.create(
            recipient=v_user,
            notification_type=Notification.TYPE_PO_AMENDMENT,
            title=f"Purchase Order Amendment: {po.po_number} V{po.version}",
            message=(
                f"Purchase Order {po.po_number} has been updated to Version {po.version} "
                f"(Reason: {amendment.reason}). Re-acknowledgement required."
            ),
            target_url=f"/orders/{po.id}/",
        )

    if po.requisition and po.requisition.requester:
        Notification.objects.create(
            recipient=po.requisition.requester,
            notification_type=Notification.TYPE_PO_AMENDMENT,
            title=f"PO {po.po_number} Amendment Approved (V{po.version})",
            message=f"Purchase Order {po.po_number} amendment #{amendment.amendment_number} was approved by {approver.get_full_name() or approver.email}.",
            target_url=f"/orders/{po.id}/",
        )

    if amendment.requested_by and amendment.requested_by != approver:
        Notification.objects.create(
            recipient=amendment.requested_by,
            notification_type=Notification.TYPE_PO_AMENDMENT,
            title=f"PO Amendment #{amendment.amendment_number} Approved",
            message=f"Your amendment request for PO {po.po_number} was approved by {approver.get_full_name() or approver.email}.",
            target_url=f"/orders/{po.id}/",
        )

    return po


@transaction.atomic
def reject_po_amendment_service(
    *,
    amendment: POAmendment,
    rejector: User,
    reason: str,
) -> PurchaseOrder:
    """
    Procurement Manager governance action to reject a pending PO Amendment:
    - Verifies rejector role authorization.
    - Enforces mandatory rejection reason.
    - Reverts PO status to previous operational state.
    - Marks amendment as REJECTED with rejection note.
    - Notifies requester of rejection reason.
    """
    if amendment.status != POAmendment.STATUS_PENDING:
        raise ValidationError(
            f"Amendment #{amendment.amendment_number} is in '{amendment.status}' state and cannot be rejected."
        )

    if not reason or not str(reason).strip():
        raise ValidationError("Rejection justification reason is mandatory.")

    request = approval_engine.get_pending_request(ApprovalPolicy.MODULE_PO_AMENDMENT, amendment)
    if request is None:  # amendment requested before the engine existed
        request = approval_engine.open_approval_request(
            module=ApprovalPolicy.MODULE_PO_AMENDMENT,
            target=amendment,
            amount=abs(amendment.delta_amount),
            maker=amendment.requested_by,
            department=amendment.po.cost_center.department,
            cost_center=amendment.po.cost_center,
            required=True,
            record_submission=False,
        )
    approval_engine.reject_request(
        request=request,
        actor=rejector,
        comments=reason,
        states=("AMENDMENT_PENDING", "AMENDMENT_REJECTED"),
    )

    po = amendment.po
    prev_status = amendment.previous_version_snapshot.get(
        "status", PurchaseOrder.STATUS_ACKNOWLEDGED
    )
    if prev_status == PurchaseOrder.STATUS_AMENDMENT_PENDING:
        prev_status = PurchaseOrder.STATUS_ACKNOWLEDGED

    po.status = prev_status
    po.save(update_fields=["status", "updated_at"])

    amendment.status = POAmendment.STATUS_REJECTED
    amendment.rejection_reason = reason.strip()
    amendment.approved_by = rejector
    amendment.approved_at = timezone.now()
    amendment.save(
        update_fields=[
            "status",
            "rejection_reason",
            "approved_by",
            "approved_at",
            "updated_at",
        ]
    )

    AuditLog.objects.create(
        actor=rejector,
        action=AuditLog.ACTION_UPDATE,
        target_model="PurchaseOrder",
        target_object_id=str(po.id),
        previous_state={"status": PurchaseOrder.STATUS_AMENDMENT_PENDING},
        new_state={
            "status": po.status,
            "amendment_number": amendment.amendment_number,
            "amendment_status": POAmendment.STATUS_REJECTED,
            "rejection_reason": amendment.rejection_reason,
            "rejected_by": rejector.email,
        },
    )

    if amendment.requested_by:
        Notification.objects.create(
            recipient=amendment.requested_by,
            notification_type=Notification.TYPE_PO_AMENDMENT,
            title=f"PO Amendment #{amendment.amendment_number} Rejected: {po.po_number}",
            message=(
                f"Your amendment request for PO {po.po_number} was rejected by "
                f"{rejector.get_full_name() or rejector.email}. Reason: {reason}"
            ),
            target_url=f"/orders/{po.id}/",
        )

    return po


@transaction.atomic
def amend_purchase_order_service(
    *,
    po: PurchaseOrder,
    reason: str,
    updated_line_items: list,
    requested_by_user: User,
    tax_rate: Decimal = None,
    auto_approve: bool = True,
    approver: User = None,
) -> POAmendment:
    """
    Submits a PO Amendment request, optionally approving it if auto_approve=True.
    When auto_approve is False, PO transitions to AMENDMENT_PENDING and awaits managerial review.
    In the production web application and REST APIs, request_po_amendment_service is called directly,
    ensuring PO transitions to AMENDMENT_PENDING and awaits managerial review.
    """
    amendment = request_po_amendment_service(
        po=po,
        reason=reason,
        updated_line_items=updated_line_items,
        requested_by_user=requested_by_user,
        tax_rate=tax_rate,
    )

    if auto_approve:
        eff_approver = approver
        if not eff_approver:
            if getattr(requested_by_user, "is_superuser", False):
                eff_approver = requested_by_user
            else:
                eff_approver = (
                    User.objects.filter(role__code=Role.PROC_MGR, is_active=True)
                    .exclude(id=requested_by_user.id)
                    .first()
                )
                if not eff_approver:
                    eff_approver = User.objects.create_user(
                        email=f"system.manager.{po.id.hex[:6]}@procuresphere.com",
                        role=Role.objects.filter(code=Role.PROC_MGR).first()
                        or requested_by_user.role,
                        first_name="System",
                        last_name="Manager",
                    )
        approve_po_amendment_service(amendment=amendment, approver=eff_approver)
        po.refresh_from_db()

    return amendment


@transaction.atomic
def cancel_purchase_order_governance_service(
    *, po: PurchaseOrder, actor: User, reason: str
) -> PurchaseOrder:
    """
    Procurement Manager governance action to cancel a Purchase Order.
    - Transitions PO status to CANCELLED.
    - Releases unfulfilled financial commitment back to the budget.
    - Creates a reversal SpendLedger entry.
    - Records an append-only AuditLog entry.
    """
    if not reason or not str(reason).strip():
        raise ValidationError("Cancellation reason / justification is mandatory.")

    if po.status == PurchaseOrder.STATUS_CANCELLED:
        raise ValidationError(f"Purchase Order {po.po_number} is already cancelled.")

    if po.status == PurchaseOrder.STATUS_COMPLETED:
        raise ValidationError(
            f"Cannot cancel completed/fulfilled Purchase Order {po.po_number}."
        )

    previous_status = po.status
    po.status = PurchaseOrder.STATUS_CANCELLED
    po.save(update_fields=["status", "updated_at"])
    _refresh_requisition_status(po, actor)

    # A cancelled PO must leave nothing waiting for approval (itself, a pending amendment, or a
    # budget exception for it).
    approval_engine.cancel_pending_request(ApprovalPolicy.MODULE_PO, po, actor=actor, reason="PO cancelled")
    approval_engine.cancel_pending_request(
        ApprovalPolicy.MODULE_BUDGET_EXCEPTION, po, actor=actor, reason="PO cancelled"
    )
    for pending in po.amendments.filter(status=POAmendment.STATUS_PENDING):
        approval_engine.cancel_pending_request(
            ApprovalPolicy.MODULE_PO_AMENDMENT, pending, actor=actor, reason="PO cancelled"
        )
        pending.status = POAmendment.STATUS_REJECTED
        pending.rejection_reason = "Purchase order was cancelled."
        pending.save(update_fields=["status", "rejection_reason", "updated_at"])

    # Calculate unfulfilled commitment amount to release
    total_received_value = Decimal("0.00")
    for line in po.lines.all():
        if line.quantity_received:
            total_received_value += line.quantity_received * line.unit_price

    unfulfilled_commitment = max(
        Decimal("0.00"), po.total_amount - total_received_value
    )

    # Only release what this PO actually committed (a never-issued DRAFT/APPROVAL PO has none)
    budget = get_po_budget(po, required=False)
    if budget and unfulfilled_commitment > Decimal("0.00"):
        unfulfilled_commitment = min(unfulfilled_commitment, po_net_commitment(po))
        post_ledger_entry_service(
            budget=budget,
            entry_type=SpendLedger.ENTRY_COMMITMENT,
            amount=-unfulfilled_commitment,
            reference_number=f"{po.po_number}-CANCEL",
            description=f"Commitment release on PO Cancellation: {po.po_number} ({reason})",
        )
    elif not budget:
        unfulfilled_commitment = Decimal("0.00")

    AuditLog.objects.create(
        actor=actor,
        action=AuditLog.ACTION_REJECT,
        target_model="PurchaseOrder",
        target_object_id=str(po.id),
        previous_state={
            "status": previous_status,
            "total_amount": str(po.total_amount),
        },
        new_state={
            "status": po.status,
            "reason": reason,
            "released_commitment": str(unfulfilled_commitment),
        },
    )

    return po


@transaction.atomic
def submit_purchase_order_for_approval_service(
    *, po: PurchaseOrder, actor: User
) -> PurchaseOrder:
    """
    Submits a draft PO for managerial review and approval.
    Transitions DRAFT -> APPROVAL.
    """
    if po.status != PurchaseOrder.STATUS_DRAFT:
        raise ValidationError(
            f"Cannot submit PO in status '{po.status}' for approval. Must be DRAFT."
        )

    previous_status = po.status
    po.status = PurchaseOrder.STATUS_APPROVAL
    po.save(update_fields=["status", "updated_at"])
    pending_approval = _open_po_request(po, actor, required=False)
    if pending_approval:
        _notify_request_approvers(
            pending_approval,
            notification_type=Notification.TYPE_APPROVAL_REQUIRED,
            title=f"PO Approval Required: {po.po_number}",
            message=f"Purchase Order {po.po_number} (${po.total_amount:,.2f}) requires your approval.",
            target_url=f"/orders/{po.id}/",
        )

    AuditLog.objects.create(
        actor=actor,
        action=AuditLog.ACTION_SUBMIT,
        target_model="PurchaseOrder",
        target_object_id=str(po.id),
        previous_state={"status": previous_status},
        new_state={"status": po.status},
    )
    return po


@transaction.atomic
def approve_purchase_order_service(
    *, po: PurchaseOrder, approver: User, comments: str = ""
) -> PurchaseOrder:
    """
    Approves a PO and transitions to ISSUED state.
    Commits funds against the designated Cost Center budget.
    """
    if po.status not in [PurchaseOrder.STATUS_DRAFT, PurchaseOrder.STATUS_APPROVAL]:
        raise ValidationError(
            f"Cannot approve PO in status '{po.status}'. Must be DRAFT or APPROVAL."
        )

    if po.vendor.status == Vendor.STATUS_SUSPENDED:
        raise ValidationError(
            f"Vendor '{po.vendor.legal_name}' is suspended and cannot be issued POs."
        )

    # Routed by the approval engine when a PO policy applies (a PO approved straight from DRAFT
    # is routed now); otherwise the historical rule applies (Procurement Manager / Super Admin).
    request = approval_engine.get_pending_request(ApprovalPolicy.MODULE_PO, po)
    if request is None:
        request = _open_po_request(po, po.created_by, required=False)
    if request is not None:
        is_final_step = request.current_step + 1 >= len(request.steps)
        request = approval_engine.approve_step(
            request=request,
            actor=approver,
            comments=comments,
            states=(po.status, PurchaseOrder.STATUS_ISSUED if is_final_step else PurchaseOrder.STATUS_APPROVAL),
        )
        if request.status != ApprovalRequest.STATUS_APPROVED:
            if po.status != PurchaseOrder.STATUS_APPROVAL:
                po.status = PurchaseOrder.STATUS_APPROVAL
                po.save(update_fields=["status", "updated_at"])
            step = request.active_step
            _notify_request_approvers(
                request,
                notification_type=Notification.TYPE_APPROVAL_REQUIRED,
                title=f"PO Approval Required: {po.po_number} (Step {step['step_number']})",
                message=f"Purchase Order {po.po_number} awaits {step['role_name']} approval.",
                target_url=f"/orders/{po.id}/",
            )
            return po  # partially approved: the PO is issued only after the last step
    else:
        _assert_legacy_po_approver(approver)

    # Commit funds first: converts the requisition reservation and refuses an overspend (unless
    # an approved budget exception covers it), in which case the whole approval rolls back and
    # the PO stays unapproved.
    commit_po_budget_service(po=po, user=approver)

    previous_status = po.status
    po.status = PurchaseOrder.STATUS_ISSUED
    po.save(update_fields=["status", "updated_at"])
    _refresh_requisition_status(po, approver)

    AuditLog.objects.create(
        actor=approver,
        action=AuditLog.ACTION_APPROVE,
        target_model="PurchaseOrder",
        target_object_id=str(po.id),
        previous_state={"status": previous_status},
        new_state={"status": po.status, "comments": comments},
    )
    return po


@transaction.atomic
def reject_purchase_order_service(
    *, po: PurchaseOrder, approver: User, reason: str
) -> PurchaseOrder:
    """
    Rejects a PO in approval, returning it to DRAFT state.
    """
    if not reason:
        raise ValidationError("Rejection reason is required.")

    if po.status != PurchaseOrder.STATUS_APPROVAL:
        raise ValidationError(
            f"Cannot reject PO in status '{po.status}'. Must be APPROVAL."
        )

    request = approval_engine.get_pending_request(ApprovalPolicy.MODULE_PO, po)
    if request is not None:
        approval_engine.reject_request(
            request=request, actor=approver, comments=reason, states=(po.status, PurchaseOrder.STATUS_DRAFT)
        )
    else:
        _assert_legacy_po_approver(approver)

    previous_status = po.status
    po.status = PurchaseOrder.STATUS_DRAFT
    po.save(update_fields=["status", "updated_at"])

    AuditLog.objects.create(
        actor=approver,
        action=AuditLog.ACTION_REJECT,
        target_model="PurchaseOrder",
        target_object_id=str(po.id),
        previous_state={"status": previous_status},
        new_state={"status": po.status, "rejection_reason": reason},
    )
    return po


@transaction.atomic
def request_po_budget_exception_service(
    *, po: PurchaseOrder, requested_by: User, reason: str
) -> ApprovalRequest:
    """
    Asks for permission to commit a PO that would overspend its budget. The request is routed
    through the BUDGET_EXCEPTION policy (amount = the shortfall). Once approved it is a one-time
    permission, consumed when the PO's commitment is posted (approve / issue the PO afterwards).
    """
    reason = (reason or "").strip()
    if len(reason) < 10:
        raise ValidationError("A justification of at least 10 characters is required.")
    if po.status not in (PurchaseOrder.STATUS_DRAFT, PurchaseOrder.STATUS_APPROVAL):
        raise ValidationError(
            f"A budget exception can only be requested for a PO that has not been issued (status {po.status})."
        )
    shortfall = po_budget_shortfall(po)
    if shortfall <= Decimal("0.00"):
        raise ValidationError("This PO fits within its budget; no exception is needed.")
    request = approval_engine.open_approval_request(
        module=ApprovalPolicy.MODULE_BUDGET_EXCEPTION,
        target=po,
        amount=shortfall,
        maker=requested_by,
        department=po.cost_center.department,
        cost_center=po.cost_center,
        exception_reason=reason,
        required=True,
    )
    _notify_request_approvers(
        request,
        notification_type=Notification.TYPE_APPROVAL_REQUIRED,
        title=f"Budget Exception Requested: {po.po_number}",
        message=f"PO {po.po_number} exceeds its budget by ${shortfall:,.2f}. Reason: {reason}",
        target_url=f"/orders/{po.id}/",
    )
    return request


@transaction.atomic
def decide_po_budget_exception_service(
    *, po: PurchaseOrder, actor: User, approve: bool, comments: str = ""
) -> ApprovalRequest:
    """Approve (next step of) or reject the pending budget exception for a PO."""
    request = approval_engine.get_pending_request(ApprovalPolicy.MODULE_BUDGET_EXCEPTION, po)
    if request is None:
        raise ValidationError(f"There is no pending budget exception for PO {po.po_number}.")
    if not approve:
        return approval_engine.reject_request(request=request, actor=actor, comments=comments)
    request = approval_engine.approve_step(request=request, actor=actor, comments=comments)
    if request.status != ApprovalRequest.STATUS_APPROVED:
        step = request.active_step
        _notify_request_approvers(
            request,
            notification_type=Notification.TYPE_APPROVAL_REQUIRED,
            title=f"Budget Exception Approval Required: {po.po_number} (Step {step['step_number']})",
            message=f"PO {po.po_number} budget exception awaits {step['role_name']} approval.",
            target_url=f"/orders/{po.id}/",
        )
    return request

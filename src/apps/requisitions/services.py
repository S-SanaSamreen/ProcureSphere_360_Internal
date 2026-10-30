from decimal import Decimal

from django.core.exceptions import ValidationError
from django.db import transaction
from django.utils import timezone

from apps.accounts.models import Role, User
from apps.approvals import engine
from apps.approvals.models import ApprovalAction, ApprovalPolicy, ApprovalRequest
from apps.approvals.services import record_approval_action_service
from apps.audit.models import AuditLog
from apps.budgets.services import (
    BudgetOverrunException,
    check_and_reserve_budget_service,
    release_budget_reservation_service,
)
from apps.core.validators import validate_pr_attachment
from apps.notifications.models import Notification
from apps.organization.models import CostCenter, Department

from .models import PRAttachment, PRLine, PurchaseRequisition

FULFILMENT_STATUSES = (
    PurchaseRequisition.STATUS_APPROVED,
    PurchaseRequisition.STATUS_SOURCING,
    PurchaseRequisition.STATUS_PO_ISSUED,
)


@transaction.atomic
def refresh_requisition_fulfilment_status(requisition, *, actor=None):
    """
    Keeps an approved requisition's lifecycle state (PRD 4: ... APPROVED -> SOURCING / PO) in
    line with what actually exists, so it can never drift from the underlying documents:

      * any issued PO (not DRAFT / in approval / CANCELLED)  -> PO_ISSUED
      * else any non-cancelled sourcing event                -> SOURCING
      * else                                                 -> APPROVED

    Only requisitions already past approval are touched (a DRAFT, rejected or cancelled PR is
    never resurrected). Every change is written to the audit log.
    """
    from apps.orders.models import PurchaseOrder
    from apps.sourcing.models import SourcingEvent

    requisition = PurchaseRequisition.objects.select_for_update().get(pk=requisition.pk)
    if requisition.status not in FULFILMENT_STATUSES:
        return requisition

    has_issued_po = (
        PurchaseOrder.objects.filter(requisition=requisition)
        .exclude(
            status__in=[
                PurchaseOrder.STATUS_DRAFT,
                PurchaseOrder.STATUS_APPROVAL,
                PurchaseOrder.STATUS_CANCELLED,
            ]
        )
        .exists()
    )
    has_live_event = (
        SourcingEvent.objects.filter(requisition=requisition)
        .exclude(status=SourcingEvent.STATUS_CANCELLED)
        .exists()
    )
    if has_issued_po:
        target = PurchaseRequisition.STATUS_PO_ISSUED
    elif has_live_event:
        target = PurchaseRequisition.STATUS_SOURCING
    else:
        target = PurchaseRequisition.STATUS_APPROVED

    if target != requisition.status:
        previous = requisition.status
        requisition.status = target
        requisition.save(update_fields=["status", "updated_at"])
        AuditLog.objects.create(
            actor=actor if getattr(actor, "is_authenticated", False) else None,
            action=AuditLog.ACTION_UPDATE,
            target_model="PurchaseRequisition",
            target_object_id=str(requisition.id),
            previous_state={"status": previous},
            new_state={"status": target, "reason": "fulfilment status follows POs / sourcing events"},
        )
    return requisition


@transaction.atomic
def create_purchase_requisition_service(
    *,
    title: str,
    justification: str,
    requester: User,
    department: Department,
    cost_center: CostCenter,
    requested_delivery_date,
    line_items: list,
    attachments: list = None,
) -> PurchaseRequisition:
    """
    Creates a new PurchaseRequisition in DRAFT status with atomic document number generation.
    """
    if not line_items:
        raise ValidationError("Purchase Requisition must contain at least one line item.")

    if getattr(requester, "cost_center", None) and requester.cost_center != cost_center:
        raise ValidationError(
            f"Requester '{requester.email}' is only authorized to create requisitions for Cost Center '{requester.cost_center.code}'."
        )

    if cost_center.department_id != department.id:
        raise ValidationError(
            f"Cost Center '{cost_center.code}' ({cost_center.name}) does not belong to Department '{department.name}'."
        )

    if requested_delivery_date:
        from datetime import date, datetime

        if isinstance(requested_delivery_date, str):
            try:
                deliv_obj = datetime.strptime(
                    str(requested_delivery_date).split("T")[0], "%Y-%m-%d"
                ).date()
            except ValueError:
                deliv_obj = None
        elif isinstance(requested_delivery_date, (datetime, date)):
            deliv_obj = (
                requested_delivery_date
                if isinstance(requested_delivery_date, date)
                else requested_delivery_date.date()
            )
        else:
            deliv_obj = None

        if deliv_obj and deliv_obj < timezone.now().date():
            raise ValidationError("Requested delivery date cannot be in the past.")

    from apps.core.services import generate_document_number

    pr_number = generate_document_number(PurchaseRequisition, "pr_number", "PR")

    pr = PurchaseRequisition.objects.create(
        pr_number=pr_number,
        title=title,
        justification=justification,
        requester=requester,
        department=department,
        cost_center=cost_center,
        status=PurchaseRequisition.STATUS_DRAFT,
        requested_delivery_date=requested_delivery_date,
        total_amount=Decimal("0.00"),
    )

    total = Decimal("0.00")
    for item in line_items:
        qty = Decimal(str(item["quantity"]))
        unit_price = Decimal(str(item["estimated_unit_price"]))
        if qty <= Decimal("0.00"):
            raise ValidationError(
                f"Quantity for line item '{item.get('item_description', '')}' must be greater than zero."
            )
        if qty % 1 != 0:
            raise ValidationError(
                f"Quantity for line item '{item.get('item_description', '')}' must be a whole number (no decimal points allowed)."
            )
        if unit_price < Decimal("0.00"):
            raise ValidationError(
                f"Estimated unit price for line item '{item.get('item_description', '')}' cannot be negative."
            )
        line = PRLine.objects.create(
            requisition=pr,
            item_description=item["item_description"],
            quantity=qty,
            unit_of_measure=item.get("unit_of_measure", "EA"),
            estimated_unit_price=unit_price,
            specifications=item.get("specifications", ""),
        )
        total += line.estimated_total

    pr.total_amount = total

    # We don't block Drafts from being created if over budget.
    # The actual budget reservation and BudgetOverrunException routing happens when the PR is submitted.

    pr.save(update_fields=["total_amount", "updated_at"])

    if attachments:
        for uploaded_file in attachments:
            validate_pr_attachment(uploaded_file)
            PRAttachment.objects.create(
                requisition=pr,
                uploaded_by=requester,
                title=uploaded_file.name,
                file=uploaded_file,
            )

    AuditLog.objects.create(
        actor=requester,
        action=AuditLog.ACTION_CREATE,
        target_model="PurchaseRequisition",
        target_object_id=str(pr.id),
        new_state={
            "pr_number": pr.pr_number,
            "total_amount": str(pr.total_amount),
            "status": pr.status,
        },
    )

    return pr


def _open_pr_requests(requisition, *, overrun_reason="", shortfall=None):
    """
    Opens the approval request(s) for a submitted PR:
      * the normal PR chain (cost center > department > global policy), bound to the policy
        version selected right now;
      * for a budget overrun, a BUDGET_EXCEPTION request that must be approved first.
    """
    maker = requisition.requester
    pr_request = engine.open_approval_request(
        module=ApprovalPolicy.MODULE_PR,
        target=requisition,
        amount=requisition.total_amount,
        maker=maker,
        department=requisition.department,
        cost_center=requisition.cost_center,
        exception_reason=overrun_reason,
        record_submission=False,
    )
    exception_request = None
    if overrun_reason:
        exception_request = engine.open_approval_request(
            module=ApprovalPolicy.MODULE_BUDGET_EXCEPTION,
            target=requisition,
            amount=shortfall if shortfall is not None else requisition.total_amount,
            maker=maker,
            department=requisition.department,
            cost_center=requisition.cost_center,
            exception_reason=overrun_reason,
            record_submission=False,
        )
    return pr_request, exception_request


def _pending_request(requisition, module, *, amount=None):
    """
    The pending request for this PR and module. Requisitions submitted before the engine
    existed (or whose status was set directly) have none: open one now, fast-forwarded past
    any approvals already recorded for the old chain.
    """
    request = engine.get_pending_request(module, requisition)
    if request:
        return request
    request = engine.open_approval_request(
        module=module,
        target=requisition,
        amount=amount if amount is not None else requisition.total_amount,
        maker=requisition.requester,
        department=requisition.department,
        cost_center=requisition.cost_center,
        record_submission=False,
    )
    if module == ApprovalPolicy.MODULE_PR:
        prior = ApprovalAction.objects.filter(
            target_object_id=requisition.id,
            target_model_name="PurchaseRequisition",
            action=ApprovalAction.ACTION_APPROVE,
            approval_request__isnull=True,
        ).count()
        if prior:
            request.current_step = min(prior, max(len(request.steps) - 1, 0))
            request.save(update_fields=["current_step", "updated_at"])
    return request


def _notify_step_approvers(requisition, request, *, title, message):
    for approver in engine.step_approvers(request):
        Notification.objects.create(
            recipient=approver,
            notification_type=Notification.TYPE_APPROVAL_REQUIRED,
            title=title,
            message=message,
            target_url=f"/requisitions/{requisition.id}/",
        )


@transaction.atomic
def submit_purchase_requisition_service(
    *, requisition: PurchaseRequisition, user: User
) -> PurchaseRequisition:
    """
    Submits a PR for approval:
    1. Runs the pre-approval budget check & locks the reservation (an overrun is an exception).
    2. Routes it through the approval engine, bound to the policy version in force right now.
    3. Transitions PR status to MANAGER_REVIEW (or BUDGET_REVIEW for an overrun).
    """
    if requisition.status != PurchaseRequisition.STATUS_DRAFT:
        raise ValidationError(
            f"Cannot submit requisition in status '{requisition.status}'. Must be DRAFT."
        )

    if requisition.total_amount <= Decimal("0.00"):
        raise ValidationError("Requisition total amount must be greater than zero.")

    overrun_reason = ""
    shortfall = None
    try:
        check_and_reserve_budget_service(requisition=requisition, requested_by_user=user)
        requisition.status = PurchaseRequisition.STATUS_MANAGER_REVIEW
    except BudgetOverrunException as exc:
        requisition.status = PurchaseRequisition.STATUS_BUDGET_REVIEW
        shortfall = exc.requested_amount - exc.budget.available_amount
        overrun_reason = (
            f"Budget overrun of ${shortfall:,.2f} on cost center {requisition.cost_center.code}"
        )

    # Fail closed: no matching policy rejects the submission (and rolls back the reservation).
    pr_request, exception_request = _open_pr_requests(
        requisition, overrun_reason=overrun_reason, shortfall=shortfall
    )

    previous_status = PurchaseRequisition.STATUS_DRAFT
    requisition.save(update_fields=["status", "updated_at"])

    record_approval_action_service(
        target_object_id=requisition.id,
        target_model_name="PurchaseRequisition",
        actor=user,
        action=ApprovalAction.ACTION_SUBMIT,
        previous_state=previous_status,
        new_state=requisition.status,
        comments=(
            f"PR submitted for approval workflow under policy '{pr_request.policy.name}' "
            f"v{pr_request.policy.version}."
        ),
        approval_request=pr_request,
    )

    if requisition.status == PurchaseRequisition.STATUS_BUDGET_REVIEW:
        title = f"Budget Alert: PR {requisition.pr_number} Exceeds Budget"
        message = (
            f"Requisition {requisition.pr_number} (${requisition.total_amount:,.2f}) exceeds the "
            f"available budget for Cost Center {requisition.cost_center.code} and requires budget "
            "exception approval."
        )
        _notify_step_approvers(requisition, exception_request, title=title, message=message)
    else:
        _notify_step_approvers(
            requisition,
            pr_request,
            title=f"New PR Approval Request: {requisition.pr_number}",
            message=(
                f"Requisition {requisition.pr_number} ({requisition.title}) amounting to "
                f"${requisition.total_amount:,.2f} requires your approval review."
            ),
        )

    return requisition


@transaction.atomic
def approve_purchase_requisition_service(
    *, requisition: PurchaseRequisition, approver: User, comments: str = ""
) -> PurchaseRequisition:
    """
    Approves the next step of a PR's approval chain (SMK-PR-050). A PR in BUDGET_REVIEW first
    needs its budget exception approved (Finance by default); only then does the normal chain
    start. All authorisation - role / specific approver / delegation, department, organisation,
    approval limit, maker-checker and four-eyes - is enforced by the approval engine.
    """
    allowed_statuses = [
        PurchaseRequisition.STATUS_SUBMITTED,
        PurchaseRequisition.STATUS_MANAGER_REVIEW,
        PurchaseRequisition.STATUS_BUDGET_REVIEW,
    ]
    requisition = (
        PurchaseRequisition.objects.select_for_update()
        .select_related("department", "cost_center", "requester")
        .get(id=requisition.id)
    )
    if requisition.status not in allowed_statuses:
        raise ValidationError(f"Cannot approve requisition in status '{requisition.status}'.")

    previous_status = requisition.status

    if previous_status == PurchaseRequisition.STATUS_BUDGET_REVIEW:
        request = _pending_request(requisition, ApprovalPolicy.MODULE_BUDGET_EXCEPTION)
        request = engine.approve_step(
            request=request,
            actor=approver,
            comments=comments or "Budget exception step approved.",
            states=(previous_status, previous_status),
        )
        if request.status != ApprovalRequest.STATUS_APPROVED:
            _notify_step_approvers(
                requisition,
                request,
                title=f"Budget Exception Approval Required: PR {requisition.pr_number}",
                message=f"Requisition {requisition.pr_number} needs the next budget exception approval.",
            )
            return requisition
        # The overrun is authorised: take the reservation, then the normal chain begins.
        check_and_reserve_budget_service(
            requisition=requisition, requested_by_user=approver, force_overrun=True
        )
        requisition.status = PurchaseRequisition.STATUS_MANAGER_REVIEW
        requisition.save(update_fields=["status", "updated_at"])
        pr_request = _pending_request(requisition, ApprovalPolicy.MODULE_PR)
        _notify_step_approvers(
            requisition,
            pr_request,
            title=f"New PR Approval Request: {requisition.pr_number}",
            message=(
                f"Requisition {requisition.pr_number} ({requisition.title}) passed budget exception "
                "review and now requires your approval."
            ),
        )
        return requisition

    request = _pending_request(requisition, ApprovalPolicy.MODULE_PR)
    is_final_step = request.current_step + 1 >= len(request.steps)
    next_status = (
        PurchaseRequisition.STATUS_APPROVED
        if is_final_step
        else PurchaseRequisition.STATUS_MANAGER_REVIEW
    )
    request = engine.approve_step(
        request=request,
        actor=approver,
        comments=comments,
        states=(previous_status, next_status),
    )
    requisition.status = next_status
    requisition.save(update_fields=["status", "updated_at"])

    if requisition.status == PurchaseRequisition.STATUS_APPROVED:
        if requisition.requester:
            try:
                Notification.objects.create(
                    recipient=requisition.requester,
                    notification_type=Notification.TYPE_APPROVAL_REQUIRED,
                    title=f"Requisition Approved: {requisition.pr_number}",
                    message=f"Your requisition {requisition.pr_number} has been fully approved and is progressing to sourcing/PO execution.",
                    target_url=f"/requisitions/{requisition.id}/",
                )
            except Exception:
                pass
        try:
            proc_execs = User.objects.filter(role__code=Role.PROC_EXEC, is_active=True)
            for pe in proc_execs:
                Notification.objects.create(
                    recipient=pe,
                    notification_type=Notification.TYPE_APPROVAL_REQUIRED,
                    title="Approved PR Ready for Sourcing",
                    message=f"Requisition {requisition.pr_number} has been approved by {approver.email} and is ready on your Sourcing Intake.",
                    target_url=f"/requisitions/{requisition.id}/",
                )
        except Exception:
            pass
    else:
        step = request.active_step
        _notify_step_approvers(
            requisition,
            request,
            title=f"Approval Required: PR {requisition.pr_number} (Step {step['step_number']})",
            message=(
                f"Requisition {requisition.pr_number} requires your approval for Step "
                f"{step['step_number']} ({step['role_name']})."
            ),
        )

    return requisition


@transaction.atomic
def reject_purchase_requisition_service(
    *, requisition: PurchaseRequisition, approver: User, comments: str
) -> PurchaseRequisition:
    allowed_statuses = [
        PurchaseRequisition.STATUS_SUBMITTED,
        PurchaseRequisition.STATUS_MANAGER_REVIEW,
        PurchaseRequisition.STATUS_BUDGET_REVIEW,
    ]
    requisition = PurchaseRequisition.objects.select_for_update().get(id=requisition.id)
    if requisition.status not in allowed_statuses:
        raise ValidationError(f"Cannot reject requisition in status '{requisition.status}'.")

    if not comments or not comments.strip():
        raise ValidationError("Rejection justification comments are mandatory.")

    previous_status = requisition.status
    module = (
        ApprovalPolicy.MODULE_BUDGET_EXCEPTION
        if previous_status == PurchaseRequisition.STATUS_BUDGET_REVIEW
        else ApprovalPolicy.MODULE_PR
    )
    try:
        request = _pending_request(requisition, module)
    except ValidationError:
        request = None  # no policy resolvable (legacy data): rejection must still be possible
    if request is not None:
        # only an approver on the chain (or a Super Admin) may reject
        engine.reject_request(
            request=request,
            actor=approver,
            comments=comments,
            states=(previous_status, PurchaseRequisition.STATUS_REJECTED),
        )
    # whichever request is still open for this PR (normal chain / budget exception) ends here
    for other in (ApprovalPolicy.MODULE_PR, ApprovalPolicy.MODULE_BUDGET_EXCEPTION):
        engine.cancel_pending_request(
            other, requisition, actor=approver, reason="Requisition rejected"
        )

    requisition.status = PurchaseRequisition.STATUS_REJECTED
    requisition.save(update_fields=["status", "updated_at"])

    # Release any active budget reservations for this requisition
    release_budget_reservation_service(
        requisition=requisition, user=approver, reason=f"PR Rejected: {comments}"
    )

    if request is None:
        record_approval_action_service(
            target_object_id=requisition.id,
            target_model_name="PurchaseRequisition",
            actor=approver,
            action=ApprovalAction.ACTION_REJECT,
            previous_state=previous_status,
            new_state=requisition.status,
            comments=comments,
        )

    if requisition.requester:
        try:
            Notification.objects.create(
                recipient=requisition.requester,
                notification_type=Notification.TYPE_APPROVAL_REQUIRED,
                title=f"Requisition Rejected: {requisition.pr_number}",
                message=f"Your requisition {requisition.pr_number} was rejected by {approver.email}. Reason: {comments}",
                target_url=f"/requisitions/{requisition.id}/",
            )
        except Exception:
            pass

    return requisition

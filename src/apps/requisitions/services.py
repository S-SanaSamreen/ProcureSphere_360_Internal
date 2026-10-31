from decimal import Decimal

from django.core.exceptions import ValidationError
from django.db import transaction
from django.utils import timezone

from apps.accounts.models import Role, User
from apps.accounts.permissions import forbid_non_transacting_actor
from apps.approvals import engine
from apps.approvals.models import ApprovalAction, ApprovalPolicy, ApprovalRequest
from apps.approvals.services import record_approval_action_service
from apps.audit.models import AuditLog
from apps.budgets.models import BudgetReservation
from apps.budgets.services import (
    BudgetOverrunException,
    check_and_reserve_budget_service,
    pr_budget_position,
    release_budget_reservation_service,
)
from apps.core.validators import validate_pr_attachment
from apps.notifications.models import Notification
from apps.organization.models import CostCenter, Department

from .models import PRAttachment, PRLine, PurchaseRequisition

DEFAULT_PR_EXCEPTION_POLICY = "PR Budget Exception - Procurement Manager (built-in)"

FULFILMENT_STATUSES = (
    PurchaseRequisition.STATUS_APPROVED,
    PurchaseRequisition.STATUS_SOURCING,
    PurchaseRequisition.STATUS_PO_ISSUED,
    PurchaseRequisition.STATUS_COMPLETED,
)


def _is_fully_delivered_and_paid(requisition) -> bool:
    """
    The request is fulfilled when every live PO raised from it is fully received (COMPLETED) and
    every invoice against those POs is settled: at least one PAID invoice per PO and nothing
    still open (rejected invoices do not count).
    """
    from apps.invoices.models import SupplierInvoice
    from apps.orders.models import PurchaseOrder

    pos = list(
        PurchaseOrder.objects.filter(requisition=requisition).exclude(
            status=PurchaseOrder.STATUS_CANCELLED
        )
    )
    if not pos or any(po.status != PurchaseOrder.STATUS_COMPLETED for po in pos):
        return False
    for po in pos:
        invoices = SupplierInvoice.objects.filter(po=po).exclude(
            status=SupplierInvoice.STATUS_REJECTED
        )
        if not invoices.exists() or invoices.exclude(status=SupplierInvoice.STATUS_PAID).exists():
            return False
    return True


@transaction.atomic
def refresh_requisition_fulfilment_status(requisition, *, actor=None):
    """
    Keeps an approved requisition's lifecycle state (PRD 4: ... APPROVED -> SOURCING / PO) in
    line with what actually exists, so it can never drift from the underlying documents:

      * every PO fully received and every invoice paid       -> COMPLETED
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
    if has_issued_po and _is_fully_delivered_and_paid(requisition):
        target = PurchaseRequisition.STATUS_COMPLETED
    elif has_issued_po:
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
            new_state={
                "status": target,
                "reason": "fulfilment status follows POs / sourcing events",
            },
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
    forbid_non_transacting_actor(requester, "create purchase requisitions")
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


def _open_pr_requests(requisition):
    """
    Opens the normal PR approval chain (cost center > department > global policy), bound to
    the policy version selected right now. The budget is only soft-checked at submit; a budget
    exception request is raised later, by Finance's approval, if the PR is out of budget.
    """
    return engine.open_approval_request(
        module=ApprovalPolicy.MODULE_PR,
        target=requisition,
        amount=requisition.total_amount,
        maker=requisition.requester,
        department=requisition.department,
        cost_center=requisition.cost_center,
        record_submission=False,
    )


def _status_for_step(step):
    """
    PR status implied by an approval step: Finance/AP's step is the Budget Review; every other
    (department / escalation) step is the Manager Review.
    """
    if step and step.get("role_code") == Role.FINANCE_AP and not step.get("exception"):
        return PurchaseRequisition.STATUS_BUDGET_REVIEW
    return PurchaseRequisition.STATUS_MANAGER_REVIEW


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
    1. Soft budget check only (WITHIN_BUDGET / OUT_OF_BUDGET + shortfall is recorded on the
       submission action); nothing is reserved until Finance approves.
    2. Routes it through the approval engine, bound to the policy version in force right now.
    3. Transitions PR status to MANAGER_REVIEW (BUDGET_REVIEW if Finance is the first step).
    """
    forbid_non_transacting_actor(user, "submit purchase requisitions")
    if requisition.status not in (
        PurchaseRequisition.STATUS_DRAFT,
        PurchaseRequisition.STATUS_RETURNED,
    ):
        raise ValidationError(
            f"Cannot submit requisition in status '{requisition.status}'. "
            "Must be DRAFT or RETURNED."
        )

    if requisition.total_amount <= Decimal("0.00"):
        raise ValidationError("Requisition total amount must be greater than zero.")

    position = pr_budget_position(requisition)
    if position["budget"] is None:
        raise ValidationError(
            "No active fiscal period budget allocation found for Cost Center "
            f"'{requisition.cost_center.code}'."
        )

    # Fail closed: no matching policy rejects the submission.
    pr_request = _open_pr_requests(requisition)

    previous_status = requisition.status
    requisition.status = _status_for_step(pr_request.active_step)
    requisition.save(update_fields=["status", "updated_at"])

    budget_note = f"Budget check: {position['status']}"
    if not position["within_budget"]:
        budget_note += f" (shortfall ${position['shortfall']:,.2f})"
    record_approval_action_service(
        target_object_id=requisition.id,
        target_model_name="PurchaseRequisition",
        actor=user,
        action=ApprovalAction.ACTION_SUBMIT,
        previous_state=previous_status,
        new_state=requisition.status,
        comments=(
            f"PR submitted for approval workflow under policy '{pr_request.policy.name}' "
            f"v{pr_request.policy.version}. {budget_note}."
        ),
        approval_request=pr_request,
    )

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


def _pr_budget_exception_policy(shortfall, requisition):
    """
    The chain that must approve an out-of-budget PR (PRD 14): the matching budget-exception
    *exception-route* policy (budget-exception module, else PR module) when a Super Admin defined one, otherwise a built-in single
    Procurement Manager step. The built-in policy is flagged as an exception route so it is
    never picked as the normal budget-exception chain (e.g. for PO overruns).
    """
    from apps.approvals.services import add_approval_step_service, create_approval_policy_service

    policy = engine.select_policy(
        ApprovalPolicy.MODULE_BUDGET_EXCEPTION,
        shortfall,
        requisition.department,
        requisition.cost_center,
        exception=True,
    )
    if policy and policy.steps.exists():
        return policy
    policy = engine.select_policy(
        ApprovalPolicy.MODULE_PR,
        requisition.total_amount,
        requisition.department,
        requisition.cost_center,
        exception=True,
    )
    if policy and policy.steps.exists():
        return policy
    proc_role = Role.objects.filter(code=Role.PROC_MGR).first()
    if proc_role is None:
        raise ValidationError(
            "No budget exception policy is configured and the Procurement Manager role is "
            "missing; the over-budget requisition cannot be routed."
        )
    policy = ApprovalPolicy.objects.filter(
        module=ApprovalPolicy.MODULE_BUDGET_EXCEPTION,
        is_exception_route=True,
        is_active=True,
        superseded_by__isnull=True,
        name=DEFAULT_PR_EXCEPTION_POLICY,
    ).first()
    if policy is None:
        policy = create_approval_policy_service(
            name=DEFAULT_PR_EXCEPTION_POLICY,
            module=ApprovalPolicy.MODULE_BUDGET_EXCEPTION,
            min_amount=Decimal("0.00"),
            is_exception_route=True,
        )
        add_approval_step_service(
            policy=policy,
            step_number=1,
            approver_role=proc_role,
            description="Budget Exception Approval",
        )
    return policy


def _route_budget_exception(requisition, *, shortfall, actor):
    """Finance cannot silently approve an out-of-budget PR: open the budget-exception chain."""
    reason = f"Budget overrun of ${shortfall:,.2f} on cost center {requisition.cost_center.code}"
    exception_request = engine.open_approval_request(
        module=ApprovalPolicy.MODULE_BUDGET_EXCEPTION,
        target=requisition,
        amount=shortfall,
        maker=requisition.requester,
        department=requisition.department,
        cost_center=requisition.cost_center,
        record_submission=False,
        policy=_pr_budget_exception_policy(shortfall, requisition),
    )
    exception_request.exception_reason = reason
    exception_request.save(update_fields=["exception_reason", "updated_at"])
    record_approval_action_service(
        target_object_id=requisition.id,
        target_model_name="PurchaseRequisition",
        actor=actor,
        action=ApprovalAction.ACTION_SUBMIT,
        previous_state=PurchaseRequisition.STATUS_BUDGET_REVIEW,
        new_state=PurchaseRequisition.STATUS_BUDGET_REVIEW,
        comments=f"Out of budget at Finance approval: {reason}. Budget exception required.",
        approval_request=exception_request,
    )
    _notify_step_approvers(
        requisition,
        exception_request,
        title=f"Budget Alert: PR {requisition.pr_number} Exceeds Budget",
        message=(
            f"Requisition {requisition.pr_number} (${requisition.total_amount:,.2f}) exceeds the "
            f"available budget for Cost Center {requisition.cost_center.code} and requires budget "
            "exception approval."
        ),
    )
    return exception_request


def _notify_requester_approved(requisition, approver):
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
        org_id = requisition.department.organization_id
        if org_id:
            proc_execs = proc_execs.filter(organization_id=org_id)
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


def _has_open_reservation(requisition) -> bool:
    return BudgetReservation.objects.filter(
        requisition=requisition, status=BudgetReservation.STATUS_RESERVED
    ).exists()


@transaction.atomic
def approve_purchase_requisition_service(
    *, requisition: PurchaseRequisition, approver: User, comments: str = ""
) -> PurchaseRequisition:
    """
    Approves the next step of a PR's approval chain. Department level(s) keep the PR in
    MANAGER_REVIEW; when Finance/AP's step becomes active it is BUDGET_REVIEW. Approving the
    last step reserves the budget and makes the PR APPROVED - unless the PR is out of budget,
    in which case a budget-exception request (exception policy, else Procurement Manager) must
    be approved first. All authorisation - role / specific approver / delegation, department,
    organisation, approval limit, maker-checker and four-eyes - is enforced by the engine.
    """
    forbid_non_transacting_actor(approver, "approve purchase requisitions")
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

    exception_request = engine.get_pending_request(
        ApprovalPolicy.MODULE_BUDGET_EXCEPTION, requisition
    )
    if exception_request is not None:
        request = engine.approve_step(
            request=exception_request,
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
        # The overrun is authorised: take the reservation (recorded as over budget).
        if not _has_open_reservation(requisition):
            check_and_reserve_budget_service(
                requisition=requisition, requested_by_user=approver, force_overrun=True
            )
        request.consumed_at = timezone.now()
        request.save(update_fields=["consumed_at", "updated_at"])
        pr_request = engine.get_pending_request(ApprovalPolicy.MODULE_PR, requisition)
        if pr_request is not None:
            # In flight from before the Finance-first flow: the normal chain now begins.
            requisition.status = _status_for_step(pr_request.active_step)
            requisition.save(update_fields=["status", "updated_at"])
            _notify_step_approvers(
                requisition,
                pr_request,
                title=f"New PR Approval Request: {requisition.pr_number}",
                message=(
                    f"Requisition {requisition.pr_number} ({requisition.title}) passed budget "
                    "exception review and now requires your approval."
                ),
            )
            return requisition
        requisition.status = PurchaseRequisition.STATUS_APPROVED
        requisition.save(update_fields=["status", "updated_at"])
        _notify_requester_approved(requisition, approver)
        return requisition

    request = _pending_request(requisition, ApprovalPolicy.MODULE_PR)
    is_final_step = request.current_step + 1 >= len(request.steps)

    if not is_final_step:
        next_status = _status_for_step(request.steps[request.current_step + 1])
        approved = engine.approve_step(
            request=request,
            actor=approver,
            comments=comments,
            states=(previous_status, next_status),
        )
        requisition.status = next_status
        requisition.save(update_fields=["status", "updated_at"])
        step = approved.active_step
        _notify_step_approvers(
            requisition,
            approved,
            title=f"Approval Required: PR {requisition.pr_number} (Step {step['step_number']})",
            message=(
                f"Requisition {requisition.pr_number} requires your approval for Step "
                f"{step['step_number']} ({step['role_name']})."
            ),
        )
        return requisition

    # Final step (Finance / AP): re-check the budget now - it may have moved since submit.
    position = pr_budget_position(requisition)
    out_of_budget = not _has_open_reservation(requisition) and not position["within_budget"]
    shortfall = position["shortfall"]
    engine.approve_step(
        request=request,
        actor=approver,
        comments=comments,
        states=(
            previous_status,
            (
                PurchaseRequisition.STATUS_BUDGET_REVIEW
                if out_of_budget
                else PurchaseRequisition.STATUS_APPROVED
            ),
        ),
    )
    if not out_of_budget and not _has_open_reservation(requisition):
        try:
            check_and_reserve_budget_service(requisition=requisition, requested_by_user=approver)
        except BudgetOverrunException as exc:  # lost a race with another reservation
            out_of_budget = True
            shortfall = exc.requested_amount - exc.budget.available_amount
    if out_of_budget:
        requisition.status = PurchaseRequisition.STATUS_BUDGET_REVIEW
        requisition.save(update_fields=["status", "updated_at"])
        _route_budget_exception(requisition, shortfall=shortfall, actor=approver)
        return requisition

    requisition.status = PurchaseRequisition.STATUS_APPROVED
    requisition.save(update_fields=["status", "updated_at"])
    _notify_requester_approved(requisition, approver)
    return requisition


@transaction.atomic
def reject_purchase_requisition_service(
    *, requisition: PurchaseRequisition, approver: User, comments: str
) -> PurchaseRequisition:
    forbid_non_transacting_actor(approver, "reject purchase requisitions")
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
        if engine.get_pending_request(ApprovalPolicy.MODULE_BUDGET_EXCEPTION, requisition)
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


@transaction.atomic
def return_purchase_requisition_service(
    *, requisition: PurchaseRequisition, approver: User, comments: str
) -> PurchaseRequisition:
    """
    An approver on the active step (Department Approver / Finance-AP) returns
    the PR to its requester for clarification. A comment is mandatory. The open approval
    request is closed as RETURNED (history kept, ApprovalAction RETURNED recorded), any budget
    reservation is released and the requester is notified. The requester then edits and
    resubmits, which opens a new approval request bound to the policy version then in force.
    """
    forbid_non_transacting_actor(approver, "return purchase requisitions")
    allowed_statuses = [
        PurchaseRequisition.STATUS_SUBMITTED,
        PurchaseRequisition.STATUS_MANAGER_REVIEW,
        PurchaseRequisition.STATUS_BUDGET_REVIEW,
    ]
    requisition = PurchaseRequisition.objects.select_for_update().get(id=requisition.id)
    if requisition.status not in allowed_statuses:
        raise ValidationError(f"Cannot return requisition in status '{requisition.status}'.")
    if not comments or not comments.strip():
        raise ValidationError("A comment explaining what needs clarification is mandatory.")

    previous_status = requisition.status
    module = (
        ApprovalPolicy.MODULE_BUDGET_EXCEPTION
        if engine.get_pending_request(ApprovalPolicy.MODULE_BUDGET_EXCEPTION, requisition)
        else ApprovalPolicy.MODULE_PR
    )
    request = _pending_request(requisition, module)
    engine.return_request(
        request=request,
        actor=approver,
        comments=comments,
        states=(previous_status, PurchaseRequisition.STATUS_RETURNED),
    )
    for other in (ApprovalPolicy.MODULE_PR, ApprovalPolicy.MODULE_BUDGET_EXCEPTION):
        engine.cancel_pending_request(
            other, requisition, actor=approver, reason="Requisition returned for clarification"
        )

    requisition.status = PurchaseRequisition.STATUS_RETURNED
    requisition.save(update_fields=["status", "updated_at"])
    release_budget_reservation_service(
        requisition=requisition, user=approver, reason=f"PR returned: {comments.strip()}"
    )

    if requisition.requester:
        Notification.objects.create(
            recipient=requisition.requester,
            notification_type=Notification.TYPE_PR_RETURNED,
            title=f"Requisition Returned for Clarification: {requisition.pr_number}",
            message=(
                f"Your requisition {requisition.pr_number} was returned by {approver.email}. "
                f"Comment: {comments.strip()}. Please update it and resubmit."
            ),
            target_url=f"/requisitions/{requisition.id}/",
        )
    return requisition


@transaction.atomic
def withdraw_purchase_requisition_service(
    *, requisition: PurchaseRequisition, user: User, reason: str = ""
) -> PurchaseRequisition:
    """
    The requester withdraws a PR before final approval (in review or returned): it becomes
    CANCELLED, the reservation is released, pending approval requests are closed and the
    withdrawal is audit-logged.
    """
    requisition = PurchaseRequisition.objects.select_for_update().get(id=requisition.id)
    if requisition.requester_id != user.id:
        raise ValidationError("Only the requester can withdraw this requisition.")
    if requisition.status not in (
        PurchaseRequisition.STATUS_SUBMITTED,
        PurchaseRequisition.STATUS_MANAGER_REVIEW,
        PurchaseRequisition.STATUS_BUDGET_REVIEW,
        PurchaseRequisition.STATUS_RETURNED,
    ):
        raise ValidationError(f"Cannot withdraw requisition in status '{requisition.status}'.")
    previous = requisition.status
    requisition.status = PurchaseRequisition.STATUS_CANCELLED
    requisition.save(update_fields=["status", "updated_at"])
    release_budget_reservation_service(
        requisition=requisition, user=user, reason=reason or "PR withdrawn by requester"
    )
    for module in (ApprovalPolicy.MODULE_PR, ApprovalPolicy.MODULE_BUDGET_EXCEPTION):
        engine.cancel_pending_request(
            module, requisition, actor=user, reason="Requisition withdrawn by requester"
        )
    AuditLog.objects.create(
        actor=user,
        action=AuditLog.ACTION_CANCEL,
        target_model="PurchaseRequisition",
        target_object_id=str(requisition.id),
        previous_state={"status": previous},
        new_state={"status": requisition.status, "reason": reason or "withdrawn by requester"},
    )
    return requisition

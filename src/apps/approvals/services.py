from decimal import Decimal

from django.core.exceptions import ValidationError
from django.db import transaction
from django.utils import timezone

from apps.accounts.models import Role, User
from apps.audit.models import AuditLog

from .models import ApprovalAction, ApprovalDelegate, ApprovalPolicy, ApprovalStep


@transaction.atomic
def create_approval_policy_service(
    *,
    name: str,
    module: str,
    min_amount: Decimal = Decimal("0.00"),
    max_amount=None,
    department=None,
    cost_center=None,
    is_exception_route: bool = False,
    created_by=None,
) -> ApprovalPolicy:
    """
    Creates version 1 of an Approval Policy (steps are added with add_approval_step_service,
    or use engine.create_policy_with_steps to create both at once).
    """
    policy = ApprovalPolicy.objects.create(
        name=name,
        module=module,
        min_amount=min_amount,
        max_amount=max_amount,
        department=department,
        cost_center=cost_center,
        is_exception_route=is_exception_route,
        created_by=created_by,
    )
    return policy


@transaction.atomic
def add_approval_step_service(
    *,
    policy: ApprovalPolicy,
    step_number: int,
    approver_role: Role,
    specific_approver: User = None,
    description: str = "",
) -> ApprovalStep:
    """
    Adds an ordered step to an Approval Policy.
    """
    step = ApprovalStep.objects.create(
        policy=policy,
        step_number=step_number,
        approver_role=approver_role,
        specific_approver=specific_approver,
        description=description,
    )
    return step


def resolve_active_delegate(approver: User) -> User:
    """
    Resolves if an approver has an active delegate for today's date.
    If active delegate exists, returns the delegate user; otherwise returns the original approver.
    """
    today = timezone.now().date()
    delegate_record = ApprovalDelegate.objects.filter(
        approver=approver,
        is_active=True,
        start_date__lte=today,
        end_date__gte=today,
    ).first()

    if delegate_record and delegate_record.delegate:
        return delegate_record.delegate
    return approver


def evaluate_approval_chain(*, module: str, amount: Decimal, department=None, cost_center=None):
    """
    Evaluates server-side approval routing for a module & amount: most specific policy wins
    (cost center, then department, then global), highest matching amount tier, latest active
    version. Returns [{step_number, policy_step, approver_role, specific_approver,
    effective_approver, description}] (empty when no policy matches).

    This is a read-only preview. Requests that are actually submitted are bound to a policy
    version by engine.open_approval_request and no longer consult this function.
    """
    from .engine import _ensure_default_steps, select_policy

    policy = select_policy(module, amount, department, cost_center)
    if not policy:
        return []
    _ensure_default_steps(policy)
    chain = []
    for step in policy.steps.select_related("approver_role", "specific_approver").order_by(
        "step_number"
    ):
        effective_user = None
        if step.specific_approver:
            effective_user = resolve_active_delegate(step.specific_approver)
        chain.append(
            {
                "step_number": step.step_number,
                "policy_step": step,
                "approver_role": step.approver_role,
                "specific_approver": step.specific_approver,
                "effective_approver": effective_user,
                "description": step.description,
            }
        )
    return chain


@transaction.atomic
def record_approval_action_service(
    *,
    target_object_id,
    target_model_name: str,
    actor: User,
    action: str,
    previous_state: str,
    new_state: str,
    policy_step: ApprovalStep = None,
    comments: str = "",
    approval_request=None,
) -> ApprovalAction:
    """
    Records an append-only ApprovalAction and corresponding AuditLog entry inside a transaction.
    """
    approval_action = ApprovalAction.objects.create(
        approval_request=approval_request,
        policy_step=policy_step,
        target_object_id=target_object_id,
        target_model_name=target_model_name,
        actor=actor,
        action=action,
        comments=comments,
        previous_state=previous_state,
        new_state=new_state,
    )

    # Write append-only AuditLog with accurate action mapping
    if action == ApprovalAction.ACTION_APPROVE:
        audit_action = AuditLog.ACTION_APPROVE
    elif action == ApprovalAction.ACTION_REJECT:
        audit_action = AuditLog.ACTION_REJECT
    elif action == ApprovalAction.ACTION_SUBMIT:
        audit_action = AuditLog.ACTION_CREATE
    else:
        audit_action = AuditLog.ACTION_UPDATE

    AuditLog.objects.create(
        actor=actor,
        action=audit_action,
        target_model=target_model_name,
        target_object_id=str(target_object_id),
        previous_state={"status": previous_state},
        new_state={"status": new_state, "comments": comments},
    )

    return approval_action


@transaction.atomic
def set_approval_delegate_service(
    *, approver: User, delegate: User, start_date, end_date, reason: str = ""
) -> ApprovalDelegate:
    """
    Sets up a temporary delegate for an approver (approver unavailability, PRD 4.2). The delegate
    acts with the approver's authority - role, department and approval limit - for the period.
    """
    from django.core.exceptions import ValidationError
    from django.utils.dateparse import parse_date

    start = parse_date(str(start_date)) if not hasattr(start_date, "year") else start_date
    end = parse_date(str(end_date)) if not hasattr(end_date, "year") else end_date
    if not start or not end:
        raise ValidationError("Valid start and end dates (YYYY-MM-DD) are required.")
    if end < start:
        raise ValidationError("The delegation end date cannot be before its start date.")
    if end < timezone.localdate():
        raise ValidationError("The delegation period is already over.")
    if delegate.pk == approver.pk:
        raise ValidationError("You cannot delegate to yourself.")
    if not delegate.is_active or getattr(delegate, "vendor_id", None) or not delegate.role_id:
        raise ValidationError("The delegate must be an active internal user with a role.")
    if ApprovalDelegate.objects.filter(
        approver=approver,
        delegate=delegate,
        is_active=True,
        start_date__lte=end,
        end_date__gte=start,
    ).exists():
        raise ValidationError("An overlapping delegation to this user already exists.")

    delegate_record = ApprovalDelegate.objects.create(
        approver=approver,
        delegate=delegate,
        start_date=start,
        end_date=end,
        is_active=True,
        reason=reason,
    )
    AuditLog.objects.create(
        actor=approver,
        action=AuditLog.ACTION_CREATE,
        target_model="ApprovalDelegate",
        target_object_id=str(delegate_record.id),
        new_state={
            "approver": approver.email,
            "delegate": delegate.email,
            "start_date": str(start),
            "end_date": str(end),
            "reason": reason,
        },
    )
    return delegate_record


@transaction.atomic
def revoke_approval_delegate_service(
    *, delegation: ApprovalDelegate, actor: User
) -> ApprovalDelegate:
    """Ends a delegation early. Only the approver who granted it may."""
    from django.core.exceptions import ValidationError

    if delegation.approver_id != actor.id:
        raise ValidationError("Only the approver who granted a delegation can revoke it.")
    if not delegation.is_active:
        return delegation
    delegation.is_active = False
    delegation.save(update_fields=["is_active", "updated_at"])
    AuditLog.objects.create(
        actor=actor,
        action=AuditLog.ACTION_UPDATE,
        target_model="ApprovalDelegate",
        target_object_id=str(delegation.id),
        previous_state={"is_active": True},
        new_state={"is_active": False, "revoked_by": actor.email},
    )
    return delegation


class DecisionHandler:
    """How to approve / reject one kind of target through its own (engine-backed) service."""

    def __init__(self, model, approve, reject, return_=None):
        self.model = model
        self.approve = approve
        self.reject = reject
        self.return_ = return_


def decision_handlers() -> dict:
    """
    The ONLY transaction types that can be decided through the generic approval entry points
    (inbox, API). Each handler calls the owning module's service, which enforces the approval
    engine (role / delegation / limits / separation of duties). Anything not listed here cannot
    be approved generically - there is deliberately no "just set the status" fallback.
    """
    from apps.budgets import services as budget_services
    from apps.budgets.models import BudgetTransfer
    from apps.contracts.models import Contract
    from apps.contracts.services import approve_business_service, reject_business_service
    from apps.invoices.models import MatchException
    from apps.invoices.services import resolve_match_exception_service
    from apps.orders import services as order_services
    from apps.orders.models import POAmendment, PurchaseOrder
    from apps.requisitions import services as pr_services
    from apps.requisitions.models import PurchaseRequisition
    from apps.sourcing import services as sourcing_services
    from apps.sourcing.models import AwardDecision

    from . import engine

    def pr_approve(t, actor, c, module=None):
        pr_services.approve_purchase_requisition_service(requisition=t, approver=actor, comments=c)

    def pr_reject(t, actor, c, module=None):
        pr_services.reject_purchase_requisition_service(requisition=t, approver=actor, comments=c)

    def pr_return(t, actor, c, module=None):
        pr_services.return_purchase_requisition_service(requisition=t, approver=actor, comments=c)

    def po_approve(t, actor, c, module=None):
        if module == ApprovalPolicy.MODULE_BUDGET_EXCEPTION or (
            module is None
            and engine.get_pending_request(ApprovalPolicy.MODULE_BUDGET_EXCEPTION, t)
            and not engine.get_pending_request(ApprovalPolicy.MODULE_PO, t)
        ):
            order_services.decide_po_budget_exception_service(
                po=t, actor=actor, approve=True, comments=c
            )
        else:
            order_services.approve_purchase_order_service(po=t, approver=actor, comments=c)

    def po_reject(t, actor, c, module=None):
        if module == ApprovalPolicy.MODULE_BUDGET_EXCEPTION or (
            module is None
            and engine.get_pending_request(ApprovalPolicy.MODULE_BUDGET_EXCEPTION, t)
            and not engine.get_pending_request(ApprovalPolicy.MODULE_PO, t)
        ):
            order_services.decide_po_budget_exception_service(
                po=t, actor=actor, approve=False, comments=c
            )
        else:
            order_services.reject_purchase_order_service(po=t, approver=actor, reason=c)

    return {
        "PurchaseRequisition": DecisionHandler(
            PurchaseRequisition, pr_approve, pr_reject, pr_return
        ),
        "PurchaseOrder": DecisionHandler(PurchaseOrder, po_approve, po_reject),
        "POAmendment": DecisionHandler(
            POAmendment,
            lambda t, a, c, module=None: order_services.approve_po_amendment_service(
                amendment=t, approver=a, comments=c
            ),
            lambda t, a, c, module=None: order_services.reject_po_amendment_service(
                amendment=t, rejector=a, reason=c
            ),
        ),
        "AwardDecision": DecisionHandler(
            AwardDecision,
            lambda t, a, c, module=None: sourcing_services.approve_award_service(
                event=t.event, approver=a, comments=c
            ),
            lambda t, a, c, module=None: sourcing_services.reject_award_service(
                event=t.event, approver=a, comments=c
            ),
        ),
        "Contract": DecisionHandler(
            Contract,
            lambda t, a, c, module=None: approve_business_service(contract=t, user=a, notes=c),
            lambda t, a, c, module=None: reject_business_service(contract=t, user=a, reason=c),
        ),
        "BudgetTransfer": DecisionHandler(
            BudgetTransfer,
            lambda t, a, c, module=None: budget_services.approve_budget_transfer_service(
                transfer=t, approver=a, comments=c
            ),
            lambda t, a, c, module=None: budget_services.reject_budget_transfer_service(
                transfer=t, approver=a, comments=c
            ),
        ),
        "MatchException": DecisionHandler(
            MatchException,
            lambda t, a, c, module=None: resolve_match_exception_service(
                match_exception=t,
                resolved_by_user=a,
                resolution_notes=c or "Approved",
                action="RESOLVE",
            ),
            lambda t, a, c, module=None: resolve_match_exception_service(
                match_exception=t, resolved_by_user=a, resolution_notes=c, action="REJECT"
            ),
        ),
    }


def get_decision_target(model_name: str, object_id):
    """The target object for a decidable type, or None if the type/object is unknown."""
    handler = decision_handlers().get(model_name)
    if handler is None:
        return None
    return handler.model.objects.filter(pk=object_id).first()


@transaction.atomic
def process_approval_action_service(
    *, target_object, actor: User, action: str, comments: str = "", module=None
):
    """
    Approve or reject a target through its owning module's service. The approval engine - not
    this function - decides whether ``actor`` may act, so there is no way to approve something
    by pointing this at it. Unknown target types are refused outright.
    Always returns the latest persisted ApprovalAction for the target.
    """
    from apps.accounts.permissions import forbid_non_transacting_actor

    forbid_non_transacting_actor(actor, "approve, reject or return business requests")
    name = target_object.__class__.__name__
    handler = decision_handlers().get(name)
    if handler is None:
        raise ValidationError(
            f"{name} records cannot be approved through the generic approval endpoint."
        )

    act_str = str(action).upper()
    if act_str in ("APPROVE", "APPROVED"):
        handler.approve(target_object, actor, comments, module=module)
    elif act_str in ("REJECT", "REJECTED"):
        handler.reject(target_object, actor, comments, module=module)
    elif act_str in ("RETURN", "RETURNED"):
        if handler.return_ is None:
            raise ValidationError(f"{name} records cannot be returned for clarification.")
        handler.return_(target_object, actor, comments, module=module)
    else:
        raise ValidationError(f"Unknown approval action '{action}'. Use APPROVE, REJECT or RETURN.")

    return (
        ApprovalAction.objects.filter(
            target_object_id=str(target_object.id), target_model_name=name
        )
        .order_by("-created_at")
        .first()
    )

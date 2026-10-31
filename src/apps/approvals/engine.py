"""
Generic approval engine.

Every approvable transaction (requisition, PO, PO amendment, sourcing award, contract business
approval, invoice match exception, budget exception) is routed through here, so routing is
configuration (ApprovalPolicy + ApprovalStep) rather than a role check buried in each module.

Guarantees:
  * Routing is evaluated server-side from amount, department, cost center and transaction type
    (most specific policy wins: cost center > department > global; highest matching tier).
  * A request is bound to the policy VERSION selected when it was submitted and keeps a snapshot
    of that policy's steps, so editing / superseding / deactivating a policy never re-routes
    work that is already in flight.
  * A request can be raised as an *exception*; the steps of a matching exception-route policy
    are then appended after the normal chain.
  * Maker-checker: whoever made the request can never approve it, and nobody can approve two
    steps of the same request (four-eyes) - this applies to Super Admin too.
  * Per-user approval limits are enforced at decision time.
  * Delegation: an active delegate acts with the authority of the person they cover.
"""

from decimal import Decimal

from django.core.exceptions import ValidationError
from django.db import transaction
from django.db.models import Q
from django.utils import timezone

from apps.accounts.models import Role, User
from apps.audit.models import AuditLog

from .models import (
    ApprovalAction,
    ApprovalPolicy,
    ApprovalRequest,
    ApprovalStep,
)

MODULE_LABELS = dict(ApprovalPolicy.MODULE_CHOICES)


# ----------------------------------------------------------------------------- policy selection
def _candidates(module, amount, exception):
    return ApprovalPolicy.objects.filter(
        Q(max_amount__isnull=True) | Q(max_amount__gte=amount),
        module=module,
        is_active=True,
        min_amount__lte=amount,
        is_exception_route=exception,
        superseded_by__isnull=True,
    ).order_by("-min_amount", "-version", "-created_at")


def select_policy(module, amount, department=None, cost_center=None, *, exception=False):
    """
    The single policy that routes this transaction, or None. Most specific scope wins
    (cost center, then department, then global); within a scope the highest matching amount
    tier wins; superseded and inactive versions are never selected.
    """
    amount = Decimal(str(amount or 0))
    qs = _candidates(module, amount, exception)
    policy = None
    if cost_center is not None:
        policy = qs.filter(cost_center=cost_center).first()
    if not policy and department is not None:
        policy = qs.filter(department=department, cost_center__isnull=True).first()
    if not policy:
        policy = qs.filter(department__isnull=True, cost_center__isnull=True).first()
    return policy


def _ensure_default_steps(policy):
    """Legacy: a PR / PO-amendment policy created without steps gets the historical defaults."""
    if policy.steps.exists():
        return
    dept_role = Role.objects.filter(code=Role.DEPT_APPROVER).first()
    proc_role = Role.objects.filter(code=Role.PROC_MGR).first()
    if policy.module == ApprovalPolicy.MODULE_PR:
        if dept_role:
            ApprovalStep.objects.get_or_create(
                policy=policy,
                step_number=1,
                defaults={"approver_role": dept_role, "description": "Department Manager Approval"},
            )
        if proc_role:
            ApprovalStep.objects.get_or_create(
                policy=policy,
                step_number=2,
                defaults={
                    "approver_role": proc_role,
                    "description": "Procurement Manager Approval",
                },
            )
    elif policy.module == ApprovalPolicy.MODULE_PO_AMENDMENT and proc_role:
        ApprovalStep.objects.get_or_create(
            policy=policy,
            step_number=1,
            defaults={"approver_role": proc_role, "description": "Procurement Manager Approval"},
        )


def _snapshot_steps(policy, *, offset=0, exception=False):
    _ensure_default_steps(policy)
    rows = []
    for step in policy.steps.select_related("approver_role", "specific_approver").order_by(
        "step_number"
    ):
        rows.append(
            {
                "step_number": offset + len(rows) + 1,
                "policy_id": str(policy.id),
                "policy_step_id": str(step.id),
                "role_id": str(step.approver_role_id),
                "role_code": step.approver_role.code,
                "role_name": step.approver_role.name,
                "specific_approver_id": (
                    str(step.specific_approver_id) if step.specific_approver_id else None
                ),
                "description": step.description,
                "exception": exception,
            }
        )
    return rows


def no_policy_error(module, amount, department=None, cost_center=None):
    scope = []
    if department is not None:
        scope.append(f"department '{department.name}'")
    if cost_center is not None:
        scope.append(f"cost center '{cost_center.code}'")
    scope_txt = " and ".join(scope) if scope else "Global"
    return ValidationError(
        f"No active approval policy found for {MODULE_LABELS.get(module, module)} "
        f"amount ${Decimal(str(amount or 0)):,.2f} ({scope_txt}). "
        "Submission rejected under fail-closed routing rule."
    )


# ----------------------------------------------------------------------------- opening a request
@transaction.atomic
def open_approval_request(
    *,
    module,
    target,
    amount,
    maker=None,
    department=None,
    cost_center=None,
    exception_reason="",
    required=True,
    record_submission=True,
):
    """
    Starts (or returns the already pending) approval cycle for ``target``.

    required=True  : no matching policy -> ValidationError (fail closed)
    required=False : no matching policy -> returns None (that transaction needs no approval)
    """
    target_name = target.__class__.__name__
    existing = (
        ApprovalRequest.objects.select_for_update()
        .filter(
            module=module,
            target_model_name=target_name,
            target_object_id=target.pk,
            status=ApprovalRequest.STATUS_PENDING,
        )
        .first()
    )
    if existing:
        return existing

    amount = Decimal(str(amount or 0)).quantize(Decimal("0.01"))
    policy = select_policy(module, amount, department, cost_center)
    if not policy:
        if required:
            raise no_policy_error(module, amount, department, cost_center)
        return None

    steps = _snapshot_steps(policy)
    if not steps:
        raise ValidationError(
            f"Approval policy '{policy.name}' (v{policy.version}) has no approval steps configured."
        )
    exception_policy = None
    exception_reason = (exception_reason or "").strip()
    if exception_reason:
        exception_policy = select_policy(module, amount, department, cost_center, exception=True)
        if exception_policy:
            steps += _snapshot_steps(exception_policy, offset=len(steps), exception=True)

    request = ApprovalRequest.objects.create(
        module=module,
        target_model_name=target_name,
        target_object_id=target.pk,
        policy=policy,
        exception_policy=exception_policy,
        amount=amount,
        department=department,
        cost_center=cost_center,
        exception_reason=exception_reason,
        steps=steps,
        created_by=maker if getattr(maker, "pk", None) else None,
    )
    if record_submission:
        _record_action(
            request,
            actor=maker,
            action=ApprovalAction.ACTION_SUBMIT,
            previous_state="DRAFT",
            new_state="PENDING",
            comments=(
                f"Submitted under policy '{policy.name}' v{policy.version}"
                + (f"; exception route: {exception_reason}" if exception_reason else "")
            ),
        )
    return request


def get_pending_request(module, target):
    return ApprovalRequest.objects.filter(
        module=module,
        target_model_name=target.__class__.__name__,
        target_object_id=target.pk,
        status=ApprovalRequest.STATUS_PENDING,
    ).first()


def get_latest_request(module, target):
    return ApprovalRequest.objects.filter(
        module=module, target_model_name=target.__class__.__name__, target_object_id=target.pk
    ).first()


# ----------------------------------------------------------------------------- authorization
def acting_principals(actor):
    """The actor plus every user the actor is currently an active delegate for."""
    today = timezone.localdate()
    covered = list(
        User.objects.filter(
            delegator_records__delegate=actor,
            delegator_records__is_active=True,
            delegator_records__start_date__lte=today,
            delegator_records__end_date__gte=today,
        ).distinct()
    )
    return [actor] + covered


def _request_org_id(request):
    if request.cost_center_id and request.cost_center.department_id:
        return request.cost_center.department.organization_id
    if request.department_id:
        return request.department.organization_id
    return None


def _is_cost_center_manager(principal, request) -> bool:
    """The manager of the request's cost center may act for it even from another department."""
    if not request.cost_center_id:
        return False
    return request.cost_center.manager_id == principal.pk


def _principal_matches(principal, step, request):
    """True when this principal may take the step. Raises only for the department mismatch."""
    request_org = _request_org_id(request)
    if request_org and principal.organization_id and principal.organization_id != request_org:
        return False
    if step.get("specific_approver_id"):
        return str(principal.id) == step["specific_approver_id"]
    if str(principal.role_id) != step["role_id"]:
        return False
    if (
        step["role_code"] == Role.DEPT_APPROVER
        and request.department_id
        and principal.department_id
        and principal.department_id != request.department_id
        and not _is_cost_center_manager(principal, request)
    ):
        raise ValidationError(
            "Department Approver can only approve requisitions for their own department."
        )
    return True


def authorize_step(request, step, actor):
    """
    Returns the principal whose authority the actor exercises (themselves, or the person they
    are covering as an active delegate). Raises ValidationError when the actor may not take
    this step.
    """
    last_error = None
    for principal in acting_principals(actor):
        try:
            if _principal_matches(principal, step, request):
                return principal
        except ValidationError as exc:
            last_error = exc
    if actor.is_superuser:
        return actor  # administrative override of the role requirement (SoD still applies)
    if last_error:
        raise last_error
    raise ValidationError(
        f"User '{actor.email}' is not authorized to approve step {step['step_number']}. "
        f"Required role: '{step['role_name']}'."
    )


def check_approval_limit(principal, amount):
    # Read the limit fresh: a limit lowered after this user object was loaded must still bind.
    limit = (
        User.objects.filter(pk=principal.pk).values_list("approval_limit", flat=True).first()
        if getattr(principal, "pk", None)
        else getattr(principal, "approval_limit", None)
    )
    if limit is not None and Decimal(str(amount)) > limit:
        raise ValidationError(
            f"Transaction value ${Decimal(str(amount)):,.2f} exceeds the approval limit of "
            f"${limit:,.2f} for {principal.email}. It must be approved by someone with a higher limit."
        )


def user_in_policy_roles(module, user) -> bool:
    """
    Coarse entry gate for approval screens/APIs: could this user ever be asked to decide for
    this module (their role, or a named approval, or someone they cover as delegate, appears in
    an active policy step)? The precise question - may they decide THIS request now - is always
    answered by the engine at decision time; this only decides who may open the screen.
    """
    if not getattr(user, "is_authenticated", False):
        return False
    if user.is_superuser:
        return True
    steps = ApprovalStep.objects.filter(
        policy__module=module, policy__is_active=True, policy__superseded_by__isnull=True
    )
    # also anyone still on the chain of a request that is in flight under an older version
    pending_roles = set()
    for req in ApprovalRequest.objects.filter(module=module, status=ApprovalRequest.STATUS_PENDING):
        pending_roles.update(step["role_id"] for step in req.steps)
    for principal in acting_principals(user):
        if principal.role_id and (
            steps.filter(approver_role_id=principal.role_id).exists()
            or str(principal.role_id) in pending_roles
        ):
            return True
        if steps.filter(specific_approver_id=principal.pk).exists():
            return True
    return False


def can_act(request, actor):
    """Non-raising check used to build inboxes: could this user take the active step now?"""
    step = request.active_step
    if step is None:
        return False
    try:
        principal = authorize_step(request, step, actor)
        check_approval_limit(principal, request.amount)
    except ValidationError:
        return False
    if request.created_by_id == actor.id:
        return False
    return not request.actions.filter(actor=actor, action=ApprovalAction.ACTION_APPROVE).exists()


def step_approvers(request):
    """Users who could approve the active step right now (used for notifications / reminders)."""
    step = request.active_step
    if step is None:
        return []
    qs = User.objects.filter(is_active=True)
    if step.get("specific_approver_id"):
        approvers = list(qs.filter(pk=step["specific_approver_id"]))
        today = timezone.localdate()
        approvers += list(
            qs.filter(
                delegated_records__approver_id=step["specific_approver_id"],
                delegated_records__is_active=True,
                delegated_records__start_date__lte=today,
                delegated_records__end_date__gte=today,
            ).distinct()
        )
        return [u for u in approvers if can_act(request, u)]
    candidates = qs.filter(role_id=step["role_id"])
    if step["role_code"] == Role.DEPT_APPROVER and request.department_id:
        dept = candidates.filter(department_id=request.department_id)
        candidates = dept if dept.exists() else candidates
        manager_id = request.cost_center.manager_id if request.cost_center_id else None
        if manager_id:
            candidates = candidates | qs.filter(pk=manager_id, role_id=step["role_id"])
    return [u for u in candidates if can_act(request, u)]


# ----------------------------------------------------------------------------- decisions
def _record_action(request, *, actor, action, previous_state, new_state, comments, step=None):
    from .services import record_approval_action_service

    recorder = actor if getattr(actor, "pk", None) else request.created_by
    if recorder is None:
        return None  # system-opened request with no human actor to attribute the entry to
    policy_step = None
    if step and step.get("policy_step_id"):
        policy_step = ApprovalStep.objects.filter(pk=step["policy_step_id"]).first()
    return record_approval_action_service(
        target_object_id=request.target_object_id,
        target_model_name=request.target_model_name,
        actor=recorder,
        action=action,
        previous_state=previous_state,
        new_state=new_state,
        policy_step=policy_step,
        comments=comments,
        approval_request=request,
    )


@transaction.atomic
def approve_step(*, request, actor, comments="", states=None):
    """
    Records the actor's approval of the active step and advances the request. When the last
    step is approved the request becomes APPROVED; the calling module then applies its own
    transition. Raises ValidationError when the actor may not approve.

    ``states`` = (previous, new) lets a module label the recorded action with its own status
    names (e.g. MANAGER_REVIEW -> APPROVED); by default the request's PENDING/APPROVED are used.
    """
    request = ApprovalRequest.objects.select_for_update().get(pk=request.pk)
    if request.status != ApprovalRequest.STATUS_PENDING:
        raise ValidationError(
            f"This approval request is already {request.get_status_display().lower()}."
        )
    step = request.active_step
    if step is None:
        raise ValidationError("All approval steps for this request have already been completed.")

    # Role first (a user who may not approve at all is told so), then who made it, then limits.
    principal = authorize_step(request, step, actor)
    if request.created_by_id == actor.id:
        raise ValidationError(
            "Separation of duties violation: Self-approval is strictly prohibited; the person "
            "who submitted this request cannot approve it."
        )
    if request.actions.filter(actor=actor, action=ApprovalAction.ACTION_APPROVE).exists():
        raise ValidationError(
            "You have already approved an earlier step of this request; a different approver "
            "is required for each step (four-eyes principle)."
        )
    check_approval_limit(principal, request.amount)

    note = comments or f"Step {step['step_number']} approved."
    if principal.pk != actor.pk:
        note += f" (acting as delegate for {principal.email})"
    is_final = request.current_step + 1 >= len(request.steps)
    request.current_step += 1
    if is_final:
        request.status = ApprovalRequest.STATUS_APPROVED
        request.decided_at = timezone.now()
    request.save(update_fields=["current_step", "status", "decided_at", "updated_at"])

    _record_action(
        request,
        actor=actor,
        action=ApprovalAction.ACTION_APPROVE,
        previous_state=states[0] if states else "PENDING",
        new_state=states[1] if states else ("APPROVED" if is_final else "PENDING"),
        comments=note,
        step=step,
    )
    return request


@transaction.atomic
def reject_request(*, request, actor, comments, states=None):
    """Any approver on the chain (or a Super Admin) may reject; a justification is mandatory."""
    request = ApprovalRequest.objects.select_for_update().get(pk=request.pk)
    if request.status != ApprovalRequest.STATUS_PENDING:
        raise ValidationError(
            f"This approval request is already {request.get_status_display().lower()}."
        )
    if not (comments or "").strip():
        raise ValidationError("Rejection justification comments are mandatory.")

    allowed = actor.is_superuser
    if not allowed:
        for step in request.steps:
            try:
                authorize_step(request, step, actor)
                allowed = True
                break
            except ValidationError:
                continue
    if not allowed:
        raise ValidationError(f"User '{actor.email}' is not an approver for this request.")

    request.status = ApprovalRequest.STATUS_REJECTED
    request.decided_at = timezone.now()
    request.save(update_fields=["status", "decided_at", "updated_at"])
    _record_action(
        request,
        actor=actor,
        action=ApprovalAction.ACTION_REJECT,
        previous_state=states[0] if states else "PENDING",
        new_state=states[1] if states else "REJECTED",
        comments=comments.strip(),
        step=request.active_step,
    )
    return request


@transaction.atomic
def cancel_pending_request(module, target, *, actor=None, reason="Target cancelled"):
    """Withdraws the pending request for a target that no longer needs approval."""
    request = (
        ApprovalRequest.objects.select_for_update()
        .filter(
            module=module,
            target_model_name=target.__class__.__name__,
            target_object_id=target.pk,
            status=ApprovalRequest.STATUS_PENDING,
        )
        .first()
    )
    if not request:
        return None
    request.status = ApprovalRequest.STATUS_CANCELLED
    request.decided_at = timezone.now()
    request.save(update_fields=["status", "decided_at", "updated_at"])
    AuditLog.objects.create(
        actor=actor if getattr(actor, "pk", None) else None,
        action=AuditLog.ACTION_CANCEL,
        target_model="ApprovalRequest",
        target_object_id=str(request.id),
        previous_state={"status": "PENDING"},
        new_state={"status": "CANCELLED", "reason": reason},
    )
    return request


def pending_requests_for_user(user, *, modules=None):
    """Pending requests whose active step this user could decide right now."""
    qs = ApprovalRequest.objects.filter(status=ApprovalRequest.STATUS_PENDING).select_related(
        "policy", "department", "cost_center", "created_by"
    )
    if modules:
        qs = qs.filter(module__in=modules)
    return [r for r in qs.order_by("created_at") if can_act(r, user)]


# ----------------------------------------------------------------------------- policy versioning
def _validate_steps(steps):
    if not steps:
        raise ValidationError("An approval policy needs at least one approval step.")
    for idx, step in enumerate(steps, start=1):
        if not step.get("approver_role"):
            raise ValidationError(f"Step {idx} needs an approver role.")


@transaction.atomic
def create_policy_with_steps(
    *,
    name,
    module,
    steps,
    min_amount=Decimal("0.00"),
    max_amount=None,
    department=None,
    cost_center=None,
    is_exception_route=False,
    is_active=True,
    actor=None,
    change_note="",
):
    """Creates version 1 of a policy together with its ordered steps."""
    _validate_steps(steps)
    _validate_range(min_amount, max_amount)
    policy = ApprovalPolicy.objects.create(
        name=name,
        module=module,
        min_amount=min_amount,
        max_amount=max_amount,
        department=department,
        cost_center=cost_center,
        is_exception_route=is_exception_route,
        is_active=is_active,
        created_by=actor if getattr(actor, "pk", None) else None,
        change_note=change_note,
    )
    _write_steps(policy, steps)
    _audit_policy(actor, policy, None, "created")
    return policy


def _validate_range(min_amount, max_amount):
    if min_amount is not None and Decimal(str(min_amount)) < 0:
        raise ValidationError("Minimum amount cannot be negative.")
    if (
        max_amount is not None
        and min_amount is not None
        and Decimal(str(min_amount)) > Decimal(str(max_amount))
    ):
        raise ValidationError("Minimum amount cannot be greater than maximum amount.")


def _write_steps(policy, steps):
    for idx, step in enumerate(steps, start=1):
        ApprovalStep.objects.create(
            policy=policy,
            step_number=idx,
            approver_role=step["approver_role"],
            specific_approver=step.get("specific_approver"),
            description=step.get("description", ""),
        )


def _audit_policy(actor, policy, previous, verb):
    AuditLog.objects.create(
        actor=actor if getattr(actor, "pk", None) else None,
        action=AuditLog.ACTION_CREATE if previous is None else AuditLog.ACTION_UPDATE,
        target_model="ApprovalPolicy",
        target_object_id=str(policy.id),
        previous_state=(
            {"version": previous.version, "id": str(previous.id), "is_active": previous.is_active}
            if previous
            else None
        ),
        new_state={
            "event": verb,
            "policy_key": str(policy.policy_key),
            "version": policy.version,
            "module": policy.module,
            "min_amount": str(policy.min_amount),
            "max_amount": None if policy.max_amount is None else str(policy.max_amount),
            "department": str(policy.department_id) if policy.department_id else None,
            "cost_center": str(policy.cost_center_id) if policy.cost_center_id else None,
            "is_exception_route": policy.is_exception_route,
            "is_active": policy.is_active,
            "steps": [
                {
                    "step": s.step_number,
                    "role": s.approver_role.code,
                    "specific_approver": (
                        str(s.specific_approver_id) if s.specific_approver_id else None
                    ),
                }
                for s in policy.steps.select_related("approver_role").order_by("step_number")
            ],
            "change_note": policy.change_note,
        },
    )


_UNSET = object()


@transaction.atomic
def revise_approval_policy(
    *,
    policy,
    actor,
    name=_UNSET,
    min_amount=_UNSET,
    max_amount=_UNSET,
    department=_UNSET,
    cost_center=_UNSET,
    is_exception_route=_UNSET,
    is_active=_UNSET,
    steps=_UNSET,
    change_note="",
):
    """
    "Editing" a policy: creates the next version and supersedes the current one. Requests that
    were routed under the old version are untouched. Only the latest version can be revised.
    """
    policy = ApprovalPolicy.objects.select_for_update().get(pk=policy.pk)
    if policy.superseded_by_id:
        raise ValidationError(
            f"Version {policy.version} of '{policy.name}' has already been superseded; revise the latest version."
        )

    def pick(value, current):
        return current if value is _UNSET else value

    new_min = pick(min_amount, policy.min_amount)
    new_max = pick(max_amount, policy.max_amount)
    _validate_range(new_min, new_max)
    new_steps = steps
    if new_steps is _UNSET:
        new_steps = [
            {
                "approver_role": s.approver_role,
                "specific_approver": s.specific_approver,
                "description": s.description,
            }
            for s in policy.steps.select_related("approver_role", "specific_approver").order_by(
                "step_number"
            )
        ]
    _validate_steps(new_steps)

    new_policy = ApprovalPolicy.objects.create(
        name=pick(name, policy.name),
        module=policy.module,
        min_amount=new_min,
        max_amount=new_max,
        department=pick(department, policy.department),
        cost_center=pick(cost_center, policy.cost_center),
        is_exception_route=pick(is_exception_route, policy.is_exception_route),
        is_active=pick(is_active, policy.is_active),
        policy_key=policy.policy_key,
        version=policy.version + 1,
        created_by=actor if getattr(actor, "pk", None) else None,
        change_note=(change_note or "")[:255],
    )
    _write_steps(new_policy, new_steps)

    # supersede the old row (these are the only fields allowed to change on a used policy)
    ApprovalPolicy.objects.filter(pk=policy.pk).update(
        superseded_by=new_policy, is_active=False, updated_at=timezone.now()
    )
    _audit_policy(actor, new_policy, policy, "revised")
    return new_policy


@transaction.atomic
def deactivate_approval_policy(*, policy, actor, reason=""):
    """Retires a policy (history is kept; requests already routed under it are unaffected)."""
    policy = ApprovalPolicy.objects.select_for_update().get(pk=policy.pk)
    if not policy.is_active:
        return policy
    policy.is_active = False
    policy.change_note = (reason or "Deactivated")[:255]
    policy.save(update_fields=["is_active", "change_note", "updated_at"])
    _audit_policy(actor, policy, policy, "deactivated")
    return policy

import logging
from decimal import Decimal

from django.conf import settings
from django.core.exceptions import ValidationError
from django.db import transaction
from django.db.models import Q, Sum
from django.utils import timezone

from apps.accounts.permissions import forbid_non_transacting_actor
from apps.audit.models import AuditLog
from apps.organization.models import CostCenter, FiscalPeriod

from .models import Budget, BudgetReservation, BudgetTransfer, SpendLedger

logger = logging.getLogger(__name__)

# Which Budget counter each ledger entry type feeds. Amounts are signed per entry.
LEDGER_BUCKETS = {
    SpendLedger.ENTRY_RESERVATION: "reserved_amount",
    SpendLedger.ENTRY_COMMITMENT: "committed_amount",
    SpendLedger.ENTRY_ACTUAL: "actual_amount",
}
CENT = Decimal("0.01")


class BudgetOverrunException(Exception):
    def __init__(self, message, budget=None, requested_amount=None):
        self.message = message
        self.budget = budget
        self.requested_amount = requested_amount
        super().__init__(self.message)


def _assert_period_open(fiscal_period, action):
    if fiscal_period.is_closed:
        raise ValidationError(
            f"Fiscal period '{fiscal_period.name}' is closed; cannot {action}. "
            "Reopen the period to continue."
        )


@transaction.atomic
def allocate_budget_service(
    *,
    cost_center: CostCenter,
    fiscal_period: FiscalPeriod,
    allocated_amount: Decimal,
    allow_overspend: bool = False,
) -> Budget:
    """
    Allocates budget to a CostCenter for a FiscalPeriod inside an atomic transaction.
    """
    _assert_period_open(fiscal_period, "allocate budget")
    budget, created = Budget.objects.get_or_create(
        cost_center=cost_center,
        fiscal_period=fiscal_period,
        defaults={"allocated_amount": allocated_amount, "allow_overspend": allow_overspend},
    )
    if not created:
        budget.allocated_amount = allocated_amount
        budget.allow_overspend = allow_overspend
        budget.save(update_fields=["allocated_amount", "allow_overspend", "updated_at"])
    return budget


def get_budget_for_cost_center(cost_center, *, on_date=None, lock=False) -> Budget:
    """
    Returns the budget of the OPEN fiscal period covering ``on_date`` (default: today).
    Never falls back to "the most recently created budget": spend must land in the period it
    belongs to. Raises ValidationError when no such allocation exists.
    """
    on_date = on_date or timezone.localdate()
    qs = Budget.objects.filter(
        cost_center=cost_center,
        fiscal_period__start_date__lte=on_date,
        fiscal_period__end_date__gte=on_date,
        fiscal_period__is_closed=False,
    )
    if lock:
        qs = qs.select_for_update()
    budget = qs.order_by("-fiscal_period__start_date").first()
    if not budget:
        raise ValidationError(
            f"No active fiscal period budget allocation found for Cost Center '{cost_center.code}'."
        )
    return budget


def budget_headroom(budget: Budget) -> Decimal:
    """Unclamped allocated - (reserved + committed + actual). Negative means overspent."""
    return budget.allocated_amount - (
        budget.reserved_amount + budget.committed_amount + budget.actual_amount
    )


@transaction.atomic
def post_ledger_entry_service(
    *, budget: Budget, entry_type: str, amount, reference_number: str, description: str
):
    """
    The ONLY place that moves money between budget buckets. Locks the budget row, applies the
    signed amount to the matching counter and appends the matching immutable ledger entry in
    the same transaction, so counters can always be re-derived from the ledger.

    A decrease can never take a bucket below zero; if it would, only what is actually held is
    moved (and logged), so ledger and counters stay equal.
    """
    if entry_type not in LEDGER_BUCKETS and entry_type != SpendLedger.ENTRY_TRANSFER:
        raise ValidationError(f"Unknown spend ledger entry type '{entry_type}'.")
    amount = Decimal(str(amount)).quantize(CENT)
    if amount == Decimal("0.00"):
        return None

    locked = Budget.objects.select_for_update().select_related("fiscal_period").get(pk=budget.pk)
    _assert_period_open(locked.fiscal_period, "post ledger entries")
    if entry_type == SpendLedger.ENTRY_TRANSFER:
        # Allocation moves are never clamped: allocated may not drop below what is consumed.
        consumed = locked.reserved_amount + locked.committed_amount + locked.actual_amount
        if locked.allocated_amount + amount < consumed:
            raise ValidationError(
                f"Transfer would reduce the allocation of '{locked.cost_center.code}' to "
                f"${locked.allocated_amount + amount:,.2f}, below the consumed amount "
                f"${consumed:,.2f}."
            )
        locked.allocated_amount = locked.allocated_amount + amount
        locked.save(update_fields=["allocated_amount", "updated_at"])
        entry = SpendLedger.objects.create(
            budget=locked,
            entry_type=entry_type,
            amount=amount,
            reference_number=reference_number,
            description=description[:255],
        )
        budget.allocated_amount = locked.allocated_amount
        budget.updated_at = locked.updated_at
        return entry
    field = LEDGER_BUCKETS[entry_type]
    current = getattr(locked, field)
    if current + amount < Decimal("0.00"):
        logger.warning(
            "Ledger decrease of %s on %s exceeds held %s for budget %s (ref %s); clamping.",
            amount,
            field,
            current,
            locked.pk,
            reference_number,
        )
        amount = -current
        if amount == Decimal("0.00"):
            _sync_instance(budget, locked)
            return None
    setattr(locked, field, current + amount)
    locked.save(update_fields=[field, "updated_at"])
    entry = SpendLedger.objects.create(
        budget=locked,
        entry_type=entry_type,
        amount=amount,
        reference_number=reference_number,
        description=description[:255],
    )
    _sync_instance(budget, locked)
    return entry


def _sync_instance(target: Budget, source: Budget):
    for field in LEDGER_BUCKETS.values():
        setattr(target, field, getattr(source, field))
    target.updated_at = source.updated_at


def reconcile_budget(budget: Budget) -> dict:
    """Compares stored counters with the totals derived from the ledger."""
    totals = {
        row["entry_type"]: row["total"] or Decimal("0.00")
        for row in SpendLedger.objects.filter(budget=budget)
        .values("entry_type")
        .annotate(total=Sum("amount"))
    }
    result = {"budget": budget, "in_sync": True, "buckets": {}}
    for entry_type, field in LEDGER_BUCKETS.items():
        ledger = totals.get(entry_type, Decimal("0.00"))
        stored = getattr(budget, field)
        result["buckets"][field] = {
            "stored": stored,
            "ledger": ledger,
            "drift": stored - ledger,
        }
        if stored != ledger:
            result["in_sync"] = False
    return result


def po_net_commitment(po) -> Decimal:
    """Net commitment ledgered for a PO and its amendments/cancellation (not invoice conversion)."""
    num = po.po_number
    total = (
        SpendLedger.objects.filter(entry_type=SpendLedger.ENTRY_COMMITMENT)
        .filter(
            Q(reference_number=num)
            | Q(reference_number__startswith=f"{num}-AMD")
            | Q(reference_number=f"{num}-CANCEL")
        )
        .aggregate(total=Sum("amount"))["total"]
    )
    return total or Decimal("0.00")


def get_po_budget(po, *, required=True):
    """The budget a PO's commitment was posted to (period-correct), else resolve by period."""
    entry = (
        SpendLedger.objects.filter(
            entry_type=SpendLedger.ENTRY_COMMITMENT, reference_number=po.po_number
        )
        .select_related("budget")
        .order_by("created_at")
        .first()
    )
    if entry:
        return entry.budget
    if not required:
        return None
    return get_budget_for_cost_center(po.cost_center)


def requisition_po_capacity(requisition, *, exclude_po=None) -> dict:
    """
    How much more pre-tax PO value may still be raised against a requisition:
    ceiling = estimate x (1 + tolerance%), minus non-cancelled POs already raised.
    """
    from apps.orders.models import PurchaseOrder

    tolerance = Decimal(str(getattr(settings, "PO_OVER_REQUISITION_TOLERANCE_PCT", "10")))
    ceiling = requisition.total_amount * (Decimal("1.00") + tolerance / Decimal("100"))
    qs = PurchaseOrder.objects.filter(requisition=requisition).exclude(
        status=PurchaseOrder.STATUS_CANCELLED
    )
    if exclude_po is not None:
        qs = qs.exclude(pk=exclude_po.pk)
    already = qs.aggregate(total=Sum("subtotal"))["total"] or Decimal("0.00")
    return {
        "tolerance": tolerance,
        "ceiling": ceiling,
        "already": already,
        "remaining": max(ceiling - already, Decimal("0.00")),
    }


def validate_po_requisition_link(po) -> None:
    """
    A PO raised from a requisition must be backed by an APPROVED requisition on the same cost
    center, and the POs raised against one requisition may not exceed its estimate (plus the
    configured tolerance).
    """
    requisition = po.requisition
    if not requisition:
        return
    from apps.requisitions.models import PurchaseRequisition

    if requisition.status not in (
        PurchaseRequisition.STATUS_APPROVED,
        PurchaseRequisition.STATUS_SOURCING,
        PurchaseRequisition.STATUS_PO_ISSUED,
    ):
        raise ValidationError(
            f"Requisition {requisition.pr_number} is in status '{requisition.status}'. "
            "A purchase order can only be raised against an APPROVED requisition."
        )
    if requisition.cost_center_id != po.cost_center_id:
        raise ValidationError(
            f"Purchase order cost center '{po.cost_center.code}' differs from requisition "
            f"{requisition.pr_number} cost center '{requisition.cost_center.code}'."
        )
    capacity = requisition_po_capacity(requisition, exclude_po=po)
    if capacity["already"] + po.subtotal > capacity["ceiling"]:
        raise ValidationError(
            f"Purchase orders against requisition {requisition.pr_number} would total "
            f"${capacity['already'] + po.subtotal:,.2f} (pre-tax), above the requisition estimate "
            f"of ${requisition.total_amount:,.2f} (+{capacity['tolerance']}% tolerance = "
            f"${capacity['ceiling']:,.2f}). Raise a revised or additional requisition."
        )


def _conversion_plan(po):
    """(reservation, amount) pairs that this PO's commitment will consume from its requisition."""
    plan = []
    if po.requisition_id:
        remaining = po.subtotal
        reservations = (
            BudgetReservation.objects.select_for_update()
            .select_related("budget")
            .filter(requisition=po.requisition, status=BudgetReservation.STATUS_RESERVED)
            .order_by("created_at")
        )
        for res in reservations:
            take = min(res.open_amount, remaining)
            if take > Decimal("0.00"):
                plan.append((res, take))
                remaining -= take
    return plan


def po_budget_shortfall(po) -> Decimal:
    """
    How far committing this PO would exceed its budget (0 when it fits, or when the budget is
    flagged ``allow_overspend``). The reservation the PO would consume is credited first.
    """
    budget = get_budget_for_cost_center(po.cost_center)
    if budget.allow_overspend:
        return Decimal("0.00")
    released = sum(
        (take for res, take in _conversion_plan(po) if res.budget_id == budget.id),
        Decimal("0.00"),
    )
    return max(po.total_amount - (budget_headroom(budget) + released), Decimal("0.00"))


def usable_budget_exception(po, shortfall):
    """An approved, not yet used budget exception on this PO that covers ``shortfall``."""
    from apps.approvals.models import ApprovalPolicy, ApprovalRequest

    return (
        ApprovalRequest.objects.select_for_update()
        .filter(
            module=ApprovalPolicy.MODULE_BUDGET_EXCEPTION,
            target_model_name=po.__class__.__name__,
            target_object_id=po.pk,
            status=ApprovalRequest.STATUS_APPROVED,
            consumed_at__isnull=True,
            amount__gte=shortfall,
        )
        .order_by("created_at")
        .first()
    )


@transaction.atomic
def commit_po_budget_service(*, po, user=None):
    """
    Commits a PO's value against its cost center budget (idempotent per PO):
      1. validates the requisition link (approved, same cost center, within cap);
      2. converts the requisition's reservation into this commitment, so the same spend is
         never counted twice;
      3. refuses the commitment if it would push the budget over its allocation, unless the
         budget is flagged ``allow_overspend`` or an APPROVED, unused budget exception (raised
         through the approval engine, covering the shortfall) exists for this PO - that
         exception is a one-time permission and is consumed here.
    """
    if po_net_commitment(po) > Decimal("0.00"):
        return None  # already committed

    validate_po_requisition_link(po)
    budget = get_budget_for_cost_center(po.cost_center, lock=True)

    plan = _conversion_plan(po)
    released_here = sum((take for res, take in plan if res.budget_id == budget.id), Decimal("0.00"))
    headroom = budget_headroom(budget) + released_here
    exception_grant = None
    if po.total_amount > headroom and not budget.allow_overspend:
        shortfall = po.total_amount - headroom
        exception_grant = usable_budget_exception(po, shortfall)
        if exception_grant is None:
            raise ValidationError(
                f"Insufficient budget for cost center {po.cost_center.code}: PO {po.po_number} needs "
                f"${po.total_amount:,.2f} but only ${max(headroom, Decimal('0.00')):,.2f} is available. "
                "Finance must increase the allocation, or a budget exception of at least "
                f"${shortfall:,.2f} must be requested and approved for this PO first."
            )

    for res, take in plan:
        post_ledger_entry_service(
            budget=res.budget,
            entry_type=SpendLedger.ENTRY_RESERVATION,
            amount=-take,
            reference_number=res.requisition.pr_number,
            description=f"Reservation converted to commitment on PO {po.po_number}",
        )
        res.converted_amount += take
        if res.converted_amount >= res.amount:
            res.status = BudgetReservation.STATUS_COMMITTED
        res.save(update_fields=["converted_amount", "status", "updated_at"])

    entry = post_ledger_entry_service(
        budget=budget,
        entry_type=SpendLedger.ENTRY_COMMITMENT,
        amount=po.total_amount,
        reference_number=po.po_number,
        description=f"PO Commitment for {po.vendor.legal_name}",
    )
    if exception_grant is not None:
        exception_grant.consumed_at = timezone.now()
        exception_grant.save(update_fields=["consumed_at", "updated_at"])
    AuditLog.objects.create(
        actor=user,
        action=AuditLog.ACTION_UPDATE,
        target_model="Budget",
        target_object_id=str(budget.id),
        new_state={
            "po_number": po.po_number,
            "committed": str(po.total_amount),
            "reservation_converted": str(sum((t for _, t in plan), Decimal("0.00"))),
            "budget_exception_used": str(exception_grant.id) if exception_grant else None,
        },
    )
    return entry


def validate_budget_availability_service(*, cost_center: CostCenter, amount: Decimal, today=None):
    """
    Validates if a cost center has sufficient available budget for an amount.
    Raises ValidationError if insufficient budget.
    """
    if not today:
        from django.utils import timezone

        today = timezone.now().date()

    budget = Budget.objects.filter(
        cost_center=cost_center,
        fiscal_period__start_date__lte=today,
        fiscal_period__end_date__gte=today,
        fiscal_period__is_closed=False,
    ).first()

    if not budget:
        raise ValidationError(
            f"No active fiscal period budget allocation found for Cost Center '{cost_center.code}'."
        )

    if amount > budget.available_amount and not budget.allow_overspend:
        raise ValidationError(
            f"Insufficient budget in Cost Center '{cost_center.code}'. "
            f"Requested: ${amount:,.2f}, Available: ${budget.available_amount:,.2f}."
        )

    return budget


def pr_budget_position(requisition) -> dict:
    """
    Read-only (soft) budget check for a purchase requisition - nothing is reserved. Returns the
    figures of the budget covering the PR's cost center and whether the PR total fits.

    ``within_budget`` is True when the total fits the available amount (or the budget allows
    overspend); ``shortfall`` is the amount above what is available (0 when within budget).
    ``budget`` is None when no open-period allocation exists (then ``within_budget`` is False).
    """
    today = (
        requisition.created_at.date()
        if requisition.created_at
        else requisition.requested_delivery_date
    )
    budget = (
        Budget.objects.filter(
            cost_center=requisition.cost_center,
            fiscal_period__start_date__lte=today,
            fiscal_period__end_date__gte=today,
            fiscal_period__is_closed=False,
        )
        .select_related("fiscal_period")
        .first()
    )
    total = requisition.total_amount
    if budget is None:
        return {
            "budget": None,
            "total": total,
            "allocated": Decimal("0.00"),
            "reserved": Decimal("0.00"),
            "committed": Decimal("0.00"),
            "available": Decimal("0.00"),
            "within_budget": False,
            "status": "OUT_OF_BUDGET",
            "shortfall": total,
        }
    available = budget.available_amount
    within = budget.allow_overspend or total <= available
    return {
        "budget": budget,
        "total": total,
        "allocated": budget.allocated_amount,
        "reserved": budget.reserved_amount,
        "committed": budget.committed_amount,
        "available": available,
        "within_budget": within,
        "status": "WITHIN_BUDGET" if within else "OUT_OF_BUDGET",
        "shortfall": Decimal("0.00") if within else max(total - available, Decimal("0.00")),
    }


@transaction.atomic
def check_and_reserve_budget_service(
    *, requisition, requested_by_user, force_overrun=False
) -> BudgetReservation:
    """
    Checks available budget for a PurchaseRequisition's cost center.
    If available (or forced), locks a BudgetReservation in RESERVED status and writes a SpendLedger entry.
    Raises BudgetOverrunException if insufficient funds.
    """
    cost_center = requisition.cost_center
    today = (
        requisition.created_at.date()
        if requisition.created_at
        else requisition.requested_delivery_date
    )

    budget = (
        Budget.objects.select_for_update()
        .filter(
            cost_center=cost_center,
            fiscal_period__start_date__lte=today,
            fiscal_period__end_date__gte=today,
            fiscal_period__is_closed=False,
        )
        .first()
    )

    if not budget:
        raise ValidationError(
            f"No active fiscal period budget allocation found for Cost Center '{cost_center.code}'."
        )

    amount = requisition.total_amount
    if amount > budget.available_amount and not budget.allow_overspend and not force_overrun:
        raise BudgetOverrunException(
            f"Insufficient budget in Cost Center '{cost_center.code}'. "
            f"Requested: ${amount:,.2f}, Available: ${budget.available_amount:,.2f}.",
            budget=budget,
            requested_amount=amount,
        )

    # Create BudgetReservation and post it through the single ledger choke point
    reservation = BudgetReservation.objects.create(
        budget=budget,
        requisition=requisition,
        amount=amount,
        status=BudgetReservation.STATUS_RESERVED,
    )
    post_ledger_entry_service(
        budget=budget,
        entry_type=SpendLedger.ENTRY_RESERVATION,
        amount=amount,
        reference_number=requisition.pr_number,
        description=f"PR Budget Reservation: {requisition.title}",
    )

    AuditLog.objects.create(
        actor=requested_by_user,
        action=AuditLog.ACTION_CREATE,
        target_model="BudgetReservation",
        target_object_id=str(reservation.id),
        new_state={
            "amount": str(amount),
            "pr_number": requisition.pr_number,
            "cost_center": cost_center.code,
        },
    )

    return reservation


@transaction.atomic
def convert_commitment_to_actual_service(*, po, invoice, user) -> SpendLedger:
    """
    Moves spend from COMMITMENT to ACTUAL when an invoice is approved/paid.
    """
    budget = get_po_budget(po, required=False)
    if not budget:
        try:
            budget = get_budget_for_cost_center(po.cost_center)
        except ValidationError:
            raise ValidationError(f"No budget found for Cost Center '{po.cost_center.code}'.")

    amount = invoice.total_amount
    # Relieve the commitment (never below what is held), then book the actual spend.
    post_ledger_entry_service(
        budget=budget,
        entry_type=SpendLedger.ENTRY_COMMITMENT,
        amount=-amount,
        reference_number=invoice.invoice_number,
        description=f"Commitment relieved by invoice for PO {po.po_number}",
    )
    ledger_entry = post_ledger_entry_service(
        budget=budget,
        entry_type=SpendLedger.ENTRY_ACTUAL,
        amount=amount,
        reference_number=invoice.invoice_number,
        description=f"Actual Invoice Spend for PO {po.po_number}",
    )

    AuditLog.objects.create(
        actor=user,
        action=AuditLog.ACTION_UPDATE,
        target_model="Budget",
        target_object_id=str(budget.id),
        new_state={
            "actual_amount": str(budget.actual_amount),
            "invoice_number": invoice.invoice_number,
            "po_number": po.po_number,
        },
    )

    return ledger_entry


@transaction.atomic
def release_budget_reservation_service(
    *, requisition, user, reason: str = "Requisition Rejected/Cancelled"
) -> None:
    """
    Releases active budget reservations for a rejected or cancelled PurchaseRequisition.
    Deducts reserved_amount from Budget, updates BudgetReservation status to RELEASED,
    and logs AuditLog event.
    """
    reservations = BudgetReservation.objects.select_for_update().filter(
        requisition=requisition, status=BudgetReservation.STATUS_RESERVED
    )
    for res in reservations:
        # Only the part not already converted into a PO commitment is still held.
        post_ledger_entry_service(
            budget=res.budget,
            entry_type=SpendLedger.ENTRY_RESERVATION,
            amount=-res.open_amount,
            reference_number=requisition.pr_number,
            description=f"Reservation released: {reason}",
        )

        res.status = BudgetReservation.STATUS_RELEASED
        res.save(update_fields=["status", "updated_at"])

        AuditLog.objects.create(
            actor=user,
            action=AuditLog.ACTION_UPDATE,
            target_model="BudgetReservation",
            target_object_id=str(res.id),
            new_state={"status": res.status, "reason": reason},
        )


# ==============================================================================
# BUDGET TRANSFERS (Finance requests; approval engine decides; ledger posts)
# ==============================================================================


def _role_code(user):
    return getattr(user, "role_code", None) if user else None


def _budget_org(budget):
    dept = budget.cost_center.department
    return dept.organization if dept else None


def _assert_transfer_feasible(source: Budget, destination: Budget, amount: Decimal):
    consumed = source.reserved_amount + source.committed_amount + source.actual_amount
    if source.allocated_amount - amount < consumed:
        raise ValidationError(
            f"Transfer of ${amount:,.2f} would drop '{source.cost_center.code}' below its "
            f"consumed amount (allocated ${source.allocated_amount:,.2f}, consumed "
            f"${consumed:,.2f}, transferable ${max(source.allocated_amount - consumed, Decimal('0.00')):,.2f})."
        )
    _assert_period_open(source.fiscal_period, "transfer budget")


@transaction.atomic
def request_budget_transfer_service(
    *, source: Budget, destination: Budget, amount, reason: str, requested_by
) -> BudgetTransfer:
    """Finance requests a move of allocated budget; routed through the approval engine."""
    forbid_non_transacting_actor(requested_by, "request budget transfers")
    from apps.approvals import engine
    from apps.approvals.models import ApprovalPolicy

    if _role_code(requested_by) != "FINANCE_AP":
        raise ValidationError("Only Finance can request budget transfers.")
    try:
        amount = Decimal(str(amount)).quantize(CENT)
    except Exception:
        raise ValidationError("Transfer amount must be a valid number.")
    if amount <= Decimal("0.00"):
        raise ValidationError("Transfer amount must be greater than zero.")
    if not (reason or "").strip():
        raise ValidationError("A reason is required for a budget transfer.")
    if source.pk == destination.pk:
        raise ValidationError("Source and destination budgets must differ.")

    source = (
        Budget.objects.select_for_update()
        .select_related("cost_center__department", "fiscal_period")
        .get(pk=source.pk)
    )
    destination = Budget.objects.select_related("cost_center__department", "fiscal_period").get(
        pk=destination.pk
    )
    if source.fiscal_period_id != destination.fiscal_period_id:
        raise ValidationError("Transfers are only allowed within the same fiscal period.")
    src_org, dst_org = _budget_org(source), _budget_org(destination)
    user_org = getattr(requested_by, "effective_organization", None)
    if src_org is None or dst_org is None or src_org.pk != dst_org.pk:
        raise ValidationError(
            "Transfers are only allowed between cost centers of one organization."
        )
    if user_org is None or user_org.pk != src_org.pk:
        raise ValidationError("You can only transfer budget inside your own organization.")
    _assert_transfer_feasible(source, destination, amount)

    transfer = BudgetTransfer.objects.create(
        source=source,
        destination=destination,
        amount=amount,
        reason=reason.strip(),
        requested_by=requested_by,
        created_by=requested_by,
    )
    engine.open_approval_request(
        module=ApprovalPolicy.MODULE_BUDGET_TRANSFER,
        target=transfer,
        amount=amount,
        maker=requested_by,
        department=source.cost_center.department,
        cost_center=source.cost_center,
        exception_reason="",
        required=True,
    )
    from apps.audit.services import create_audit_log_service

    create_audit_log_service(
        actor=requested_by,
        action=AuditLog.ACTION_SUBMIT,
        target_model="BudgetTransfer",
        target_object_id=transfer.id,
        new_state={
            "source": source.cost_center.code,
            "destination": destination.cost_center.code,
            "amount": str(amount),
            "reason": transfer.reason,
        },
    )
    return transfer


def _notify_transfer_approvers(transfer, request):
    from apps.approvals import engine
    from apps.notifications.models import Notification
    from apps.notifications.services import notify_users

    notify_users(
        users=engine.step_approvers(request),
        notification_type=Notification.TYPE_APPROVAL_REQUIRED,
        title="Budget transfer approval required",
        message=(
            f"Transfer ${transfer.amount:,.2f} from {transfer.source.cost_center.code} to "
            f"{transfer.destination.cost_center.code}. Reason: {transfer.reason[:300]}"
        ),
        target_url="/budgets/transfers/",
    )


@transaction.atomic
def approve_budget_transfer_service(
    *, transfer: BudgetTransfer, approver, comments: str = ""
) -> BudgetTransfer:
    """Engine approval; on the final step the allocation moves via the ledger choke point."""
    forbid_non_transacting_actor(approver, "approve budget transfers")
    from apps.approvals import engine
    from apps.approvals.models import ApprovalPolicy
    from apps.audit.services import create_audit_log_service

    transfer = BudgetTransfer.objects.select_for_update().get(pk=transfer.pk)
    if transfer.status != BudgetTransfer.STATUS_PENDING:
        raise ValidationError(f"This transfer is already {transfer.status.lower()}.")
    request = engine.get_pending_request(ApprovalPolicy.MODULE_BUDGET_TRANSFER, transfer)
    if request is None:
        raise ValidationError("No pending approval request exists for this transfer.")
    is_final = request.current_step + 1 >= len(request.steps)
    if is_final:
        # Re-check feasibility under lock first: spend may have grown since the request.
        source = (
            Budget.objects.select_for_update()
            .select_related("cost_center", "fiscal_period")
            .get(pk=transfer.source_id)
        )
        _assert_transfer_feasible(source, transfer.destination, transfer.amount)
    request = engine.approve_step(
        request=request,
        actor=approver,
        comments=comments,
        states=("PENDING", "APPROVED" if is_final else "PENDING"),
    )
    if request.status != "APPROVED":
        _notify_transfer_approvers(transfer, request)
        return transfer

    ref = f"TRF-{str(transfer.id)[:8].upper()}"
    label = f"{transfer.source.cost_center.code} -> {transfer.destination.cost_center.code}"
    post_ledger_entry_service(
        budget=transfer.source,
        entry_type=SpendLedger.ENTRY_TRANSFER,
        amount=-transfer.amount,
        reference_number=ref,
        description=f"Budget transfer out {label}",
    )
    post_ledger_entry_service(
        budget=transfer.destination,
        entry_type=SpendLedger.ENTRY_TRANSFER,
        amount=transfer.amount,
        reference_number=ref,
        description=f"Budget transfer in {label}",
    )
    transfer.status = BudgetTransfer.STATUS_APPROVED
    transfer.decided_by = approver
    transfer.decided_at = timezone.now()
    transfer.decision_comments = (comments or "").strip()
    transfer.save()
    create_audit_log_service(
        actor=approver,
        action=AuditLog.ACTION_APPROVE,
        target_model="BudgetTransfer",
        target_object_id=transfer.id,
        previous_state={"status": BudgetTransfer.STATUS_PENDING},
        new_state={"status": transfer.status, "amount": str(transfer.amount), "route": label},
    )
    return transfer


@transaction.atomic
def reject_budget_transfer_service(
    *, transfer: BudgetTransfer, approver, comments: str
) -> BudgetTransfer:
    forbid_non_transacting_actor(approver, "reject budget transfers")
    from apps.approvals import engine
    from apps.approvals.models import ApprovalPolicy
    from apps.audit.services import create_audit_log_service

    transfer = BudgetTransfer.objects.select_for_update().get(pk=transfer.pk)
    if transfer.status != BudgetTransfer.STATUS_PENDING:
        raise ValidationError(f"This transfer is already {transfer.status.lower()}.")
    request = engine.get_pending_request(ApprovalPolicy.MODULE_BUDGET_TRANSFER, transfer)
    if request is None:
        raise ValidationError("No pending approval request exists for this transfer.")
    engine.reject_request(
        request=request, actor=approver, comments=comments, states=("PENDING", "REJECTED")
    )
    transfer.status = BudgetTransfer.STATUS_REJECTED
    transfer.decided_by = approver
    transfer.decided_at = timezone.now()
    transfer.decision_comments = (comments or "").strip()
    transfer.save()
    create_audit_log_service(
        actor=approver,
        action=AuditLog.ACTION_REJECT,
        target_model="BudgetTransfer",
        target_object_id=transfer.id,
        previous_state={"status": BudgetTransfer.STATUS_PENDING},
        new_state={"status": transfer.status, "comments": transfer.decision_comments},
    )
    return transfer

"""PO approval, PO budget exceptions and PO amendments on the approval engine (group D)."""

import datetime
from decimal import Decimal

import pytest
from django.core.exceptions import ValidationError

from apps.accounts.models import Role, User
from apps.approvals import engine
from apps.approvals.models import ApprovalPolicy, ApprovalRequest
from apps.budgets.models import Budget
from apps.budgets.services import reconcile_budget
from apps.orders.models import POAmendment, PurchaseOrder
from apps.orders.services import (
    approve_po_amendment_service,
    approve_purchase_order_service,
    cancel_purchase_order_governance_service,
    decide_po_budget_exception_service,
    generate_purchase_order_service,
    reject_po_amendment_service,
    reject_purchase_order_service,
    request_po_amendment_service,
    request_po_budget_exception_service,
    submit_purchase_order_for_approval_service,
)
from apps.requisitions.models import PurchaseRequisition
from tests.test_group_c_lifecycle import D, make_pr, world  # noqa: F401  (shared fixture)

pytestmark = pytest.mark.django_db

PO = ApprovalPolicy.MODULE_PO
AMD = ApprovalPolicy.MODULE_PO_AMENDMENT
BX = ApprovalPolicy.MODULE_BUDGET_EXCEPTION


def extra_users(w):
    org = w["org"]
    roles = w["db_roles"]
    return {
        "mgr2": User.objects.create_user(
            email="mgr2@d.test",
            password="Password123!",
            role=roles[Role.PROC_MGR],
            organization=org,
        ),
        "fin2": User.objects.create_user(
            email="fin2@d.test",
            password="Password123!",
            role=roles[Role.FINANCE_AP],
            organization=org,
        ),
    }


def policy(w, name, roles, module=PO, **kw):
    return engine.create_policy_with_steps(
        name=name, module=module, steps=[{"approver_role": w["db_roles"][r]} for r in roles], **kw
    )


def make_po(w, amount=1000, pr=None, **kw):
    return generate_purchase_order_service(
        vendor=w["vendor"],
        cost_center=w["cc"],
        requisition=pr,
        created_by_user=w["pe"],
        line_items=[{"item_description": "Item", "quantity": 1, "unit_price": amount}],
        **kw,
    )


def committed(w):
    return Budget.objects.get(cost_center=w["cc"]).committed_amount


# ======================================================================= PO approval
def test_without_a_po_policy_pos_are_issued_immediately_as_before(world):  # noqa: F811
    w = world
    po = make_po(w)
    assert po.status == PurchaseOrder.STATUS_ISSUED and committed(w) == D("1100.00")
    assert not ApprovalRequest.objects.filter(module=PO).exists()


def test_without_a_po_policy_a_draft_po_still_needs_a_manager(world):  # noqa: F811
    w = world
    po = make_po(w, initial_status=PurchaseOrder.STATUS_DRAFT)
    with pytest.raises(ValidationError, match="Only Procurement Managers can approve"):
        approve_purchase_order_service(po=po, approver=w["pe"])
    assert (
        approve_purchase_order_service(po=po, approver=w["mgr"]).status
        == PurchaseOrder.STATUS_ISSUED
    )


def test_a_matching_po_policy_holds_the_po_in_approval_until_the_chain_completes(
    world,
):  # noqa: F811
    w = world
    policy(w, "PO chain", [Role.PROC_MGR, Role.FINANCE_AP])
    po = make_po(w)
    assert po.status == PurchaseOrder.STATUS_APPROVAL
    assert committed(w) == D(0)  # nothing is committed until the PO is actually issued
    req = ApprovalRequest.objects.get(module=PO, target_object_id=po.id)
    assert [s["role_code"] for s in req.steps] == ["PROC_MGR", "FINANCE_AP"]

    with pytest.raises(ValidationError, match="not authorized to approve step 1"):
        approve_purchase_order_service(po=po, approver=w["finance"])  # wrong order/role
    po = approve_purchase_order_service(po=po, approver=w["mgr"])
    assert po.status == PurchaseOrder.STATUS_APPROVAL and committed(w) == D(0)  # half way
    po = approve_purchase_order_service(po=po, approver=w["finance"])
    assert po.status == PurchaseOrder.STATUS_ISSUED and committed(w) == D("1100.00")
    assert ApprovalRequest.objects.get(pk=req.pk).status == "APPROVED"
    assert reconcile_budget(Budget.objects.get(cost_center=w["cc"]))["in_sync"]


def test_po_creator_cannot_approve_their_own_po(world):  # noqa: F811
    w = world
    policy(w, "PO by execs", [Role.PROC_EXEC])
    po = make_po(w)  # created by w["pe"]
    with pytest.raises(ValidationError, match="Self-approval"):
        approve_purchase_order_service(po=po, approver=w["pe"])


def test_po_amount_tiers_only_large_orders_need_approval(world):  # noqa: F811
    w = world
    policy(w, "Large POs", [Role.PROC_MGR], min_amount=D(5000))
    small = make_po(w, 1000)
    large = make_po(w, 6000)
    assert small.status == PurchaseOrder.STATUS_ISSUED
    assert large.status == PurchaseOrder.STATUS_APPROVAL


def test_po_above_its_requisition_estimate_takes_the_exception_route(world, settings):  # noqa: F811
    settings.PO_OVER_REQUISITION_TOLERANCE_PCT = "50"
    w = world
    policy(w, "PO std", [Role.PROC_MGR])
    policy(w, "PO exception", [Role.FINANCE_AP], is_exception_route=True)
    pr = make_pr(w, 10000)
    within = make_po(w, 10000, pr=pr)
    assert [
        s["role_code"]
        for s in ApprovalRequest.objects.get(module=PO, target_object_id=within.id).steps
    ] == ["PROC_MGR"]
    cancel_purchase_order_governance_service(po=within, actor=w["mgr"], reason="redo")

    over = make_po(w, 12000, pr=pr)
    req = ApprovalRequest.objects.get(module=PO, target_object_id=over.id)
    assert [s["role_code"] for s in req.steps] == ["PROC_MGR", "FINANCE_AP"]
    assert "exceeds requisition estimate" in req.exception_reason


def test_submitting_a_draft_po_routes_it_and_rejection_returns_it_to_draft(world):  # noqa: F811
    w = world
    policy(w, "PO std", [Role.PROC_MGR])
    po = make_po(w, initial_status=PurchaseOrder.STATUS_DRAFT)
    assert not ApprovalRequest.objects.filter(module=PO).exists()
    submit_purchase_order_for_approval_service(po=po, actor=w["pe"])
    req = ApprovalRequest.objects.get(module=PO, target_object_id=po.id)
    assert req.status == "PENDING"
    with pytest.raises(ValidationError, match="not an approver"):
        reject_purchase_order_service(po=po, approver=w["requester"], reason="no")
    po = reject_purchase_order_service(po=po, approver=w["mgr"], reason="Needs a better price")
    assert po.status == PurchaseOrder.STATUS_DRAFT
    assert ApprovalRequest.objects.get(pk=req.pk).status == "REJECTED"


def test_cancelling_a_po_withdraws_its_pending_approvals(world):  # noqa: F811
    w = world
    policy(w, "PO std", [Role.PROC_MGR])
    po = make_po(w)
    cancel_purchase_order_governance_service(po=po, actor=w["mgr"], reason="Not needed anymore")
    assert not ApprovalRequest.objects.filter(status="PENDING", target_object_id=po.id).exists()


def test_po_approval_limit_applies(world):  # noqa: F811
    w = world
    policy(w, "PO std", [Role.PROC_MGR])
    po = make_po(w, 8000)
    User.objects.filter(pk=w["mgr"].pk).update(approval_limit=D(5000))
    with pytest.raises(ValidationError, match="approval limit"):
        approve_purchase_order_service(po=po, approver=w["mgr"])
    assert PurchaseOrder.objects.get(pk=po.pk).status == PurchaseOrder.STATUS_APPROVAL


# ======================================================================= PO budget exception
def tight_budget(w, allocated=2000):
    Budget.objects.filter(cost_center=w["cc"]).update(allocated_amount=D(allocated))


def test_an_overspending_po_needs_an_approved_budget_exception_and_uses_it_once(
    world,
):  # noqa: F811
    w = world
    tight_budget(w, 2000)
    with pytest.raises(ValidationError, match="budget exception"):
        make_po(w, 5000)  # 5,500 against 2,000 -> refused outright

    draft = make_po(w, 5000, initial_status=PurchaseOrder.STATUS_DRAFT)
    with pytest.raises(ValidationError, match="at least 10 characters"):
        request_po_budget_exception_service(po=draft, requested_by=w["pe"], reason="short")
    req = request_po_budget_exception_service(
        po=draft, requested_by=w["pe"], reason="Critical hardware replacement before audit"
    )
    assert req.amount == D("3500.00") and [s["role_code"] for s in req.steps] == ["FINANCE_AP"]

    # no exception yet -> still refused
    with pytest.raises(ValidationError, match="Insufficient budget"):
        approve_purchase_order_service(po=draft, approver=w["mgr"])

    with pytest.raises(ValidationError, match="not authorized"):
        decide_po_budget_exception_service(po=draft, actor=w["mgr"], approve=True)
    decide_po_budget_exception_service(
        po=draft, actor=w["finance"], approve=True, comments="Granted"
    )
    req.refresh_from_db()
    assert req.status == "APPROVED" and req.consumed_at is None

    issued = approve_purchase_order_service(po=draft, approver=w["mgr"])
    assert issued.status == PurchaseOrder.STATUS_ISSUED and committed(w) == D("5500.00")
    req.refresh_from_db()
    assert req.consumed_at is not None  # one-time permission


def test_a_budget_exception_is_for_that_po_and_that_amount_only(world):  # noqa: F811
    w = world
    tight_budget(w, 2000)
    small = make_po(w, 5000, initial_status=PurchaseOrder.STATUS_DRAFT)  # shortfall 3,500
    request_po_budget_exception_service(
        po=small, requested_by=w["pe"], reason="Granted for small order"
    )
    decide_po_budget_exception_service(po=small, actor=w["finance"], approve=True)

    other = make_po(w, 5000, initial_status=PurchaseOrder.STATUS_DRAFT)
    with pytest.raises(ValidationError, match="Insufficient budget"):
        approve_purchase_order_service(po=other, approver=w["mgr"])  # not covered by small's grant

    # growing the PO after the grant (shortfall now bigger than approved) invalidates it
    from apps.orders.models import POLine

    line = small.lines.first()
    POLine.objects.filter(pk=line.pk).update(unit_price=D(9000))
    PurchaseOrder.objects.filter(pk=small.pk).update(
        subtotal=D(9000), tax_amount=D(900), total_amount=D(9900)
    )
    small.refresh_from_db()
    with pytest.raises(ValidationError, match="Insufficient budget"):
        approve_purchase_order_service(po=small, approver=w["mgr"])


def test_rejected_budget_exception_leaves_the_po_blocked(world):  # noqa: F811
    w = world
    tight_budget(w, 2000)
    draft = make_po(w, 5000, initial_status=PurchaseOrder.STATUS_DRAFT)
    request_po_budget_exception_service(
        po=draft, requested_by=w["pe"], reason="Please allow this order"
    )
    with pytest.raises(ValidationError, match="mandatory"):
        decide_po_budget_exception_service(
            po=draft, actor=w["finance"], approve=False, comments=" "
        )
    decide_po_budget_exception_service(
        po=draft, actor=w["finance"], approve=False, comments="No funds"
    )
    with pytest.raises(ValidationError, match="Insufficient budget"):
        approve_purchase_order_service(po=draft, approver=w["mgr"])
    with pytest.raises(ValidationError, match="no pending budget exception"):
        decide_po_budget_exception_service(po=draft, actor=w["finance"], approve=True)


def test_no_budget_exception_for_a_po_that_fits_or_is_already_issued(world):  # noqa: F811
    w = world
    fits = make_po(w, 100, initial_status=PurchaseOrder.STATUS_DRAFT)
    with pytest.raises(ValidationError, match="fits within its budget"):
        request_po_budget_exception_service(
            po=fits, requested_by=w["pe"], reason="Not really needed"
        )
    issued = make_po(w, 100)
    with pytest.raises(ValidationError, match="not been issued"):
        request_po_budget_exception_service(
            po=issued, requested_by=w["pe"], reason="Not really needed"
        )


def test_budget_exception_policy_can_require_several_approvers(world):  # noqa: F811
    w = world
    seeded = ApprovalPolicy.objects.get(
        module=BX, department__isnull=True, cost_center__isnull=True
    )
    engine.revise_approval_policy(
        policy=seeded,
        actor=w["admin"],
        steps=[
            {"approver_role": w["db_roles"][Role.FINANCE_AP]},
            {"approver_role": w["db_roles"][Role.PROC_MGR]},
        ],
    )
    tight_budget(w, 2000)
    draft = make_po(w, 5000, initial_status=PurchaseOrder.STATUS_DRAFT)
    request_po_budget_exception_service(
        po=draft, requested_by=w["pe"], reason="Two signatures needed"
    )
    decide_po_budget_exception_service(po=draft, actor=w["finance"], approve=True)
    with pytest.raises(ValidationError, match="Insufficient budget"):
        approve_purchase_order_service(po=draft, approver=w["mgr"])  # only half approved
    decide_po_budget_exception_service(po=draft, actor=w["mgr"], approve=True)
    assert (
        approve_purchase_order_service(po=draft, approver=extra_users(w)["mgr2"]).status == "ISSUED"
    )


# ======================================================================= amendments
def amend(w, po, price=2000, by=None):
    return request_po_amendment_service(
        po=po,
        reason="Price change",
        requested_by_user=by or w["pe"],
        updated_line_items=[{"item_description": "Item", "quantity": 1, "unit_price": price}],
    )


def acknowledged(w):
    po = make_po(w, 1000)
    PurchaseOrder.objects.filter(pk=po.pk).update(status=PurchaseOrder.STATUS_ACKNOWLEDGED)
    po.refresh_from_db()
    return po


def test_amendment_is_routed_through_a_multi_step_policy_and_applied_only_at_the_end(
    world,
):  # noqa: F811
    w = world
    seeded = ApprovalPolicy.objects.get(
        module=AMD, department__isnull=True, cost_center__isnull=True
    )
    engine.revise_approval_policy(
        policy=seeded,
        actor=w["admin"],
        steps=[
            {"approver_role": w["db_roles"][Role.PROC_MGR]},
            {"approver_role": w["db_roles"][Role.FINANCE_AP]},
        ],
    )
    po = acknowledged(w)
    amendment = amend(w, po)
    req = ApprovalRequest.objects.get(module=AMD, target_object_id=amendment.id)
    assert req.amount == D("1100.00") and len(req.steps) == 2

    po = approve_po_amendment_service(amendment=amendment, approver=w["mgr"])
    assert po.status == PurchaseOrder.STATUS_AMENDMENT_PENDING and po.version == 1
    assert committed(w) == D("1100.00")  # nothing applied yet
    amendment.refresh_from_db()
    assert amendment.status == POAmendment.STATUS_PENDING

    po = approve_po_amendment_service(amendment=amendment, approver=w["finance"])
    assert po.version == 2 and po.status == PurchaseOrder.STATUS_ISSUED
    assert committed(w) == D("2200.00")
    amendment.refresh_from_db()
    assert amendment.status == POAmendment.STATUS_APPROVED and amendment.approved_by == w["finance"]


def test_amendment_without_any_policy_is_refused_and_leaves_no_trace(world):  # noqa: F811
    w = world
    po = acknowledged(w)
    ApprovalPolicy.objects.filter(module=AMD).update(is_active=False)
    with pytest.raises(ValidationError, match="No active approval policy found"):
        amend(w, po)
    po.refresh_from_db()
    assert po.status == PurchaseOrder.STATUS_ACKNOWLEDGED and not po.amendments.exists()


def test_cost_center_amendment_policy_overrides_the_global_one(world):  # noqa: F811
    w = world
    policy(w, "CC amendments", [Role.FINANCE_AP], module=AMD, cost_center=w["cc"])
    amendment = amend(w, acknowledged(w))
    req = ApprovalRequest.objects.get(module=AMD, target_object_id=amendment.id)
    assert [s["role_code"] for s in req.steps] == ["FINANCE_AP"]
    with pytest.raises(ValidationError, match="not authorized"):
        approve_po_amendment_service(amendment=amendment, approver=w["mgr"])
    assert approve_po_amendment_service(amendment=amendment, approver=w["finance"]).version == 2


def test_amendment_requester_and_limits_are_enforced_by_the_engine(world):  # noqa: F811
    w = world
    po = acknowledged(w)
    amendment = amend(w, po, price=20000, by=w["mgr"])
    with pytest.raises(ValidationError, match="Self-approval"):
        approve_po_amendment_service(amendment=amendment, approver=w["mgr"])
    mgr2 = extra_users(w)["mgr2"]
    User.objects.filter(pk=mgr2.pk).update(approval_limit=D(1000))
    with pytest.raises(ValidationError, match="approval limit"):
        approve_po_amendment_service(amendment=amendment, approver=mgr2)
    mgr3 = User.objects.create_user(
        email="mgr3@d.test",
        password="Password123!",
        role=w["db_roles"][Role.PROC_MGR],
        organization=w["org"],
    )
    assert approve_po_amendment_service(amendment=amendment, approver=mgr3).version == 2


def test_rejecting_an_amendment_needs_an_approver_and_ends_its_request(world):  # noqa: F811
    w = world
    po = acknowledged(w)
    amendment = amend(w, po)
    with pytest.raises(ValidationError, match="not an approver"):
        reject_po_amendment_service(amendment=amendment, rejector=w["requester"], reason="no")
    reject_po_amendment_service(amendment=amendment, rejector=w["mgr"], reason="Too expensive")
    amendment.refresh_from_db()
    assert amendment.status == POAmendment.STATUS_REJECTED
    assert (
        ApprovalRequest.objects.get(module=AMD, target_object_id=amendment.id).status == "REJECTED"
    )
    assert PurchaseOrder.objects.get(pk=po.pk).status == PurchaseOrder.STATUS_ACKNOWLEDGED


def test_cancelling_a_po_closes_its_pending_amendment_and_request(world):  # noqa: F811
    w = world
    po = acknowledged(w)
    amendment = amend(w, po)
    cancel_purchase_order_governance_service(
        po=PurchaseOrder.objects.get(pk=po.pk), actor=w["mgr"], reason="Vendor insolvent"
    )
    amendment.refresh_from_db()
    assert amendment.status == POAmendment.STATUS_REJECTED
    assert (
        ApprovalRequest.objects.get(module=AMD, target_object_id=amendment.id).status == "CANCELLED"
    )

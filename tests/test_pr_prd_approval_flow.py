"""PR approval flow per the PRD (ROLE_WORKFLOWS 4.2 / 5.1 / 8.2 / 14 / 16).

Requester -> Department Approver (MANAGER_REVIEW) -> Finance/AP budget review (BUDGET_REVIEW)
-> APPROVED. The budget is only soft-checked at submit and is reserved when Finance approves;
an out-of-budget PR needs a budget exception (Procurement Manager by default) first.
"""

import datetime
from decimal import Decimal

import pytest
from django.core.exceptions import ValidationError
from django.utils import timezone

from apps.accounts.models import Role, User
from apps.approvals.models import ApprovalPolicy, ApprovalRequest
from apps.budgets.models import BudgetReservation
from apps.budgets.services import allocate_budget_service, pr_budget_position, reconcile_budget
from apps.notifications.models import Notification
from apps.organization.models import CostCenter, Department, FiscalPeriod, Organization
from apps.requisitions.models import PurchaseRequisition
from apps.requisitions.selectors import get_pr_budget_review_queue
from apps.requisitions.services import (
    approve_purchase_requisition_service as approve,
)
from apps.requisitions.services import (
    create_purchase_requisition_service,
)
from apps.requisitions.services import (
    reject_purchase_requisition_service as reject,
)
from apps.requisitions.services import (
    submit_purchase_requisition_service as submit,
)

pytestmark = pytest.mark.django_db

PR = ApprovalPolicy.MODULE_PR
BX = ApprovalPolicy.MODULE_BUDGET_EXCEPTION


def D(x):
    return Decimal(str(x))


def build_org(db_roles, tag, allocation=50000):
    today = timezone.localdate()
    org = Organization.objects.create(name=f"Org {tag}", code=f"ORG-{tag}")
    dept = Department.objects.create(name=f"Dept {tag}", code=f"D{tag}", organization=org)
    cc = CostCenter.objects.create(name=f"CC {tag}", code=f"CC-{tag}", department=dept)
    period = FiscalPeriod.objects.create(
        organization=org,
        year=today.year,
        period_number=1,
        name=f"FY-{tag}",
        start_date=today - datetime.timedelta(days=30),
        end_date=today + datetime.timedelta(days=300),
    )
    budget = allocate_budget_service(
        cost_center=cc, fiscal_period=period, allocated_amount=D(allocation)
    )

    def user(local, role_code, **extra):
        return User.objects.create_user(
            email=f"{local}.{tag.lower()}@prd.test",
            password="Password123!",
            role=db_roles[role_code],
            organization=org,
            **extra,
        )

    return dict(
        today=today,
        org=org,
        dept=dept,
        cc=cc,
        budget=budget,
        requester=user("req", Role.REQUESTER, department=dept),
        appr=user("appr", Role.DEPT_APPROVER, department=dept),
        mgr=user("mgr", Role.PROC_MGR),
        fin=user("fin", Role.FINANCE_AP),
        pe=user("pe", Role.PROC_EXEC),
    )


@pytest.fixture
def w(db_roles):
    return build_org(db_roles, "A")


def make_pr(w, amount=1000):
    return create_purchase_requisition_service(
        title="PRD flow PR",
        justification="PRD approval flow test",
        requester=w["requester"],
        department=w["dept"],
        cost_center=w["cc"],
        requested_delivery_date=w["today"] + datetime.timedelta(days=10),
        line_items=[{"item_description": "Item", "quantity": 1, "estimated_unit_price": amount}],
    )


def fresh(obj):
    obj.refresh_from_db()
    return obj


def test_default_chain_is_department_approver_then_finance(w):
    pr = submit(requisition=make_pr(w), user=w["requester"])
    req = ApprovalRequest.objects.get(module=PR, target_object_id=pr.id)
    assert [s["role_code"] for s in req.steps] == ["DEPT_APPROVER", "FINANCE_AP"]
    assert [s["role_name"] for s in req.steps][1].startswith("Finance")


def test_happy_path_under_budget_reserves_only_when_finance_approves(w):
    pr = submit(requisition=make_pr(w), user=w["requester"])
    assert pr.status == PurchaseRequisition.STATUS_MANAGER_REVIEW
    assert pr_budget_position(pr)["status"] == "WITHIN_BUDGET"
    assert not BudgetReservation.objects.filter(requisition=pr).exists()
    assert fresh(w["budget"]).reserved_amount == D(0)
    assert Notification.objects.filter(recipient=w["appr"], title__contains="PR").exists()

    pr = approve(requisition=pr, approver=w["appr"])
    assert pr.status == PurchaseRequisition.STATUS_BUDGET_REVIEW
    assert not BudgetReservation.objects.filter(requisition=pr).exists()  # still none
    assert Notification.objects.filter(recipient=w["fin"], title__contains="Step 2").exists()

    pr = approve(requisition=pr, approver=w["fin"])
    assert pr.status == PurchaseRequisition.STATUS_APPROVED
    reservation = BudgetReservation.objects.get(requisition=pr)
    assert reservation.amount == D(1000) and reservation.status == "RESERVED"
    assert fresh(w["budget"]).reserved_amount == D(1000)
    assert Notification.objects.filter(
        recipient=w["requester"], title__startswith="Requisition Approved"
    ).exists()
    assert Notification.objects.filter(
        recipient=w["pe"], title="Approved PR Ready for Sourcing"
    ).exists()
    assert not ApprovalRequest.objects.filter(module=BX, target_object_id=pr.id).exists()
    assert reconcile_budget(fresh(w["budget"]))["in_sync"]


def test_out_of_budget_pr_needs_the_exception_approver_before_approval(w):
    pr = submit(requisition=make_pr(w, 60000), user=w["requester"])  # allocation is 50,000
    position = pr_budget_position(pr)
    assert position["status"] == "OUT_OF_BUDGET" and position["shortfall"] == D(10000)
    assert pr.status == PurchaseRequisition.STATUS_MANAGER_REVIEW  # soft check does not block

    pr = approve(requisition=pr, approver=w["appr"])
    pr = approve(requisition=pr, approver=w["fin"])
    # Finance cannot silently approve an out-of-budget PR
    assert pr.status == PurchaseRequisition.STATUS_BUDGET_REVIEW
    assert not BudgetReservation.objects.filter(requisition=pr).exists()
    bx = ApprovalRequest.objects.get(module=BX, target_object_id=pr.id, status="PENDING")
    assert [s["role_code"] for s in bx.steps] == ["PROC_MGR"]  # default exception approver
    assert bx.amount == D(10000)
    assert Notification.objects.filter(
        recipient=w["mgr"], title__startswith="Budget Alert"
    ).exists()

    pr = approve(requisition=pr, approver=w["mgr"], comments="Overspend authorised")
    assert pr.status == PurchaseRequisition.STATUS_APPROVED
    assert BudgetReservation.objects.get(requisition=pr).amount == D(60000)
    bx.refresh_from_db()
    assert bx.status == ApprovalRequest.STATUS_APPROVED and bx.consumed_at is not None
    assert reconcile_budget(fresh(w["budget"]))["in_sync"]


def test_exception_policy_steps_are_used_when_one_exists(w, db_roles):
    from apps.approvals import engine

    engine.create_policy_with_steps(
        name="PR overrun route",
        module=BX,
        steps=[
            {"approver_role": db_roles[Role.PROC_MGR]},
            {"approver_role": db_roles[Role.SUPER_ADMIN]},
        ],
        is_exception_route=True,
    )
    pr = submit(requisition=make_pr(w, 60000), user=w["requester"])
    approve(requisition=pr, approver=w["appr"])
    approve(requisition=pr, approver=w["fin"])
    bx = ApprovalRequest.objects.get(module=BX, target_object_id=pr.id)
    assert [s["role_code"] for s in bx.steps] == ["PROC_MGR", "SUPER_ADMIN"]


def test_finance_reject_ends_the_pr_and_no_reservation_exists(w):
    pr = submit(requisition=make_pr(w), user=w["requester"])
    approve(requisition=pr, approver=w["appr"])
    with pytest.raises(ValidationError, match="mandatory"):
        reject(requisition=pr, approver=w["fin"], comments=" ")
    pr = reject(requisition=pr, approver=w["fin"], comments="Not budgeted for this quarter")
    assert pr.status == PurchaseRequisition.STATUS_REJECTED
    assert not BudgetReservation.objects.filter(requisition=pr).exists()
    assert fresh(w["budget"]).reserved_amount == D(0)
    assert not ApprovalRequest.objects.filter(target_object_id=pr.id, status="PENDING").exists()
    assert Notification.objects.filter(
        recipient=w["requester"], title__startswith="Requisition Rejected"
    ).exists()


def test_procurement_manager_cannot_approve_a_normal_pr_step(w):
    pr = submit(requisition=make_pr(w), user=w["requester"])
    assert not Notification.objects.filter(
        recipient=w["mgr"], title__startswith="New PR Approval"
    ).exists()
    with pytest.raises(ValidationError, match="not authorized"):
        approve(requisition=pr, approver=w["mgr"])  # department level
    approve(requisition=pr, approver=w["appr"])
    with pytest.raises(ValidationError, match="not authorized"):
        approve(requisition=pr, approver=w["mgr"])  # Finance level
    assert fresh(pr).status == PurchaseRequisition.STATUS_BUDGET_REVIEW
    assert not BudgetReservation.objects.filter(requisition=pr).exists()


def test_finance_queue_is_organization_scoped(w, db_roles):
    other = build_org(db_roles, "B")
    mine = submit(requisition=make_pr(w, 800), user=w["requester"])
    theirs = submit(requisition=make_pr(other, 900), user=other["requester"])
    approve(requisition=mine, approver=w["appr"])
    approve(requisition=theirs, approver=other["appr"])
    still_manager = submit(requisition=make_pr(w, 700), user=w["requester"])  # not at Finance yet

    rows = get_pr_budget_review_queue(w["fin"])
    assert [r["pr"].id for r in rows] == [mine.id]
    assert rows[0]["position"]["status"] == "WITHIN_BUDGET"
    assert rows[0]["position"]["available"] == D(50000)
    assert [r["pr"].id for r in get_pr_budget_review_queue(other["fin"])] == [theirs.id]
    assert still_manager.id not in [r["pr"].id for r in get_pr_budget_review_queue(w["fin"])]

    orgless = User.objects.create_user(
        email="orgless.fin@prd.test", password="Password123!", role=db_roles[Role.FINANCE_AP]
    )
    assert get_pr_budget_review_queue(orgless) == []


def test_finance_dashboard_shows_the_pr_budget_review_queue(w, db_roles, client):
    other = build_org(db_roles, "B")
    mine = submit(requisition=make_pr(w, 60000), user=w["requester"])  # out of budget
    theirs = submit(requisition=make_pr(other, 900), user=other["requester"])
    approve(requisition=mine, approver=w["appr"])
    approve(requisition=theirs, approver=other["appr"])

    client.force_login(w["fin"])
    html = client.get("/").content.decode()
    assert "PR Budget Review" in html
    assert mine.pr_number in html and theirs.pr_number not in html
    assert "Out of budget" in html and "Shortfall" in html
    assert "Approve" in html and "Reject" in html


def test_dept_approver_and_procurement_manager_queues(w, client):
    from apps.approvals.views import _get_approval_querysets

    at_dept = submit(requisition=make_pr(w, 500), user=w["requester"])
    at_finance = submit(requisition=make_pr(w, 600), user=w["requester"])
    approve(requisition=at_finance, approver=w["appr"])

    pending, _ = _get_approval_querysets(w["appr"], Role.DEPT_APPROVER)
    assert set(pending.values_list("id", flat=True)) == {at_dept.id}
    pending, _ = _get_approval_querysets(w["fin"], Role.FINANCE_AP)
    assert set(pending.values_list("id", flat=True)) == {at_finance.id}
    pending, _ = _get_approval_querysets(w["mgr"], Role.PROC_MGR)
    assert pending.count() == 0  # not on the default chain

    # ... but a budget exception makes the Procurement Manager the active approver
    big = submit(requisition=make_pr(w, 60000), user=w["requester"])
    approve(requisition=big, approver=w["appr"])
    approve(requisition=big, approver=w["fin"])
    pending, _ = _get_approval_querysets(w["mgr"], Role.PROC_MGR)
    assert set(pending.values_list("id", flat=True)) == {big.id}

    client.force_login(w["appr"])
    html = client.get("/").content.decode()
    assert at_dept.pr_number in html and at_finance.pr_number not in html


def test_requester_sees_stage_labels_and_current_approver(w, client):
    pr = submit(requisition=make_pr(w), user=w["requester"])
    client.force_login(w["requester"])
    url = f"/requisitions/{pr.pk}/"

    html = client.get(url).content.decode()
    for label in ("Manager Review", "Budget Review", "Approved"):
        assert label in html
    assert "Awaiting" in html and "Department Approver" in html

    approve(requisition=pr, approver=w["appr"])
    html = client.get(url).content.decode()
    assert 'id="pr-current-approver"' in html and "Finance" in html
    assert 'bg-primary">Budget Review' in html.replace("\n", "")

    approve(requisition=pr, approver=w["fin"])
    html = client.get(url).content.decode()
    assert 'bg-primary">Approved' in html.replace("\n", "")
    assert 'id="pr-current-approver"' not in html

    listing = client.get("/requisitions/").content.decode()
    assert pr.pr_number in listing

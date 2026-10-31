"""Purchase requisitions on the approval engine (audit group D)."""

import datetime
from decimal import Decimal

import pytest
from django.core.exceptions import ValidationError
from django.utils import timezone
from rest_framework.test import APIClient

from apps.accounts.models import Role, User
from apps.approvals import engine
from apps.approvals.models import ApprovalAction, ApprovalPolicy, ApprovalRequest
from apps.budgets.models import Budget, BudgetReservation
from apps.budgets.services import (
    allocate_budget_service,
    check_and_reserve_budget_service,
    reconcile_budget,
)
from apps.notifications.models import Notification
from apps.organization.models import CostCenter, Department, FiscalPeriod, Organization
from apps.requisitions.models import PurchaseRequisition
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


@pytest.fixture
def w(db, db_roles):
    today = timezone.localdate()
    org = Organization.objects.create(name="PRF Org", code="PRF-ORG")
    dept = Department.objects.create(name="Dept", code="PD", organization=org)
    cc = CostCenter.objects.create(name="CC", code="CC-P", department=dept)
    cc2 = CostCenter.objects.create(name="CC2", code="CC-P2", department=dept)
    period = FiscalPeriod.objects.create(
        organization=org,
        year=today.year,
        period_number=1,
        name="FY-P",
        start_date=today - datetime.timedelta(days=30),
        end_date=today + datetime.timedelta(days=300),
    )
    budget = allocate_budget_service(
        cost_center=cc, fiscal_period=period, allocated_amount=D(50000)
    )
    budget2 = allocate_budget_service(
        cost_center=cc2, fiscal_period=period, allocated_amount=D(50000)
    )

    def user(email, role_code, **extra):
        extra.setdefault("organization", org)
        return User.objects.create_user(
            email=email, password="Password123!", role=db_roles[role_code], **extra
        )

    return dict(
        today=today,
        org=org,
        dept=dept,
        cc=cc,
        cc2=cc2,
        budget=budget,
        budget2=budget2,
        roles=db_roles,
        requester=user("req@p.test", Role.REQUESTER, department=dept),
        appr=user("appr@p.test", Role.DEPT_APPROVER, department=dept),
        mgr=user("mgr@p.test", Role.PROC_MGR),
        mgr2=user("mgr2@p.test", Role.PROC_MGR),
        fin=user("fin@p.test", Role.FINANCE_AP),
        pe=user("pe@p.test", Role.PROC_EXEC),
        admin=User.objects.create_superuser(
            email="admin@p.test",
            password="Password123!",
            first_name="A",
            last_name="D",
            role=db_roles[Role.SUPER_ADMIN],
            organization=org,
        ),
    )


def make_pr(w, amount=1000, cc=None, requester=None):
    return create_purchase_requisition_service(
        title="PR",
        justification="approval engine test",
        requester=requester or w["requester"],
        department=w["dept"],
        cost_center=cc or w["cc"],
        requested_delivery_date=w["today"] + datetime.timedelta(days=10),
        line_items=[{"item_description": "Item", "quantity": 1, "estimated_unit_price": amount}],
    )


def policy(w, name, roles, **kw):
    kw.setdefault("module", PR)
    return engine.create_policy_with_steps(
        name=name, steps=[{"approver_role": w["roles"][r]} for r in roles], **kw
    )


def fresh(obj):
    obj.refresh_from_db()
    return obj


# ======================================================================= routing
def test_submit_binds_the_pr_to_the_policy_version_and_records_the_request(w):
    pr = submit(requisition=make_pr(w), user=w["requester"])
    req = ApprovalRequest.objects.get(module=PR, target_object_id=pr.id)
    assert pr.status == PurchaseRequisition.STATUS_MANAGER_REVIEW
    assert req.status == ApprovalRequest.STATUS_PENDING and req.amount == D(1000)
    assert req.department == w["dept"] and req.cost_center == w["cc"]
    assert [s["role_code"] for s in req.steps] == [
        "DEPT_APPROVER",
        "FINANCE_AP",
    ]  # seeded global policy (PRD 4.2: department, then Finance budget review)
    submitted = ApprovalAction.objects.get(approval_request=req, action="SUBMITTED")
    assert "v1" in submitted.comments and submitted.actor_id == w["requester"].id


def test_cost_center_policy_overrides_the_department_and_global_policies(w):
    policy(w, "CC-P special", [Role.FINANCE_AP], cost_center=w["cc"])
    on_special = submit(requisition=make_pr(w, cc=w["cc"]), user=w["requester"])
    on_other = submit(requisition=make_pr(w, cc=w["cc2"]), user=w["requester"])
    steps = lambda pr: [  # noqa: E731
        s["role_code"] for s in ApprovalRequest.objects.get(module=PR, target_object_id=pr.id).steps
    ]
    assert steps(on_special) == ["FINANCE_AP"]
    assert steps(on_other) == ["DEPT_APPROVER", "FINANCE_AP"]
    assert engine.can_act(
        ApprovalRequest.objects.get(target_object_id=on_special.id, module=PR), w["fin"]
    )


def test_amount_tier_routes_large_requisitions_to_a_longer_chain(w):
    seeded = ApprovalPolicy.objects.get(
        module=PR, department__isnull=True, cost_center__isnull=True
    )
    engine.revise_approval_policy(policy=seeded, actor=w["admin"], max_amount=D(4999.99))
    policy(w, "Large PRs", [Role.DEPT_APPROVER, Role.PROC_MGR, Role.FINANCE_AP], min_amount=D(5000))
    small = submit(requisition=make_pr(w, 1000), user=w["requester"])
    large = submit(requisition=make_pr(w, 6000), user=w["requester"])
    n = lambda pr: len(  # noqa: E731
        ApprovalRequest.objects.get(module=PR, target_object_id=pr.id).steps
    )
    assert (n(small), n(large)) == (2, 3)


def test_no_matching_policy_fails_closed_and_leaves_nothing_reserved(w):
    ApprovalPolicy.objects.filter(module=PR).update(is_active=False)
    pr = make_pr(w)
    with pytest.raises(ValidationError, match="No active approval policy found") as exc:
        submit(requisition=pr, user=w["requester"])
    assert "fail-closed" in str(exc.value)
    assert fresh(pr).status == PurchaseRequisition.STATUS_DRAFT
    assert fresh(w["budget"]).reserved_amount == D(0)
    assert not ApprovalRequest.objects.filter(target_object_id=pr.id).exists()


# ======================================================================= versioning in flight
def test_a_policy_change_after_submission_does_not_reroute_the_pr_in_flight(w):
    seeded = ApprovalPolicy.objects.get(
        module=PR, department__isnull=True, cost_center__isnull=True
    )
    pr = submit(requisition=make_pr(w), user=w["requester"])
    engine.revise_approval_policy(
        policy=seeded,
        actor=w["admin"],
        steps=[{"approver_role": w["roles"][Role.FINANCE_AP]}],
        change_note="Finance only",
    )
    approve(requisition=pr, approver=w["appr"])  # still the ORIGINAL step 1 (dept approver)
    pr = approve(requisition=pr, approver=w["fin"])  # and the original step 2 (Finance)
    assert pr.status == PurchaseRequisition.STATUS_APPROVED

    later = submit(requisition=make_pr(w), user=w["requester"])
    req = ApprovalRequest.objects.get(module=PR, target_object_id=later.id)
    assert [s["role_code"] for s in req.steps] == ["FINANCE_AP"] and req.policy.version == 2


# ======================================================================= decisions
def test_full_chain_approval_with_fresh_status_audit_and_notifications(w):
    pr = submit(requisition=make_pr(w), user=w["requester"])
    assert Notification.objects.filter(
        recipient=w["appr"], title__startswith="New PR Approval"
    ).exists()
    assert not Notification.objects.filter(
        recipient=w["mgr"], title__startswith="New PR Approval"
    ).exists()
    assert fresh(w["budget"]).reserved_amount == D(0)  # soft check only at submit

    pr = approve(requisition=pr, approver=w["appr"], comments="ok by dept")
    assert pr.status == PurchaseRequisition.STATUS_BUDGET_REVIEW
    assert fresh(w["budget"]).reserved_amount == D(0)
    assert Notification.objects.filter(recipient=w["fin"], title__contains="Step 2").exists()
    assert not Notification.objects.filter(recipient=w["mgr"], title__contains="Step 2").exists()

    pr = approve(requisition=pr, approver=w["fin"])
    assert pr.status == PurchaseRequisition.STATUS_APPROVED
    assert fresh(w["budget"]).reserved_amount == D(1000)  # reserved when Finance approves
    # regression: the "ready for sourcing" notification to Procurement Executives must be sent
    assert Notification.objects.filter(
        recipient=w["pe"], title="Approved PR Ready for Sourcing"
    ).exists()
    assert Notification.objects.filter(
        recipient=w["requester"], title__startswith="Requisition Approved"
    ).exists()
    req = ApprovalRequest.objects.get(module=PR, target_object_id=pr.id)
    assert req.status == ApprovalRequest.STATUS_APPROVED
    assert [
        (a.actor_id, a.new_state)
        for a in req.actions.filter(action="APPROVED").order_by("created_at")
    ] == [
        (w["appr"].id, "BUDGET_REVIEW"),
        (w["fin"].id, "APPROVED"),
    ]


def test_requester_cannot_approve_their_own_pr_even_when_they_hold_the_approver_role(w):
    # PRD 3.6: Super Admin can neither raise nor approve PRs, so the SoD rule is shown with a
    # Department Approver who raised the PR in their own department.
    pr = submit(requisition=make_pr(w, requester=w["appr"]), user=w["appr"])
    with pytest.raises(ValidationError, match="Self-approval is strictly prohibited"):
        approve(requisition=pr, approver=w["appr"])
    assert fresh(pr).status == PurchaseRequisition.STATUS_MANAGER_REVIEW


def test_one_person_cannot_approve_both_steps_of_a_pr(w):
    policy(w, "Two managers", [Role.PROC_MGR, Role.PROC_MGR], cost_center=w["cc2"])
    pr = submit(requisition=make_pr(w, cc=w["cc2"]), user=w["requester"])
    approve(requisition=pr, approver=w["mgr"])
    with pytest.raises(ValidationError, match="four-eyes"):
        approve(requisition=pr, approver=w["mgr"])
    assert approve(requisition=pr, approver=w["mgr2"]).status == PurchaseRequisition.STATUS_APPROVED


def test_wrong_role_and_wrong_department_are_refused(w):
    other_dept = Department.objects.create(name="Other", code="OD", organization=w["org"])
    stranger = User.objects.create_user(
        email="stranger@p.test",
        password="Password123!",
        role=w["roles"][Role.DEPT_APPROVER],
        organization=w["org"],
        department=other_dept,
    )
    pr = submit(requisition=make_pr(w), user=w["requester"])
    with pytest.raises(ValidationError, match="not authorized to approve step 1"):
        approve(requisition=pr, approver=w["mgr"])
    with pytest.raises(ValidationError, match="own department"):
        approve(requisition=pr, approver=stranger)


def test_approval_limit_stops_an_approver_who_is_not_allowed_to_approve_that_much(w):
    User.objects.filter(pk=w["appr"].pk).update(approval_limit=D(500))
    pr = submit(requisition=make_pr(w, 1000), user=w["requester"])
    with pytest.raises(ValidationError, match="approval limit"):
        approve(requisition=pr, approver=w["appr"])
    assert fresh(pr).status == PurchaseRequisition.STATUS_MANAGER_REVIEW
    small = submit(requisition=make_pr(w, 400), user=w["requester"])
    assert (
        approve(requisition=small, approver=w["appr"]).status
        == PurchaseRequisition.STATUS_BUDGET_REVIEW
    )


def test_rejection_requires_a_chain_member_and_a_reason_and_releases_the_reservation(w):
    pr = submit(requisition=make_pr(w), user=w["requester"])
    assert fresh(w["budget"]).reserved_amount == D(0)  # nothing reserved at submit any more
    # a reservation still held by a PR that was in flight before the Finance-first flow
    check_and_reserve_budget_service(requisition=pr, requested_by_user=w["requester"])
    assert fresh(w["budget"]).reserved_amount == D(1000)
    with pytest.raises(ValidationError, match="not an approver"):
        reject(requisition=pr, approver=w["pe"], comments="nope")
    with pytest.raises(ValidationError, match="mandatory"):
        reject(requisition=pr, approver=w["appr"], comments=" ")
    pr = reject(requisition=pr, approver=w["appr"], comments="Not justified")
    assert pr.status == PurchaseRequisition.STATUS_REJECTED
    assert fresh(w["budget"]).reserved_amount == D(0)
    req = ApprovalRequest.objects.get(module=PR, target_object_id=pr.id)
    assert req.status == ApprovalRequest.STATUS_REJECTED
    assert not ApprovalRequest.objects.filter(status="PENDING", target_object_id=pr.id).exists()
    assert reconcile_budget(fresh(w["budget"]))["in_sync"]


# ======================================================================= budget exception route
def test_out_of_budget_pr_needs_the_budget_exception_chain_after_finance(w):
    pr = submit(requisition=make_pr(w, 60000), user=w["requester"])  # allocation is 50,000
    assert pr.status == PurchaseRequisition.STATUS_MANAGER_REVIEW  # soft check only
    assert fresh(w["budget"]).reserved_amount == D(0)
    pr = approve(requisition=pr, approver=w["appr"])
    assert pr.status == PurchaseRequisition.STATUS_BUDGET_REVIEW

    # Finance cannot silently approve: the budget exception (Procurement Manager) is raised
    pr = approve(requisition=pr, approver=w["fin"], comments="over budget")
    assert pr.status == PurchaseRequisition.STATUS_BUDGET_REVIEW
    assert fresh(w["budget"]).reserved_amount == D(0)  # nothing reserved until authorised
    bx = ApprovalRequest.objects.get(module=BX, target_object_id=pr.id)
    assert bx.amount == D(10000) and [s["role_code"] for s in bx.steps] == ["PROC_MGR"]
    assert Notification.objects.filter(
        recipient=w["mgr"], title__startswith="Budget Alert"
    ).exists()

    # neither the department approver nor Finance is the budget exception authority
    for outsider in (w["appr"], w["fin"]):
        with pytest.raises(ValidationError, match="not authorized|already approved"):
            approve(requisition=pr, approver=outsider)
    assert fresh(pr).status == PurchaseRequisition.STATUS_BUDGET_REVIEW

    pr = approve(requisition=pr, approver=w["mgr"], comments="Exception granted")
    assert pr.status == PurchaseRequisition.STATUS_APPROVED
    assert fresh(w["budget"]).reserved_amount == D(60000)  # forced, authorised overrun
    assert ApprovalRequest.objects.get(module=BX, target_object_id=pr.id).status == "APPROVED"
    assert reconcile_budget(fresh(w["budget"]))["in_sync"]


def test_rejecting_a_budget_exception_ends_everything_and_reserves_nothing(w):
    pr = submit(requisition=make_pr(w, 60000), user=w["requester"])
    approve(requisition=pr, approver=w["appr"])
    pr = reject(requisition=pr, approver=w["fin"], comments="Not affordable this quarter")
    assert pr.status == PurchaseRequisition.STATUS_REJECTED
    assert not ApprovalRequest.objects.filter(status="PENDING", target_object_id=pr.id).exists()
    assert fresh(w["budget"]).reserved_amount == D(0)

    pr2 = submit(requisition=make_pr(w, 60000, cc=w["cc2"]), user=w["requester"])
    approve(requisition=pr2, approver=w["appr"])
    approve(requisition=pr2, approver=w["fin"])  # opens the budget exception
    pr2 = reject(requisition=pr2, approver=w["mgr"], comments="No overspend")
    assert pr2.status == PurchaseRequisition.STATUS_REJECTED
    assert not ApprovalRequest.objects.filter(status="PENDING", target_object_id=pr2.id).exists()
    assert fresh(w["budget2"]).reserved_amount == D(0)


def test_budget_exception_policy_is_configurable_by_amount(w):
    policy(
        w,
        "Small overruns",
        [Role.FINANCE_AP],
        module=BX,
        max_amount=D(20000),
        is_exception_route=True,
    )
    policy(
        w,
        "Big overruns",
        [Role.FINANCE_AP, Role.PROC_MGR],
        module=BX,
        min_amount=D(20000.01),
        is_exception_route=True,
    )

    def routed(amount, cc):
        pr = submit(requisition=make_pr(w, amount, cc=cc), user=w["requester"])
        approve(requisition=pr, approver=w["appr"])
        approve(requisition=pr, approver=w["fin"])
        return len(ApprovalRequest.objects.get(module=BX, target_object_id=pr.id).steps)

    # overrun 10,000 -> one exception step; overrun 40,000 -> two
    assert (routed(60000, w["cc"]), routed(90000, w["cc2"])) == (1, 2)


def test_pr_module_exception_route_policy_is_used_when_the_pr_overruns_the_budget(w):
    policy(w, "PR exception", [Role.PROC_MGR, Role.FINANCE_AP], is_exception_route=True)
    pr = submit(requisition=make_pr(w, 60000), user=w["requester"])
    req = ApprovalRequest.objects.get(module=PR, target_object_id=pr.id)
    assert [s["role_code"] for s in req.steps] == ["DEPT_APPROVER", "FINANCE_AP"]  # not appended
    approve(requisition=pr, approver=w["appr"])
    approve(requisition=pr, approver=w["fin"])
    bx = ApprovalRequest.objects.get(module=BX, target_object_id=pr.id)
    assert [s["role_code"] for s in bx.steps] == ["PROC_MGR", "FINANCE_AP"]
    assert bx.exception_reason.startswith("Budget overrun")


def test_budget_is_rechecked_when_finance_approves(w):
    pr = submit(requisition=make_pr(w, 1000), user=w["requester"])  # within budget at submit
    approve(requisition=pr, approver=w["appr"])
    other = make_pr(w, 49500)  # another PR consumes the budget in the meantime
    check_and_reserve_budget_service(requisition=other, requested_by_user=w["requester"])
    pr = approve(requisition=pr, approver=w["fin"])
    assert pr.status == PurchaseRequisition.STATUS_BUDGET_REVIEW  # now needs the exception
    assert ApprovalRequest.objects.get(module=BX, target_object_id=pr.id).amount == D(500)
    assert not BudgetReservation.objects.filter(
        requisition=pr, status=BudgetReservation.STATUS_RESERVED
    ).exists()


# ======================================================================= cancel / legacy / escalation
def test_cancelling_via_api_withdraws_the_approval_requests_and_frees_the_budget(w):
    pr = submit(requisition=make_pr(w), user=w["requester"])
    client = APIClient()
    client.force_authenticate(w["requester"])
    assert (
        client.post(f"/api/v1/requisitions/{pr.pk}/cancel/", {}, format="json").status_code == 200
    )
    assert not ApprovalRequest.objects.filter(status="PENDING", target_object_id=pr.id).exists()
    assert ApprovalRequest.objects.get(module=PR, target_object_id=pr.id).status == "CANCELLED"
    assert fresh(w["budget"]).reserved_amount == D(0)


def test_cancelling_from_the_web_ui_releases_the_reservation_and_withdraws_requests(w, client):
    pr = submit(requisition=make_pr(w), user=w["requester"])
    # a reservation still held by a PR that was in flight before the Finance-first flow
    check_and_reserve_budget_service(requisition=pr, requested_by_user=w["requester"])
    assert fresh(w["budget"]).reserved_amount == D(1000)
    client.force_login(w["requester"])
    client.post(f"/requisitions/{pr.pk}/cancel/")
    pr.refresh_from_db()
    assert pr.status == PurchaseRequisition.STATUS_CANCELLED
    assert fresh(w["budget"]).reserved_amount == D(0)
    assert not ApprovalRequest.objects.filter(status="PENDING", target_object_id=pr.id).exists()


def test_legacy_pr_without_a_request_is_picked_up_and_fast_forwarded(w):
    pr = make_pr(w)
    # simulate a PR submitted before the engine existed: status set directly, step 1 already
    # approved in the old (unlinked) ApprovalAction history
    PurchaseRequisition.objects.filter(pk=pr.pk).update(
        status=PurchaseRequisition.STATUS_MANAGER_REVIEW
    )
    ApprovalAction.objects.create(
        target_object_id=pr.id,
        target_model_name="PurchaseRequisition",
        actor=w["appr"],
        action="APPROVED",
        previous_state="SUBMITTED",
        new_state="MANAGER_REVIEW",
        comments="legacy",
    )
    pr.refresh_from_db()
    assert not ApprovalRequest.objects.filter(target_object_id=pr.id).exists()
    with pytest.raises(ValidationError, match="not authorized to approve step 2"):
        approve(requisition=pr, approver=w["appr"])  # lazily opened, resumed at step 2
    assert approve(requisition=pr, approver=w["fin"]).status == PurchaseRequisition.STATUS_APPROVED


def test_overdue_reminder_goes_to_the_current_step_approvers_only(w):
    from apps.notifications.services import scan_overdue_approvals_and_escalate_service

    pr = submit(requisition=make_pr(w), user=w["requester"])
    approve(requisition=pr, approver=w["appr"])  # now waiting on Finance / AP (step 2)
    PurchaseRequisition.objects.filter(pk=pr.pk).update(
        updated_at=timezone.now() - datetime.timedelta(hours=50)
    )
    Notification.objects.all().delete()
    scan_overdue_approvals_and_escalate_service(reminder_hours=48, escalation_hours=72)
    reminded = set(
        Notification.objects.filter(title__startswith="Reminder: PR").values_list(
            "recipient__email", flat=True
        )
    )
    assert reminded == {w["fin"].email}

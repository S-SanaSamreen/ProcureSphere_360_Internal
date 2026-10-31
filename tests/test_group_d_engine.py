"""Approval engine core (audit group D): selection, versioning, SoD, limits, delegation, exceptions."""

import datetime
from decimal import Decimal

import pytest
from django.core.exceptions import ValidationError

from apps.accounts.models import Role, User
from apps.approvals import engine
from apps.approvals.models import (
    ApprovalAction,
    ApprovalDelegate,
    ApprovalPolicy,
    ApprovalRequest,
    ApprovalStep,
)
from apps.approvals.services import set_approval_delegate_service
from apps.organization.models import CostCenter, Department, Organization
from apps.requisitions.models import PurchaseRequisition

pytestmark = pytest.mark.django_db

PR = ApprovalPolicy.MODULE_PR


def D(x):
    return Decimal(str(x))


@pytest.fixture
def w(db, db_roles):
    org = Organization.objects.create(name="Eng Org", code="ENG-ORG")
    other_org = Organization.objects.create(name="Other Org", code="OTH-ORG")
    dept_a = Department.objects.create(name="Dept A", code="DA", organization=org)
    dept_b = Department.objects.create(name="Dept B", code="DB", organization=org)
    cc_a = CostCenter.objects.create(name="CC A", code="CC-A", department=dept_a)
    cc_a2 = CostCenter.objects.create(name="CC A2", code="CC-A2", department=dept_a)

    def user(email, role_code, **extra):
        extra.setdefault("organization", org)
        return User.objects.create_user(
            email=email, password="Password123!", role=db_roles[role_code], **extra
        )

    world = dict(
        org=org,
        other_org=other_org,
        dept_a=dept_a,
        dept_b=dept_b,
        cc_a=cc_a,
        cc_a2=cc_a2,
        roles=db_roles,
        requester=user("req@e.test", Role.REQUESTER, department=dept_a),
        appr_a=user("appr.a@e.test", Role.DEPT_APPROVER, department=dept_a),
        appr_b=user("appr.b@e.test", Role.DEPT_APPROVER, department=dept_b),
        mgr=user("mgr@e.test", Role.PROC_MGR),
        mgr2=user("mgr2@e.test", Role.PROC_MGR),
        fin=user("fin@e.test", Role.FINANCE_AP),
        pe=user("pe@e.test", Role.PROC_EXEC),
        outsider_mgr=user("out@e.test", Role.PROC_MGR, organization=other_org),
        admin=User.objects.create_superuser(
            email="admin@e.test",
            password="Password123!",
            first_name="A",
            last_name="D",
            role=db_roles[Role.SUPER_ADMIN],
            organization=org,
        ),
    )
    # a clean slate for the PR module so selection tests are deterministic
    ApprovalPolicy.objects.filter(module=PR).delete()
    return world


def mk_policy(w, name, steps, **kw):
    kw.setdefault("module", PR)
    return engine.create_policy_with_steps(
        name=name, steps=[{"approver_role": w["roles"][r], **extra} for r, extra in steps], **kw
    )


def target(w, amount=1000):
    return PurchaseRequisition.objects.create(
        pr_number=f"PR-ENG-{PurchaseRequisition.objects.count() + 1}",
        title="t",
        justification="j",
        requester=w["requester"],
        department=w["dept_a"],
        cost_center=w["cc_a"],
        total_amount=D(amount),
        requested_delivery_date=datetime.date.today() + datetime.timedelta(days=5),
    )


def open_req(w, t=None, amount=1000, **kw):
    t = t or target(w, amount)
    kw.setdefault("department", w["dept_a"])
    kw.setdefault("cost_center", w["cc_a"])
    return engine.open_approval_request(
        module=PR, target=t, amount=amount, maker=w["requester"], **kw
    )


# ======================================================================= selection
def test_most_specific_scope_wins_cost_center_then_department_then_global(w):
    g = mk_policy(w, "Global", [(Role.PROC_MGR, {})])
    d = mk_policy(w, "Dept A", [(Role.DEPT_APPROVER, {})], department=w["dept_a"])
    c = mk_policy(w, "CC A", [(Role.FINANCE_AP, {})], cost_center=w["cc_a"])
    sel = engine.select_policy
    assert sel(PR, 100, w["dept_a"], w["cc_a"]) == c  # cost center beats department
    assert sel(PR, 100, w["dept_a"], w["cc_a2"]) == d  # other cc in the department -> department
    assert sel(PR, 100, w["dept_b"], None) == g  # no scoped policy -> global
    assert sel(PR, 100, None, None) == g


def test_amount_tiers_pick_the_matching_band(w):
    low = mk_policy(w, "Low", [(Role.DEPT_APPROVER, {})], min_amount=0, max_amount=999.99)
    high = mk_policy(w, "High", [(Role.PROC_MGR, {})], min_amount=1000)
    assert engine.select_policy(PR, 500) == low
    assert engine.select_policy(PR, 1000) == high
    assert engine.select_policy(PR, 5_000_000) == high


def test_transaction_types_are_independent(w):
    mk_policy(w, "PR only", [(Role.PROC_MGR, {})])
    assert engine.select_policy(ApprovalPolicy.MODULE_PO, 100) is None


def test_exception_route_policies_are_never_the_normal_chain(w):
    mk_policy(w, "Exc only", [(Role.FINANCE_AP, {})], is_exception_route=True)
    assert engine.select_policy(PR, 100) is None
    assert engine.select_policy(PR, 100, exception=True) is not None


def test_no_policy_fails_closed_unless_the_module_makes_approval_optional(w):
    t = target(w)
    with pytest.raises(ValidationError, match="No active approval policy found") as exc:
        engine.open_approval_request(module=PR, target=t, amount=100, maker=w["requester"])
    assert "fail-closed" in str(exc.value)
    assert (
        engine.open_approval_request(
            module=PR, target=t, amount=100, maker=w["requester"], required=False
        )
        is None
    )
    assert not ApprovalRequest.objects.exists()


# ======================================================================= versioning
def test_request_is_bound_to_the_policy_version_it_was_routed_under(w):
    v1 = mk_policy(w, "Standard", [(Role.DEPT_APPROVER, {}), (Role.PROC_MGR, {})])
    req = open_req(w)
    assert req.policy == v1 and [s["role_code"] for s in req.steps] == ["DEPT_APPROVER", "PROC_MGR"]

    v2 = engine.revise_approval_policy(
        policy=v1,
        actor=w["admin"],
        steps=[{"approver_role": w["roles"][Role.FINANCE_AP]}],
        change_note="Finance only from now on",
    )
    assert (v2.version, v2.policy_key) == (2, v1.policy_key)
    v1.refresh_from_db()
    assert v1.superseded_by_id == v2.id and v1.is_active is False

    req.refresh_from_db()
    assert req.policy_id == v1.id  # in-flight request keeps its version...
    assert [s["role_code"] for s in req.steps] == ["DEPT_APPROVER", "PROC_MGR"]  # ...and its steps
    new_req = open_req(w)
    assert new_req.policy_id == v2.id and [s["role_code"] for s in new_req.steps] == ["FINANCE_AP"]


def test_an_in_flight_request_still_completes_after_its_policy_is_superseded_and_deactivated(w):
    v1 = mk_policy(w, "Std", [(Role.DEPT_APPROVER, {}), (Role.PROC_MGR, {})])
    req = open_req(w)
    engine.revise_approval_policy(policy=v1, actor=w["admin"], name="Renamed")
    engine.deactivate_approval_policy(
        policy=ApprovalPolicy.objects.get(version=2), actor=w["admin"]
    )
    engine.approve_step(request=req, actor=w["appr_a"])
    done = engine.approve_step(request=req, actor=w["mgr"])
    assert done.status == ApprovalRequest.STATUS_APPROVED


def test_used_policy_versions_are_immutable(w):
    v1 = mk_policy(w, "Std", [(Role.PROC_MGR, {})])
    open_req(w)
    v1.min_amount = D(5)
    with pytest.raises(ValidationError, match="cannot be edited in place"):
        v1.save()
    v1.refresh_from_db()
    with pytest.raises(ValidationError, match="cannot be changed"):
        ApprovalStep.objects.create(
            policy=v1, step_number=2, approver_role=w["roles"][Role.FINANCE_AP]
        )
    with pytest.raises(ValidationError, match="cannot be changed"):
        v1.steps.first().delete()
    step = v1.steps.first()
    step.description = "tampered"
    with pytest.raises(ValidationError):
        step.save()
    v1.name = "Renamed is fine"
    v1.save()  # cosmetic change allowed


def test_unused_policy_can_still_be_edited_directly(w):
    p = mk_policy(w, "Draft policy", [(Role.PROC_MGR, {})])
    p.min_amount = D(10)
    p.save()
    p.refresh_from_db()
    assert p.min_amount == D(10)


def test_only_the_latest_version_can_be_revised_and_revisions_are_audited(w):
    from apps.audit.models import AuditLog

    v1 = mk_policy(w, "Std", [(Role.PROC_MGR, {})])
    v2 = engine.revise_approval_policy(policy=v1, actor=w["admin"], max_amount=D(500))
    with pytest.raises(ValidationError, match="already been superseded"):
        engine.revise_approval_policy(policy=v1, actor=w["admin"], max_amount=D(900))
    assert AuditLog.objects.filter(
        target_model="ApprovalPolicy", target_object_id=str(v2.id)
    ).exists()
    with pytest.raises(ValidationError, match="greater than maximum"):
        engine.revise_approval_policy(
            policy=v2, actor=w["admin"], min_amount=D(900), max_amount=D(100)
        )


def test_a_policy_needs_steps(w):
    with pytest.raises(ValidationError, match="at least one approval step"):
        engine.create_policy_with_steps(name="Empty", module=PR, steps=[])


# ======================================================================= decisions
def test_multi_step_chain_requires_the_right_role_at_each_step(w):
    mk_policy(w, "Std", [(Role.DEPT_APPROVER, {}), (Role.PROC_MGR, {})])
    req = open_req(w)
    with pytest.raises(ValidationError, match="not authorized to approve step 1"):
        engine.approve_step(request=req, actor=w["mgr"])  # step 2's role at step 1
    engine.approve_step(request=req, actor=w["appr_a"])
    req.refresh_from_db()
    assert req.status == ApprovalRequest.STATUS_PENDING and req.current_step == 1
    with pytest.raises(ValidationError, match="not authorized to approve step 2"):
        engine.approve_step(request=req, actor=w["fin"])
    done = engine.approve_step(request=req, actor=w["mgr"])
    assert done.status == ApprovalRequest.STATUS_APPROVED and done.decided_at
    with pytest.raises(ValidationError, match="already approved"):
        engine.approve_step(request=req, actor=w["mgr2"])


def test_department_approver_is_limited_to_their_own_department(w):
    mk_policy(w, "Std", [(Role.DEPT_APPROVER, {})])
    req = open_req(w)
    with pytest.raises(ValidationError, match="own department"):
        engine.approve_step(request=req, actor=w["appr_b"])
    engine.approve_step(request=req, actor=w["appr_a"])


def test_approver_from_another_organisation_cannot_approve(w):
    mk_policy(w, "Std", [(Role.PROC_MGR, {})])
    req = open_req(w)
    with pytest.raises(ValidationError, match="not authorized"):
        engine.approve_step(request=req, actor=w["outsider_mgr"])


def test_maker_can_never_approve_their_own_request(w):
    # PRD 3.6: Super Admin cannot approve anything, so the SoD check is exercised with a
    # manager who is both maker and eligible approver.
    mk_policy(w, "Std", [(Role.PROC_MGR, {})])
    t = target(w)
    req = engine.open_approval_request(module=PR, target=t, amount=100, maker=w["mgr2"])
    with pytest.raises(ValidationError, match="Self-approval is strictly prohibited"):
        engine.approve_step(request=req, actor=w["mgr2"])
    mgr_req = engine.open_approval_request(module=PR, target=target(w), amount=100, maker=w["mgr"])
    with pytest.raises(ValidationError, match="Self-approval"):
        engine.approve_step(request=mgr_req, actor=w["mgr"])


def test_nobody_approves_two_steps_of_the_same_request(w):
    mk_policy(w, "Std", [(Role.PROC_MGR, {}), (Role.PROC_MGR, {})])
    req = open_req(w)
    engine.approve_step(request=req, actor=w["mgr"])
    with pytest.raises(ValidationError, match="four-eyes"):
        engine.approve_step(request=req, actor=w["mgr"])
    engine.approve_step(request=req, actor=w["mgr2"])  # a different person completes it

    mk_policy(w, "Std2", [(Role.DEPT_APPROVER, {}), (Role.PROC_MGR, {})], department=w["dept_b"])
    req2 = open_req(w, department=w["dept_b"], cost_center=None)
    # PRD 3.6: Super Admin may not stand in for any step (fix the policy/users instead)
    with pytest.raises(ValidationError, match="configure the system"):
        engine.approve_step(request=req2, actor=w["admin"])


def test_specific_approver_steps_accept_only_that_person(w):
    mk_policy(w, "Named", [(Role.PROC_MGR, {"specific_approver": w["mgr2"]})])
    req = open_req(w)
    with pytest.raises(ValidationError, match="not authorized"):
        engine.approve_step(request=req, actor=w["mgr"])  # right role, wrong person
    engine.approve_step(request=req, actor=w["mgr2"])


def test_every_decision_is_recorded_against_the_request(w):
    mk_policy(w, "Std", [(Role.DEPT_APPROVER, {}), (Role.PROC_MGR, {})])
    req = open_req(w)
    engine.approve_step(request=req, actor=w["appr_a"], comments="looks fine")
    engine.approve_step(request=req, actor=w["mgr"])
    rows = list(
        ApprovalAction.objects.filter(approval_request=req, action="APPROVED").order_by(
            "created_at"
        )
    )
    assert [r.actor_id for r in rows] == [w["appr_a"].id, w["mgr"].id]
    assert rows[0].comments == "looks fine" and rows[0].policy_step is not None


# ======================================================================= approval limits
def test_approval_limit_blocks_larger_transactions_and_allows_up_to_the_limit(w):
    mk_policy(w, "Std", [(Role.PROC_MGR, {})])
    User.objects.filter(pk=w["mgr"].pk).update(approval_limit=D(5000))
    w["mgr"].refresh_from_db()
    big = open_req(w, amount=5000.01)
    with pytest.raises(ValidationError, match="approval limit"):
        engine.approve_step(request=big, actor=w["mgr"])
    exact = open_req(w, amount=5000)
    assert engine.approve_step(request=exact, actor=w["mgr"]).status == "APPROVED"
    # someone with no limit can take the big one
    assert engine.approve_step(request=big, actor=w["mgr2"]).status == "APPROVED"


def test_approval_limit_applies_to_a_manager_with_a_low_limit(w):
    mk_policy(w, "Std", [(Role.PROC_MGR, {})])
    User.objects.filter(pk=w["mgr"].pk).update(approval_limit=D(100))
    w["mgr"].refresh_from_db()
    with pytest.raises(ValidationError, match="approval limit"):
        engine.approve_step(request=open_req(w, amount=101), actor=w["mgr"])


def test_can_act_reflects_role_limit_and_maker(w):
    mk_policy(w, "Std", [(Role.PROC_MGR, {})])
    User.objects.filter(pk=w["mgr2"].pk).update(approval_limit=D(10))
    w["mgr2"].refresh_from_db()
    req = open_req(w, amount=1000)
    assert engine.can_act(req, w["mgr"]) is True
    assert engine.can_act(req, w["mgr2"]) is False  # limit
    assert engine.can_act(req, w["appr_a"]) is False  # wrong role
    assert engine.can_act(req, w["requester"]) is False
    # role + limit + organisation filters: mgr2 (limit) and outsider_mgr (other org) are excluded
    assert {u.id for u in engine.step_approvers(req)} == {w["mgr"].id}


# ======================================================================= delegation
def test_active_delegate_acts_with_the_delegators_authority_and_it_is_recorded(w):
    mk_policy(w, "Std", [(Role.DEPT_APPROVER, {})])
    today = datetime.date.today()
    set_approval_delegate_service(
        approver=w["appr_a"],
        delegate=w["pe"],
        start_date=today - datetime.timedelta(days=1),
        end_date=today + datetime.timedelta(days=3),
        reason="Annual leave",
    )
    req = open_req(w)
    assert engine.can_act(req, w["pe"]) is True
    engine.approve_step(request=req, actor=w["pe"])
    action = ApprovalAction.objects.get(approval_request=req, action="APPROVED")
    assert (
        action.actor_id == w["pe"].id
        and f"acting as delegate for {w['appr_a'].email}" in action.comments
    )


def test_expired_or_future_delegation_gives_no_authority(w):
    mk_policy(w, "Std", [(Role.DEPT_APPROVER, {})])
    today = datetime.date.today()
    # historical (expired) and future delegations are inserted directly: the service refuses to
    # create a delegation whose period is already over
    ApprovalDelegate.objects.create(
        approver=w["appr_a"],
        delegate=w["pe"],
        start_date=today - datetime.timedelta(days=10),
        end_date=today - datetime.timedelta(days=2),
    )
    ApprovalDelegate.objects.create(
        approver=w["appr_a"],
        delegate=w["fin"],
        start_date=today + datetime.timedelta(days=2),
        end_date=today + datetime.timedelta(days=9),
    )
    req = open_req(w)
    for actor in (w["pe"], w["fin"]):
        with pytest.raises(ValidationError, match="not authorized"):
            engine.approve_step(request=req, actor=actor)


def test_delegate_inherits_the_delegators_department_and_limit(w):
    mk_policy(w, "Std", [(Role.DEPT_APPROVER, {})])
    today = datetime.date.today()
    User.objects.filter(pk=w["appr_a"].pk).update(approval_limit=D(100))
    set_approval_delegate_service(
        approver=w["appr_a"],
        delegate=w["pe"],
        start_date=today,
        end_date=today + datetime.timedelta(days=2),
    )
    with pytest.raises(ValidationError, match="approval limit"):
        engine.approve_step(request=open_req(w, amount=500), actor=w["pe"])
    # a delegate of Dept B's approver has no authority over Dept A requests
    set_approval_delegate_service(
        approver=w["appr_b"],
        delegate=w["fin"],
        start_date=today,
        end_date=today + datetime.timedelta(days=2),
    )
    with pytest.raises(ValidationError, match="own department"):
        engine.approve_step(request=open_req(w, amount=50), actor=w["fin"])


# ======================================================================= exception routing
def test_exception_requests_append_the_exception_policys_steps(w):
    mk_policy(w, "Std", [(Role.PROC_MGR, {})])
    exc = mk_policy(w, "Overrun", [(Role.FINANCE_AP, {})], is_exception_route=True)

    normal = open_req(w)
    assert [s["role_code"] for s in normal.steps] == [
        "PROC_MGR"
    ] and normal.exception_policy is None

    flagged = open_req(w, exception_reason="Budget overrun of $5,000")
    assert [s["role_code"] for s in flagged.steps] == ["PROC_MGR", "FINANCE_AP"]
    assert [s["exception"] for s in flagged.steps] == [False, True]
    assert [s["step_number"] for s in flagged.steps] == [1, 2]
    assert flagged.exception_policy == exc and "Budget overrun" in flagged.exception_reason

    engine.approve_step(request=flagged, actor=w["mgr"])
    with pytest.raises(ValidationError, match="not authorized"):
        engine.approve_step(request=flagged, actor=w["mgr2"])  # step 2 needs Finance
    assert engine.approve_step(request=flagged, actor=w["fin"]).status == "APPROVED"


def test_exception_flag_without_an_exception_policy_uses_the_normal_chain(w):
    mk_policy(w, "Std", [(Role.PROC_MGR, {})])
    req = open_req(w, exception_reason="Above estimate")
    assert len(req.steps) == 1 and req.exception_policy is None and req.exception_reason


# ======================================================================= reject / cancel / idempotency
def test_reject_needs_comments_and_a_chain_member_and_ends_the_request(w):
    mk_policy(w, "Std", [(Role.DEPT_APPROVER, {}), (Role.PROC_MGR, {})])
    req = open_req(w)
    with pytest.raises(ValidationError, match="mandatory"):
        engine.reject_request(request=req, actor=w["mgr"], comments="  ")
    with pytest.raises(ValidationError, match="not an approver"):
        engine.reject_request(request=req, actor=w["pe"], comments="no")
    done = engine.reject_request(request=req, actor=w["mgr"], comments="Out of policy")
    assert done.status == ApprovalRequest.STATUS_REJECTED
    with pytest.raises(ValidationError, match="already rejected"):
        engine.approve_step(request=req, actor=w["appr_a"])


def test_opening_twice_returns_the_same_pending_request_and_cancel_closes_it(w):
    mk_policy(w, "Std", [(Role.PROC_MGR, {})])
    t = target(w)
    r1 = open_req(w, t=t)
    r2 = open_req(w, t=t)
    assert r1.pk == r2.pk and ApprovalRequest.objects.count() == 1
    engine.cancel_pending_request(PR, t, actor=w["requester"])
    r1.refresh_from_db()
    assert r1.status == ApprovalRequest.STATUS_CANCELLED
    assert open_req(w, t=t).pk != r1.pk  # a fresh cycle can start afterwards


def test_pending_requests_for_user_lists_only_what_they_can_decide(w):
    mk_policy(w, "Std", [(Role.PROC_MGR, {})])
    req = open_req(w)
    assert [r.pk for r in engine.pending_requests_for_user(w["mgr"])] == [req.pk]
    assert engine.pending_requests_for_user(w["appr_a"]) == []
    assert engine.pending_requests_for_user(w["requester"]) == []
    engine.approve_step(request=req, actor=w["mgr"])
    assert engine.pending_requests_for_user(w["mgr2"]) == []


# ======================================================================= migrations
def test_migrations_gave_every_existing_policy_its_own_key_and_seeded_the_defaults(db):
    """Runs against the policies created by migrations 0003/0006 in a freshly migrated database."""
    policies = list(ApprovalPolicy.objects.all())
    assert policies, "migrations should have seeded default policies"
    assert len({p.policy_key for p in policies}) == len(policies)  # unique, not one shared value
    assert all(p.version == 1 and p.superseded_by_id is None for p in policies)
    seeded_modules = {p.module for p in policies if not p.is_exception_route}
    assert {
        "PR",
        "PO_AMENDMENT",
        "SOURCING",
        "CONTRACT",
        "INVOICE",
        "BUDGET_EXCEPTION",
    } <= seeded_modules
    assert "PO" not in seeded_modules  # PO approval is opt-in
    for p in policies:
        assert p.steps.count() >= 1

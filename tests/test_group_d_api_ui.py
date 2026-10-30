"""Approval API/UI hardening, policy versioning screens, limits, delegation, inbox (group D)."""

import datetime

import pytest
from django.core.exceptions import ValidationError
from django.urls import reverse
from rest_framework.test import APIClient

from apps.accounts.models import Role, User
from apps.approvals import engine
from apps.approvals.models import (
    ApprovalAction,
    ApprovalDelegate,
    ApprovalPolicy,
    ApprovalRequest,
)
from apps.approvals.services import (
    process_approval_action_service,
    revoke_approval_delegate_service,
    set_approval_delegate_service,
)
from apps.audit.models import AuditLog
from apps.orders.models import PurchaseOrder
from apps.orders.services import generate_purchase_order_service, request_po_amendment_service
from apps.organization.models import CostCenter
from tests.test_group_c_lifecycle import D, world  # noqa: F401  (shared fixture)

pytestmark = pytest.mark.django_db

PO = ApprovalPolicy.MODULE_PO


def api(user):
    c = APIClient()
    c.force_authenticate(user)
    return c


def role(w, code):
    return w["db_roles"][code]


def po_policy(w, roles=(Role.PROC_MGR,), **kw):
    return engine.create_policy_with_steps(
        name="PO policy", module=PO, steps=[{"approver_role": role(w, r)} for r in roles], **kw
    )


def pending_po(w, amount=1000):
    po_policy(w)
    return generate_purchase_order_service(
        vendor=w["vendor"],
        cost_center=w["cc"],
        created_by_user=w["pe"],
        line_items=[{"item_description": "Item", "quantity": 1, "unit_price": amount}],
    )


@pytest.fixture
def superadmin(db, db_roles, world):  # noqa: F811
    return User.objects.create_superuser(
        email="root@d.test",
        password="Password123!",
        first_name="R",
        last_name="T",
        role=db_roles[Role.SUPER_ADMIN],
        organization=world["org"],
    )


# ======================================================================= generic approval hole
def test_approval_action_endpoint_refuses_unknown_target_types_and_changes_nothing(
    world,
):  # noqa: F811
    w = world
    action = ApprovalAction.objects.create(
        target_model_name="Budget",
        target_object_id=w["cc"].pk,
        actor=w["requester"],
        action=ApprovalAction.ACTION_SUBMIT,
        previous_state="",
        new_state="SUBMITTED",
    )
    before = ApprovalAction.objects.count()
    for verb in ("approve", "reject"):
        resp = api(w["mgr"]).post(
            f"/api/v1/approvals/actions/{action.pk}/{verb}/", {"comment": "x"}, format="json"
        )
        assert resp.status_code == 400 and resp.data["error"]["code"] == "NOT_DECIDABLE"
    assert ApprovalAction.objects.count() == before  # no fabricated approval row


def test_approval_action_endpoint_cannot_approve_a_po_without_authority(world):  # noqa: F811
    w = world
    po = pending_po(w)
    action = ApprovalAction.objects.filter(target_model_name="PurchaseOrder").first() or (
        ApprovalAction.objects.create(
            target_model_name="PurchaseOrder",
            target_object_id=po.pk,
            actor=w["pe"],
            action=ApprovalAction.ACTION_SUBMIT,
            previous_state="DRAFT",
            new_state="APPROVAL",
        )
    )
    resp = api(w["requester"]).post(
        f"/api/v1/approvals/actions/{action.pk}/approve/", {"comment": "let me"}, format="json"
    )
    assert resp.status_code in (400, 403)
    po.refresh_from_db()
    assert po.status == PurchaseOrder.STATUS_APPROVAL

    resp = api(w["mgr"]).post(
        f"/api/v1/approvals/actions/{action.pk}/approve/", {"comment": "ok"}, format="json"
    )
    assert resp.status_code == 200
    po.refresh_from_db()
    assert po.status == PurchaseOrder.STATUS_ISSUED


def test_generic_service_never_sets_a_status_directly(world):  # noqa: F811
    w = world
    with pytest.raises(ValidationError, match="cannot be approved through the generic"):
        process_approval_action_service(target_object=w["vendor"], actor=w["mgr"], action="APPROVE")
    w["vendor"].refresh_from_db()
    assert w["vendor"].status == "ACTIVE"
    with pytest.raises(ValidationError, match="Unknown approval action"):
        process_approval_action_service(target_object=pending_po(w), actor=w["mgr"], action="MAYBE")


def test_comments_are_new_attributed_entries_and_never_edit_history(world):  # noqa: F811
    w = world
    po = pending_po(w)
    original = ApprovalAction.objects.create(
        target_model_name="PurchaseOrder",
        target_object_id=po.pk,
        actor=w["pe"],
        action=ApprovalAction.ACTION_SUBMIT,
        previous_state="DRAFT",
        new_state="APPROVAL",
        comments="Original submission",
    )
    url = f"/api/v1/approvals/actions/{original.pk}/comment/"
    assert api(w["mgr"]).post(url, {"comment": "  "}, format="json").status_code == 400
    resp = api(w["mgr"]).post(url, {"comment": "Check the unit price"}, format="json")
    assert resp.status_code == 201
    original.refresh_from_db()
    assert original.comments == "Original submission"  # untouched
    added = ApprovalAction.objects.get(pk=resp.data["id"])
    assert added.action == ApprovalAction.ACTION_COMMENT and added.actor == w["mgr"]
    assert added.comments == "Check the unit price"


# ======================================================================= policy API
POLICY = {
    "name": "API policy",
    "module": "CONTRACT",
    "min_amount": "0",
    "steps_input": [{"approver_role": "PROC_MGR", "description": "Manager"}],
}


def test_policy_api_create_requires_steps_and_a_valid_band(world, superadmin):  # noqa: F811
    c = api(superadmin)
    bad = dict(POLICY, steps_input=[])
    assert c.post("/api/v1/approvals/policies/", bad, format="json").status_code == 400
    bad = dict(POLICY, min_amount="500", max_amount="100")
    assert c.post("/api/v1/approvals/policies/", bad, format="json").status_code == 400
    bad = dict(POLICY, steps_input=[{"approver_role": "NO_SUCH_ROLE"}])
    assert c.post("/api/v1/approvals/policies/", bad, format="json").status_code == 400
    ok = c.post("/api/v1/approvals/policies/", POLICY, format="json")
    assert ok.status_code == 201, ok.data
    assert ok.data["version"] == 1 and ok.data["steps"][0]["approver_role_code"] == "PROC_MGR"


def test_policy_api_update_creates_a_new_version_and_retires_the_old(
    world, superadmin
):  # noqa: F811
    c = api(superadmin)
    v1 = c.post("/api/v1/approvals/policies/", POLICY, format="json").data
    resp = c.patch(
        f"/api/v1/approvals/policies/{v1['id']}/",
        {
            "max_amount": "9999",
            "change_note": "Cap at 9,999",
            "steps_input": [{"approver_role": "FINANCE_AP"}],
        },
        format="json",
    )
    assert resp.status_code == 200, resp.data
    v2 = resp.data
    assert v2["version"] == 2 and v2["id"] != v1["id"] and v2["policy_key"] == v1["policy_key"]
    assert (
        v2["steps"][0]["approver_role_code"] == "FINANCE_AP" and v2["change_note"] == "Cap at 9,999"
    )
    old = ApprovalPolicy.objects.get(pk=v1["id"])
    assert (
        old.superseded_by_id
        and not old.is_active
        and old.steps.first().approver_role.code == "PROC_MGR"
    )

    stale = c.patch(f"/api/v1/approvals/policies/{v1['id']}/", {"max_amount": "1"}, format="json")
    assert stale.status_code == 400  # only the latest version can be revised
    changed_module = c.patch(
        f"/api/v1/approvals/policies/{v2['id']}/", {"module": "PO"}, format="json"
    )
    assert changed_module.status_code == 400

    current = c.get("/api/v1/approvals/policies/?current=true&module=CONTRACT").data
    ids = [p["id"] for p in (current["results"] if isinstance(current, dict) else current)]
    assert v2["id"] in ids and v1["id"] not in ids


def test_policy_api_delete_retires_but_never_deletes(world, superadmin):  # noqa: F811
    c = api(superadmin)
    v1 = c.post("/api/v1/approvals/policies/", POLICY, format="json").data
    resp = c.delete(f"/api/v1/approvals/policies/{v1['id']}/")
    assert resp.status_code == 200 and resp.data["is_active"] is False
    assert ApprovalPolicy.objects.filter(pk=v1["id"]).exists()
    assert (
        AuditLog.objects.filter(target_model="ApprovalPolicy", target_object_id=v1["id"]).count()
        >= 2
    )


def test_policy_api_forbidden_for_non_admin_roles(world):  # noqa: F811
    w = world
    for user in (w["requester"], w["pe"], w["finance"]):
        assert (
            api(user).post("/api/v1/approvals/policies/", POLICY, format="json").status_code == 403
        )


# ======================================================================= Super Admin screens
def test_policy_screen_creates_a_policy_with_cost_center_and_chain(
    world, superadmin, client
):  # noqa: F811
    w = world
    client.force_login(superadmin)
    resp = client.post(
        "/superadmin/policies/create/",
        {
            "name": "CC special",
            "module": "PO",
            "cost_center": str(w["cc"].pk),
            "min_amount": "0",
            "is_active": "on",
            "step_role_1": str(role(w, Role.PROC_MGR).pk),
            "step_role_2": str(role(w, Role.FINANCE_AP).pk),
            "step_description_2": "Finance sign-off",
        },
    )
    assert resp.status_code == 200
    policy = ApprovalPolicy.objects.get(name="CC special")
    assert policy.cost_center == w["cc"] and policy.version == 1
    assert [s.approver_role.code for s in policy.steps.order_by("step_number")] == [
        "PROC_MGR",
        "FINANCE_AP",
    ]


def test_policy_screen_validates_chain_band_and_scope(world, superadmin, client):  # noqa: F811
    w = world
    client.force_login(superadmin)
    base = {"name": "Bad", "module": "PO", "min_amount": "0", "is_active": "on"}
    resp = client.post("/superadmin/policies/create/", base)
    assert "at least one approval step" in resp.content.decode()
    resp = client.post(
        "/superadmin/policies/create/",
        dict(base, min_amount="900", max_amount="100", step_role_1=str(role(w, Role.PROC_MGR).pk)),
    )
    assert "cannot be greater than maximum" in resp.content.decode()
    other_dept_cc = CostCenter.objects.create(
        name="Elsewhere", code="CC-ELSE", department=w["dept"]
    )
    from apps.organization.models import Department

    dept_b = Department.objects.create(name="Dept B", code="DB", organization=w["org"])
    resp = client.post(
        "/superadmin/policies/create/",
        dict(
            base,
            department=str(dept_b.pk),
            cost_center=str(other_dept_cc.pk),
            step_role_1=str(role(w, Role.PROC_MGR).pk),
        ),
    )
    assert "does not belong to the chosen department" in resp.content.decode()
    resp = client.post(
        "/superadmin/policies/create/",
        dict(base, step_approver_2=str(w["mgr"].pk), step_role_1=str(role(w, Role.PROC_MGR).pk)),
    )
    assert "Choose the approver role" in resp.content.decode()
    assert not ApprovalPolicy.objects.filter(name="Bad").exists()


def test_policy_screen_edit_creates_a_new_version_and_blocks_superseded_edits(
    world, superadmin, client
):  # noqa: F811
    w = world
    v1 = po_policy(w, roles=(Role.PROC_MGR,))
    client.force_login(superadmin)
    resp = client.post(
        f"/superadmin/policies/{v1.pk}/edit/",
        {
            "name": "PO policy",
            "module": "PO",
            "min_amount": "0",
            "max_amount": "50000",
            "is_active": "on",
            "step_role_1": str(role(w, Role.PROC_MGR).pk),
            "step_role_2": str(role(w, Role.FINANCE_AP).pk),
            "change_note": "Add finance",
        },
    )
    assert resp.status_code == 200
    v1.refresh_from_db()
    v2 = ApprovalPolicy.objects.get(policy_key=v1.policy_key, version=2)
    assert (
        v1.superseded_by_id == v2.id and v2.change_note == "Add finance" and v2.steps.count() == 2
    )

    stale = client.get(f"/superadmin/policies/{v1.pk}/edit/")
    assert stale.status_code == 409
    listing = client.get("/superadmin/policies/").content.decode()
    assert "v2" in listing and "Revise" in listing
    assert f"/superadmin/policies/{v1.pk}/edit/" not in listing  # superseded rows are hidden...
    history = client.get("/superadmin/policies/?history=1").content.decode()
    assert "Superseded" in history  # ...unless history is requested


def test_policy_screens_are_super_admin_only(world, client):  # noqa: F811
    w = world
    client.force_login(w["mgr"])
    assert client.get("/superadmin/policies/").status_code in (302, 403)
    assert client.post("/superadmin/policies/create/", {"name": "x"}).status_code in (302, 403)


# ======================================================================= approval limits
def test_user_form_saves_and_validates_the_approval_limit(world, superadmin, client):  # noqa: F811
    w = world
    client.force_login(superadmin)
    target = w["mgr"]
    base = {
        "email": target.email,
        "role": str(target.role_id),
        "is_active": "on",
        "approval_limit": "25000.00",
    }
    resp = client.post(f"/superadmin/users/{target.pk}/edit/", base)
    assert resp.status_code in (200, 302)
    target.refresh_from_db()
    assert target.approval_limit == D("25000.00")

    resp = client.post(f"/superadmin/users/{target.pk}/edit/", dict(base, approval_limit="-5"))
    assert "cannot be negative" in resp.content.decode()
    target.refresh_from_db()
    assert target.approval_limit == D("25000.00")


def test_users_api_exposes_the_limit_and_only_admins_can_set_it(world, superadmin):  # noqa: F811
    w = world
    resp = api(superadmin).patch(
        f"/api/v1/accounts/users/{w['mgr'].pk}/", {"approval_limit": "1000.00"}, format="json"
    )
    assert resp.status_code == 200 and resp.data["approval_limit"] == "1000.00"
    forbidden = api(w["mgr"]).patch(
        f"/api/v1/accounts/users/{w['mgr'].pk}/", {"approval_limit": None}, format="json"
    )
    assert forbidden.status_code in (403, 404)
    w["mgr"].refresh_from_db()
    assert w["mgr"].approval_limit == D("1000.00")  # an approver cannot lift their own limit


# ======================================================================= delegation
def test_delegation_service_validation(world):  # noqa: F811
    w = world
    today = datetime.date.today()
    ok = dict(start_date=today, end_date=today + datetime.timedelta(days=3))
    with pytest.raises(ValidationError, match="yourself"):
        set_approval_delegate_service(approver=w["mgr"], delegate=w["mgr"], **ok)
    with pytest.raises(ValidationError, match="before its start"):
        set_approval_delegate_service(
            approver=w["mgr"],
            delegate=w["pe"],
            start_date=today,
            end_date=today - datetime.timedelta(days=1),
        )
    with pytest.raises(ValidationError, match="already over"):
        set_approval_delegate_service(
            approver=w["mgr"],
            delegate=w["pe"],
            start_date=today - datetime.timedelta(days=9),
            end_date=today - datetime.timedelta(days=2),
        )
    with pytest.raises(ValidationError, match="Valid start and end dates"):
        set_approval_delegate_service(
            approver=w["mgr"], delegate=w["pe"], start_date="nope", end_date="2026-01-01"
        )
    inactive = User.objects.create_user(
        email="gone@d.test", password="Password123!", role=role(w, Role.PROC_MGR), is_active=False
    )
    with pytest.raises(ValidationError, match="active internal user"):
        set_approval_delegate_service(approver=w["mgr"], delegate=inactive, **ok)
    vendor_user = User.objects.create_user(
        email="v@d.test",
        password="Password123!",
        role=role(w, Role.VENDOR_USER),
        vendor=w["vendor"],
    )
    with pytest.raises(ValidationError, match="active internal user"):
        set_approval_delegate_service(approver=w["mgr"], delegate=vendor_user, **ok)

    created = set_approval_delegate_service(approver=w["mgr"], delegate=w["pe"], **ok)
    assert created.is_active
    assert AuditLog.objects.filter(
        target_model="ApprovalDelegate", target_object_id=str(created.id)
    ).exists()
    with pytest.raises(ValidationError, match="overlapping"):
        set_approval_delegate_service(approver=w["mgr"], delegate=w["pe"], **ok)


def test_revoking_a_delegation_removes_the_authority_immediately(world):  # noqa: F811
    w = world
    pending_po(w)
    today = datetime.date.today()
    deleg = set_approval_delegate_service(
        approver=w["mgr"],
        delegate=w["finance"],
        start_date=today,
        end_date=today + datetime.timedelta(days=5),
    )
    req = ApprovalRequest.objects.get(module=PO)
    assert engine.can_act(req, w["finance"])
    with pytest.raises(ValidationError, match="Only the approver"):
        revoke_approval_delegate_service(delegation=deleg, actor=w["pe"])
    revoke_approval_delegate_service(delegation=deleg, actor=w["mgr"])
    assert not engine.can_act(req, w["finance"])
    ApprovalDelegate.objects.get(pk=deleg.pk).is_active is False


def test_delegation_screen_creates_and_revokes(world, client):  # noqa: F811
    w = world
    client.force_login(w["mgr"])
    today = datetime.date.today()
    client.post(
        reverse("approvals_delegation"),
        {
            "delegate_id": str(w["pe"].pk),
            "start_date": today.isoformat(),
            "end_date": (today + datetime.timedelta(days=2)).isoformat(),
            "reason": "Leave",
        },
    )
    deleg = ApprovalDelegate.objects.get(approver=w["mgr"])
    assert deleg.is_active
    page = client.get(reverse("approvals_delegation")).content.decode()
    assert "Revoke" in page and "pe@l.test" in page
    client.post(reverse("approvals_delegation_revoke", args=[deleg.pk]))
    deleg.refresh_from_db()
    assert deleg.is_active is False
    # someone else cannot revoke it
    other = set_approval_delegate_service(
        approver=w["mgr"],
        delegate=w["finance"],
        start_date=today,
        end_date=today + datetime.timedelta(days=2),
    )
    client.force_login(w["pe"])
    client.post(reverse("approvals_delegation_revoke", args=[other.pk]))
    other.refresh_from_db()
    assert other.is_active is True


# ======================================================================= generic inbox
def test_inbox_lists_non_requisition_work_the_user_can_decide_and_decides_it(
    world, client
):  # noqa: F811
    w = world
    po = pending_po(w, 4000)
    client.force_login(w["mgr"])
    page = client.get(reverse("approvals_inbox")).content.decode()
    assert "Other approvals awaiting you" in page and po.po_number in page
    assert "Purchase Order" in page and "Step 1/1" in page

    client.force_login(w["requester"])
    assert (
        "Other approvals awaiting you"
        not in client.get(reverse("approvals_inbox")).content.decode()
    )

    req = ApprovalRequest.objects.get(module=PO, target_object_id=po.id)
    client.force_login(w["mgr"])
    resp = client.post(
        reverse("approval_request_decide", args=[req.pk]), {"action": "APPROVE", "comments": "ok"}
    )
    assert resp.status_code == 302
    po.refresh_from_db()
    assert po.status == PurchaseOrder.STATUS_ISSUED
    assert (
        "Other approvals awaiting you"
        not in client.get(reverse("approvals_inbox")).content.decode()
    )


def test_inbox_decision_enforces_the_engine_and_requires_a_reason_to_reject(
    world, client
):  # noqa: F811
    w = world
    po = pending_po(w)
    req = ApprovalRequest.objects.get(module=PO, target_object_id=po.id)
    url = reverse("approval_request_decide", args=[req.pk])

    client.force_login(w["requester"])  # not an approver
    client.post(url, {"action": "APPROVE"})
    po.refresh_from_db()
    assert po.status == PurchaseOrder.STATUS_APPROVAL

    client.force_login(w["mgr"])
    client.post(url, {"action": "REJECT", "comments": ""})  # reason mandatory
    po.refresh_from_db()
    assert po.status == PurchaseOrder.STATUS_APPROVAL
    client.post(url, {"action": "REJECT", "comments": "Wrong vendor"})
    po.refresh_from_db()
    assert po.status == PurchaseOrder.STATUS_DRAFT
    assert ApprovalRequest.objects.get(pk=req.pk).status == "REJECTED"


def test_inbox_handles_amendments_and_budget_exceptions(world, client):  # noqa: F811
    from apps.budgets.models import Budget
    from apps.orders.services import request_po_budget_exception_service

    w = world
    po = generate_purchase_order_service(
        vendor=w["vendor"],
        cost_center=w["cc"],
        created_by_user=w["pe"],
        line_items=[{"item_description": "Item", "quantity": 1, "unit_price": 1000}],
    )
    PurchaseOrder.objects.filter(pk=po.pk).update(status=PurchaseOrder.STATUS_ACKNOWLEDGED)
    po.refresh_from_db()
    amendment = request_po_amendment_service(
        po=po,
        reason="Price",
        requested_by_user=w["pe"],
        updated_line_items=[{"item_description": "Item", "quantity": 1, "unit_price": 1500}],
    )
    client.force_login(w["mgr"])
    assert "amendment #1" in client.get(reverse("approvals_inbox")).content.decode()
    req = ApprovalRequest.objects.get(module="PO_AMENDMENT", target_object_id=amendment.id)
    client.post(
        reverse("approval_request_decide", args=[req.pk]), {"action": "APPROVE", "comments": "ok"}
    )
    po.refresh_from_db()
    assert po.version == 2

    # budget exception on a draft PO, decided by Finance from the inbox
    Budget.objects.filter(cost_center=w["cc"]).update(allocated_amount=D(3000))
    draft = generate_purchase_order_service(
        vendor=w["vendor"],
        cost_center=w["cc"],
        created_by_user=w["pe"],
        line_items=[{"item_description": "Big", "quantity": 1, "unit_price": 9000}],
        initial_status=PurchaseOrder.STATUS_DRAFT,
    )
    bx = request_po_budget_exception_service(
        po=draft, requested_by=w["pe"], reason="Urgent replacement needed"
    )
    client.force_login(w["finance"])
    assert "Budget Exception" in client.get(reverse("approvals_inbox")).content.decode()
    client.post(
        reverse("approval_request_decide", args=[bx.pk]),
        {"action": "APPROVE", "comments": "Granted"},
    )
    bx.refresh_from_db()
    assert bx.status == "APPROVED"
    draft.refresh_from_db()
    assert draft.status == PurchaseOrder.STATUS_DRAFT  # exception granted, PO itself not yet issued


def test_decide_view_ignores_requests_that_are_no_longer_pending(world, client):  # noqa: F811
    w = world
    po = pending_po(w)
    req = ApprovalRequest.objects.get(module=PO, target_object_id=po.id)
    client.force_login(w["mgr"])
    url = reverse("approval_request_decide", args=[req.pk])
    client.post(url, {"action": "APPROVE"})
    again = client.post(url, {"action": "APPROVE"}, follow=True)
    assert "no longer pending" in again.content.decode()

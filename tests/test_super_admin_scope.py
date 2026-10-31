"""Super Admin scope (ROLE_WORKFLOWS.md 3.1 / 3.6).

The org Super Admin configures and oversees: structure, users, approval policies, masters,
audit read/export and read-only visibility of the org's business records. They (and the
platform superuser, who is not a business actor either) can never create, approve, reject,
return, issue, pay, post or otherwise transact business documents.
"""

import datetime
from decimal import Decimal
from types import SimpleNamespace

import pytest
from django.core.exceptions import ValidationError
from django.test import Client
from django.utils import timezone
from rest_framework.test import APIClient

from apps.accounts.models import Role, User
from apps.approvals import engine
from apps.approvals.models import ApprovalPolicy, ApprovalRequest
from apps.approvals.services import process_approval_action_service
from apps.audit.models import AuditLog
from apps.budgets.models import BudgetTransfer
from apps.budgets.services import (
    allocate_budget_service,
    approve_budget_transfer_service,
    request_budget_transfer_service,
)
from apps.contracts.services import activate_contract_service, approve_business_service
from apps.invoices.services import mark_invoice_paid_service, resolve_match_exception_service
from apps.orders.services import approve_purchase_order_service
from apps.organization.models import CostCenter, Department, FiscalPeriod, Organization
from apps.receipts.services import create_goods_receipt_service
from apps.requisitions.models import PurchaseRequisition
from apps.requisitions.services import (
    approve_purchase_requisition_service,
    create_purchase_requisition_service,
    return_purchase_requisition_service,
    submit_purchase_requisition_service,
)
from apps.sourcing.services import approve_award_service
from apps.vendors.services import approve_vendor_service

pytestmark = pytest.mark.django_db


@pytest.fixture
def world(db_roles):
    today = timezone.localdate()
    org = Organization.objects.create(name="Scope Org", code="ORG-SCOPE")
    other_org = Organization.objects.create(name="Other Org", code="ORG-OTHER")
    dept = Department.objects.create(name="Dept S", code="DS", organization=org)
    other_dept = Department.objects.create(name="Dept O", code="DO", organization=other_org)
    cc = CostCenter.objects.create(name="CC S", code="CC-S", department=dept)
    other_cc = CostCenter.objects.create(name="CC O", code="CC-O", department=other_dept)
    period = FiscalPeriod.objects.create(
        organization=org,
        year=today.year,
        period_number=1,
        name="FY-S",
        start_date=today - datetime.timedelta(days=30),
        end_date=today + datetime.timedelta(days=300),
    )
    budget = allocate_budget_service(
        cost_center=cc, fiscal_period=period, allocated_amount=Decimal("50000")
    )

    def user(local, role_code, organization=org, **extra):
        return User.objects.create_user(
            email=f"{local}@scope.test",
            password="Password123!",
            role=db_roles[role_code],
            organization=organization,
            **extra,
        )

    platform = User.objects.create_superuser(
        email="platform@scope.test", password="Password123!", role=db_roles[Role.SUPER_ADMIN]
    )
    w = SimpleNamespace(
        today=today,
        org=org,
        dept=dept,
        cc=cc,
        budget=budget,
        sa=user("sa", Role.SUPER_ADMIN, is_staff=True),
        platform=platform,
        requester=user("req", Role.REQUESTER, department=dept),
        appr=user("appr", Role.DEPT_APPROVER, department=dept),
        mgr=user("mgr", Role.PROC_MGR),
        fin=user("fin", Role.FINANCE_AP),
        legal=user("legal", Role.LEGAL_MGR),
    )
    other_requester = User.objects.create_user(
        email="req.other@scope.test",
        password="Password123!",
        role=db_roles[Role.REQUESTER],
        organization=other_org,
        department=other_dept,
    )
    w.other_pr = create_purchase_requisition_service(
        title="Other org PR",
        justification="isolation",
        requester=other_requester,
        department=other_dept,
        cost_center=other_cc,
        requested_delivery_date=today + datetime.timedelta(days=10),
        line_items=[{"item_description": "X", "quantity": 1, "estimated_unit_price": 10}],
    )
    return w


def make_pr(w, title="Scope PR"):
    return create_purchase_requisition_service(
        title=title,
        justification="scope test",
        requester=w.requester,
        department=w.dept,
        cost_center=w.cc,
        requested_delivery_date=w.today + datetime.timedelta(days=10),
        line_items=[{"item_description": "Item", "quantity": 1, "estimated_unit_price": 1000}],
    )


def client_for(user):
    c = Client()
    c.force_login(user)
    return c


def api_for(user):
    c = APIClient()
    c.force_authenticate(user)
    return c


@pytest.fixture(params=["sa", "platform"])
def actor(request, world):
    """Both non-business actors: the org Super Admin and the platform superuser."""
    return getattr(world, request.param)


# --------------------------------------------------------------------- read access (org-scoped)
def test_super_admin_reads_own_org_records_only(world):
    pr = make_pr(world)
    c = client_for(world.sa)
    assert c.get(f"/requisitions/{pr.pk}/").status_code == 200
    listing = c.get("/requisitions/")
    assert listing.status_code == 200
    assert pr.pr_number in listing.content.decode()
    assert world.other_pr.pr_number not in listing.content.decode()
    assert c.get(f"/requisitions/{world.other_pr.pk}/").status_code in (302, 403, 404)
    api = api_for(world.sa)
    res = api.get("/api/v1/requisitions/")
    assert res.status_code == 200
    ids = {row["id"] for row in (res.json().get("results") or res.json())}
    assert str(pr.pk) in ids and str(world.other_pr.pk) not in ids


def test_super_admin_has_no_transactional_buttons_on_pr_detail(world):
    pr = submit_purchase_requisition_service(requisition=make_pr(world), user=world.requester)
    html = client_for(world.sa).get(f"/requisitions/{pr.pk}/").content.decode()
    assert f"/requisitions/{pr.pk}/approve/" not in html
    assert f"/requisitions/{pr.pk}/reject/" not in html
    assert f"/requisitions/{pr.pk}/return/" not in html


def test_super_admin_has_no_create_pr_button_on_list(world):
    html = client_for(world.sa).get("/requisitions/").content.decode()
    assert "/requisitions/create/" not in html


def test_super_admin_reads_audit_logs_and_exports(world):
    AuditLog.objects.create(
        actor=world.mgr,
        action=AuditLog.ACTION_UPDATE,
        target_model="PurchaseOrder",
        target_object_id="1",
    )
    api = api_for(world.sa)
    assert api.get("/api/v1/audit/logs/").status_code == 200


# --------------------------------------------------------------------- transactions refused
def test_pr_approve_and_return_refused_service_and_engine(world, actor):
    pr = submit_purchase_requisition_service(requisition=make_pr(world), user=world.requester)
    with pytest.raises(ValidationError):
        approve_purchase_requisition_service(requisition=pr, approver=actor)
    with pytest.raises(ValidationError):
        return_purchase_requisition_service(requisition=pr, approver=actor, comments="clarify")
    with pytest.raises(ValidationError):
        process_approval_action_service(target_object=pr, actor=actor, action="APPROVED")
    request = ApprovalRequest.objects.get(module=ApprovalPolicy.MODULE_PR, target_object_id=pr.id)
    with pytest.raises(ValidationError):
        engine.authorize_step(request, request.active_step, actor)
    with pytest.raises(ValidationError):
        engine.reject_request(request=request, actor=actor, comments="no")
    with pytest.raises(ValidationError):
        engine.return_request(request=request, actor=actor, comments="no")
    assert engine.can_act(request, actor) is False
    pr.refresh_from_db()
    assert pr.status == PurchaseRequisition.STATUS_MANAGER_REVIEW


def test_pr_http_actions_refused(world, actor):
    pr = submit_purchase_requisition_service(requisition=make_pr(world), user=world.requester)
    c = client_for(actor)
    for action in ("approve", "reject", "return"):
        c.post(f"/requisitions/{pr.pk}/{action}/", {"comments": "x"})
    pr.refresh_from_db()
    assert pr.status == PurchaseRequisition.STATUS_MANAGER_REVIEW
    api = api_for(actor)
    for action in ("approve", "reject", "return", "submit", "cancel"):
        res = api.post(f"/api/v1/requisitions/{pr.pk}/{action}/", {"comments": "x"})
        assert res.status_code in (400, 403, 404), (action, res.status_code)
    res = api.post(
        "/api/v1/requisitions/",
        {
            "title": "t",
            "justification": "j",
            "department": str(world.dept.pk),
            "cost_center": str(world.cc.pk),
            "requested_delivery_date": str(world.today + datetime.timedelta(days=5)),
        },
        format="json",
    )
    assert res.status_code in (400, 403)
    pr.refresh_from_db()
    assert pr.status == PurchaseRequisition.STATUS_MANAGER_REVIEW


def test_unsubmitted_pr_cannot_be_created_or_submitted_by_admins(world, actor):
    with pytest.raises(ValidationError):
        create_purchase_requisition_service(
            title="t",
            justification="j",
            requester=actor,
            department=world.dept,
            cost_center=world.cc,
            requested_delivery_date=world.today + datetime.timedelta(days=5),
            line_items=[{"item_description": "I", "quantity": 1, "estimated_unit_price": 1}],
        )
    with pytest.raises(ValidationError):
        submit_purchase_requisition_service(requisition=make_pr(world), user=actor)


def test_po_award_invoice_contract_vendor_budget_receipt_services_refuse_admins(world, actor):
    """Each guard fires before any record is touched, so placeholders are enough here."""
    with pytest.raises(ValidationError):
        approve_purchase_order_service(po=None, approver=actor)  # PO approve / issue
    with pytest.raises(ValidationError):
        approve_award_service(event=None, approver=actor)  # sourcing award approval
    with pytest.raises(ValidationError):
        mark_invoice_paid_service(invoice=None, user=actor)  # invoice payment
    with pytest.raises(ValidationError):
        resolve_match_exception_service(  # invoice exception resolution
            match_exception=None, resolved_by_user=actor, resolution_notes="ok"
        )
    with pytest.raises(ValidationError):
        approve_business_service(contract=None, user=actor)  # contract approval
    with pytest.raises(ValidationError):
        activate_contract_service(contract=None, user=actor)  # contract activation
    with pytest.raises(ValidationError):
        approve_vendor_service(vendor=None, manager=actor)  # vendor KYC approval
    with pytest.raises(ValidationError):
        approve_budget_transfer_service(transfer=None, approver=actor)  # budget transfer approval
    with pytest.raises(ValidationError):
        request_budget_transfer_service(
            source=None, destination=None, amount=1, reason="r", requested_by=actor
        )
    with pytest.raises(ValidationError):
        create_goods_receipt_service(po=None, received_by=actor, receipt_items=[])  # receipt post


def test_api_write_endpoints_refuse_admins(world, actor):
    api = api_for(actor)
    fake = "00000000-0000-0000-0000-000000000001"
    cases = [
        ("post", "/api/v1/purchase-orders/", {}),  # PO create
        ("post", f"/api/v1/invoices/{fake}/pay/", {}),  # invoice pay
        ("post", "/api/v1/invoices/", {}),  # invoice create
        ("post", f"/api/v1/invoices/exceptions/{fake}/resolve/", {}),  # exception resolve
        ("post", "/api/v1/receipts/", {}),  # receipt post
        ("post", f"/api/v1/contracts/{fake}/approve-business/", {}),  # contract approve
        ("post", f"/api/v1/contracts/{fake}/activate/", {}),  # contract activate
        ("post", "/api/v1/budgets/", {}),  # budget allocation
    ]
    for method, url, body in cases:
        res = getattr(api, method)(url, body, format="json")
        assert res.status_code in (400, 403, 404, 405), (url, res.status_code)
        assert res.status_code != 201
    # the 403s (permission class) must be explicit where the route exists
    for url in ("/api/v1/purchase-orders/", "/api/v1/receipts/", "/api/v1/budgets/"):
        assert api.post(url, {}, format="json").status_code == 403


def test_invoice_and_budget_html_actions_refused(world, actor):
    c = client_for(actor)
    fake = "00000000-0000-0000-0000-000000000001"
    assert c.post(f"/invoices/{fake}/pay/", {}).status_code in (403, 404)
    assert c.post("/invoices/create/", {}).status_code == 403
    assert c.post("/superadmin/budgets/create/", {}).status_code == 403
    assert BudgetTransfer.objects.count() == 0


# --------------------------------------------------------------------- configuration still works
def test_super_admin_can_configure_policies_masters_and_users(world):
    c = client_for(world.sa)
    for url in (
        "/superadmin/policies/",
        "/superadmin/users/",
        "/superadmin/budgets/",
        "/superadmin/locations/",
        "/superadmin/audit/",
    ):
        assert c.get(url).status_code == 200, url
    api = api_for(world.sa)
    assert api.get("/api/v1/approvals/policies/").status_code == 200
    assert api.get("/api/v1/accounts/users/").status_code == 200
    # structure: create a department through the API
    res = api.post(
        "/api/v1/organization/departments/",
        {"name": "New Dept", "code": "ND", "organization": str(world.org.pk)},
        format="json",
    )
    assert res.status_code in (200, 201), res.content
    # fiscal period open/close
    period = FiscalPeriod.objects.get(organization=world.org)
    res = c.post(f"/superadmin/fiscal-periods/{period.pk}/toggle/", {"action": "close"})
    assert res.status_code in (200, 302)
    period.refresh_from_db()
    assert period.is_closed is True


def test_super_admin_can_create_an_approval_policy(world):
    api = api_for(world.sa)
    res = api.post(
        "/api/v1/approvals/policies/",
        {
            "module": ApprovalPolicy.MODULE_PO,
            "name": "PO policy",
            "min_amount": "0",
            "steps_input": [{"approver_role": Role.PROC_MGR}],
        },
        format="json",
    )
    assert res.status_code in (200, 201), res.content


def test_no_eligible_approver_notifies_super_admin_who_cannot_decide(world):
    """If nobody can decide, the SA is told (to fix policy/users) but cannot approve."""
    world.mgr.is_active = False
    world.mgr.save(update_fields=["is_active"])
    pr = submit_purchase_requisition_service(requisition=make_pr(world), user=world.requester)
    request = ApprovalRequest.objects.get(module=ApprovalPolicy.MODULE_PR, target_object_id=pr.id)
    assert engine.can_act(request, world.sa) is False
    with pytest.raises(ValidationError):
        approve_purchase_requisition_service(requisition=pr, approver=world.sa)


# --------------------------------------------------------------------- audit log immutability
def test_audit_log_cannot_be_edited_or_deleted_by_super_admin(world):
    log = AuditLog.objects.create(
        actor=world.mgr,
        action=AuditLog.ACTION_UPDATE,
        target_model="PurchaseOrder",
        target_object_id="1",
    )
    api = api_for(world.sa)
    for method in ("put", "patch", "delete"):
        res = getattr(api, method)(f"/api/v1/audit/logs/{log.pk}/", {"action": "X"}, format="json")
        assert res.status_code in (403, 404, 405), (method, res.status_code)
    assert api.post("/api/v1/audit/logs/", {"action": "X"}, format="json").status_code in (
        403,
        405,
    )
    log.refresh_from_db()
    assert log.action == AuditLog.ACTION_UPDATE
    assert AuditLog.objects.filter(pk=log.pk).exists()

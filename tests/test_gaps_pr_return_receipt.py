"""PR return/withdraw and receipt notice / vendor acknowledgement (ROLE_WORKFLOWS gaps)."""

import datetime
from decimal import Decimal

import pytest
from django.core.exceptions import ValidationError
from django.utils import timezone

from apps.accounts.models import Role, User
from apps.approvals.models import ApprovalAction, ApprovalPolicy, ApprovalRequest
from apps.audit.models import AuditLog
from apps.budgets.models import BudgetReservation
from apps.budgets.services import allocate_budget_service, check_and_reserve_budget_service
from apps.notifications.models import Notification
from apps.orders.models import POLine, PurchaseOrder
from apps.organization.models import CostCenter, Department, FiscalPeriod, Organization
from apps.receipts.models import GoodsReceipt
from apps.receipts.services import acknowledge_receipt_service, create_goods_receipt_service
from apps.requisitions.models import PurchaseRequisition
from apps.requisitions.services import (
    approve_purchase_requisition_service as approve,
)
from apps.requisitions.services import (
    create_purchase_requisition_service,
    return_purchase_requisition_service,
    withdraw_purchase_requisition_service,
)
from apps.requisitions.services import (
    submit_purchase_requisition_service as submit,
)
from apps.vendors.models import Vendor, VendorCategory

pytestmark = pytest.mark.django_db

PR = ApprovalPolicy.MODULE_PR


def build_org(db_roles, tag):
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
        cost_center=cc, fiscal_period=period, allocated_amount=Decimal("50000")
    )

    def user(local, role_code, **extra):
        return User.objects.create_user(
            email=f"{local}.{tag.lower()}@gap.test",
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
        fin=user("fin", Role.FINANCE_AP),
        stores=user("stores", Role.STORES_RECEIVER),
    )


@pytest.fixture
def w(db_roles):
    return build_org(db_roles, "A")


def make_pr(w, amount=1000):
    return create_purchase_requisition_service(
        title="Gap PR",
        justification="gap test",
        requester=w["requester"],
        department=w["dept"],
        cost_center=w["cc"],
        requested_delivery_date=w["today"] + datetime.timedelta(days=10),
        line_items=[{"item_description": "Item", "quantity": 1, "estimated_unit_price": amount}],
    )


# ------------------------------------------------------------------ return / withdraw
def test_approver_returns_pr_with_mandatory_comment_and_requester_resubmits(w):
    pr = submit(requisition=make_pr(w), user=w["requester"])
    first = ApprovalRequest.objects.get(module=PR, target_object_id=pr.id)

    with pytest.raises(ValidationError):
        return_purchase_requisition_service(requisition=pr, approver=w["appr"], comments="  ")
    with pytest.raises(ValidationError):  # the requester is not an approver
        return_purchase_requisition_service(requisition=pr, approver=w["requester"], comments="x")

    pr = return_purchase_requisition_service(
        requisition=pr, approver=w["appr"], comments="Need a quote"
    )
    assert pr.status == PurchaseRequisition.STATUS_RETURNED
    first.refresh_from_db()
    assert first.status == ApprovalRequest.STATUS_RETURNED
    assert ApprovalAction.objects.filter(
        approval_request=first, action=ApprovalAction.ACTION_RETURN, comments="Need a quote"
    ).exists()
    assert Notification.objects.filter(
        recipient=w["requester"], notification_type=Notification.TYPE_PR_RETURNED
    ).exists()

    # cannot be approved while returned; requester resubmits -> brand new request, history kept
    with pytest.raises(ValidationError):
        approve(requisition=pr, approver=w["appr"])
    pr = submit(requisition=pr, user=w["requester"])
    assert pr.status == PurchaseRequisition.STATUS_MANAGER_REVIEW
    reqs = ApprovalRequest.objects.filter(module=PR, target_object_id=pr.id)
    assert reqs.count() == 2
    assert reqs.filter(status=ApprovalRequest.STATUS_PENDING).count() == 1
    first.refresh_from_db()
    assert first.status == ApprovalRequest.STATUS_RETURNED


def test_finance_return_releases_reservation(w):
    pr = submit(requisition=make_pr(w), user=w["requester"])
    pr = approve(requisition=pr, approver=w["appr"])
    assert pr.status == PurchaseRequisition.STATUS_BUDGET_REVIEW
    check_and_reserve_budget_service(requisition=pr, requested_by_user=w["fin"])
    assert BudgetReservation.objects.filter(requisition=pr, status="RESERVED").exists()
    pr = return_purchase_requisition_service(
        requisition=pr, approver=w["fin"], comments="Split by quarter"
    )
    assert pr.status == PurchaseRequisition.STATUS_RETURNED
    assert not BudgetReservation.objects.filter(requisition=pr, status="RESERVED").exists()
    w["budget"].refresh_from_db()
    assert w["budget"].reserved_amount == Decimal("0")


def test_requester_withdraws_in_review_and_returned(w, db_roles):
    pr = submit(requisition=make_pr(w), user=w["requester"])
    other = User.objects.create_user(
        email="o@gap.test", password="x", role=db_roles[Role.REQUESTER], organization=w["org"]
    )
    with pytest.raises(ValidationError):
        withdraw_purchase_requisition_service(requisition=pr, user=other)
    pr = withdraw_purchase_requisition_service(requisition=pr, user=w["requester"], reason="no")
    assert pr.status == PurchaseRequisition.STATUS_CANCELLED
    assert not ApprovalRequest.objects.filter(
        module=PR, target_object_id=pr.id, status=ApprovalRequest.STATUS_PENDING
    ).exists()
    assert AuditLog.objects.filter(
        target_model="PurchaseRequisition",
        target_object_id=str(pr.id),
        action=AuditLog.ACTION_CANCEL,
    ).exists()

    pr2 = submit(requisition=make_pr(w), user=w["requester"])
    pr2 = return_purchase_requisition_service(requisition=pr2, approver=w["appr"], comments="c")
    pr2 = withdraw_purchase_requisition_service(requisition=pr2, user=w["requester"])
    assert pr2.status == PurchaseRequisition.STATUS_CANCELLED
    with pytest.raises(ValidationError):  # already cancelled
        withdraw_purchase_requisition_service(requisition=pr2, user=w["requester"])


def test_return_and_withdraw_web_and_api(client, api_client, w):
    pr = submit(requisition=make_pr(w), user=w["requester"])
    client.force_login(w["appr"])
    client.post(f"/requisitions/{pr.id}/return/", {"comments": ""})
    pr.refresh_from_db()
    assert pr.status == PurchaseRequisition.STATUS_MANAGER_REVIEW  # comment mandatory
    client.post(f"/requisitions/{pr.id}/return/", {"comments": "Clarify scope"})
    pr.refresh_from_db()
    assert pr.status == PurchaseRequisition.STATUS_RETURNED

    client.force_login(w["requester"])
    assert client.get(f"/requisitions/{pr.id}/").status_code == 200
    assert client.get(f"/requisitions/{pr.id}/edit/").status_code == 200
    client.post(f"/requisitions/{pr.id}/submit/")
    pr.refresh_from_db()
    assert pr.status == PurchaseRequisition.STATUS_MANAGER_REVIEW
    client.post(f"/requisitions/{pr.id}/withdraw/")
    pr.refresh_from_db()
    assert pr.status == PurchaseRequisition.STATUS_CANCELLED

    pr2 = submit(requisition=make_pr(w), user=w["requester"])
    api_client.force_authenticate(w["appr"])
    r = api_client.post(f"/api/v1/requisitions/{pr2.id}/return/", {"comments": ""}, format="json")
    assert r.status_code == 400
    r = api_client.post(
        f"/api/v1/requisitions/{pr2.id}/return/", {"comments": "why"}, format="json"
    )
    assert r.status_code == 200 and r.data["status"] == "RETURNED"
    api_client.force_authenticate(w["requester"])
    r = api_client.post(f"/api/v1/requisitions/{pr2.id}/withdraw/", {}, format="json")
    assert r.status_code == 200 and r.data["status"] == "CANCELLED"


# ------------------------------------------------------------------ receipts
@pytest.fixture
def rc(w, db_roles):
    other = build_org(db_roles, "B")
    cat = VendorCategory.objects.create(name="Cat", code="CAT-GAP")

    def vendor(n):
        v = Vendor.objects.create(
            vendor_number=f"VEN-GAP-{n}",
            legal_name=f"Vendor {n}",
            tax_identification_number=f"TIN-GAP-{n}",
            category=cat,
            status=Vendor.STATUS_ACTIVE,
        )
        u = User.objects.create_user(
            email=f"v{n}@gap.test",
            password="Password123!",
            role=db_roles[Role.VENDOR_USER],
            vendor=v,
        )
        return v, u

    va, ua = vendor(1)
    vb, ub = vendor(2)
    po = PurchaseOrder.objects.create(
        po_number="PO-GAP-1",
        vendor=va,
        cost_center=w["cc"],
        status=PurchaseOrder.STATUS_ISSUED,
        subtotal=Decimal("100"),
        total_amount=Decimal("100"),
    )
    line = POLine.objects.create(
        po=po,
        item_description="Thing",
        quantity=Decimal("10"),
        unit_of_measure="EA",
        unit_price=Decimal("10"),
    )
    return dict(other=other, ua=ua, ub=ub, po=po, line=line)


def post_receipt(w, rc):
    return create_goods_receipt_service(
        po=rc["po"],
        received_by=w["stores"],
        receipt_items=[
            {"po_line_id": rc["line"].id, "quantity_received": 10, "quantity_accepted": 8}
        ],
    )


def test_receipt_notifies_vendor_and_same_org_finance_only(w, rc):
    grn = post_receipt(w, rc)
    vendor_msg = Notification.objects.get(
        recipient=rc["ua"], target_url=f"/vendor/receipts/{grn.id}/"
    )
    assert "8" in vendor_msg.message and "2" in vendor_msg.message
    assert not Notification.objects.filter(recipient=rc["ub"]).exists()
    assert Notification.objects.filter(
        recipient=w["fin"], notification_type=Notification.TYPE_GOODS_RECEIVED
    ).exists()
    assert not Notification.objects.filter(recipient=rc["other"]["fin"]).exists()


def test_vendor_acknowledges_only_own_receipts(client, w, rc):
    grn = post_receipt(w, rc)
    with pytest.raises(ValidationError):
        acknowledge_receipt_service(receipt=grn, user=rc["ub"])
    with pytest.raises(ValidationError):
        acknowledge_receipt_service(receipt=grn, user=w["stores"])

    client.force_login(rc["ub"])
    assert client.post(f"/vendor/receipts/{grn.id}/acknowledge/").status_code == 404
    grn.refresh_from_db()
    assert grn.acknowledged_at is None

    client.force_login(rc["ua"])
    assert client.post(f"/vendor/receipts/{grn.id}/acknowledge/").status_code == 302
    grn.refresh_from_db()
    assert grn.acknowledged_by_vendor == rc["ua"] and grn.acknowledged_at
    assert AuditLog.objects.filter(
        target_model="GoodsReceipt", target_object_id=str(grn.id), actor=rc["ua"]
    ).exists()
    assert Notification.objects.filter(
        recipient=w["fin"], notification_type=Notification.TYPE_RECEIPT_ACKNOWLEDGED
    ).exists()
    with pytest.raises(ValidationError):
        acknowledge_receipt_service(receipt=grn, user=rc["ua"])

    for who in (w["stores"], w["fin"]):
        client.force_login(who)
        body = client.get(f"/receipts/{grn.id}/").content.decode()
        assert "Vendor Acknowledged" in body
    assert GoodsReceipt.objects.get(pk=grn.pk).is_vendor_acknowledged

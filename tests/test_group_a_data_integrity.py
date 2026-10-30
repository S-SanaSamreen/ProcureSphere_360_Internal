"""
Regression tests for the data-integrity hardening of finalised records (audit group A).

Each test asserts the *record is unchanged* after the rejected request, not just the status code.
"""

import datetime
from decimal import Decimal

import pytest
from django.utils import timezone
from rest_framework.test import APIClient

from apps.accounts.models import Role, User
from apps.approvals.models import ApprovalPolicy
from apps.approvals.services import add_approval_step_service, create_approval_policy_service
from apps.budgets.models import Budget
from apps.budgets.services import allocate_budget_service
from apps.contracts.models import Contract
from apps.contracts.services import (
    activate_contract_service,
    approve_legal_review_service,
    create_contract_service,
    submit_for_legal_review_service,
)
from apps.invoices.models import SupplierInvoice
from apps.invoices.services import create_supplier_invoice_service
from apps.orders.models import PurchaseOrder
from apps.orders.services import generate_purchase_order_service
from apps.organization.models import CostCenter, Department, FiscalPeriod, Organization
from apps.receipts.models import GoodsReceipt
from apps.receipts.services import create_goods_receipt_service
from apps.requisitions.models import PurchaseRequisition
from apps.vendors.models import Vendor, VendorCategory
from apps.vendors.services import register_vendor_service

pytestmark = pytest.mark.django_db


def _client(user):
    c = APIClient()
    c.force_authenticate(user)
    return c


@pytest.fixture
def world(db, db_roles):
    today = timezone.localdate()
    org = Organization.objects.create(name="Integrity Org", code="INT-ORG")
    dept_a = Department.objects.create(name="Dept A", code="DA", organization=org)
    dept_b = Department.objects.create(name="Dept B", code="DB", organization=org)
    cc_a = CostCenter.objects.create(name="CC A", code="CC-A", department=dept_a)
    cc_b = CostCenter.objects.create(name="CC B", code="CC-B", department=dept_b)
    period = FiscalPeriod.objects.create(
        organization=org,
        year=today.year,
        period_number=1,
        name="FY-INT",
        start_date=today - datetime.timedelta(days=30),
        end_date=today + datetime.timedelta(days=300),
    )
    budget_a = allocate_budget_service(
        cost_center=cc_a, fiscal_period=period, allocated_amount=Decimal("500000.00")
    )
    allocate_budget_service(
        cost_center=cc_b, fiscal_period=period, allocated_amount=Decimal("500000.00")
    )

    def user(email, role_code, **extra):
        return User.objects.create_user(
            email=email,
            password="Password123!",
            role=db_roles[role_code],
            organization=org,
            **extra,
        )

    users = {
        "requester": user("req@int.test", Role.REQUESTER, department=dept_a),
        "approver": user("appr@int.test", Role.DEPT_APPROVER, department=dept_a),
        "exec": user("exec@int.test", Role.PROC_EXEC),
        "mgr": user("mgr@int.test", Role.PROC_MGR),
        "finance": user("fin@int.test", Role.FINANCE_AP),
        "legal": user("legal@int.test", Role.LEGAL_MGR),
        "stores": user("stores@int.test", Role.STORES_RECEIVER),
        "auditor": user("aud@int.test", Role.AUDITOR),
    }

    category = VendorCategory.objects.create(name="HW", code="HW-INT")
    vendor = register_vendor_service(
        legal_name="Integrity Vendor",
        tax_identification_number="TIN-INT-1",
        category=category,
        email="v@int.test",
        address="1 Street",
    )
    Vendor.objects.filter(pk=vendor.pk).update(
        status=Vendor.STATUS_ACTIVE,
        is_event_eligible=True,
        event_eligibility_status=Vendor.ELIGIBILITY_ELIGIBLE,
        organization=org,
    )
    vendor.refresh_from_db()

    po = generate_purchase_order_service(
        vendor=vendor,
        cost_center=cc_a,
        created_by_user=users["exec"],
        line_items=[{"item_description": "Widget", "quantity": 10, "unit_price": 100}],
    )
    po_line = po.lines.first()
    grn = create_goods_receipt_service(
        po=po,
        received_by=users["stores"],
        receipt_items=[{"po_line": po_line, "quantity_received": 10, "quantity_accepted": 10}],
    )
    invoice = create_supplier_invoice_service(
        vendor=vendor,
        po=po,
        invoice_number="INV-INT-1",
        invoice_date=today,
        due_date=today + datetime.timedelta(days=30),
        line_items=[
            {"po_line": po_line, "item_description": "Widget", "quantity": 10, "unit_price": 100}
        ],
        created_by_user=users["finance"],
    )

    contract = create_contract_service(
        title="Integrity Contract",
        vendor=vendor,
        contract_value=Decimal("10000.00"),
        start_date=today - datetime.timedelta(days=10),
        end_date=today + datetime.timedelta(days=100),
        renewal_notice_days=30,
        contract_owner=users["legal"],
    )
    draft_contract = create_contract_service(
        title="Draft Contract",
        vendor=vendor,
        contract_value=Decimal("500.00"),
        start_date=today,
        end_date=today + datetime.timedelta(days=60),
        renewal_notice_days=10,
        contract_owner=users["legal"],
    )
    submit_for_legal_review_service(contract=contract, user=users["legal"], notes="n")
    approve_legal_review_service(contract=contract, user=users["legal"], notes="ok")
    contract = activate_contract_service(contract=contract, user=users["mgr"])

    if not ApprovalPolicy.objects.filter(module=ApprovalPolicy.MODULE_PR, is_active=True).exists():
        policy = create_approval_policy_service(
            name="PR", module=ApprovalPolicy.MODULE_PR, min_amount=Decimal("0.00")
        )
        add_approval_step_service(
            policy=policy,
            step_number=1,
            approver_role=db_roles[Role.DEPT_APPROVER],
            description="Dept",
        )

    return {
        **users,
        "org": org,
        "dept_a": dept_a,
        "dept_b": dept_b,
        "cc_a": cc_a,
        "cc_b": cc_b,
        "budget_a": budget_a,
        "vendor": vendor,
        "po": po,
        "po_line": po_line,
        "grn": grn,
        "invoice": invoice,
        "contract": contract,
        "draft_contract": draft_contract,
        "today": today,
    }


def _pr_payload(w, **over):
    payload = {
        "title": "Integrity PR",
        "justification": "Needed for integrity test",
        "department": str(w["dept_a"].pk),
        "cost_center": str(w["cc_a"].pk),
        "requested_delivery_date": (w["today"] + datetime.timedelta(days=15)).isoformat(),
        "line_items": [{"item_description": "Thing", "quantity": 2, "estimated_unit_price": "50"}],
    }
    payload.update(over)
    return payload


# --------------------------------------------------------------------------- requisitions
def test_submitted_requisition_cannot_be_edited_via_api(world):
    c = _client(world["requester"])
    r = c.post("/api/v1/requisitions/", _pr_payload(world), format="json")
    assert r.status_code == 201, r.data
    pr_id = r.data["id"]
    assert c.post(f"/api/v1/requisitions/{pr_id}/submit/", {}, format="json").status_code == 200
    pr = PurchaseRequisition.objects.get(pk=pr_id)
    assert pr.status == PurchaseRequisition.STATUS_MANAGER_REVIEW
    original_total = pr.total_amount
    original_title = pr.title

    r = c.patch(
        f"/api/v1/requisitions/{pr_id}/",
        {
            "line_items": [
                {"item_description": "x", "quantity": 1000, "estimated_unit_price": "1000"}
            ]
        },
        format="json",
    )
    assert r.status_code == 400
    r = c.patch(f"/api/v1/requisitions/{pr_id}/", {"title": "changed"}, format="json")
    assert r.status_code == 400

    pr.refresh_from_db()
    assert pr.total_amount == original_total
    assert pr.title == original_title
    assert pr.lines.count() == 1


def test_draft_requisition_can_still_be_edited(world):
    c = _client(world["requester"])
    pr_id = c.post("/api/v1/requisitions/", _pr_payload(world), format="json").data["id"]
    r = c.patch(
        f"/api/v1/requisitions/{pr_id}/",
        {"line_items": [{"item_description": "New", "quantity": 3, "estimated_unit_price": "10"}]},
        format="json",
    )
    assert r.status_code == 200, r.data
    pr = PurchaseRequisition.objects.get(pk=pr_id)
    assert pr.total_amount == Decimal("30.00")
    assert pr.lines.count() == 1


def test_requisition_cost_center_must_belong_to_department(world):
    c = _client(world["requester"])
    r = c.post(
        "/api/v1/requisitions/",
        _pr_payload(world, cost_center=str(world["cc_b"].pk)),
        format="json",
    )
    assert r.status_code == 400
    assert not PurchaseRequisition.objects.filter(title="Integrity PR").exists()


@pytest.mark.parametrize(
    "line",
    [
        {"item_description": "n", "quantity": 2, "estimated_unit_price": "-100"},
        {"item_description": "n", "quantity": 99999999, "estimated_unit_price": "99999999"},
        {"item_description": "n", "quantity": 99999999999, "estimated_unit_price": "1"},
        {"item_description": "n", "quantity": "abc", "estimated_unit_price": "1"},
        {"item_description": "n", "quantity": 1, "estimated_unit_price": "NaN"},
        {"item_description": "n", "quantity": 0, "estimated_unit_price": "5"},
        {"item_description": "n", "quantity": 1.5, "estimated_unit_price": "5"},
    ],
)
def test_requisition_invalid_lines_rejected_without_creating_pr(world, line):
    c = _client(world["requester"])
    r = c.post("/api/v1/requisitions/", _pr_payload(world, line_items=[line]), format="json")
    assert r.status_code == 400, r.data
    assert not PurchaseRequisition.objects.filter(title="Integrity PR").exists()


# --------------------------------------------------------------------------- invoices
def test_invoice_api_has_no_put_patch_delete(world):
    c = _client(world["finance"])
    inv = world["invoice"]
    url = f"/api/v1/invoices/{inv.pk}/"
    assert (
        c.patch(url, {"invoice_number": "X", "total_amount": "1"}, format="json").status_code == 405
    )
    assert c.put(url, {"invoice_number": "X"}, format="json").status_code == 405
    assert c.delete(url).status_code == 405
    inv.refresh_from_db()
    assert inv.invoice_number == "INV-INT-1"
    assert SupplierInvoice.objects.filter(pk=inv.pk).exists()


def test_paid_invoice_cannot_be_rematched(world):
    c = _client(world["finance"])
    inv = world["invoice"]
    SupplierInvoice.objects.filter(pk=inv.pk).update(status=SupplierInvoice.STATUS_PAID)
    r = c.post(f"/api/v1/invoices/{inv.pk}/run-match/", {}, format="json")
    assert r.status_code == 400
    inv.refresh_from_db()
    assert inv.status == SupplierInvoice.STATUS_PAID


def test_rejected_invoice_cannot_be_rematched(world):
    c = _client(world["finance"])
    inv = world["invoice"]
    SupplierInvoice.objects.filter(pk=inv.pk).update(status=SupplierInvoice.STATUS_REJECTED)
    assert c.post(f"/api/v1/invoices/{inv.pk}/run-match/", {}, format="json").status_code == 400
    inv.refresh_from_db()
    assert inv.status == SupplierInvoice.STATUS_REJECTED


def _invoice_payload(w, **over):
    p = {
        "vendor_id": str(w["vendor"].pk),
        "po_id": str(w["po"].pk),
        "invoice_number": "INV-NEW-1",
        "invoice_date": w["today"].isoformat(),
        "due_date": (w["today"] + datetime.timedelta(days=30)).isoformat(),
        "line_items": [
            {
                "po_line": str(w["po_line"].pk),
                "item_description": "W",
                "quantity": 1,
                "unit_price": "100",
            }
        ],
    }
    p.update(over)
    return p


def test_valid_invoice_still_created_through_api(world):
    c = _client(world["finance"])
    r = c.post("/api/v1/invoices/create-invoice/", _invoice_payload(world), format="json")
    assert r.status_code == 201, r.data


def test_invoice_requires_due_date(world):
    c = _client(world["finance"])
    payload = _invoice_payload(world)
    payload.pop("due_date")
    r = c.post("/api/v1/invoices/create-invoice/", payload, format="json")
    assert r.status_code == 400
    assert not SupplierInvoice.objects.filter(invoice_number="INV-NEW-1").exists()


@pytest.mark.parametrize(
    "dates",
    [
        ("future", 5, 35),  # invoice date in the future
        ("due_before", 0, -10),  # due date before invoice date
    ],
)
def test_invoice_invalid_dates_rejected(world, dates):
    _, inv_offset, due_offset = dates
    today = world["today"]
    c = _client(world["finance"])
    r = c.post(
        "/api/v1/invoices/create-invoice/",
        _invoice_payload(
            world,
            invoice_date=(today + datetime.timedelta(days=inv_offset)).isoformat(),
            due_date=(today + datetime.timedelta(days=due_offset)).isoformat(),
        ),
        format="json",
    )
    assert r.status_code == 400, r.data
    assert not SupplierInvoice.objects.filter(invoice_number="INV-NEW-1").exists()


def test_duplicate_invoice_number_with_whitespace_and_case_rejected(world):
    c = _client(world["finance"])
    r = c.post(
        "/api/v1/invoices/create-invoice/",
        _invoice_payload(world, invoice_number="  inv-int-1 "),
        format="json",
    )
    assert r.status_code == 400
    assert SupplierInvoice.objects.filter(vendor=world["vendor"]).count() == 1


# --------------------------------------------------------------------------- contracts
def test_active_contract_cannot_be_patched_or_deleted(world):
    c = _client(world["legal"])
    contract = world["contract"]
    versions_before = contract.versions.count()
    url = f"/api/v1/contracts/{contract.pk}/"
    r = c.patch(url, {"contract_value": "1.00", "end_date": "2099-12-31"}, format="json")
    assert r.status_code == 400
    assert c.delete(url).status_code == 400
    contract.refresh_from_db()
    assert contract.contract_value == Decimal("10000.00")
    assert contract.end_date != datetime.date(2099, 12, 31)
    assert contract.versions.count() == versions_before
    assert Contract.objects.filter(pk=contract.pk).exists()


def test_draft_contract_can_still_be_edited_and_deleted(world):
    c = _client(world["legal"])
    draft = world["draft_contract"]
    url = f"/api/v1/contracts/{draft.pk}/"
    assert c.patch(url, {"title": "Renamed draft"}, format="json").status_code == 200
    draft.refresh_from_db()
    assert draft.title == "Renamed draft"
    assert c.delete(url).status_code == 204
    assert not Contract.objects.filter(pk=draft.pk).exists()


def test_contract_renewal_rejects_past_or_non_extending_end_date(world):
    c = _client(world["legal"])
    contract = world["contract"]
    original_end = contract.end_date
    url = f"/api/v1/contracts/{contract.pk}/renew/"
    past = (world["today"] - datetime.timedelta(days=3)).isoformat()
    assert c.post(url, {"new_end_date": past, "reason": "x"}, format="json").status_code == 400
    same = original_end.isoformat()
    assert c.post(url, {"new_end_date": same, "reason": "x"}, format="json").status_code == 400
    contract.refresh_from_db()
    assert contract.end_date == original_end
    assert contract.status == Contract.STATUS_ACTIVE

    later = (original_end + datetime.timedelta(days=365)).isoformat()
    r = c.post(url, {"new_end_date": later, "reason": "x"}, format="json")
    assert r.status_code == 200, r.data
    contract.refresh_from_db()
    assert contract.end_date.isoformat() == later
    assert contract.status == Contract.STATUS_RENEWED


# --------------------------------------------------------------------------- receipts
def test_posted_receipt_cannot_be_edited_or_deleted(world):
    c = _client(world["stores"])
    grn = world["grn"]
    original_date = grn.received_date
    url = f"/api/v1/receipts/{grn.pk}/"
    assert c.patch(url, {"received_date": "2020-01-01"}, format="json").status_code == 405
    assert c.put(url, {"received_date": "2020-01-01"}, format="json").status_code == 405
    assert c.delete(url).status_code == 405
    grn.refresh_from_db()
    assert grn.received_date == original_date
    assert GoodsReceipt.objects.filter(pk=grn.pk).exists()


# --------------------------------------------------------------------------- budgets
def test_budget_allocation_cannot_drop_below_consumed(world):
    budget = world["budget_a"]
    Budget.objects.filter(pk=budget.pk).update(
        reserved_amount=Decimal("1000.00"),
        committed_amount=Decimal("2000.00"),
        actual_amount=Decimal("3000.00"),
    )
    c = _client(world["finance"])
    url = f"/api/v1/budgets/{budget.pk}/"
    assert c.patch(url, {"allocated_amount": "1.00"}, format="json").status_code == 400
    assert c.patch(url, {"allocated_amount": "5999.99"}, format="json").status_code == 400
    budget.refresh_from_db()
    assert budget.allocated_amount == Decimal("500000.00")

    assert c.patch(url, {"allocated_amount": "6000.00"}, format="json").status_code == 200
    budget.refresh_from_db()
    assert budget.allocated_amount == Decimal("6000.00")


def test_budget_with_ledger_history_cannot_be_deleted(world):
    from apps.budgets.models import SpendLedger

    budget = world["budget_a"]
    SpendLedger.objects.create(
        budget=budget,
        entry_type=SpendLedger.ENTRY_COMMITMENT,
        amount=Decimal("10.00"),
        reference_number="REF",
        description="x",
    )
    c = _client(world["finance"])
    assert c.delete(f"/api/v1/budgets/{budget.pk}/").status_code == 400
    assert Budget.objects.filter(pk=budget.pk).exists()


# --------------------------------------------------------------------------- purchase orders
def test_issued_po_cannot_be_edited_or_deleted_directly(world):
    c = _client(world["exec"])
    po = world["po"]
    assert po.status == PurchaseOrder.STATUS_ISSUED
    other_vendor = world["vendor"]
    r = c.patch(
        f"/api/v1/purchase-orders/{po.pk}/",
        {
            "cost_center": str(world["cc_b"].pk),
            "terms_and_conditions": "EDITED",
            "vendor": str(other_vendor.pk),
        },
        format="json",
    )
    assert r.status_code == 400
    assert c.delete(f"/api/v1/purchase-orders/{po.pk}/").status_code == 400
    po.refresh_from_db()
    assert po.cost_center_id == world["cc_a"].pk
    assert po.terms_and_conditions != "EDITED"
    assert po.version == 1
    assert PurchaseOrder.objects.filter(pk=po.pk).exists()


def test_completed_po_cannot_be_edited(world):
    c = _client(world["exec"])
    po = world["po"]
    PurchaseOrder.objects.filter(pk=po.pk).update(status=PurchaseOrder.STATUS_COMPLETED)
    r = c.patch(
        f"/api/v1/purchase-orders/{po.pk}/", {"terms_and_conditions": "EDITED"}, format="json"
    )
    assert r.status_code == 400
    po.refresh_from_db()
    assert po.terms_and_conditions != "EDITED"


# --------------------------------------------------------------------------- export jobs
@pytest.mark.parametrize("role", ["finance", "auditor", "mgr"])
def test_export_job_hash_cannot_be_tampered_or_deleted(world, role):
    from apps.reports.models import ExportJob

    c = _client(world[role])
    r = c.post(
        "/api/v1/reports/exports/",
        {"report_type": "spend_analytics", "export_format": "CSV", "file_hash": "0" * 64},
        format="json",
    )
    assert r.status_code == 201, r.data
    job = ExportJob.objects.get(pk=r.data["id"])
    assert job.file_hash and job.file_hash != "0" * 64  # computed server-side, not client-supplied
    real_hash = job.file_hash

    url = f"/api/v1/reports/exports/{job.pk}/"
    assert (
        c.patch(url, {"file_hash": "0" * 64, "status": "COMPLETED"}, format="json").status_code
        == 405
    )
    assert c.delete(url).status_code == 405
    job.refresh_from_db()
    assert job.file_hash == real_hash
    assert ExportJob.objects.filter(pk=job.pk).exists()

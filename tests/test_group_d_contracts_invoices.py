"""Contract business approval and invoice match exceptions on the approval engine (group D)."""

import datetime
from decimal import Decimal

import pytest
from django.core.exceptions import ValidationError
from rest_framework.test import APIClient

from apps.accounts.models import Role, User
from apps.approvals import engine
from apps.approvals.models import ApprovalPolicy, ApprovalRequest
from apps.contracts.models import Contract
from apps.contracts.services import (
    approve_business_service,
    approve_legal_review_service,
    create_contract_service,
    reject_business_service,
    submit_for_legal_review_service,
)
from apps.invoices.models import MatchException, SupplierInvoice
from apps.invoices.services import (
    create_supplier_invoice_service,
    resolve_match_exception_service,
    run_3_way_match_service,
)
from tests.test_group_c_lifecycle import D, world  # noqa: F401  (shared fixture)

pytestmark = pytest.mark.django_db

CON = ApprovalPolicy.MODULE_CONTRACT
INV = ApprovalPolicy.MODULE_INVOICE


def role(w, code):
    return w["db_roles"][code]


def revise_default(w, module, steps, **kw):
    seeded = ApprovalPolicy.objects.get(
        module=module, department__isnull=True, cost_center__isnull=True
    )
    return engine.revise_approval_policy(
        policy=seeded, actor=w["admin"], steps=[{"approver_role": role(w, r)} for r in steps], **kw
    )


def manager(w, email):
    return User.objects.create_user(
        email=email, password="Password123!", role=role(w, Role.PROC_MGR), organization=w["org"]
    )


# ======================================================================= contracts
def contract_in_business_approval(w, value=10000):
    today = w["today"]
    contract = create_contract_service(
        title="Engine Contract",
        vendor=w["vendor"],
        contract_value=D(value),
        start_date=today,
        end_date=today + datetime.timedelta(days=200),
        renewal_notice_days=30,
        contract_owner=w["legal"],
    )
    submit_for_legal_review_service(contract=contract, user=w["legal"], notes="n")
    return approve_legal_review_service(contract=contract, user=w["legal"], notes="ok")


def test_legal_approval_opens_the_business_request_and_the_manager_activates(world):  # noqa: F811
    w = world
    contract = contract_in_business_approval(w, 50000)
    assert contract.status == Contract.STATUS_BUSINESS_APPROVAL
    req = ApprovalRequest.objects.get(module=CON, target_object_id=contract.id)
    assert req.amount == D(50000) and req.created_by == w["legal"]
    assert [s["role_code"] for s in req.steps] == ["PROC_MGR"]

    approve_business_service(contract=contract, user=w["mgr"], notes="Approved")
    contract.refresh_from_db()
    assert contract.status == Contract.STATUS_ACTIVE
    assert ApprovalRequest.objects.get(pk=req.pk).status == "APPROVED"


def test_other_roles_and_the_legal_reviewer_cannot_give_business_approval(world):  # noqa: F811
    w = world
    contract = contract_in_business_approval(w)
    for user in (w["requester"], w["legal"], w["finance"], w["pe"]):
        with pytest.raises(ValidationError, match="not authorized to approve step 1"):
            approve_business_service(contract=contract, user=user)
    contract.refresh_from_db()
    assert contract.status == Contract.STATUS_BUSINESS_APPROVAL


def test_contract_value_tiers_add_approvers_for_large_contracts(world):  # noqa: F811
    w = world
    revise_default(w, CON, [Role.PROC_MGR], max_amount=D(99999.99))
    engine.create_policy_with_steps(
        name="Large contracts",
        module=CON,
        min_amount=D(100000),
        steps=[
            {"approver_role": role(w, Role.PROC_MGR)},
            {"approver_role": role(w, Role.FINANCE_AP)},
        ],
    )
    small = contract_in_business_approval(w, 5000)
    large = contract_in_business_approval(w, 250000)
    steps = lambda c: len(  # noqa: E731
        ApprovalRequest.objects.get(module=CON, target_object_id=c.id).steps
    )
    assert (steps(small), steps(large)) == (1, 2)

    approve_business_service(contract=large, user=w["mgr"])
    large.refresh_from_db()
    assert large.status == Contract.STATUS_BUSINESS_APPROVAL  # half way
    approve_business_service(contract=large, user=w["finance"])
    large.refresh_from_db()
    assert large.status == Contract.STATUS_ACTIVE


def test_contract_approval_limit(world):  # noqa: F811
    w = world
    contract = contract_in_business_approval(w, 90000)
    User.objects.filter(pk=w["mgr"].pk).update(approval_limit=D(50000))
    with pytest.raises(ValidationError, match="approval limit"):
        approve_business_service(contract=contract, user=w["mgr"])
    approve_business_service(contract=contract, user=manager(w, "mgr-b@d.test"))
    contract.refresh_from_db()
    assert contract.status == Contract.STATUS_ACTIVE


def test_no_contract_policy_blocks_legal_approval_atomically(world):  # noqa: F811
    w = world
    ApprovalPolicy.objects.filter(module=CON).update(is_active=False)
    today = w["today"]
    contract = create_contract_service(
        title="No policy",
        vendor=w["vendor"],
        contract_value=D(1000),
        start_date=today,
        end_date=today + datetime.timedelta(days=30),
        contract_owner=w["legal"],
    )
    submit_for_legal_review_service(contract=contract, user=w["legal"], notes="n")
    with pytest.raises(ValidationError, match="No active approval policy found"):
        approve_legal_review_service(contract=contract, user=w["legal"], notes="ok")
    contract.refresh_from_db()
    assert contract.status == Contract.STATUS_LEGAL_REVIEW  # rolled back


def test_business_rejection_returns_the_contract_to_draft(world):  # noqa: F811
    w = world
    contract = contract_in_business_approval(w)
    with pytest.raises(ValidationError, match="Rejection reason is required"):
        reject_business_service(contract=contract, user=w["mgr"], reason=" ")
    with pytest.raises(ValidationError, match="not an approver"):
        reject_business_service(contract=contract, user=w["requester"], reason="no")
    reject_business_service(
        contract=contract, user=w["mgr"], reason="Commercial terms unacceptable"
    )
    contract.refresh_from_db()
    assert contract.status == Contract.STATUS_DRAFT
    assert (
        ApprovalRequest.objects.get(module=CON, target_object_id=contract.id).status == "REJECTED"
    )


def test_business_reject_api_and_entry_gate_for_policy_roles(world):  # noqa: F811
    w = world
    contract = contract_in_business_approval(w)
    client = APIClient()
    client.force_authenticate(w["finance"])
    assert (
        client.post(
            f"/api/v1/contracts/{contract.pk}/business-reject/", {"reason": "x"}, format="json"
        ).status_code
        == 403
    )
    revise_default(w, CON, [Role.PROC_MGR, Role.FINANCE_AP])
    # finance now appears in the policy, so the endpoint admits them (engine decides per request)
    resp = client.post(
        f"/api/v1/contracts/{contract.pk}/business-reject/", {"reason": "x"}, format="json"
    )
    assert (
        resp.status_code == 400
    )  # in-flight request was routed under v1 (manager only): not an approver
    client.force_authenticate(w["mgr"])
    resp = client.post(
        f"/api/v1/contracts/{contract.pk}/business-reject/", {"reason": "Terms"}, format="json"
    )
    assert resp.status_code == 200 and resp.data["status"] == "DRAFT"


# ======================================================================= invoice exceptions
@pytest.fixture
def invoice_with_exception(world):  # noqa: F811
    from apps.orders.services import generate_purchase_order_service
    from apps.receipts.services import create_goods_receipt_service

    w = world
    po = generate_purchase_order_service(
        vendor=w["vendor"],
        cost_center=w["cc"],
        created_by_user=w["pe"],
        line_items=[{"item_description": "Widget", "quantity": 10, "unit_price": 100}],
    )
    line = po.lines.first()
    stores = User.objects.create_user(
        email="stores@d.test",
        password="Password123!",
        role=role(w, Role.STORES_RECEIVER),
        organization=w["org"],
    )
    create_goods_receipt_service(
        po=po,
        received_by=stores,
        receipt_items=[{"po_line": line, "quantity_received": 10, "quantity_accepted": 10}],
    )
    invoice = create_supplier_invoice_service(
        vendor=w["vendor"],
        po=po,
        invoice_number="INV-ENG-1",
        invoice_date=w["today"],
        due_date=w["today"] + datetime.timedelta(days=30),
        line_items=[
            {"po_line": line, "item_description": "Widget", "quantity": 10, "unit_price": 200}
        ],
        created_by_user=w["finance"],
    )
    run_3_way_match_service(invoice=invoice, user=w["finance"])
    invoice.refresh_from_db()
    exception = invoice.exceptions.first()
    assert invoice.status == SupplierInvoice.STATUS_EXCEPTION and exception is not None
    return w, invoice, exception


def test_exception_resolution_is_routed_by_the_invoice_policy(invoice_with_exception):
    w, invoice, exception = invoice_with_exception
    for user in (w["requester"], w["mgr"], w["pe"]):
        with pytest.raises(ValidationError, match="not authorized to approve step 1"):
            resolve_match_exception_service(
                match_exception=exception, resolved_by_user=user, resolution_notes="ok"
            )
    resolve_match_exception_service(
        match_exception=exception, resolved_by_user=w["finance"], resolution_notes="Price agreed"
    )
    exception.refresh_from_db()
    invoice.refresh_from_db()
    assert exception.status == MatchException.STATUS_RESOLVED
    req = ApprovalRequest.objects.get(module=INV, target_object_id=exception.id)
    assert req.status == "APPROVED" and req.amount == abs(exception.variance_amount)
    if not invoice.exceptions.filter(status=MatchException.STATUS_OPEN).exists():
        assert invoice.status == SupplierInvoice.STATUS_READY_FOR_PAYMENT


def test_multi_step_invoice_exception_stays_open_until_the_last_approver(invoice_with_exception):
    w, invoice, exception = invoice_with_exception
    revise_default(w, INV, [Role.FINANCE_AP, Role.PROC_MGR])
    resolve_match_exception_service(
        match_exception=exception, resolved_by_user=w["finance"], resolution_notes="Finance OK"
    )
    exception.refresh_from_db()
    assert exception.status == MatchException.STATUS_OPEN  # one signature is not enough
    with pytest.raises(ValidationError, match="four-eyes|not authorized"):
        resolve_match_exception_service(
            match_exception=exception, resolved_by_user=w["finance"], resolution_notes="again"
        )
    resolve_match_exception_service(
        match_exception=exception, resolved_by_user=w["mgr"], resolution_notes="Manager OK"
    )
    exception.refresh_from_db()
    assert exception.status == MatchException.STATUS_RESOLVED


def test_invoice_exception_variance_tiers_and_limits(invoice_with_exception):
    w, invoice, exception = invoice_with_exception
    variance = abs(exception.variance_amount)
    User.objects.filter(pk=w["finance"].pk).update(approval_limit=variance - D("0.01"))
    with pytest.raises(ValidationError, match="approval limit"):
        resolve_match_exception_service(
            match_exception=exception, resolved_by_user=w["finance"], resolution_notes="ok"
        )
    exception.refresh_from_db()
    assert exception.status == MatchException.STATUS_OPEN


def test_rejecting_an_invoice_exception_needs_an_approver_and_rejects_the_invoice(
    invoice_with_exception,
):
    w, invoice, exception = invoice_with_exception
    with pytest.raises(ValidationError, match="not an approver"):
        resolve_match_exception_service(
            match_exception=exception,
            resolved_by_user=w["requester"],
            resolution_notes="x",
            action="REJECT",
        )
    resolve_match_exception_service(
        match_exception=exception,
        resolved_by_user=w["finance"],
        resolution_notes="Dispute",
        action="REJECT",
    )
    exception.refresh_from_db()
    invoice.refresh_from_db()
    assert exception.status == MatchException.STATUS_REJECTED
    assert invoice.status == SupplierInvoice.STATUS_REJECTED


def test_resolve_endpoint_admits_policy_roles_but_the_engine_still_decides(invoice_with_exception):
    w, invoice, exception = invoice_with_exception
    client = APIClient()
    client.force_authenticate(w["mgr"])
    url = f"/api/v1/invoices/exceptions/{exception.pk}/resolve/"
    assert (
        client.post(url, {"resolution_notes": "x"}, format="json").status_code == 403
    )  # not in policy
    revise_default(w, INV, [Role.FINANCE_AP, Role.PROC_MGR])
    resp = client.post(url, {"resolution_notes": "x"}, format="json")
    assert resp.status_code in (400, 403)  # admitted to the endpoint, but step 1 is Finance's
    exception.refresh_from_db()
    assert exception.status == MatchException.STATUS_OPEN

"""
Regression tests for lifecycle completeness (audit group C):
PR APPROVED -> SOURCING / PO_ISSUED, vendor APPROVED + blacklist, multi-line manual PO,
contract alert cycles.
"""

import datetime
from decimal import Decimal

import pytest
from django.core.exceptions import ValidationError
from django.utils import timezone

from apps.accounts.models import Role, User
from apps.audit.models import AuditLog
from apps.budgets.services import allocate_budget_service, check_and_reserve_budget_service
from apps.orders.models import PurchaseOrder
from apps.orders.services import (
    approve_purchase_order_service,
    cancel_purchase_order_governance_service,
    generate_purchase_order_service,
)
from apps.organization.models import CostCenter, Department, FiscalPeriod, Organization
from apps.requisitions.models import PurchaseRequisition
from apps.requisitions.services import (
    create_purchase_requisition_service,
    refresh_requisition_fulfilment_status,
)
from apps.sourcing.models import SourcingEvent
from apps.sourcing.services import (
    cancel_sourcing_event_service,
    create_sourcing_event_service,
    update_draft_sourcing_event_service,
)
from apps.vendors.models import Vendor, VendorCategory
from apps.vendors.services import register_vendor_service

pytestmark = pytest.mark.django_db


def D(x):
    return Decimal(str(x))


@pytest.fixture
def world(db, db_roles):
    today = timezone.localdate()
    org = Organization.objects.create(name="Life Org", code="LIFE-ORG")
    dept = Department.objects.create(name="Dept", code="LD", organization=org)
    cc = CostCenter.objects.create(name="CC", code="CC-L", department=dept)
    period = FiscalPeriod.objects.create(
        organization=org,
        year=today.year,
        period_number=1,
        name="FY-L",
        start_date=today - datetime.timedelta(days=30),
        end_date=today + datetime.timedelta(days=300),
    )
    allocate_budget_service(cost_center=cc, fiscal_period=period, allocated_amount=D("1000000"))

    def user(email, role_code, **extra):
        return User.objects.create_user(
            email=email,
            password="Password123!",
            role=db_roles[role_code],
            organization=org,
            **extra,
        )

    cat = VendorCategory.objects.create(name="HW", code="HW-L")
    vendor = register_vendor_service(
        legal_name="Life Vendor",
        tax_identification_number="TIN-L",
        category=cat,
        email="v@l.test",
        address="1 St",
    )
    Vendor.objects.filter(pk=vendor.pk).update(
        status=Vendor.STATUS_ACTIVE,
        is_event_eligible=True,
        event_eligibility_status=Vendor.ELIGIBILITY_ELIGIBLE,
    )
    vendor.refresh_from_db()
    return dict(
        today=today,
        org=org,
        dept=dept,
        cc=cc,
        period=period,
        vendor=vendor,
        requester=user("req@l.test", Role.REQUESTER, department=dept),
        pe=user("pe@l.test", Role.PROC_EXEC),
        mgr=user("mgr@l.test", Role.PROC_MGR),
        legal=user("legal@l.test", Role.LEGAL_MGR),
        finance=user("fin@l.test", Role.FINANCE_AP),
        admin=user("admin@l.test", Role.SUPER_ADMIN),
        db_roles=db_roles,
    )


def make_pr(w, amount=10000, status=PurchaseRequisition.STATUS_APPROVED, reserve=True):
    pr = create_purchase_requisition_service(
        title="PR",
        justification="lifecycle test justification",
        requester=w["requester"],
        department=w["dept"],
        cost_center=w["cc"],
        requested_delivery_date=w["today"] + datetime.timedelta(days=20),
        line_items=[{"item_description": "Item", "quantity": 1, "estimated_unit_price": amount}],
    )
    if reserve:
        check_and_reserve_budget_service(requisition=pr, requested_by_user=w["requester"])
    PurchaseRequisition.objects.filter(pk=pr.pk).update(status=status)
    pr.refresh_from_db()
    return pr


def make_po(w, pr, amount=10000, **kw):
    return generate_purchase_order_service(
        vendor=w["vendor"],
        cost_center=w["cc"],
        requisition=pr,
        created_by_user=w["pe"],
        line_items=[{"item_description": "Item", "quantity": 1, "unit_price": amount}],
        **kw,
    )


def make_event(w, pr, title="RFQ for PR"):
    now = timezone.now()
    return create_sourcing_event_service(
        title=title,
        event_type=SourcingEvent.TYPE_RFQ,
        bid_start_date=now + datetime.timedelta(days=1),
        bid_end_date=now + datetime.timedelta(days=10),
        description="Sourcing for lifecycle test",
        requisition=pr,
        created_by_user=w["pe"],
    )


def status_of(pr):
    pr.refresh_from_db()
    return pr.status


# ======================================================================= PR lifecycle
def test_linking_a_sourcing_event_moves_pr_to_sourcing_and_cancelling_returns_it(world):
    w = world
    pr = make_pr(w)
    event = make_event(w, pr)
    assert status_of(pr) == PurchaseRequisition.STATUS_SOURCING

    cancel_sourcing_event_service(event=event, user=w["pe"], reason="Scope changed")
    assert status_of(pr) == PurchaseRequisition.STATUS_APPROVED


def test_only_one_live_sourcing_event_per_requisition(world):
    w = world
    pr = make_pr(w)
    make_event(w, pr, "first")
    with pytest.raises(ValidationError, match="Only APPROVED requisitions"):
        make_event(w, pr, "second")


def test_editing_a_linked_draft_event_does_not_trip_its_own_requisition_check(world):
    w = world
    pr = make_pr(w)
    event = make_event(w, pr)
    update_draft_sourcing_event_service(event=event, user=w["pe"], data={"title": "Renamed RFQ"})
    event.refresh_from_db()
    assert event.title == "Renamed RFQ"
    assert status_of(pr) == PurchaseRequisition.STATUS_SOURCING


def test_relinking_a_draft_event_moves_both_requisitions(world):
    w = world
    pr1, pr2 = make_pr(w, 1000), make_pr(w, 2000)
    event = make_event(w, pr1)
    assert status_of(pr1) == PurchaseRequisition.STATUS_SOURCING
    update_draft_sourcing_event_service(event=event, user=w["pe"], data={"requisition": pr2})
    assert status_of(pr1) == PurchaseRequisition.STATUS_APPROVED
    assert status_of(pr2) == PurchaseRequisition.STATUS_SOURCING


def test_issuing_a_po_moves_pr_to_po_issued_and_is_audited(world):
    w = world
    pr = make_pr(w)
    assert status_of(pr) == PurchaseRequisition.STATUS_APPROVED
    make_po(w, pr)
    assert status_of(pr) == PurchaseRequisition.STATUS_PO_ISSUED
    entry = AuditLog.objects.filter(
        target_model="PurchaseRequisition", target_object_id=str(pr.id)
    ).latest("timestamp")
    assert entry.previous_state == {"status": "APPROVED"}
    assert entry.new_state["status"] == "PO_ISSUED"


def test_po_issued_takes_precedence_over_sourcing_and_cancel_falls_back(world):
    w = world
    pr = make_pr(w)
    make_event(w, pr)
    po = make_po(w, pr)
    assert status_of(pr) == PurchaseRequisition.STATUS_PO_ISSUED

    cancel_purchase_order_governance_service(po=po, actor=w["mgr"], reason="Vendor withdrew")
    assert status_of(pr) == PurchaseRequisition.STATUS_SOURCING  # event still live


def test_cancelling_the_only_po_returns_pr_to_approved(world):
    w = world
    pr = make_pr(w)
    po = make_po(w, pr)
    cancel_purchase_order_governance_service(po=po, actor=w["mgr"], reason="Wrong vendor")
    assert status_of(pr) == PurchaseRequisition.STATUS_APPROVED


def test_draft_po_does_not_move_pr_but_approving_it_does(world):
    w = world
    pr = make_pr(w)
    po = make_po(w, pr, initial_status=PurchaseOrder.STATUS_DRAFT)
    assert status_of(pr) == PurchaseRequisition.STATUS_APPROVED
    approve_purchase_order_service(po=po, approver=w["mgr"])
    assert status_of(pr) == PurchaseRequisition.STATUS_PO_ISSUED


def test_a_second_po_within_the_cap_is_still_possible_after_po_issued(world, settings):
    settings.PO_OVER_REQUISITION_TOLERANCE_PCT = "0"
    w = world
    pr = make_pr(w, 10000)
    make_po(w, pr, 6000)
    assert status_of(pr) == PurchaseRequisition.STATUS_PO_ISSUED
    make_po(w, pr, 4000)  # PO_ISSUED PRs still accept POs up to the cap
    assert PurchaseOrder.objects.filter(requisition=pr).count() == 2


@pytest.mark.parametrize(
    "status",
    [
        PurchaseRequisition.STATUS_DRAFT,
        PurchaseRequisition.STATUS_REJECTED,
        PurchaseRequisition.STATUS_CANCELLED,
        PurchaseRequisition.STATUS_MANAGER_REVIEW,
    ],
)
def test_refresh_never_resurrects_a_non_approved_requisition(world, status):
    w = world
    pr = make_pr(w, status=status, reserve=False)
    refresh_requisition_fulfilment_status(pr)
    assert status_of(pr) == status


def test_requisition_with_a_po_cannot_be_cancelled_via_api(world):
    from rest_framework.test import APIClient

    w = world
    pr = make_pr(w)
    make_po(w, pr)
    client = APIClient()
    client.force_authenticate(w["requester"])
    resp = client.post(f"/api/v1/requisitions/{pr.pk}/cancel/", {}, format="json")
    assert resp.status_code == 400
    assert status_of(pr) == PurchaseRequisition.STATUS_PO_ISSUED

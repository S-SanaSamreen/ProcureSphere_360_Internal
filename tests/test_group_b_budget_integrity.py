"""
Regression tests for budget / PO financial integrity (audit group B).

Covers: reservation -> commitment conversion (no double counting), overspend refusal on PO issue
and amendment, requisition cap, fiscal-period-correct budget lookup, ledger as the source of
truth (immutability + reconciliation) and the configurable per-PO tax rate.
"""

import datetime
from decimal import Decimal
from io import StringIO

import pytest
from django.core.exceptions import ValidationError
from django.core.management import call_command
from django.core.management.base import CommandError
from django.utils import timezone

from apps.accounts.models import Role, User
from apps.budgets.models import Budget, BudgetReservation, SpendLedger
from apps.budgets.services import (
    allocate_budget_service,
    check_and_reserve_budget_service,
    po_net_commitment,
    reconcile_budget,
    release_budget_reservation_service,
)
from apps.invoices.services import create_supplier_invoice_service
from apps.orders.models import POAmendment, PurchaseOrder
from apps.orders.services import (
    approve_po_amendment_service,
    cancel_purchase_order_governance_service,
    generate_purchase_order_service,
    request_po_amendment_service,
)
from apps.organization.models import CostCenter, Department, FiscalPeriod, Organization
from apps.requisitions.models import PurchaseRequisition
from apps.requisitions.services import create_purchase_requisition_service
from apps.vendors.models import Vendor, VendorCategory
from apps.vendors.services import register_vendor_service

pytestmark = pytest.mark.django_db

TODAY = None


def D(x):
    return Decimal(str(x))


@pytest.fixture
def world(db, db_roles):
    today = timezone.localdate()
    org = Organization.objects.create(name="Budget Org", code="BUD-ORG")
    dept = Department.objects.create(name="Dept", code="BD", organization=org)
    cc = CostCenter.objects.create(name="CC", code="CC-B", department=dept)
    period = FiscalPeriod.objects.create(
        organization=org,
        year=today.year,
        period_number=1,
        name="FY-B",
        start_date=today - datetime.timedelta(days=30),
        end_date=today + datetime.timedelta(days=300),
    )
    budget = allocate_budget_service(
        cost_center=cc, fiscal_period=period, allocated_amount=D("100000.00")
    )

    def user(email, role_code, **extra):
        return User.objects.create_user(
            email=email,
            password="Password123!",
            role=db_roles[role_code],
            organization=org,
            **extra,
        )

    requester = user("req@b.test", Role.REQUESTER, department=dept)
    pe = user("pe@b.test", Role.PROC_EXEC)
    mgr = user("mgr@b.test", Role.PROC_MGR)

    cat = VendorCategory.objects.create(name="HW", code="HW-B")
    vendor = register_vendor_service(
        legal_name="Budget Vendor",
        tax_identification_number="TIN-B",
        category=cat,
        email="v@b.test",
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
        budget=budget,
        requester=requester,
        pe=pe,
        mgr=mgr,
        vendor=vendor,
    )


def make_pr(w, amount=10000, approved=True, reserve=True, cc=None):
    pr = create_purchase_requisition_service(
        title="PR",
        justification="budget test justification",
        requester=w["requester"],
        department=w["dept"],
        cost_center=cc or w["cc"],
        requested_delivery_date=w["today"] + datetime.timedelta(days=20),
        line_items=[{"item_description": "Item", "quantity": 1, "estimated_unit_price": amount}],
    )
    if reserve:
        check_and_reserve_budget_service(requisition=pr, requested_by_user=w["requester"])
    if approved:
        PurchaseRequisition.objects.filter(pk=pr.pk).update(
            status=PurchaseRequisition.STATUS_APPROVED
        )
        pr.refresh_from_db()
    return pr


def make_po(w, amount=10000, pr=None, cc=None, **kw):
    return generate_purchase_order_service(
        vendor=w["vendor"],
        cost_center=cc or w["cc"],
        requisition=pr,
        created_by_user=w["pe"],
        line_items=[{"item_description": "Item", "quantity": 1, "unit_price": amount}],
        **kw,
    )


def fresh(b):
    b.refresh_from_db()
    return b


# ------------------------------------------------------------------ reservation -> commitment
def test_po_from_requisition_converts_reservation_instead_of_double_counting(world):
    w = world
    pr = make_pr(w, 10000)
    b = fresh(w["budget"])
    assert (b.reserved_amount, b.committed_amount) == (D("10000.00"), D("0.00"))

    po = make_po(w, 10000, pr=pr)
    b = fresh(w["budget"])
    assert po.total_amount == D("11000.00")
    assert b.reserved_amount == D("0.00")  # reservation consumed...
    assert b.committed_amount == D("11000.00")  # ...and replaced, not added to
    assert b.reserved_amount + b.committed_amount == D("11000.00")

    res = BudgetReservation.objects.get(requisition=pr)
    assert res.status == BudgetReservation.STATUS_COMMITTED
    assert res.converted_amount == D("10000.00")
    assert reconcile_budget(b)["in_sync"]


def test_partial_po_converts_only_its_share_and_cancel_releases_the_remainder(world):
    w = world
    pr = make_pr(w, 10000)
    make_po(w, 4000, pr=pr)
    b = fresh(w["budget"])
    assert b.reserved_amount == D("6000.00")  # untouched balance of the PR still held
    assert b.committed_amount == D("4400.00")
    res = BudgetReservation.objects.get(requisition=pr)
    assert res.status == BudgetReservation.STATUS_RESERVED  # not fully converted yet

    release_budget_reservation_service(requisition=pr, user=w["requester"], reason="PR cancelled")
    b = fresh(w["budget"])
    assert b.reserved_amount == D("0.00")  # only the unconverted 6,000 was released
    assert b.committed_amount == D("4400.00")
    assert reconcile_budget(b)["in_sync"]


def test_commit_is_idempotent(world):
    from apps.budgets.services import commit_po_budget_service

    w = world
    po = make_po(w, 1000)
    commit_po_budget_service(po=po, user=w["pe"])
    commit_po_budget_service(po=po, user=w["pe"])
    assert fresh(w["budget"]).committed_amount == D("1100.00")
    assert SpendLedger.objects.filter(reference_number=po.po_number).count() == 1


# ------------------------------------------------------------------ overspend control
def test_po_that_overspends_budget_is_refused_and_leaves_nothing_behind(world):
    w = world
    before_pos = PurchaseOrder.objects.count()
    with pytest.raises(ValidationError, match="Insufficient budget"):
        make_po(w, 200000)
    assert PurchaseOrder.objects.count() == before_pos  # rolled back, no orphan PO
    b = fresh(w["budget"])
    assert (b.reserved_amount, b.committed_amount, b.actual_amount) == (D("0.00"),) * 3
    assert not SpendLedger.objects.filter(budget=b).exists()


def test_overspend_allowed_only_when_budget_flagged_allow_overspend(world):
    w = world
    Budget.objects.filter(pk=w["budget"].pk).update(allow_overspend=True)
    po = make_po(w, 200000)
    assert po.status == PurchaseOrder.STATUS_ISSUED
    assert fresh(w["budget"]).committed_amount == D("220000.00")


def test_headroom_counts_the_reservation_being_converted(world):
    w = world
    # 100,000 allocation: PR reserves 90,000; PO (99,000 incl. tax) only fits because the
    # 90,000 reservation is released as it is converted.
    pr = make_pr(w, 90000)
    po = make_po(w, 90000, pr=pr)
    assert po.total_amount == D("99000.00")
    assert fresh(w["budget"]).committed_amount == D("99000.00")


def test_po_above_headroom_after_conversion_is_refused(world):
    w = world
    Budget.objects.filter(pk=w["budget"].pk).update(allocated_amount=D("98000.00"))
    pr = make_pr(w, 90000)
    with pytest.raises(ValidationError, match="Insufficient budget"):
        make_po(w, 90000, pr=pr)  # needs 99,000 > 98,000
    b = fresh(w["budget"])
    assert b.reserved_amount == D("90000.00")  # reservation untouched by the failed attempt
    assert b.committed_amount == D("0.00")


def test_po_without_any_budget_for_the_cost_center_is_refused(world):
    w = world
    other_cc = CostCenter.objects.create(name="NoBudget", code="CC-NB", department=w["dept"])
    with pytest.raises(ValidationError, match="No active fiscal period budget"):
        make_po(w, 100, cc=other_cc)


# ------------------------------------------------------------------ requisition link + cap
def test_po_against_unapproved_requisition_is_refused(world):
    w = world
    pr = make_pr(w, 10000, approved=False)
    with pytest.raises(ValidationError, match="APPROVED requisition"):
        make_po(w, 10000, pr=pr)


def test_po_must_use_the_requisition_cost_center(world):
    w = world
    cc2 = CostCenter.objects.create(name="CC2", code="CC-B2", department=w["dept"])
    allocate_budget_service(cost_center=cc2, fiscal_period=w["period"], allocated_amount=D("50000"))
    pr = make_pr(w, 10000)
    with pytest.raises(ValidationError, match="differs from requisition"):
        make_po(w, 10000, pr=pr, cc=cc2)


def test_pos_against_one_requisition_are_capped(world, settings):
    settings.PO_OVER_REQUISITION_TOLERANCE_PCT = "0"
    w = world
    pr = make_pr(w, 10000)
    make_po(w, 6000, pr=pr)
    make_po(w, 4000, pr=pr)  # exactly reaches the estimate
    with pytest.raises(ValidationError, match="requisition estimate"):
        make_po(w, 1, pr=pr)
    assert PurchaseOrder.objects.filter(requisition=pr).count() == 2


def test_cap_tolerance_allows_a_modest_quote_overrun(world, settings):
    settings.PO_OVER_REQUISITION_TOLERANCE_PCT = "10"
    w = world
    pr = make_pr(w, 10000)
    make_po(w, 10900, pr=pr)  # 9% over estimate, within 10%
    with pytest.raises(ValidationError, match="requisition estimate"):
        make_po(w, 500, pr=pr)  # would take total to 11,400 (> 11,000)


def test_cancelled_po_frees_requisition_cap(world, settings):
    settings.PO_OVER_REQUISITION_TOLERANCE_PCT = "0"
    w = world
    pr = make_pr(w, 10000)
    po = make_po(w, 10000, pr=pr)
    cancel_purchase_order_governance_service(po=po, actor=w["mgr"], reason="Wrong vendor")
    make_po(w, 10000, pr=pr)  # allowed again


# ------------------------------------------------------------------ fiscal period correctness
def test_commitment_lands_in_the_open_period_not_the_latest_created_budget(world):
    w = world
    today = w["today"]
    old_period = FiscalPeriod.objects.create(
        organization=w["org"],
        year=today.year - 1,
        period_number=1,
        name="FY-OLD",
        start_date=today - datetime.timedelta(days=400),
        end_date=today - datetime.timedelta(days=100),
    )
    future_period = FiscalPeriod.objects.create(
        organization=w["org"],
        year=today.year + 1,
        period_number=1,
        name="FY-NEXT",
        start_date=today + datetime.timedelta(days=301),
        end_date=today + datetime.timedelta(days=600),
    )
    # created AFTER the current-period budget, so "latest created" would pick one of these
    old_b = allocate_budget_service(
        cost_center=w["cc"], fiscal_period=old_period, allocated_amount=D("5")
    )
    next_b = allocate_budget_service(
        cost_center=w["cc"], fiscal_period=future_period, allocated_amount=D("5")
    )

    po = make_po(w, 1000)
    assert fresh(w["budget"]).committed_amount == D("1100.00")
    assert fresh(old_b).committed_amount == D("0.00")
    assert fresh(next_b).committed_amount == D("0.00")
    assert SpendLedger.objects.get(reference_number=po.po_number).budget_id == w["budget"].pk


def test_closed_fiscal_period_cannot_receive_commitments(world):
    w = world
    FiscalPeriod.objects.filter(pk=w["period"].pk).update(is_closed=True)
    with pytest.raises(ValidationError, match="No active fiscal period budget"):
        make_po(w, 100)


def test_amendment_and_invoice_follow_the_budget_the_po_was_committed_to(world):
    w = world
    po = make_po(w, 1000)
    # a later-created budget for another period must not capture follow-up movements
    today = w["today"]
    other_period = FiscalPeriod.objects.create(
        organization=w["org"],
        year=today.year + 1,
        period_number=1,
        name="FY-N2",
        start_date=today + datetime.timedelta(days=301),
        end_date=today + datetime.timedelta(days=600),
    )
    other_b = allocate_budget_service(
        cost_center=w["cc"], fiscal_period=other_period, allocated_amount=D("100000")
    )
    PurchaseOrder.objects.filter(pk=po.pk).update(status=PurchaseOrder.STATUS_ACKNOWLEDGED)
    po.refresh_from_db()
    amd = request_po_amendment_service(
        po=po,
        reason="more",
        requested_by_user=w["pe"],
        updated_line_items=[{"item_description": "Item", "quantity": 1, "unit_price": 2000}],
    )
    approve_po_amendment_service(amendment=amd, approver=w["mgr"])
    assert fresh(w["budget"]).committed_amount == D("2200.00")
    assert fresh(other_b).committed_amount == D("0.00")


# ------------------------------------------------------------------ amendments
def _ack(po):
    PurchaseOrder.objects.filter(pk=po.pk).update(status=PurchaseOrder.STATUS_ACKNOWLEDGED)
    po.refresh_from_db()
    return po


def test_amendment_increase_beyond_headroom_is_refused_at_request(world):
    w = world
    po = _ack(make_po(w, 1000))
    with pytest.raises(ValidationError, match="Insufficient budget"):
        request_po_amendment_service(
            po=po,
            reason="huge",
            requested_by_user=w["pe"],
            updated_line_items=[{"item_description": "Item", "quantity": 1, "unit_price": 500000}],
        )
    assert not POAmendment.objects.filter(po=po).exists()


def test_amendment_is_rechecked_at_approval_when_budget_was_used_meanwhile(world):
    w = world
    po = _ack(make_po(w, 1000))
    amd = request_po_amendment_service(
        po=po,
        reason="more",
        requested_by_user=w["pe"],
        updated_line_items=[{"item_description": "Item", "quantity": 1, "unit_price": 80000}],
    )  # fits now (88,000 delta ~ 86,900 vs ~98,900 headroom)
    make_po(w, 50000)  # consumes the headroom in the meantime
    with pytest.raises(ValidationError, match="Insufficient budget"):
        approve_po_amendment_service(amendment=amd, approver=w["mgr"])
    amd.refresh_from_db()
    po.refresh_from_db()
    assert amd.status == POAmendment.STATUS_PENDING
    assert po.version == 1
    assert po.total_amount == D("1100.00")


def test_amendment_decrease_releases_commitment_through_the_ledger(world):
    w = world
    po = _ack(make_po(w, 10000))
    amd = request_po_amendment_service(
        po=po,
        reason="less",
        requested_by_user=w["pe"],
        updated_line_items=[{"item_description": "Item", "quantity": 1, "unit_price": 5000}],
    )
    approve_po_amendment_service(amendment=amd, approver=w["mgr"])
    b = fresh(w["budget"])
    assert b.committed_amount == D("5500.00")
    assert po_net_commitment(po) == D("5500.00")
    assert reconcile_budget(b)["in_sync"]


# ------------------------------------------------------------------ ledger is the source of truth
def test_full_lifecycle_keeps_ledger_and_counters_in_sync(world):
    from apps.budgets.services import convert_commitment_to_actual_service

    w = world
    pr = make_pr(w, 10000)
    po = make_po(w, 10000, pr=pr)
    _ack(po)
    amd = request_po_amendment_service(
        po=po,
        reason="more",
        requested_by_user=w["pe"],
        updated_line_items=[{"item_description": "Item", "quantity": 1, "unit_price": 12000}],
    )
    approve_po_amendment_service(amendment=amd, approver=w["mgr"])

    class _Inv:
        total_amount = D("5000.00")
        invoice_number = "INV-LC-1"

    convert_commitment_to_actual_service(po=po, invoice=_Inv(), user=w["mgr"])
    b = fresh(w["budget"])
    assert b.reserved_amount == D("0.00")
    assert b.committed_amount == D("8200.00")  # 13,200 committed - 5,000 invoiced
    assert b.actual_amount == D("5000.00")
    result = reconcile_budget(b)
    assert result["in_sync"], result

    cancel_purchase_order_governance_service(po=po, actor=w["mgr"], reason="cancel rest")
    assert reconcile_budget(fresh(w["budget"]))["in_sync"]


def test_cancelling_a_never_committed_draft_po_does_not_touch_the_budget(world):
    w = world
    keep = make_po(w, 5000)  # committed 5,500 by a different PO
    draft = make_po(w, 3000, initial_status=PurchaseOrder.STATUS_DRAFT)
    assert fresh(w["budget"]).committed_amount == D("5500.00")
    cancel_purchase_order_governance_service(po=draft, actor=w["mgr"], reason="not needed")
    assert fresh(w["budget"]).committed_amount == D("5500.00")  # keep's commitment intact
    assert PurchaseOrder.objects.get(pk=keep.pk).status == PurchaseOrder.STATUS_ISSUED


def test_ledger_entries_are_immutable(world):
    w = world
    make_po(w, 100)
    entry = SpendLedger.objects.first()
    with pytest.raises(ValidationError):
        SpendLedger.objects.filter(pk=entry.pk).update(amount=D("1"))
    with pytest.raises(ValidationError):
        SpendLedger.objects.filter(pk=entry.pk).delete()
    with pytest.raises(ValidationError):
        entry.delete()
    entry.amount = D("1")
    with pytest.raises(ValidationError):
        entry.save()
    entry.refresh_from_db()
    assert entry.amount != D("1")


def test_reconcile_command_detects_and_fixes_drift_by_appending(world):
    w = world
    make_po(w, 1000)
    call_command("reconcile_budgets", stdout=StringIO())  # in sync: no error

    Budget.objects.filter(pk=w["budget"].pk).update(committed_amount=D("999999.00"))  # tamper
    before = SpendLedger.objects.count()
    with pytest.raises(CommandError):
        call_command("reconcile_budgets", stdout=StringIO())
    assert SpendLedger.objects.count() == before  # report-only mode writes nothing

    call_command("reconcile_budgets", "--fix", stdout=StringIO())
    assert SpendLedger.objects.count() == before + 1  # appended, never edited
    assert reconcile_budget(fresh(w["budget"]))["in_sync"]
    call_command("reconcile_budgets", stdout=StringIO())


# ------------------------------------------------------------------ configurable tax rate
def test_po_tax_rate_defaults_from_settings_and_is_stored_per_po(world, settings):
    w = world
    settings.DEFAULT_PO_TAX_RATE = "0.18"
    po = make_po(w, 1000)
    assert po.tax_rate == D("0.18")
    assert po.tax_amount == D("180.00")
    assert po.total_amount == D("1180.00")

    settings.DEFAULT_PO_TAX_RATE = "0.05"  # later change must not rewrite existing orders
    po.refresh_from_db()
    assert po.tax_rate == D("0.18")
    assert make_po(w, 1000).tax_rate == D("0.05")


def test_explicit_tax_rate_and_validation(world):
    w = world
    assert make_po(w, 1000, tax_rate=D("0")).total_amount == D("1000.00")
    with pytest.raises(ValidationError, match="Tax rate"):
        make_po(w, 1000, tax_rate=D("1.5"))
    with pytest.raises(ValidationError, match="Tax rate"):
        make_po(w, 1000, tax_rate=D("-0.1"))


def test_amendment_uses_the_pos_own_tax_rate_not_a_hardcoded_ten_percent(world, settings):
    w = world
    settings.DEFAULT_PO_TAX_RATE = "0.18"
    po = _ack(make_po(w, 1000))
    amd = request_po_amendment_service(
        po=po,
        reason="more",
        requested_by_user=w["pe"],
        updated_line_items=[{"item_description": "Item", "quantity": 1, "unit_price": 2000}],
    )
    approve_po_amendment_service(amendment=amd, approver=w["mgr"])
    po.refresh_from_db()
    assert po.subtotal == D("2000.00")
    assert po.tax_amount == D("360.00")  # 18%, not 10%
    assert po.total_amount == D("2360.00")


def test_invoice_tax_follows_the_po_tax_rate(world, settings):
    w = world
    settings.DEFAULT_PO_TAX_RATE = "0.18"
    po = _ack(make_po(w, 1000))
    inv = create_supplier_invoice_service(
        vendor=w["vendor"],
        po=po,
        invoice_number="INV-TAX-1",
        invoice_date=w["today"],
        due_date=w["today"] + datetime.timedelta(days=30),
        line_items=[
            {
                "po_line": po.lines.first(),
                "item_description": "Item",
                "quantity": 1,
                "unit_price": 1000,
            }
        ],
        created_by_user=w["pe"],
    )
    assert inv.tax_amount == D("180.00")
    assert inv.total_amount == D("1180.00")


# ------------------------------------------------------------------ demo seed obeys the rules
def test_demo_seed_runs_on_a_fresh_database_and_leaves_consistent_budgets(settings, tmp_path):
    from rest_framework.test import APIClient

    settings.MEDIA_ROOT = str(tmp_path / "media")
    call_command("seed_demo_data", verbosity=0)  # first run on an empty DB must succeed
    call_command("seed_demo_data", verbosity=0)  # and be idempotent

    for budget in Budget.objects.all():
        assert reconcile_budget(budget)["in_sync"], budget
        assert budget.available_amount >= D("0.00")

    pr = PurchaseRequisition.objects.get(title="Enterprise Core Routers Upgrade")
    assert pr.status == PurchaseRequisition.STATUS_PO_ISSUED  # approved, then a PO was issued
    po = PurchaseOrder.objects.get(requisition=pr)
    res = BudgetReservation.objects.get(requisition=pr)
    assert res.status == BudgetReservation.STATUS_COMMITTED  # no stranded reservation

    # internal demo users belong to the organization, so Finance actually sees the data
    finance = User.objects.get(email="finance@hpe.com")
    client = APIClient()
    client.force_authenticate(finance)
    assert client.get("/api/v1/purchase-orders/").data["count"] >= 1
    assert client.get("/api/v1/budgets/").data["count"] >= 1
    assert po.vendor.status == Vendor.STATUS_ACTIVE

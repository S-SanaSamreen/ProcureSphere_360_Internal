"""
PRD ROLE_WORKFLOWS gaps: sourcing controls, invoice exception options, service entries,
vendor bank/tax change requests, over-delivery tolerance and budget transfers.
"""

import datetime as dt
from datetime import timedelta
from decimal import Decimal

import pytest
from django.core.exceptions import PermissionDenied, ValidationError
from django.utils import timezone

from apps.accounts.models import Role, User
from apps.audit.models import AuditLog
from apps.budgets import services as budget_services
from apps.budgets.models import Budget, BudgetTransfer, SpendLedger
from apps.invoices.models import MatchException, SupplierInvoice
from apps.invoices.services import (
    create_supplier_invoice_service,
    hold_exception_until_receipt_service,
    request_credit_note_service,
    run_3_way_match_service,
    submit_revised_invoice_service,
)
from apps.notifications.models import Notification
from apps.orders import service_entries as se
from apps.orders.models import ServiceEntry
from apps.orders.services import generate_purchase_order_service
from apps.organization.models import CostCenter, Department, FiscalPeriod, Organization
from apps.receipts.services import create_goods_receipt_service
from apps.requisitions.services import create_purchase_requisition_service
from apps.sourcing import services as svc
from apps.sourcing.models import ProcurementControlSetting, SourcingDeviation, SourcingEvent
from apps.vendors import services as vsvc
from apps.vendors.models import Vendor, VendorChangeRequest
from tests.test_pe_rfq_sourcing import make_event, open_event

pytestmark = pytest.mark.django_db


def invite(world, event, *keys):
    svc.invite_vendors_to_event_service(
        event=event, vendor_ids=[world[k].id for k in keys], invited_by=world["exec"]
    )


def configure(world, **kw):
    return ProcurementControlSetting.objects.update_or_create(
        organization=world["org"], defaults=kw
    )[0]


# ----------------------------------------------------------------------------- sourcing
def test_extend_deadline_requires_reason_notifies_vendors_and_audits(procurement_world):
    w = procurement_world
    event = open_event(w)
    new_deadline = event.bid_end_date + timedelta(days=2)
    with pytest.raises(ValidationError, match="reason"):
        svc.extend_bid_deadline_service(
            event=event, user=w["exec"], new_deadline=new_deadline, reason=" "
        )
    before = Notification.objects.filter(recipient=w["user_a"]).count()
    svc.extend_bid_deadline_service(
        event=event, user=w["exec"], new_deadline=new_deadline, reason="Vendors asked for time"
    )
    event.refresh_from_db()
    assert event.bid_end_date == new_deadline
    assert event.deadline_extensions.count() == 1
    assert Notification.objects.filter(recipient=w["user_a"]).count() == before + 1
    assert Notification.objects.filter(recipient=w["user_b"], title__contains="extended").exists()
    assert AuditLog.objects.filter(
        target_model="SourcingEvent", new_state__action="DEADLINE_EXTENDED"
    ).exists()
    with pytest.raises(ValidationError, match="later"):
        svc.extend_bid_deadline_service(
            event=event, user=w["exec"], new_deadline=new_deadline, reason="again"
        )


def test_extend_deadline_restricted_to_procurement_of_the_same_org(procurement_world):
    w = procurement_world
    event = open_event(w)
    later = event.bid_end_date + timedelta(days=1)
    for key in ("finance", "requester", "user_a"):
        with pytest.raises(PermissionDenied):
            svc.extend_bid_deadline_service(
                event=event, user=w[key], new_deadline=later, reason="x"
            )
    other = Organization.objects.create(name="Other", code="OTH")
    outsider = User.objects.create_user(
        email="pm@other.test", password="Password123!", role=w["mgr"].role, organization=other
    )
    with pytest.raises(PermissionDenied):
        svc.extend_bid_deadline_service(event=event, user=outsider, new_deadline=later, reason="x")
    orgless = User.objects.create_user(
        email="pm2@none.test", password="Password123!", role=w["mgr"].role
    )
    with pytest.raises(PermissionDenied):
        svc.extend_bid_deadline_service(event=event, user=orgless, new_deadline=later, reason="x")


def test_extend_deadline_not_after_close(procurement_world):
    w = procurement_world
    event = open_event(w)
    event.bid_end_date = timezone.now() - timedelta(minutes=1)
    event.save(update_fields=["bid_end_date"])
    with pytest.raises(ValidationError, match="passed|open"):
        svc.extend_bid_deadline_service(
            event=event,
            user=w["exec"],
            new_deadline=timezone.now() + timedelta(days=1),
            reason="late",
        )


def test_min_bidder_rule_blocks_publish_until_deviation_approved(procurement_world):
    w = procurement_world
    configure(w, min_bidders=3, enforce_min_bidders=True)
    event = make_event(w)
    invite(w, event, "vendor_a", "vendor_b")
    with pytest.raises(ValidationError, match="minimum is 3"):
        svc.publish_sourcing_event_service(event=event, user=w["exec"])
    dev = svc.request_deviation_service(
        event=event,
        user=w["exec"],
        kind=SourcingDeviation.KIND_MIN_BIDDERS_PUBLISH,
        reason="Niche category",
    )
    with pytest.raises(PermissionDenied):
        svc.decide_deviation_service(deviation=dev, approver=w["exec2"], approve=True)
    svc.decide_deviation_service(deviation=dev, approver=w["mgr"], approve=True, comments="ok")
    svc.publish_sourcing_event_service(event=event, user=w["exec"])
    event.refresh_from_db()
    assert event.status in (SourcingEvent.STATUS_PUBLISHED, SourcingEvent.STATUS_BID_WINDOW)
    assert AuditLog.objects.filter(target_model="SourcingDeviation", action="APPROVE").exists()


def test_default_settings_keep_existing_events_publishable(procurement_world):
    w = procurement_world
    event = open_event(w, vendors=("vendor_a",))  # single vendor, no controls configured
    assert event.status in (SourcingEvent.STATUS_PUBLISHED, SourcingEvent.STATUS_BID_WINDOW)


def test_deviation_cannot_be_self_approved_and_rejection_needs_reason(procurement_world):
    w = procurement_world
    configure(w, enforce_min_bidders=True)
    event = make_event(w)
    dev = svc.request_deviation_service(
        event=event,
        user=w["mgr"],
        kind=SourcingDeviation.KIND_MIN_BIDDERS_PUBLISH,
        reason="x",
    )
    with pytest.raises(ValidationError, match="Separation"):
        svc.decide_deviation_service(deviation=dev, approver=w["mgr"], approve=True)
    dev2 = svc.request_deviation_service(
        event=event, user=w["exec"], kind=SourcingDeviation.KIND_SOLE_SOURCE, reason="only one"
    )
    with pytest.raises(ValidationError, match="reason"):
        svc.decide_deviation_service(deviation=dev2, approver=w["mgr"], approve=False)


def test_min_bids_gate_on_close_and_award(procurement_world):
    w = procurement_world
    configure(w, min_bidders=2, enforce_min_bidders=True)
    event = open_event(w)  # 2 invited: passes publish
    from tests.test_pe_rfq_sourcing import submit

    submit(w, event, "vendor_a", "user_a")
    with pytest.raises(ValidationError, match="minimum is 2"):
        svc.close_bid_window_service(event=event, user=w["exec"])
    dev = svc.request_deviation_service(
        event=event,
        user=w["exec"],
        kind=SourcingDeviation.KIND_MIN_BIDDERS_AWARD,
        reason="Other vendor declined",
    )
    svc.decide_deviation_service(deviation=dev, approver=w["mgr"], approve=True)
    svc.close_bid_window_service(event=event, user=w["exec"])
    event.refresh_from_db()
    assert event.status == SourcingEvent.STATUS_TECHNICAL_REVIEW


def test_sole_source_needs_justification_and_manager_approval_before_publish(procurement_world):
    w = procurement_world
    event = make_event(w)
    invite(w, event, "vendor_a")
    with pytest.raises(ValidationError, match="justification"):
        svc.set_sole_source_service(event=event, user=w["exec"], justification="  ")
    svc.set_sole_source_service(event=event, user=w["exec"], justification="Proprietary system")
    with pytest.raises(ValidationError, match="approval"):
        svc.publish_sourcing_event_service(event=event, user=w["exec"])
    dev = event.deviations.get(kind=SourcingDeviation.KIND_SOLE_SOURCE)
    svc.decide_deviation_service(deviation=dev, approver=w["mgr"], approve=True)
    svc.publish_sourcing_event_service(event=event, user=w["exec"])
    event.refresh_from_db()
    assert event.is_sole_source and event.status != SourcingEvent.STATUS_DRAFT


def test_high_value_publish_needs_manager_approval(procurement_world, monkeypatch):
    w = procurement_world
    configure(w, publish_approval_threshold=Decimal("1000.00"))
    event = make_event(w)
    invite(w, event, "vendor_a", "vendor_b")
    monkeypatch.setattr(svc, "_estimated_value", lambda e: Decimal("5000.00"))
    with pytest.raises(ValidationError, match="Procurement Manager approval"):
        svc.publish_sourcing_event_service(event=event, user=w["exec"])
    dev = svc.request_deviation_service(
        event=event,
        user=w["exec"],
        kind=SourcingDeviation.KIND_HIGH_VALUE_PUBLISH,
        reason="Board-approved spend",
    )
    svc.decide_deviation_service(deviation=dev, approver=w["mgr"], approve=True)
    svc.publish_sourcing_event_service(event=event, user=w["exec"])


# ----------------------------------------------------------------------------- PO helpers
def make_po(w, *, service=False, qty=10, price=100):
    dept = w["cost_center"].department
    pr = create_purchase_requisition_service(
        title="Gap PR",
        justification="Gap tests justification text",
        requester=w["requester"],
        department=dept,
        cost_center=w["cost_center"],
        requested_delivery_date=timezone.now().date() + timedelta(days=30),
        line_items=[{"item_description": "Item", "quantity": qty, "estimated_unit_price": price}],
    )
    pr.status = pr.STATUS_APPROVED
    pr.save(update_fields=["status"])
    po = generate_purchase_order_service(
        vendor=w["vendor_a"],
        cost_center=w["cost_center"],
        requisition=pr,
        line_items=[{"item_description": "Item", "quantity": qty, "unit_price": price}],
        created_by_user=w["requester"],
    )
    line = po.lines.first()
    if service:
        line.is_service = True
        line.save(update_fields=["is_service"])
    return po, line


def make_invoice(w, po, line, qty, price=100, number="INV-G-1"):
    return create_supplier_invoice_service(
        vendor=w["vendor_a"],
        po=po,
        invoice_number=number,
        invoice_date=timezone.now().date(),
        due_date=timezone.now().date() + timedelta(days=30),
        line_items=[
            {
                "po_line": line,
                "item_description": "Item",
                "quantity": qty,
                "unit_price": price,
            }
        ],
        created_by_user=w["requester"],
    )


def receive(w, po, line, qty):
    return create_goods_receipt_service(
        po=po,
        received_by=w["stores"],
        receipt_items=[{"po_line": line, "quantity_received": qty, "quantity_accepted": qty}],
    )


# ----------------------------------------------------------------------------- invoices
def qty_exception(w):
    po, line = make_po(w)
    receive(w, po, line, 4)
    invoice = make_invoice(w, po, line, 10)
    invoice, excs = run_3_way_match_service(invoice=invoice, user=w["finance"])
    assert invoice.status == SupplierInvoice.STATUS_EXCEPTION
    return po, line, invoice, excs[0]


def test_request_credit_note_keeps_exception_open_and_vendor_can_revise(procurement_world):
    w = procurement_world
    po, line, invoice, exc = qty_exception(w)
    with pytest.raises(ValidationError, match="Permission"):
        request_credit_note_service(match_exception=exc, user=w["exec"], notes="x")
    request_credit_note_service(match_exception=exc, user=w["finance"], notes="Bill 4 units only")
    exc.refresh_from_db()
    invoice.refresh_from_db()
    assert exc.status == MatchException.STATUS_AWAITING_VENDOR
    assert invoice.status == SupplierInvoice.STATUS_EXCEPTION
    assert Notification.objects.filter(
        recipient=w["user_a"], title__contains="Credit note"
    ).exists()

    with pytest.raises(ValidationError, match="another vendor"):
        submit_revised_invoice_service(
            invoice=invoice,
            vendor_user=w["user_b"],
            line_items=[{"line_id": invoice.lines.first().pk, "quantity": 4, "unit_price": 100}],
        )
    inv, new_excs = submit_revised_invoice_service(
        invoice=invoice,
        vendor_user=w["user_a"],
        line_items=[{"line_id": invoice.lines.first().pk, "quantity": 4, "unit_price": 100}],
        notes="corrected",
    )
    exc.refresh_from_db()
    assert exc.status == MatchException.STATUS_RESOLVED and not new_excs
    assert inv.status == SupplierInvoice.STATUS_READY_FOR_PAYMENT
    assert inv.total_amount == Decimal("440.00")


def test_hold_until_receipt_posted_then_auto_rematch(procurement_world):
    w = procurement_world
    po, line, invoice, exc = qty_exception(w)
    hold_exception_until_receipt_service(match_exception=exc, user=w["finance"], notes="wait GRN")
    exc.refresh_from_db()
    assert exc.status == MatchException.STATUS_HELD
    receive(w, po, line, 6)  # now 10 accepted in total
    invoice.refresh_from_db()
    exc.refresh_from_db()
    assert exc.status == MatchException.STATUS_RESOLVED
    assert invoice.status == SupplierInvoice.STATUS_READY_FOR_PAYMENT


def test_hold_only_for_quantity_exceptions_and_finance(procurement_world):
    w = procurement_world
    po, line, invoice, exc = qty_exception(w)
    with pytest.raises(ValidationError, match="Permission"):
        hold_exception_until_receipt_service(match_exception=exc, user=w["mgr"], notes="n")
    exc.exception_type = MatchException.TYPE_PRICE_VARIANCE
    exc.save()
    with pytest.raises(ValidationError, match="quantity"):
        hold_exception_until_receipt_service(match_exception=exc, user=w["finance"], notes="n")


def test_exception_detail_page_exposes_new_actions(procurement_world, client):
    w = procurement_world
    _, _, _, exc = qty_exception(w)
    client.force_login(w["finance"])
    html = client.get(f"/invoices/exceptions/{exc.id}/").content.decode()
    assert "REQUEST_CREDIT_NOTE" in html and "HOLD" in html
    resp = client.post(
        f"/invoices/exceptions/{exc.id}/resolve/",
        {"action": "HOLD", "resolution_notes": "waiting for GRN"},
    )
    assert resp.status_code == 302
    exc.refresh_from_db()
    assert exc.status == MatchException.STATUS_HELD


# ----------------------------------------------------------------------------- service entries
def test_service_entry_flow_counts_toward_three_way_match(procurement_world):
    w = procurement_world
    po, line = make_po(w, service=True)
    invoice = make_invoice(w, po, line, 5)
    invoice, excs = run_3_way_match_service(invoice=invoice, user=w["finance"])
    assert excs and invoice.status == SupplierInvoice.STATUS_EXCEPTION  # nothing accepted yet

    with pytest.raises(PermissionDenied):
        se.record_service_entry_service(
            po_line=line, user=w["finance"], milestone="Phase 1", percent_complete=50
        )
    entry = se.record_service_entry_service(
        po_line=line, user=w["stores"], milestone="Phase 1", percent_complete=50
    )
    assert se.confirmed_service_quantity(line) == Decimal("0.00")  # not yet confirmed
    with pytest.raises(PermissionDenied):
        se.decide_service_entry_service(entry=entry, user=w["stores"], confirm=True)
    with pytest.raises(PermissionDenied):
        se.decide_service_entry_service(entry=entry, user=w["exec"], confirm=True)
    se.decide_service_entry_service(entry=entry, user=w["requester"], confirm=True)
    entry.refresh_from_db()
    assert entry.status == ServiceEntry.STATUS_CONFIRMED
    assert se.confirmed_service_quantity(line) == Decimal("5.00")

    invoice, excs2 = run_3_way_match_service(invoice=invoice, user=w["finance"])
    assert not excs2 and invoice.status == SupplierInvoice.STATUS_READY_FOR_PAYMENT


def test_service_entry_validation(procurement_world):
    w = procurement_world
    po, line = make_po(w, service=True)
    goods_po, goods_line = make_po(w)
    with pytest.raises(ValidationError, match="not a service"):
        se.record_service_entry_service(
            po_line=goods_line, user=w["stores"], milestone="m", percent_complete=10
        )
    se.record_service_entry_service(
        po_line=line, user=w["stores"], milestone="m1", percent_complete=80
    )
    with pytest.raises(ValidationError, match="exceed 100"):
        se.record_service_entry_service(
            po_line=line, user=w["stores"], milestone="m2", percent_complete=30
        )
    with pytest.raises(ValidationError, match="greater than 0"):
        se.record_service_entry_service(
            po_line=line, user=w["stores"], milestone="m3", percent_complete=0
        )


def test_service_entry_other_org_stores_cannot_record(procurement_world):
    w = procurement_world
    po, line = make_po(w, service=True)
    other = Organization.objects.create(name="O2", code="O2")
    outsider = User.objects.create_user(
        email="stores@o2.test", password="Password123!", role=w["stores"].role, organization=other
    )
    with pytest.raises(PermissionDenied):
        se.record_service_entry_service(
            po_line=line, user=outsider, milestone="m", percent_complete=10
        )


# ----------------------------------------------------------------------------- vendor changes
def test_active_vendor_bank_change_waits_for_manager_approval(procurement_world):
    w = procurement_world
    vendor = w["vendor_a"]
    vendor.organization = w["org"]
    vendor.bank_account_number = "OLD-ACC"
    vendor.save(update_fields=["organization", "bank_account_number"])

    vsvc.update_vendor_profile_service(
        vendor=vendor,
        user=w["user_a"],
        data={"bank_account_number": "NEW-ACC", "trade_name": "Alpha Trading"},
    )
    vendor.refresh_from_db()
    assert vendor.bank_account_number == "OLD-ACC"  # old value stays until approved
    assert vendor.trade_name == "Alpha Trading"  # non-sensitive fields still apply
    req = VendorChangeRequest.objects.get(vendor=vendor)
    assert req.status == VendorChangeRequest.STATUS_PENDING
    assert req.changes["bank_account_number"] == {"old": "OLD-ACC", "new": "NEW-ACC"}

    with pytest.raises(PermissionDenied):
        vsvc.decide_vendor_change_request_service(
            change_request=req, approver=w["exec"], approve=True
        )
    vsvc.decide_vendor_change_request_service(
        change_request=req, approver=w["mgr"], approve=True, comments="verified by phone"
    )
    vendor.refresh_from_db()
    assert vendor.bank_account_number == "NEW-ACC"
    assert AuditLog.objects.filter(target_model="VendorChangeRequest").exists()
    assert AuditLog.objects.filter(target_model="Vendor", action="APPROVE").exists()


def test_vendor_change_rejected_keeps_old_and_non_active_applies_directly(procurement_world):
    w = procurement_world
    vendor = w["vendor_a"]
    vendor.organization = w["org"]
    vendor.bank_name = "Old Bank"
    vendor.save(update_fields=["organization", "bank_name"])
    vsvc.update_vendor_profile_service(vendor=vendor, user=w["user_a"], data={"bank_name": "Evil"})
    req = VendorChangeRequest.objects.get(vendor=vendor)
    with pytest.raises(ValidationError, match="reason"):
        vsvc.decide_vendor_change_request_service(
            change_request=req, approver=w["mgr"], approve=False
        )
    vsvc.decide_vendor_change_request_service(
        change_request=req, approver=w["mgr"], approve=False, comments="not verified"
    )
    vendor.refresh_from_db()
    assert vendor.bank_name == "Old Bank"

    draft = w["vendor_draft"]
    vsvc.update_vendor_profile_service(vendor=draft, user=w["exec"], data={"bank_name": "Direct"})
    draft.refresh_from_db()
    assert draft.bank_name == "Direct"
    assert not VendorChangeRequest.objects.filter(vendor=draft).exists()


def test_vendor_change_approval_is_org_scoped_and_internal_edit_is_staged(procurement_world):
    w = procurement_world
    vendor = w["vendor_a"]
    vendor.organization = w["org"]
    vendor.save(update_fields=["organization"])
    vsvc.update_vendor_service(
        vendor=vendor,
        actor=w["exec"],
        legal_name=vendor.legal_name,
        trade_name=vendor.trade_name,
        email=vendor.email,
        phone=vendor.phone,
        address=vendor.address,
        category=vendor.category,
        bank_name="Staged Bank",
        bank_account_number=vendor.bank_account_number,
        bank_routing_code=vendor.bank_routing_code,
    )
    vendor.refresh_from_db()
    assert vendor.bank_name != "Staged Bank"
    req = VendorChangeRequest.objects.get(vendor=vendor)
    other = Organization.objects.create(name="O3", code="O3")
    foreign_pm = User.objects.create_user(
        email="pm@o3.test", password="Password123!", role=w["mgr"].role, organization=other
    )
    with pytest.raises(PermissionDenied):
        vsvc.decide_vendor_change_request_service(
            change_request=req, approver=foreign_pm, approve=True
        )


# ----------------------------------------------------------------------------- over-delivery
def test_over_delivery_tolerance(procurement_world):
    w = procurement_world
    po, line = make_po(w, qty=100)
    with pytest.raises(ValidationError, match="cannot exceed remaining quantity"):
        receive(w, po, line, 105)  # default tolerance 0
    configure(w, over_delivery_tolerance_pct=Decimal("5.00"))
    receive(w, po, line, 105)  # within +5%
    line.refresh_from_db()
    assert line.quantity_received == Decimal("105.00")
    po2, line2 = make_po(w, qty=100)
    with pytest.raises(ValidationError, match="5.00% over-delivery tolerance"):
        receive(w, po2, line2, 106)


# ----------------------------------------------------------------------------- budget transfer
def two_budgets(w, dest_alloc="1000.00"):
    dept = w["cost_center"].department
    cc2 = CostCenter.objects.create(name="Ops", code="CC-T-02", department=dept)
    src = Budget.objects.get(cost_center=w["cost_center"])
    dst = Budget.objects.create(
        cost_center=cc2, fiscal_period=src.fiscal_period, allocated_amount=Decimal(dest_alloc)
    )
    return src, dst


def test_budget_transfer_requires_finance_and_engine_approval(procurement_world):
    w = procurement_world
    src, dst = two_budgets(w)
    with pytest.raises(ValidationError, match="Finance"):
        budget_services.request_budget_transfer_service(
            source=src, destination=dst, amount=500, reason="r", requested_by=w["exec"]
        )
    transfer = budget_services.request_budget_transfer_service(
        source=src, destination=dst, amount=500, reason="Rebalance", requested_by=w["finance"]
    )
    src.refresh_from_db()
    assert transfer.status == BudgetTransfer.STATUS_PENDING
    assert src.allocated_amount == Decimal("10000000.00")  # nothing moves before approval
    with pytest.raises(ValidationError, match="Self-approval|not authorized"):
        budget_services.approve_budget_transfer_service(transfer=transfer, approver=w["finance"])
    budget_services.approve_budget_transfer_service(
        transfer=transfer, approver=w["mgr"], comments="ok"
    )
    src.refresh_from_db()
    dst.refresh_from_db()
    transfer.refresh_from_db()
    assert transfer.status == BudgetTransfer.STATUS_APPROVED
    assert src.allocated_amount == Decimal("9999500.00")
    assert dst.allocated_amount == Decimal("1500.00")
    entries = SpendLedger.objects.filter(entry_type=SpendLedger.ENTRY_TRANSFER)
    assert sorted(e.amount for e in entries) == [Decimal("-500.00"), Decimal("500.00")]
    assert budget_services.reconcile_budget(src)["in_sync"]
    assert AuditLog.objects.filter(target_model="BudgetTransfer", action="APPROVE").exists()


def test_budget_transfer_blocked_below_consumed_and_across_scopes(procurement_world):
    w = procurement_world
    src, dst = two_budgets(w)
    budget_services.post_ledger_entry_service(
        budget=dst,
        entry_type=SpendLedger.ENTRY_COMMITMENT,
        amount=800,
        reference_number="PO-X",
        description="committed",
    )
    with pytest.raises(ValidationError, match="consumed"):
        budget_services.request_budget_transfer_service(
            source=dst, destination=src, amount=300, reason="r", requested_by=w["finance"]
        )
    budget_services.request_budget_transfer_service(
        source=dst, destination=src, amount=200, reason="ok", requested_by=w["finance"]
    )
    # different fiscal period
    other_period = FiscalPeriod.objects.create(
        organization=w["org"],
        year=2099,
        period_number=2,
        name="FY-NEXT",
        start_date=dt.date(2099, 1, 1),
        end_date=dt.date(2099, 12, 31),
    )
    foreign = Budget.objects.create(
        cost_center=dst.cost_center, fiscal_period=other_period, allocated_amount=Decimal("10")
    )
    with pytest.raises(ValidationError, match="same fiscal period"):
        budget_services.request_budget_transfer_service(
            source=src, destination=foreign, amount=1, reason="r", requested_by=w["finance"]
        )
    # different organization
    org2 = Organization.objects.create(name="O4", code="O4")
    dept2 = Department.objects.create(name="D4", code="D4", organization=org2)
    cc_other = CostCenter.objects.create(name="X", code="CC-X", department=dept2)
    other_budget = Budget.objects.create(
        cost_center=cc_other, fiscal_period=src.fiscal_period, allocated_amount=Decimal("10")
    )
    with pytest.raises(ValidationError, match="one organization"):
        budget_services.request_budget_transfer_service(
            source=src, destination=other_budget, amount=1, reason="r", requested_by=w["finance"]
        )


def test_budget_transfer_recheck_at_approval_and_rejection(procurement_world):
    w = procurement_world
    src, dst = two_budgets(w, dest_alloc="1000.00")
    transfer = budget_services.request_budget_transfer_service(
        source=dst, destination=src, amount=900, reason="move", requested_by=w["finance"]
    )
    budget_services.post_ledger_entry_service(
        budget=dst,
        entry_type=SpendLedger.ENTRY_RESERVATION,
        amount=500,
        reference_number="PR-Y",
        description="reserved after request",
    )
    with pytest.raises(ValidationError, match="consumed"):
        budget_services.approve_budget_transfer_service(transfer=transfer, approver=w["mgr"])
    budget_services.reject_budget_transfer_service(
        transfer=transfer, approver=w["mgr"], comments="no headroom"
    )
    transfer.refresh_from_db()
    dst.refresh_from_db()
    assert transfer.status == BudgetTransfer.STATUS_REJECTED
    assert dst.allocated_amount == Decimal("1000.00")


def test_budget_transfers_page_is_org_scoped(procurement_world, client):
    w = procurement_world
    src, dst = two_budgets(w)
    budget_services.request_budget_transfer_service(
        source=src, destination=dst, amount=10, reason="r", requested_by=w["finance"]
    )
    client.force_login(w["finance"])
    assert b"CC-T-02" in client.get("/budgets/transfers/").content
    other = Organization.objects.create(name="O5", code="O5")
    stranger = User.objects.create_user(
        email="fin@o5.test", password="Password123!", role=w["finance"].role, organization=other
    )
    client.force_login(stranger)
    page = client.get("/budgets/transfers/").content
    assert b"CC-T-02" not in page
    orgless = User.objects.create_user(
        email="fin@none.test", password="Password123!", role=w["finance"].role
    )
    client.force_login(orgless)
    assert b"CC-T-02" not in client.get("/budgets/transfers/").content


def test_role_constants_used_in_tests_exist():
    assert Role.FINANCE_AP and Role.PROC_MGR and Role.STORES_RECEIVER
    assert Vendor.STATUS_ACTIVE

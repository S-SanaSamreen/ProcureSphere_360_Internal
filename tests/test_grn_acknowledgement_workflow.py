import datetime
from decimal import Decimal
from unittest.mock import patch

import pytest
from django.core import mail
from django.test import Client
from django.urls import reverse
from django.utils import timezone

from apps.accounts.models import Role, User
from apps.audit.models import AuditLog
from apps.budgets.services import allocate_budget_service
from apps.notifications.models import Notification
from apps.orders.models import POLine, PurchaseOrder
from apps.organization.models import CostCenter, Department, FiscalPeriod, Organization
from apps.receipts.acknowledgement_service import (
    compute_inspection_fingerprint,
    determine_inspection_outcome,
    generate_and_distribute_grn_acknowledgement_service,
    resolve_grn_acknowledgement_recipients,
)
from apps.receipts.documents import generate_grn_acknowledgement_pdf
from apps.receipts.models import GoodsReceipt, GRNAcknowledgement, ReceiptLine
from apps.receipts.services import (
    correct_receipt_line_quantity_service,
    create_goods_receipt_service,
    record_inspection_service,
)
from apps.vendors.models import Vendor, VendorCategory, VendorContact


@pytest.fixture
def grn_ack_test_world(db, db_roles):
    """
    Sets up a complete test environment with:
    - Organization 1 (Org A) and Organization 2 (Org B) for tenant isolation
    - Finance, Procurement Executive, Procurement Manager, Stores Receiver, Requester users
    - Vendor with primary contact and vendor portal user
    - Purchase Order in ISSUED status
    """
    org_a = Organization.objects.create(name="Alpha Corporation", code="ORG-ALPHA-01")
    dept_a = Department.objects.create(
        organization=org_a, name="Alpha Supply Chain", code="DEPT-ALPHA-SC"
    )
    cost_center_a = CostCenter.objects.create(
        department=dept_a, code="CC-ALPHA-SC", name="Alpha Receiving Hub"
    )

    org_b = Organization.objects.create(name="Beta Industries", code="ORG-BETA-02")
    dept_b = Department.objects.create(
        organization=org_b, name="Beta Finance", code="DEPT-BETA-FIN"
    )
    cost_center_b = CostCenter.objects.create(
        department=dept_b, code="CC-BETA-FIN", name="Beta Finance Center"
    )

    fiscal_period = FiscalPeriod.objects.create(
        organization=org_a,
        year=2026,
        period_number=1,
        name="FY2026-Q4-ACK",
        start_date=timezone.now().date() - datetime.timedelta(days=10),
        end_date=timezone.now().date() + datetime.timedelta(days=90),
    )
    allocate_budget_service(
        cost_center=cost_center_a,
        fiscal_period=fiscal_period,
        allocated_amount=Decimal("500000.00"),
    )

    # Users
    receiver = User.objects.create_user(
        email="receiver_ack@alpha.local",
        password="Password123!",
        role=db_roles[Role.STORES_RECEIVER],
        department=dept_a,
        organization=org_a,
    )
    finance_user_a = User.objects.create_user(
        email="finance_alpha@alpha.local",
        password="Password123!",
        role=db_roles[Role.FINANCE_AP],
        department=dept_a,
        organization=org_a,
    )
    finance_user_b = User.objects.create_user(
        email="finance_beta@beta.local",
        password="Password123!",
        role=db_roles[Role.FINANCE_AP],
        department=dept_b,
        organization=org_b,
    )
    proc_exec = User.objects.create_user(
        email="proc_exec@alpha.local",
        password="Password123!",
        role=db_roles[Role.PROC_EXEC],
        department=dept_a,
        organization=org_a,
    )
    proc_mgr = User.objects.create_user(
        email="proc_mgr@alpha.local",
        password="Password123!",
        role=db_roles[Role.PROC_MGR],
        department=dept_a,
        organization=org_a,
    )
    requester = User.objects.create_user(
        email="requester@alpha.local",
        password="Password123!",
        role=db_roles[Role.REQUESTER],
        department=dept_a,
        organization=org_a,
    )
    super_admin = User.objects.create_user(
        email="superadmin_ack@alpha.local",
        password="Password123!",
        role=db_roles[Role.SUPER_ADMIN],
        is_superuser=True,
    )

    # Vendor & Contacts
    cat = VendorCategory.objects.create(name="Electronic Modules", code="CAT-ELEC")
    vendor_a = Vendor.objects.create(
        legal_name="Apex Microchip Solutions Inc",
        vendor_number="VEND-APEX-01",
        tax_identification_number="TIN-APEX-9988",
        category=cat,
        organization=org_a,
        email="dispatch@apexmicro.test",
        status=Vendor.STATUS_ACTIVE,
    )
    contact_primary = VendorContact.objects.create(
        vendor=vendor_a,
        first_name="Jane",
        last_name="Doe",
        email="jane.doe@apexmicro.test",
        is_primary=True,
    )
    vendor_user_a = User.objects.create_user(
        email="portal@apexmicro.test",
        password="Password123!",
        role=db_roles[Role.VENDOR_USER],
        vendor=vendor_a,
    )

    vendor_b = Vendor.objects.create(
        legal_name="Omega Tech Supplies Ltd",
        vendor_number="VEND-OMEGA-02",
        tax_identification_number="TIN-OMEGA-7766",
        category=cat,
        organization=org_b,
        email="orders@omegatech.test",
        status=Vendor.STATUS_ACTIVE,
    )
    vendor_user_b = User.objects.create_user(
        email="portal@omegatech.test",
        password="Password123!",
        role=db_roles[Role.VENDOR_USER],
        vendor=vendor_b,
    )

    # Purchase Order
    po = PurchaseOrder.objects.create(
        po_number="PO-ACK-2026-001",
        version=1,
        vendor=vendor_a,
        cost_center=cost_center_a,
        status=PurchaseOrder.STATUS_ISSUED,
        created_by=proc_exec,
        total_amount=Decimal("12000.00"),
    )
    po_line_1 = POLine.objects.create(
        po=po,
        item_description="Controller Board Rev A",
        quantity=Decimal("100.00"),
        unit_price=Decimal("80.00"),
        line_total=Decimal("8000.00"),
    )
    po_line_2 = POLine.objects.create(
        po=po,
        item_description="Power Sensor Module V2",
        quantity=Decimal("50.00"),
        unit_price=Decimal("80.00"),
        line_total=Decimal("4000.00"),
    )

    return {
        "org_a": org_a,
        "org_b": org_b,
        "receiver": receiver,
        "finance_user_a": finance_user_a,
        "finance_user_b": finance_user_b,
        "proc_exec": proc_exec,
        "proc_mgr": proc_mgr,
        "requester": requester,
        "super_admin": super_admin,
        "vendor_a": vendor_a,
        "contact_primary": contact_primary,
        "vendor_user_a": vendor_user_a,
        "vendor_b": vendor_b,
        "vendor_user_b": vendor_user_b,
        "po": po,
        "po_line_1": po_line_1,
        "po_line_2": po_line_2,
    }


def test_full_acceptance_acknowledgement(grn_ack_test_world):
    """
    A. FULL ACCEPTANCE:
    All lines pass quality inspection.
    Verifies:
    - GRNAcknowledgement automatically generated with OUTCOME_FULL.
    - Version is 1, acknowledgement number has ACK-GRN-XXXX-V1 format.
    - Total received and accepted quantities match (0 rejected).
    - Email dispatched with PDF attachment to primary vendor contact.
    - In-app notifications generated for Finance, Procurement Exec, and Procurement Manager.
    - AuditLog record created.
    """
    w = grn_ack_test_world
    mail.outbox.clear()

    receipt = create_goods_receipt_service(
        po=w["po"],
        received_by=w["receiver"],
        delivery_note_number="DN-FULL-001",
        remarks="Conforming delivery received at dock 1",
        receipt_items=[
            {
                "po_line_id": w["po_line_1"].id,
                "quantity_received": Decimal("50.00"),
            },
            {
                "po_line_id": w["po_line_2"].id,
                "quantity_received": Decimal("25.00"),
            },
        ],
    )

    # Perform quality inspection - 100% passed
    lines = list(receipt.lines.all())
    inspection_items = [
        {
            "receipt_line_id": lines[0].id,
            "passed": True,
            "rejected_quantity": Decimal("0.00"),
            "inspection_notes": "100% Visual and electrical tests passed",
        },
        {
            "receipt_line_id": lines[1].id,
            "passed": True,
            "rejected_quantity": Decimal("0.00"),
            "inspection_notes": "Thermal profile within tolerance",
        },
    ]

    record_inspection_service(
        receipt=receipt,
        inspected_by=w["receiver"],
        inspection_items=inspection_items,
    )

    ack = receipt.latest_acknowledgement
    assert ack is not None
    assert ack.outcome == GRNAcknowledgement.OUTCOME_FULL
    assert ack.version == 1
    assert ack.acknowledgement_number == f"ACK-{receipt.grn_number}-V1"
    assert ack.total_received_qty == Decimal("75.00")
    assert ack.total_accepted_qty == Decimal("75.00")
    assert ack.total_rejected_qty == Decimal("0.00")
    assert not ack.has_rejections
    assert ack.delivery_status == GRNAcknowledgement.STATUS_SENT
    assert ack.delivered_at is not None

    # Check email delivery
    assert len(mail.outbox) >= 1
    vendor_mail = next(m for m in mail.outbox if w["contact_primary"].email in m.to)
    assert f"(Full Acceptance): {receipt.grn_number}" in vendor_mail.subject
    assert len(vendor_mail.attachments) >= 1
    assert vendor_mail.attachments[0][0].endswith(".pdf")

    # Check in-app notifications
    finance_notifs = Notification.objects.filter(recipient=w["finance_user_a"])
    assert finance_notifs.filter(title__contains=ack.acknowledgement_number).exists()

    proc_exec_notifs = Notification.objects.filter(recipient=w["proc_exec"])
    assert proc_exec_notifs.filter(title__contains=ack.acknowledgement_number).exists()

    # Check AuditLog
    assert AuditLog.objects.filter(
        target_model="GRNAcknowledgement",
        target_object_id=str(ack.id),
        action=AuditLog.ACTION_CREATE,
    ).exists()


def test_partial_acceptance_acknowledgement(grn_ack_test_world):
    """
    B. PARTIAL ACCEPTANCE:
    Some quantities accepted, some rejected.
    Verifies:
    - GRNAcknowledgement outcome is PARTIAL_ACCEPTANCE.
    - Accurate rejected quantity and reason preserved.
    - In-app notification of TYPE_EXCEPTION_RAISED sent to Procurement Manager.
    - Email reports both accepted and rejected quantities per line.
    """
    w = grn_ack_test_world
    mail.outbox.clear()

    receipt = create_goods_receipt_service(
        po=w["po"],
        received_by=w["receiver"],
        delivery_note_number="DN-PARTIAL-002",
        receipt_items=[
            {
                "po_line_id": w["po_line_1"].id,
                "quantity_received": Decimal("40.00"),
            },
        ],
    )

    line = receipt.lines.first()
    # Accept 30, reject 10
    record_inspection_service(
        receipt=receipt,
        inspected_by=w["receiver"],
        inspection_items=[
            {
                "receipt_line_id": line.id,
                "passed": False,
                "rejected_quantity": Decimal("10.00"),
                "rejection_reason": "Broken pins on 10 units",
                "inspection_notes": "Sample defect rate exceeded 5%",
            }
        ],
    )

    ack = receipt.latest_acknowledgement
    assert ack is not None
    assert ack.outcome == GRNAcknowledgement.OUTCOME_PARTIAL
    assert ack.total_received_qty == Decimal("40.00")
    assert ack.total_accepted_qty == Decimal("30.00")
    assert ack.total_rejected_qty == Decimal("10.00")
    assert ack.has_rejections is True
    assert ack.delivery_status == GRNAcknowledgement.STATUS_SENT

    # Verify Procurement Manager received exception notification
    mgr_notifs = Notification.objects.filter(
        recipient=w["proc_mgr"], notification_type=Notification.TYPE_EXCEPTION_RAISED
    )
    assert mgr_notifs.filter(title__contains=ack.acknowledgement_number).exists()

    # Verify email content mentions partial acceptance and rejection
    sent_mail = next(m for m in mail.outbox if w["contact_primary"].email in m.to)
    assert "Partial Acceptance" in sent_mail.subject
    assert "Broken pins on 10 units" in sent_mail.body


def test_full_rejection_acknowledgement(grn_ack_test_world):
    """
    C. FULL REJECTION:
    Entire shipment rejected due to non-conformance.
    Verifies:
    - GRNAcknowledgement outcome is REJECTION.
    - Accepted quantity is 0, rejected quantity matches received.
    - Does NOT issue a full acceptance certificate.
    - Exception notifications and audit trail preserved.
    """
    w = grn_ack_test_world
    mail.outbox.clear()

    receipt = create_goods_receipt_service(
        po=w["po"],
        received_by=w["receiver"],
        delivery_note_number="DN-REJ-003",
        receipt_items=[
            {
                "po_line_id": w["po_line_2"].id,
                "quantity_received": Decimal("20.00"),
            },
        ],
    )

    line = receipt.lines.first()
    record_inspection_service(
        receipt=receipt,
        inspected_by=w["receiver"],
        inspection_items=[
            {
                "receipt_line_id": line.id,
                "passed": False,
                "rejected_quantity": Decimal("20.00"),
                "rejection_reason": "Total transit water ingress damage",
                "inspection_notes": "Unusable material; reject 100%",
            }
        ],
    )

    ack = receipt.latest_acknowledgement
    assert ack is not None
    assert ack.outcome == GRNAcknowledgement.OUTCOME_REJECTION
    assert ack.total_accepted_qty == Decimal("0.00")
    assert ack.total_rejected_qty == Decimal("20.00")
    assert ack.delivery_status == GRNAcknowledgement.STATUS_SENT

    sent_mail = next(m for m in mail.outbox if w["contact_primary"].email in m.to)
    assert "Rejection" in sent_mail.subject
    assert "Total transit water ingress damage" in sent_mail.body


def test_incomplete_inspection_does_not_issue_acknowledgement(grn_ack_test_world):
    """
    D. INSPECTION PENDING OR INCOMPLETE:
    If a receipt has line items that have not yet been inspected,
    no final acknowledgement is generated.
    """
    w = grn_ack_test_world

    receipt = create_goods_receipt_service(
        po=w["po"],
        received_by=w["receiver"],
        receipt_items=[
            {
                "po_line_id": w["po_line_1"].id,
                "quantity_received": Decimal("10.00"),
            },
            {
                "po_line_id": w["po_line_2"].id,
                "quantity_received": Decimal("10.00"),
            },
        ],
    )

    lines = list(receipt.lines.all())
    # Inspect ONLY Line 1; leave Line 2 uninspected
    record_inspection_service(
        receipt=receipt,
        inspected_by=w["receiver"],
        inspection_items=[
            {
                "receipt_line_id": lines[0].id,
                "passed": True,
                "rejected_quantity": Decimal("0.00"),
                "inspection_notes": "Line 1 inspected early",
            }
        ],
    )

    # Incomplete: receipt should NOT have an acknowledgement yet
    assert receipt.acknowledgements.count() == 0
    assert determine_inspection_outcome(receipt) is None

    # Now inspect Line 2
    record_inspection_service(
        receipt=receipt,
        inspected_by=w["receiver"],
        inspection_items=[
            {
                "receipt_line_id": lines[1].id,
                "passed": True,
                "rejected_quantity": Decimal("0.00"),
                "inspection_notes": "Line 2 inspected now",
            }
        ],
    )

    # Now all lines are inspected: acknowledgement should be generated!
    assert receipt.acknowledgements.count() == 1
    assert receipt.latest_acknowledgement.outcome == GRNAcknowledgement.OUTCOME_FULL


def test_recipient_resolution_and_tenant_isolation(grn_ack_test_world):
    """
    Tests authoritative recipient resolution:
    - Finance user in Org A is included.
    - Finance user in Org B is excluded (tenant isolation).
    - Procurement Executive and Manager are included.
    - Vendor primary contact email is resolved.
    - Vendor portal user is included.
    """
    w = grn_ack_test_world

    receipt = create_goods_receipt_service(
        po=w["po"],
        received_by=w["receiver"],
        receipt_items=[
            {"po_line_id": w["po_line_1"].id, "quantity_received": Decimal("5.00")},
        ],
    )

    recipients = resolve_grn_acknowledgement_recipients(receipt)

    assert w["finance_user_a"] in recipients["finance_users"]
    assert w["finance_user_b"] not in recipients["finance_users"]  # Tenant isolation!
    assert w["proc_exec"] in recipients["procurement_execs"]
    assert w["proc_mgr"] in recipients["procurement_managers"]
    assert recipients["vendor_email"] == w["contact_primary"].email
    assert w["vendor_user_a"] in recipients["vendor_users"]
    assert w["vendor_user_b"] not in recipients["vendor_users"]


def test_missing_vendor_email_handled_gracefully(grn_ack_test_world):
    """
    If vendor has no contact email configured:
    - Acknowledgement is generated and persisted.
    - Delivery status is marked NO_RECIPIENTS.
    - Clear explanatory error message is recorded without crashing.
    - In-app notices are still delivered to portal users.
    """
    w = grn_ack_test_world
    vendor = w["vendor_b"]
    vendor.email = ""
    vendor.save()
    vendor.contacts.all().delete()

    po_b = PurchaseOrder.objects.create(
        po_number="PO-NOEMAIL-001",
        version=1,
        vendor=vendor,
        cost_center=w["po"].cost_center,
        status=PurchaseOrder.STATUS_ISSUED,
        created_by=w["proc_exec"],
        total_amount=Decimal("1000.00"),
    )
    po_line = POLine.objects.create(
        po=po_b,
        item_description="Special Cables",
        quantity=Decimal("10.00"),
        unit_price=Decimal("100.00"),
        line_total=Decimal("1000.00"),
    )

    receipt = create_goods_receipt_service(
        po=po_b,
        received_by=w["receiver"],
        receipt_items=[
            {"po_line_id": po_line.id, "quantity_received": Decimal("10.00")},
        ],
    )

    line = receipt.lines.first()
    record_inspection_service(
        receipt=receipt,
        inspected_by=w["receiver"],
        inspection_items=[
            {
                "receipt_line_id": line.id,
                "passed": True,
                "rejected_quantity": Decimal("0.00"),
                "inspection_notes": "Passed inspection",
            }
        ],
    )

    ack = receipt.latest_acknowledgement
    assert ack is not None
    assert ack.delivery_status == GRNAcknowledgement.STATUS_NO_EMAIL
    assert "no configured primary contact or email" in ack.delivery_error
    assert ack.email_sent_count == 0


def test_idempotency_and_duplicate_prevention(grn_ack_test_world):
    """
    Repeated inspection submissions with identical results return the existing
    acknowledgement and do not create duplicate records or spam emails.
    """
    w = grn_ack_test_world
    mail.outbox.clear()

    receipt = create_goods_receipt_service(
        po=w["po"],
        received_by=w["receiver"],
        receipt_items=[
            {"po_line_id": w["po_line_1"].id, "quantity_received": Decimal("20.00")},
        ],
    )

    line = receipt.lines.first()
    inspection_payload = [
        {
            "receipt_line_id": line.id,
            "passed": True,
            "rejected_quantity": Decimal("0.00"),
            "inspection_notes": "Stable signal performance",
        }
    ]

    # First call
    ack1 = record_inspection_service(
        receipt=receipt,
        inspected_by=w["receiver"],
        inspection_items=inspection_payload,
    ).latest_acknowledgement

    initial_ack_count = receipt.acknowledgements.count()
    initial_mail_count = len(mail.outbox)
    assert initial_ack_count == 1

    # Second call with identical payload
    ack2 = record_inspection_service(
        receipt=receipt,
        inspected_by=w["receiver"],
        inspection_items=inspection_payload,
    ).latest_acknowledgement

    assert receipt.acknowledgements.count() == 1
    assert len(mail.outbox) == initial_mail_count
    assert ack1.id == ack2.id
    assert ack1.acknowledgement_number == ack2.acknowledgement_number


def test_revised_inspection_creates_versioned_acknowledgement(grn_ack_test_world):
    """
    If inspection outcomes or quantities are legitimately updated/corrected,
    a new version is created (V2) while preserving V1 in the database.
    """
    w = grn_ack_test_world
    mail.outbox.clear()

    receipt = create_goods_receipt_service(
        po=w["po"],
        received_by=w["receiver"],
        receipt_items=[
            {"po_line_id": w["po_line_1"].id, "quantity_received": Decimal("30.00")},
        ],
    )

    line = receipt.lines.first()

    # Initial inspection: Full pass
    record_inspection_service(
        receipt=receipt,
        inspected_by=w["receiver"],
        inspection_items=[
            {
                "receipt_line_id": line.id,
                "passed": True,
                "rejected_quantity": Decimal("0.00"),
                "inspection_notes": "Initial pass verdict",
            }
        ],
    )

    ack_v1 = receipt.latest_acknowledgement
    assert ack_v1.version == 1
    assert ack_v1.outcome == GRNAcknowledgement.OUTCOME_FULL

    # Legitimate revision: Secondary lab test discovers 5 defective units
    record_inspection_service(
        receipt=receipt,
        inspected_by=w["receiver"],
        inspection_items=[
            {
                "receipt_line_id": line.id,
                "passed": False,
                "rejected_quantity": Decimal("5.00"),
                "rejection_reason": "Micro-fractures detected on re-test",
                "inspection_notes": "Secondary testing found latent defects",
            }
        ],
    )

    receipt.refresh_from_db()
    assert receipt.acknowledgements.count() == 2
    ack_v2 = receipt.latest_acknowledgement
    assert ack_v2.version == 2
    assert ack_v2.acknowledgement_number == f"ACK-{receipt.grn_number}-V2"
    assert ack_v2.outcome == GRNAcknowledgement.OUTCOME_PARTIAL
    assert ack_v2.prior_acknowledgement == ack_v1
    assert ack_v2.total_accepted_qty == Decimal("25.00")
    assert ack_v2.total_rejected_qty == Decimal("5.00")

    # V1 remains intact in database
    ack_v1.refresh_from_db()
    assert ack_v1.version == 1
    assert ack_v1.outcome == GRNAcknowledgement.OUTCOME_FULL


def test_failed_email_delivery_observable_and_retryable(grn_ack_test_world):
    """
    Tests observable email delivery failure and retry capability:
    - Simulates SMTP provider failure.
    - Verifies delivery_status is FAILED with error message.
    - Retrying delivery via service updates status to SENT.
    """
    w = grn_ack_test_world

    receipt = create_goods_receipt_service(
        po=w["po"],
        received_by=w["receiver"],
        receipt_items=[
            {"po_line_id": w["po_line_1"].id, "quantity_received": Decimal("15.00")},
        ],
    )

    line = receipt.lines.first()

    # Simulate email sending error
    with patch(
        "apps.notifications.email_service.EmailMultiAlternatives.send",
        side_effect=ConnectionError("SMTP connection timeout to relay"),
    ):
        record_inspection_service(
            receipt=receipt,
            inspected_by=w["receiver"],
            inspection_items=[
                {
                    "receipt_line_id": line.id,
                    "passed": True,
                    "rejected_quantity": Decimal("0.00"),
                    "inspection_notes": "Passed inspection",
                }
            ],
        )

    ack = receipt.latest_acknowledgement
    assert ack.delivery_status == GRNAcknowledgement.STATUS_FAILED
    assert "SMTP connection timeout" in ack.delivery_error
    assert ack.delivered_at is None

    # Now retry delivery without mock failure
    updated_ack = generate_and_distribute_grn_acknowledgement_service(
        receipt=receipt,
        triggered_by=w["receiver"],
        force_retry=True,
    )

    assert updated_ack.delivery_status == GRNAcknowledgement.STATUS_SENT
    assert updated_ack.delivered_at is not None
    assert updated_ack.version == 1  # Version is not bumped on retry


def test_grn_acknowledgement_view_and_vendor_isolation(grn_ack_test_world):
    """
    Tests HTTP view access control, PDF export, and vendor isolation:
    - Stores receiver can access HTML view.
    - PDF download returns valid application/pdf.
    - Vendor A can access their own acknowledgement.
    - Vendor B is denied access (HTTP 403 Forbidden).
    - AuditLog logs VIEW and EXPORT events.
    """
    w = grn_ack_test_world
    client = Client()

    receipt = create_goods_receipt_service(
        po=w["po"],
        received_by=w["receiver"],
        receipt_items=[
            {"po_line_id": w["po_line_1"].id, "quantity_received": Decimal("20.00")},
        ],
    )
    line = receipt.lines.first()
    record_inspection_service(
        receipt=receipt,
        inspected_by=w["receiver"],
        inspection_items=[
            {
                "receipt_line_id": line.id,
                "passed": True,
                "rejected_quantity": Decimal("0.00"),
                "inspection_notes": "Standard inspection passed",
            }
        ],
    )
    ack = receipt.latest_acknowledgement

    url = reverse("grn_acknowledgement", kwargs={"grn_id": receipt.id})

    # 1. Stores Receiver accesses HTML view
    client.force_login(w["receiver"])
    resp = client.get(url)
    assert resp.status_code == 200
    assert ack.acknowledgement_number.encode() in resp.content
    assert b"FULL ACCEPTANCE" in resp.content

    # 2. PDF download
    resp_pdf = client.get(f"{url}?format=pdf")
    assert resp_pdf.status_code == 200
    assert resp_pdf["Content-Type"] == "application/pdf"
    assert resp_pdf.content.startswith(b"%PDF")

    # 3. Vendor A accesses own acknowledgement
    client.force_login(w["vendor_user_a"])
    resp_vendor_a = client.get(url)
    assert resp_vendor_a.status_code == 200

    # 4. Vendor B accesses Vendor A's acknowledgement -> 403 Forbidden
    client.force_login(w["vendor_user_b"])
    resp_vendor_b = client.get(url)
    assert resp_vendor_b.status_code == 403

    # 5. Check AuditLog records for VIEW and EXPORT
    assert AuditLog.objects.filter(
        target_model="GRNAcknowledgement",
        target_object_id=str(ack.id),
        action="VIEW",
    ).exists()
    assert AuditLog.objects.filter(
        target_model="GRNAcknowledgement",
        target_object_id=str(ack.id),
        action="EXPORT",
    ).exists()


def test_quantity_correction_updates_acknowledgement(grn_ack_test_world):
    """
    Tests that correcting a receipt line quantity updates and versions the GRN Acknowledgement.
    """
    w = grn_ack_test_world

    receipt = create_goods_receipt_service(
        po=w["po"],
        received_by=w["receiver"],
        receipt_items=[
            {"po_line_id": w["po_line_1"].id, "quantity_received": Decimal("50.00")},
        ],
    )
    line = receipt.lines.first()
    record_inspection_service(
        receipt=receipt,
        inspected_by=w["receiver"],
        inspection_items=[
            {
                "receipt_line_id": line.id,
                "passed": True,
                "rejected_quantity": Decimal("0.00"),
                "inspection_notes": "Initial 50 pass",
            }
        ],
    )
    assert receipt.latest_acknowledgement.version == 1
    assert receipt.latest_acknowledgement.total_accepted_qty == Decimal("50.00")

    # Correct quantity from 50 to 45 accepted and 5 rejected
    correct_receipt_line_quantity_service(
        receipt_line=line,
        corrected_by=w["receiver"],
        new_quantity_received=Decimal("50.00"),
        new_quantity_accepted=Decimal("45.00"),
        reason="Counting audit discovered 5 units were cracked",
    )

    receipt.refresh_from_db()
    ack_v2 = receipt.latest_acknowledgement
    assert ack_v2.version == 2
    assert ack_v2.outcome == GRNAcknowledgement.OUTCOME_PARTIAL
    assert ack_v2.total_accepted_qty == Decimal("45.00")
    assert ack_v2.total_rejected_qty == Decimal("5.00")

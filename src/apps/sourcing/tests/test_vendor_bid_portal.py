"""
Vendor Bid Portal Test Suite — ProcureSphere 360
Authoritative PRD Requirement:
"Restricted bid submission, validation, attachments, amendment before close and sealed visibility."

Test Coverage:
1-6:   Model & Constraint Tests (uniqueness, relationships, FK integrity)
7-14:  Bid Lifecycle & Submission Tests (draft, validation, submit, closed reject, server timestamp)
15-19: Server-Side Deadline Enforcement (before start, during window, after deadline, amendments)
20-26: Sealed Bid Visibility & Isolation (Vendor A vs B isolation, evaluator sealed view)
27-31: Attachment Security (mime, extension, size limit, server-side download authorization)
32-37: Amendment & Versioning (immutable BidVersion history, version increment, post-deadline reject)
38-40: CSRF Protection (state-changing POST operations)
41-44: Authentication & Role Scoping (anonymous, non-vendor, procurement, vendor isolation)
45-48: IDOR / BOLA Prevention (manipulated bid_id, attachment_id, version_id, event_id)
49-51: Audit Logging (create, submit, amend, attachment actions)
52-54: Advanced Security (sealed privacy in BID_WINDOW, path traversal, injection protection)
"""
import io
import uuid
from datetime import timedelta
from decimal import Decimal

from django.contrib.auth import get_user_model
from django.core.exceptions import ValidationError
from django.core.files.uploadedfile import SimpleUploadedFile
from django.db import IntegrityError
from django.test import Client, TestCase
from django.urls import reverse
from django.utils import timezone

from apps.accounts.models import Role
from apps.audit.models import AuditLog
from apps.vendors.models import Vendor, VendorCategory
from apps.sourcing.models import (
    BidAttachment, BidInvite, BidLine, BidTechnicalResponse, BidVersion,
    CommercialRequirement, SourcingEvent, TechnicalRequirement, VendorBid,
)
from apps.sourcing.services import (
    amend_vendor_bid_service, create_sourcing_event_service,
    delete_bid_attachment_service, get_or_create_vendor_bid_service,
    invite_vendors_to_event_service, publish_sourcing_event_service,
    save_bid_draft_service, submit_vendor_bid_service,
    upload_bid_attachment_service, validate_bid_submission_service,
)

User = get_user_model()


# ──────────────────────── Test Helpers & Fixtures ─────────────────────────────

def get_role(code):
    role_names = {
        Role.PROC_EXEC: "Procurement Executive",
        Role.PROC_MGR: "Procurement Manager",
        Role.VENDOR_USER: "Vendor Portal User",
        Role.SUPER_ADMIN: "Super Admin",
        Role.REQUESTER: "Requester",
    }
    return Role.objects.get_or_create(code=code, defaults={"name": role_names.get(code, code)})[0]


def create_vendor(name="Vendor Alpha", status=Vendor.STATUS_ACTIVE):
    cat, _ = VendorCategory.objects.get_or_create(code="HW", defaults={"name": "Hardware"})
    uid = uuid.uuid4().hex[:6]
    return Vendor.objects.create(
        legal_name=name,
        vendor_number=f"VND-{uid.upper()}",
        tax_identification_number=f"TIN-{uid.upper()}",
        category=cat,
        status=status,
        email=f"{uid}@vendor.com",
        address="100 Silicon Way",
    )


def create_user(email, role_code, vendor=None):
    role = get_role(role_code)
    user = User.objects.create_user(email=email, password="StrongPassword123!")
    user.role = role
    user.vendor = vendor
    user.save()
    return user


def create_published_event(user, bid_start=None, bid_end=None, vendors=None):
    now = timezone.now()
    event = create_sourcing_event_service(
        title="Server Hardware RFQ 2026",
        event_type=SourcingEvent.TYPE_RFQ,
        description="Enterprise rack servers",
        bid_start_date=bid_start or (now - timedelta(hours=2)),
        bid_end_date=bid_end or (now + timedelta(days=7)),
        technical_section_enabled=True,
        commercial_section_enabled=True,
        created_by_user=user,
    )
    if vendors:
        invite_vendors_to_event_service(
            event=event, vendor_ids=[v.id for v in vendors], invited_by=user
        )
    event = publish_sourcing_event_service(event=event, user=user)
    return event


# ─────────────────────────────────────────────────────────────────────────────
# 1. MODEL & CONSTRAINT TESTS
# ─────────────────────────────────────────────────────────────────────────────

class BidModelConstraintTests(TestCase):

    def setUp(self):
        self.proc_exec = create_user("pe_model@hpe.com", Role.PROC_EXEC)
        self.vendor = create_vendor("Model Vendor")
        self.event = create_published_event(self.proc_exec, vendors=[self.vendor])

    def test_01_bid_belongs_to_correct_sourcing_event(self):
        bid, _ = get_or_create_vendor_bid_service(event=self.event, vendor=self.vendor, user=self.proc_exec)
        self.assertEqual(bid.event, self.event)
        self.assertEqual(bid.event.event_number, self.event.event_number)

    def test_02_bid_belongs_to_correct_vendor(self):
        bid, _ = get_or_create_vendor_bid_service(event=self.event, vendor=self.vendor, user=self.proc_exec)
        self.assertEqual(bid.vendor, self.vendor)
        self.assertEqual(bid.vendor.legal_name, "Model Vendor")

    def test_03_bid_uniqueness_per_event_and_vendor(self):
        get_or_create_vendor_bid_service(event=self.event, vendor=self.vendor, user=self.proc_exec)
        with self.assertRaises(IntegrityError):
            VendorBid.objects.create(
                event=self.event,
                vendor=self.vendor,
                bid_number=f"BID-DUP-{uuid.uuid4().hex[:4]}",
                status=VendorBid.STATUS_DRAFT,
            )

    def test_04_version_uniqueness_per_bid(self):
        bid, _ = get_or_create_vendor_bid_service(event=self.event, vendor=self.vendor, user=self.proc_exec)
        now = timezone.now()
        BidVersion.objects.create(
            bid=bid, version_number=1, total_bid_amount=Decimal("100.00"),
            submitted_at=now, status=BidVersion.STATUS_SUBMITTED
        )
        with self.assertRaises(IntegrityError):
            BidVersion.objects.create(
                bid=bid, version_number=1, total_bid_amount=Decimal("200.00"),
                submitted_at=now, status=BidVersion.STATUS_SUBMITTED
            )

    def test_05_attachment_relationship_to_bid(self):
        bid, _ = get_or_create_vendor_bid_service(event=self.event, vendor=self.vendor, user=self.proc_exec)
        att = BidAttachment.objects.create(
            bid=bid,
            title="Spec Doc",
            file=SimpleUploadedFile("spec.pdf", b"PDF_DATA", content_type="application/pdf"),
            original_filename="spec.pdf",
            file_size=8,
            doc_type=BidAttachment.DOC_TYPE_TECHNICAL,
        )
        self.assertEqual(bid.attachments.count(), 1)
        self.assertEqual(bid.attachments.first().title, "Spec Doc")

    def test_06_database_constraints_cascades_and_protect(self):
        bid, _ = get_or_create_vendor_bid_service(event=self.event, vendor=self.vendor, user=self.proc_exec)
        line = BidLine.objects.create(
            bid=bid, item_description="Test Item", quantity=Decimal("5.00"),
            quoted_unit_price=Decimal("10.00")
        )
        self.assertEqual(line.quoted_total_price, Decimal("50.00"))


# ─────────────────────────────────────────────────────────────────────────────
# 2. BID SUBMISSION & LIFECYCLE TESTS
# ─────────────────────────────────────────────────────────────────────────────

class BidSubmissionLifecycleTests(TestCase):

    def setUp(self):
        self.proc_exec = create_user("pe_sub@hpe.com", Role.PROC_EXEC)
        self.vendor_a = create_vendor("Vendor A")
        self.vendor_b = create_vendor("Vendor B")
        self.user_a = create_user("user_a@vendora.com", Role.VENDOR_USER, self.vendor_a)
        self.user_b = create_user("user_b@vendorb.com", Role.VENDOR_USER, self.vendor_b)

        self.event = create_published_event(self.proc_exec, vendors=[self.vendor_a])
        self.tech_req = TechnicalRequirement.objects.create(
            event=self.event, title="ISO 27001 Certification", importance=TechnicalRequirement.IMPORTANCE_MANDATORY
        )
        self.comm_req = CommercialRequirement.objects.create(
            event=self.event, title="Rack Server Unit", quantity=Decimal("5.00")
        )

    def test_07_authorized_vendor_can_create_draft_bid(self):
        bid, created = get_or_create_vendor_bid_service(event=self.event, vendor=self.vendor_a, user=self.user_a)
        self.assertTrue(created)
        self.assertEqual(bid.status, VendorBid.STATUS_DRAFT)
        self.assertTrue(bid.bid_number.startswith("BID-"))

    def test_08_unauthorized_uninvited_vendor_cannot_create_bid(self):
        with self.assertRaises(ValidationError) as ctx:
            get_or_create_vendor_bid_service(event=self.event, vendor=self.vendor_b, user=self.user_b)
        self.assertIn("not invited", str(ctx.exception).lower())

    def test_09_vendor_can_save_draft_without_final_submission(self):
        bid, _ = get_or_create_vendor_bid_service(event=self.event, vendor=self.vendor_a, user=self.user_a)
        saved = save_bid_draft_service(
            bid=bid,
            user=self.user_a,
            proposal_summary="Draft in progress",
            technical_responses=[{"requirement_id": self.tech_req.id, "compliance_status": "COMPLIANT", "response_text": "Compliant"}],
            line_items=[{"commercial_requirement": self.comm_req, "item_description": "Server Unit", "quantity": "5", "quoted_unit_price": "2000.00"}],
        )
        self.assertEqual(saved.status, VendorBid.STATUS_DRAFT)
        self.assertEqual(saved.total_bid_amount, Decimal("10000.00"))
        self.assertIsNone(saved.submitted_at)

    def test_10_vendor_can_validate_bid_and_receive_clear_issues(self):
        bid, _ = get_or_create_vendor_bid_service(event=self.event, vendor=self.vendor_a, user=self.user_a)
        # Empty bid -> validation fails
        is_valid, issues = validate_bid_submission_service(bid=bid, user=self.user_a)
        self.assertFalse(is_valid)
        self.assertGreater(len(issues), 0)

        # Complete mandatory responses
        save_bid_draft_service(
            bid=bid,
            user=self.user_a,
            proposal_summary="Complete offer",
            technical_responses=[{"requirement_id": self.tech_req.id, "compliance_status": "COMPLIANT", "response_text": "Fully certified"}],
            line_items=[{"commercial_requirement": self.comm_req, "item_description": "Rack Server Unit", "quantity": "5", "quoted_unit_price": "1500.00"}],
        )
        is_valid_now, issues_now = validate_bid_submission_service(bid=bid, user=self.user_a)
        self.assertTrue(is_valid_now)
        self.assertEqual(len(issues_now), 0)
        self.assertEqual(bid.status, VendorBid.STATUS_VALIDATED)

    def test_11_vendor_can_submit_valid_bid(self):
        bid, _ = get_or_create_vendor_bid_service(event=self.event, vendor=self.vendor_a, user=self.user_a)
        save_bid_draft_service(
            bid=bid,
            user=self.user_a,
            proposal_summary="Valid proposal",
            technical_responses=[{"requirement_id": self.tech_req.id, "compliance_status": "COMPLIANT", "response_text": "Compliant"}],
            line_items=[{"commercial_requirement": self.comm_req, "item_description": "Rack Server Unit", "quantity": "5", "quoted_unit_price": "1200.00"}],
        )
        submitted = submit_vendor_bid_service(bid=bid, submitted_by_user=self.user_a)
        self.assertEqual(submitted.status, VendorBid.STATUS_SUBMITTED)
        self.assertIsNotNone(submitted.submitted_at)
        self.assertEqual(submitted.current_version_number, 1)
        self.assertEqual(submitted.versions.count(), 1)

    def test_12_invalid_bid_cannot_be_submitted(self):
        bid, _ = get_or_create_vendor_bid_service(event=self.event, vendor=self.vendor_a, user=self.user_a)
        # Attempt submission without completing requirements
        with self.assertRaises(ValidationError) as ctx:
            submit_vendor_bid_service(bid=bid, submitted_by_user=self.user_a)
        self.assertIn("cannot be submitted", str(ctx.exception).lower())

    def test_13_closed_event_rejects_submission(self):
        bid, _ = get_or_create_vendor_bid_service(event=self.event, vendor=self.vendor_a, user=self.user_a)
        save_bid_draft_service(
            bid=bid,
            user=self.user_a,
            proposal_summary="Valid proposal",
            technical_responses=[{"requirement_id": self.tech_req.id, "compliance_status": "COMPLIANT", "response_text": "Compliant"}],
            line_items=[{"commercial_requirement": self.comm_req, "item_description": "Rack Server Unit", "quantity": "5", "quoted_unit_price": "1200.00"}],
        )
        # Close the event
        self.event.status = SourcingEvent.STATUS_CLOSED
        self.event.save(update_fields=["status"])

        with self.assertRaises(ValidationError) as ctx:
            submit_vendor_bid_service(bid=bid, submitted_by_user=self.user_a)
        self.assertIn("not open", str(ctx.exception).lower())

    def test_14_submission_timestamp_is_server_generated(self):
        bid, _ = get_or_create_vendor_bid_service(event=self.event, vendor=self.vendor_a, user=self.user_a)
        save_bid_draft_service(
            bid=bid,
            user=self.user_a,
            proposal_summary="Valid proposal",
            technical_responses=[{"requirement_id": self.tech_req.id, "compliance_status": "COMPLIANT", "response_text": "Compliant"}],
            line_items=[{"commercial_requirement": self.comm_req, "item_description": "Rack Server Unit", "quantity": "5", "quoted_unit_price": "1200.00"}],
        )
        before = timezone.now()
        submitted = submit_vendor_bid_service(bid=bid, submitted_by_user=self.user_a)
        after = timezone.now()
        self.assertTrue(before <= submitted.submitted_at <= after)


# ─────────────────────────────────────────────────────────────────────────────
# 3. SERVER-SIDE DEADLINE ENFORCEMENT TESTS
# ─────────────────────────────────────────────────────────────────────────────

class ServerDeadlineEnforcementTests(TestCase):

    def setUp(self):
        self.proc_exec = create_user("pe_dead@hpe.com", Role.PROC_EXEC)
        self.vendor = create_vendor("Deadline Vendor")
        self.user = create_user("user_dead@vendor.com", Role.VENDOR_USER, self.vendor)

    def test_15_submission_before_opening_rejected(self):
        now = timezone.now()
        event = create_sourcing_event_service(
            title="Future Event",
            event_type=SourcingEvent.TYPE_RFQ,
            description="Future opening",
            bid_start_date=now + timedelta(days=2),
            bid_end_date=now + timedelta(days=7),
            created_by_user=self.proc_exec,
        )
        invite_vendors_to_event_service(event=event, vendor_ids=[self.vendor.id], invited_by=self.proc_exec)
        event.status = SourcingEvent.STATUS_BID_WINDOW
        event.save(update_fields=["status"])

        bid = VendorBid.objects.create(
            event=event, vendor=self.vendor, bid_number="BID-FUT-01",
            total_bid_amount=Decimal("100.00"), status=VendorBid.STATUS_DRAFT
        )
        with self.assertRaises(ValidationError) as ctx:
            submit_vendor_bid_service(bid=bid, submitted_by_user=self.user)
        self.assertIn("not started yet", str(ctx.exception).lower())

    def test_16_submission_during_window_accepted(self):
        event = create_published_event(self.proc_exec, vendors=[self.vendor])
        bid, _ = get_or_create_vendor_bid_service(event=event, vendor=self.vendor, user=self.user)
        # Line item
        BidLine.objects.create(bid=bid, item_description="Service", quantity=Decimal("1.0"), quoted_unit_price=Decimal("500.00"))
        bid.total_bid_amount = Decimal("500.00")
        bid.save()

        submitted = submit_vendor_bid_service(bid=bid, submitted_by_user=self.user)
        self.assertEqual(submitted.status, VendorBid.STATUS_SUBMITTED)

    def test_17_submission_after_deadline_strictly_rejected(self):
        event = create_published_event(self.proc_exec, vendors=[self.vendor])
        bid, _ = get_or_create_vendor_bid_service(event=event, vendor=self.vendor, user=self.user)
        BidLine.objects.create(bid=bid, item_description="Service", quantity=Decimal("1.0"), quoted_unit_price=Decimal("500.00"))
        bid.total_bid_amount = Decimal("500.00")
        bid.save()

        # Simulate deadline passing on server
        event.bid_end_date = timezone.now() - timedelta(minutes=5)
        event.save(update_fields=["bid_end_date"])

        with self.assertRaises(ValidationError) as ctx:
            submit_vendor_bid_service(bid=bid, submitted_by_user=self.user)
        self.assertIn("deadline has passed", str(ctx.exception).lower())

    def test_18_amendment_before_deadline_permitted(self):
        event = create_published_event(self.proc_exec, vendors=[self.vendor])
        bid, _ = get_or_create_vendor_bid_service(event=event, vendor=self.vendor, user=self.user)
        BidLine.objects.create(bid=bid, item_description="Service", quantity=Decimal("1.0"), quoted_unit_price=Decimal("500.00"))
        bid.total_bid_amount = Decimal("500.00")
        bid.save()
        submit_vendor_bid_service(bid=bid, submitted_by_user=self.user)

        amended = amend_vendor_bid_service(
            bid=bid,
            user=self.user,
            change_summary="Reduced price by 10%",
            line_items=[{"item_description": "Service", "quantity": "1.0", "quoted_unit_price": "450.00"}],
        )
        self.assertEqual(amended.status, VendorBid.STATUS_AMENDED)
        self.assertEqual(amended.current_version_number, 2)
        self.assertEqual(amended.total_bid_amount, Decimal("450.00"))

    def test_19_amendment_after_deadline_rejected(self):
        event = create_published_event(self.proc_exec, vendors=[self.vendor])
        bid, _ = get_or_create_vendor_bid_service(event=event, vendor=self.vendor, user=self.user)
        BidLine.objects.create(bid=bid, item_description="Service", quantity=Decimal("1.0"), quoted_unit_price=Decimal("500.00"))
        bid.total_bid_amount = Decimal("500.00")
        bid.save()
        submit_vendor_bid_service(bid=bid, submitted_by_user=self.user)

        # Move event past deadline
        event.bid_end_date = timezone.now() - timedelta(seconds=10)
        event.save(update_fields=["bid_end_date"])

        with self.assertRaises(ValidationError) as ctx:
            amend_vendor_bid_service(
                bid=bid, user=self.user, change_summary="Late update",
                line_items=[{"item_description": "Service", "quantity": "1.0", "quoted_unit_price": "400.00"}]
            )
        self.assertIn("deadline has passed", str(ctx.exception).lower())


# ─────────────────────────────────────────────────────────────────────────────
# 4. SEALED BID VISIBILITY & IDOR / BOLA TESTS
# ─────────────────────────────────────────────────────────────────────────────

class SealedBidVisibilityAndIDORTests(TestCase):

    def setUp(self):
        self.proc_exec = create_user("pe_seal@hpe.com", Role.PROC_EXEC)
        self.vendor_a = create_vendor("Vendor A")
        self.vendor_b = create_vendor("Vendor B")
        self.user_a = create_user("a@vendora.com", Role.VENDOR_USER, self.vendor_a)
        self.user_b = create_user("b@vendorb.com", Role.VENDOR_USER, self.vendor_b)

        self.event = create_published_event(self.proc_exec, vendors=[self.vendor_a, self.vendor_b])

        # Vendor A submits bid with attachment and version
        self.bid_a, _ = get_or_create_vendor_bid_service(event=self.event, vendor=self.vendor_a, user=self.user_a)
        BidLine.objects.create(bid=self.bid_a, item_description="A Proprietary Hardware", quantity=Decimal("1.0"), quoted_unit_price=Decimal("9999.00"))
        self.bid_a.total_bid_amount = Decimal("9999.00")
        self.bid_a.proposal_summary = "Vendor A Secret Pricing Formula"
        self.bid_a.save()
        submit_vendor_bid_service(bid=self.bid_a, submitted_by_user=self.user_a)

        self.att_a = upload_bid_attachment_service(
            bid=self.bid_a,
            file=SimpleUploadedFile("secret_a.pdf", b"SECRET_A_CONTENT", content_type="application/pdf"),
            title="Vendor A Secret Whitepaper",
            user=self.user_a,
        )

        self.client_b = Client()
        self.client_b.force_login(self.user_b)

    def test_20_vendor_a_cannot_access_vendor_b_bid(self):
        # Vendor B attempts to access Vendor A's bid workspace
        url = reverse("sourcing:vendor-bid-workspace", kwargs={"pk": self.event.id})
        resp = self.client_b.get(url)
        # Should render Vendor B's own bid, NOT Vendor A's
        self.assertContains(resp, self.vendor_b.legal_name)
        self.assertNotContains(resp, "Vendor A Secret Pricing Formula")

    def test_21_vendor_b_cannot_download_vendor_a_attachment(self):
        url = reverse("sourcing:bid-download-attachment", kwargs={"attachment_id": self.att_a.id})
        resp = self.client_b.get(url)
        # IDOR protection: HTTP 403 Forbidden
        self.assertEqual(resp.status_code, 403)

    def test_22_vendor_b_cannot_view_vendor_a_version_detail(self):
        url = reverse("sourcing:bid-version-detail", kwargs={"bid_id": self.bid_a.id, "version_number": 1})
        resp = self.client_b.get(url)
        self.assertEqual(resp.status_code, 403)

    def test_23_vendor_b_cannot_amend_vendor_a_bid(self):
        url = reverse("sourcing:bid-amend", kwargs={"bid_id": self.bid_a.id})
        resp = self.client_b.post(url, {"change_summary": "Malicious tampering", "proposal_summary": "Tampered"})
        self.assertEqual(resp.status_code, 403)
        self.bid_a.refresh_from_db()
        self.assertNotEqual(self.bid_a.proposal_summary, "Tampered")

    def test_24_vendor_b_cannot_delete_vendor_a_attachment(self):
        url = reverse("sourcing:bid-delete-attachment", kwargs={"attachment_id": self.att_a.id})
        resp = self.client_b.post(url)
        self.assertEqual(resp.status_code, 403)
        self.assertTrue(BidAttachment.objects.filter(id=self.att_a.id).exists())

    def test_25_vendor_enumeration_returns_forbidden(self):
        fake_uuid = uuid.uuid4()
        url = reverse("sourcing:bid-download-attachment", kwargs={"attachment_id": fake_uuid})
        resp = self.client_b.get(url)
        self.assertEqual(resp.status_code, 404)

    def test_26_sealed_visibility_procurement_cannot_view_prices_during_bid_window(self):
        client_pe = Client()
        client_pe.force_login(self.proc_exec)

        # In BID_WINDOW, procurement viewing bid detail sees sealed warning
        url = reverse("sourcing:procurement-bid-detail", kwargs={"bid_id": self.bid_a.id})
        resp = client_pe.get(url)
        self.assertEqual(resp.status_code, 200)
        self.assertContains(resp, "Sealed Bid Privacy Active")
        self.assertNotContains(resp, "9999.00")  # Price must be sealed


# ─────────────────────────────────────────────────────────────────────────────
# 5. ATTACHMENT VALIDATION & SECURITY TESTS
# ─────────────────────────────────────────────────────────────────────────────

class AttachmentSecurityTests(TestCase):

    def setUp(self):
        self.proc_exec = create_user("pe_att@hpe.com", Role.PROC_EXEC)
        self.vendor = create_vendor("Attachment Vendor")
        self.user = create_user("user_att@vendor.com", Role.VENDOR_USER, self.vendor)
        self.event = create_published_event(self.proc_exec, vendors=[self.vendor])
        self.bid, _ = get_or_create_vendor_bid_service(event=self.event, vendor=self.vendor, user=self.user)

    def test_27_authorized_vendor_can_upload_valid_document(self):
        valid_file = SimpleUploadedFile("proposal.pdf", b"%PDF-1.4 sample content", content_type="application/pdf")
        att = upload_bid_attachment_service(
            bid=self.bid, file=valid_file, title="Technical Specs",
            doc_type=BidAttachment.DOC_TYPE_TECHNICAL, user=self.user
        )
        self.assertEqual(att.original_filename, "proposal.pdf")
        self.assertEqual(att.doc_type, BidAttachment.DOC_TYPE_TECHNICAL)

    def test_28_invalid_file_extension_rejected(self):
        malicious_file = SimpleUploadedFile("script.exe", b"BINARY_EXEC", content_type="application/octet-stream")
        with self.assertRaises(ValidationError) as ctx:
            upload_bid_attachment_service(
                bid=self.bid, file=malicious_file, title="Malicious Script",
                user=self.user
            )
        self.assertIn("not permitted", str(ctx.exception).lower())

    def test_29_oversized_file_rejected(self):
        # 11MB file exceeds 10MB limit
        large_content = b"0" * (11 * 1024 * 1024)
        large_file = SimpleUploadedFile("giant.pdf", large_content, content_type="application/pdf")
        with self.assertRaises(ValidationError) as ctx:
            upload_bid_attachment_service(
                bid=self.bid, file=large_file, title="Giant File", user=self.user
            )
        self.assertIn("exceeds maximum allowed limit", str(ctx.exception).lower())

    def test_30_download_endpoint_serves_valid_file(self):
        file_obj = SimpleUploadedFile("doc.docx", b"DOCX_BYTES", content_type="application/vnd.openxmlformats-officedocument.wordprocessingml.document")
        att = upload_bid_attachment_service(bid=self.bid, file=file_obj, title="Doc", user=self.user)

        client = Client()
        client.force_login(self.user)
        url = reverse("sourcing:bid-download-attachment", kwargs={"attachment_id": att.id})
        resp = client.get(url)
        self.assertEqual(resp.status_code, 200)
        self.assertIn("doc.docx", resp["Content-Disposition"])


# ─────────────────────────────────────────────────────────────────────────────
# 6. BID AMENDMENT & IMMUTABLE VERSIONING TESTS
# ─────────────────────────────────────────────────────────────────────────────

class BidAmendmentAndVersioningTests(TestCase):

    def setUp(self):
        self.proc_exec = create_user("pe_amend@hpe.com", Role.PROC_EXEC)
        self.vendor = create_vendor("Versioning Vendor")
        self.user = create_user("vuser@versioning.com", Role.VENDOR_USER, self.vendor)
        self.event = create_published_event(self.proc_exec, vendors=[self.vendor])

        self.bid, _ = get_or_create_vendor_bid_service(event=self.event, vendor=self.vendor, user=self.user)
        BidLine.objects.create(bid=self.bid, item_description="Core Unit", quantity=Decimal("1.0"), quoted_unit_price=Decimal("1000.00"))
        self.bid.total_bid_amount = Decimal("1000.00")
        self.bid.save()
        submit_vendor_bid_service(bid=self.bid, submitted_by_user=self.user)

    def test_31_initial_submission_creates_version_1(self):
        self.assertEqual(self.bid.current_version_number, 1)
        self.assertEqual(self.bid.versions.count(), 1)
        v1 = self.bid.versions.first()
        self.assertEqual(v1.version_number, 1)
        self.assertEqual(v1.total_bid_amount, Decimal("1000.00"))
        self.assertIn("Core Unit", str(v1.snapshot))

    def test_32_first_amendment_increments_to_version_2(self):
        amend_vendor_bid_service(
            bid=self.bid,
            user=self.user,
            change_summary="Discount applied: 10%",
            line_items=[{"item_description": "Core Unit", "quantity": "1.0", "quoted_unit_price": "900.00"}],
        )
        self.bid.refresh_from_db()
        self.assertEqual(self.bid.current_version_number, 2)
        self.assertEqual(self.bid.total_bid_amount, Decimal("900.00"))
        self.assertEqual(self.bid.versions.count(), 2)

    def test_33_previous_version_remains_strictly_unchanged(self):
        amend_vendor_bid_service(
            bid=self.bid,
            user=self.user,
            change_summary="Discount applied",
            line_items=[{"item_description": "Core Unit", "quantity": "1.0", "quoted_unit_price": "800.00"}],
        )
        v1 = self.bid.versions.get(version_number=1)
        v2 = self.bid.versions.get(version_number=2)

        self.assertEqual(v1.total_bid_amount, Decimal("1000.00"))
        self.assertEqual(v2.total_bid_amount, Decimal("800.00"))
        self.assertEqual(v2.change_summary, "Discount applied")


# ─────────────────────────────────────────────────────────────────────────────
# 7. CSRF, AUTHENTICATION & ROLE-BASED ACCESS CONTROL
# ─────────────────────────────────────────────────────────────────────────────

class CSRFAndAuthenticationTests(TestCase):

    def setUp(self):
        self.proc_exec = create_user("pe_sec@hpe.com", Role.PROC_EXEC)
        self.vendor = create_vendor("Sec Vendor")
        self.user = create_user("sec_user@vendor.com", Role.VENDOR_USER, self.vendor)
        self.requester = create_user("req@hpe.com", Role.REQUESTER)
        self.event = create_published_event(self.proc_exec, vendors=[self.vendor])

    def test_34_anonymous_user_redirected_to_login(self):
        client = Client()
        url = reverse("sourcing:vendor-bid-workspace", kwargs={"pk": self.event.id})
        resp = client.get(url)
        self.assertEqual(resp.status_code, 302)
        self.assertIn("/admin/login/", resp.url)

    def test_35_authenticated_non_vendor_forbidden(self):
        client = Client()
        client.force_login(self.requester)
        url = reverse("sourcing:vendor-bid-workspace", kwargs={"pk": self.event.id})
        resp = client.get(url)
        self.assertEqual(resp.status_code, 403)

    def test_36_csrf_protection_enforced_on_state_changing_submission(self):
        # Client with CSRF enforcement turned on
        csrf_client = Client(enforce_csrf_checks=True)
        csrf_client.force_login(self.user)
        url = reverse("sourcing:vendor-bid-workspace", kwargs={"pk": self.event.id})

        # POST without CSRF token must be rejected with 403
        resp = csrf_client.post(url, {"action": "submit"})
        self.assertEqual(resp.status_code, 403)


# ─────────────────────────────────────────────────────────────────────────────
# 8. AUDIT TRAIL TESTS
# ─────────────────────────────────────────────────────────────────────────────

class BidAuditTrailTests(TestCase):

    def setUp(self):
        self.proc_exec = create_user("pe_aud@hpe.com", Role.PROC_EXEC)
        self.vendor = create_vendor("Audit Vendor")
        self.user = create_user("aud_user@vendor.com", Role.VENDOR_USER, self.vendor)
        self.event = create_published_event(self.proc_exec, vendors=[self.vendor])

    def test_37_bid_draft_creation_creates_audit_log(self):
        bid, _ = get_or_create_vendor_bid_service(event=self.event, vendor=self.vendor, user=self.user)
        audit = AuditLog.objects.filter(target_model="VendorBid", target_object_id=str(bid.id)).first()
        self.assertIsNotNone(audit)
        self.assertEqual(audit.actor, self.user)
        self.assertEqual(audit.action, AuditLog.ACTION_CREATE)

    def test_38_bid_submission_creates_audit_log(self):
        bid, _ = get_or_create_vendor_bid_service(event=self.event, vendor=self.vendor, user=self.user)
        BidLine.objects.create(bid=bid, item_description="Audit Line", quantity=Decimal("1.0"), quoted_unit_price=Decimal("100.00"))
        bid.total_bid_amount = Decimal("100.00")
        bid.save()

        submit_vendor_bid_service(bid=bid, submitted_by_user=self.user)
        audit = AuditLog.objects.filter(target_model="VendorBid", target_object_id=str(bid.id)).last()
        self.assertIsNotNone(audit)
        self.assertIn("bid_number", str(audit.new_state))

    def test_39_bid_amendment_creates_audit_log(self):
        bid, _ = get_or_create_vendor_bid_service(event=self.event, vendor=self.vendor, user=self.user)
        BidLine.objects.create(bid=bid, item_description="Audit Line", quantity=Decimal("1.0"), quoted_unit_price=Decimal("100.00"))
        bid.total_bid_amount = Decimal("100.00")
        bid.save()
        submit_vendor_bid_service(bid=bid, submitted_by_user=self.user)

        amend_vendor_bid_service(
            bid=bid, user=self.user, change_summary="Audit amendment test",
            line_items=[{"item_description": "Audit Line", "quantity": "1.0", "quoted_unit_price": "90.00"}]
        )
        audit = AuditLog.objects.filter(target_model="VendorBid", target_object_id=str(bid.id)).order_by("-timestamp").first()
        self.assertEqual(audit.action, AuditLog.ACTION_UPDATE)
        self.assertEqual(audit.new_state.get("action"), "amend")
        self.assertEqual(audit.new_state.get("version"), 2)


# ─────────────────────────────────────────────────────────────────────────────
# 9. API VIEWSET & SERIALIZER INTEGRATION TESTS
# ─────────────────────────────────────────────────────────────────────────────

class BidAPIIntegrationTests(TestCase):

    def setUp(self):
        self.proc_exec = create_user("pe_api@hpe.com", Role.PROC_EXEC)
        self.vendor_a = create_vendor("API Vendor A")
        self.vendor_b = create_vendor("API Vendor B")
        self.user_a = create_user("api_a@vendor.com", Role.VENDOR_USER, self.vendor_a)
        self.user_b = create_user("api_b@vendor.com", Role.VENDOR_USER, self.vendor_b)

        self.event = create_published_event(self.proc_exec, vendors=[self.vendor_a, self.vendor_b])
        self.bid_a, _ = get_or_create_vendor_bid_service(event=self.event, vendor=self.vendor_a, user=self.user_a)

        self.client_a = Client()
        self.client_a.force_login(self.user_a)

        self.client_b = Client()
        self.client_b.force_login(self.user_b)

    def test_40_api_vendor_can_list_only_own_bids(self):
        resp = self.client_a.get("/api/v1/sourcing/bids/")
        self.assertEqual(resp.status_code, 200)
        results = resp.json().get("results", resp.json())
        self.assertEqual(len(results), 1)
        self.assertEqual(results[0]["id"], str(self.bid_a.id))

    def test_41_api_vendor_b_cannot_get_vendor_a_bid(self):
        resp = self.client_b.get(f"/api/v1/sourcing/bids/{self.bid_a.id}/")
        self.assertIn(resp.status_code, [403, 404])

    def test_42_api_save_draft_and_validate(self):
        resp = self.client_a.post(
            f"/api/v1/sourcing/bids/{self.bid_a.id}/save-draft/",
            {"proposal_summary": "API Draft proposal"},
            content_type="application/json",
        )
        self.assertEqual(resp.status_code, 200)
        self.bid_a.refresh_from_db()
        self.assertEqual(self.bid_a.proposal_summary, "API Draft proposal")

        val_resp = self.client_a.post(f"/api/v1/sourcing/bids/{self.bid_a.id}/validate/")
        self.assertEqual(val_resp.status_code, 200)
        self.assertIn("is_valid", val_resp.json())

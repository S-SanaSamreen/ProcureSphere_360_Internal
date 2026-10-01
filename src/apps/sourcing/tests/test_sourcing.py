"""
Sourcing module tests — models, services, views, security.
Covers: event lifecycle, vendor invitations, technical/commercial requirements,
clarifications, deadline enforcement, IDOR protection, RBAC.
"""
import uuid
from datetime import timedelta

from django.contrib.auth import get_user_model
from django.core.exceptions import ValidationError
from django.test import Client, TestCase
from django.urls import reverse
from django.utils import timezone

from apps.accounts.models import Role
from apps.vendors.models import Vendor, VendorCategory
from apps.sourcing.models import (
    BidInvite, Clarification, CommercialRequirement, SourcingEvent, TechnicalRequirement, VendorBid,
)
from apps.sourcing.services import (
    add_commercial_requirement_service, add_technical_requirement_service,
    close_sourcing_event_service, create_sourcing_event_service,
    invite_vendors_to_event_service, publish_sourcing_event_service,
    respond_to_clarification_service, submit_clarification_service,
)

User = get_user_model()


# ──────────────────────── Fixtures ───────────────────────────────────────────

def make_role(code):
    role_names = {
        Role.PROC_EXEC: "Procurement Executive",
        Role.PROC_MGR: "Procurement Manager",
        Role.VENDOR_USER: "Vendor Portal User",
        Role.SUPER_ADMIN: "Super Admin",
    }
    return Role.objects.get_or_create(code=code, defaults={"name": role_names.get(code, code)})[0]


def make_vendor_category():
    return VendorCategory.objects.get_or_create(
        code="IT", defaults={"name": "IT Services"}
    )[0]


def make_vendor(name="Acme Corp", status=Vendor.STATUS_ACTIVE):
    cat = make_vendor_category()
    return Vendor.objects.create(
        legal_name=name,
        vendor_number=f"VND-{uuid.uuid4().hex[:6].upper()}",
        tax_identification_number=f"TAX-{uuid.uuid4().hex[:8]}",
        category=cat,
        status=status,
        email=f"vendor-{uuid.uuid4().hex[:6]}@example.com",
        address="123 Test Street",
    )


def make_user(email, role_code, vendor=None):
    role = make_role(role_code)
    user = User.objects.create_user(email=email, password="TestPass123!")
    user.role = role
    user.vendor = vendor
    user.save()
    return user


def make_event(user, bid_start=None, bid_end=None, status=SourcingEvent.STATUS_DRAFT):
    now = timezone.now()
    event = create_sourcing_event_service(
        title="Test RFQ Event",
        event_type=SourcingEvent.TYPE_RFQ,
        description="Test description",
        bid_start_date=bid_start or now + timedelta(hours=1),
        bid_end_date=bid_end or now + timedelta(days=7),
        created_by_user=user,
    )
    if status != SourcingEvent.STATUS_DRAFT:
        event.status = status
        event.save()
    return event


# ──────────────────────── Model Tests ────────────────────────────────────────

class SourcingEventModelTests(TestCase):

    def setUp(self):
        self.proc_exec = make_user("procexec@test.com", Role.PROC_EXEC)

    def test_event_created_with_unique_number(self):
        e1 = make_event(self.proc_exec)
        e2 = make_event(self.proc_exec)
        self.assertNotEqual(e1.event_number, e2.event_number)

    def test_event_number_format_rfq(self):
        event = create_sourcing_event_service(
            title="T", event_type=SourcingEvent.TYPE_RFQ,
            description="d", created_by_user=self.proc_exec,
        )
        self.assertTrue(event.event_number.startswith("RFQ-"))

    def test_event_number_format_rfp(self):
        event = create_sourcing_event_service(
            title="T", event_type=SourcingEvent.TYPE_RFP,
            description="d", created_by_user=self.proc_exec,
        )
        self.assertTrue(event.event_number.startswith("RFP-"))

    def test_invalid_bid_dates_raises(self):
        now = timezone.now()
        with self.assertRaises(ValidationError):
            create_sourcing_event_service(
                title="T", event_type=SourcingEvent.TYPE_RFQ, description="d",
                bid_start_date=now + timedelta(days=5),
                bid_end_date=now + timedelta(days=1),
                created_by_user=self.proc_exec,
            )

    def test_default_status_is_draft(self):
        event = make_event(self.proc_exec)
        self.assertEqual(event.status, SourcingEvent.STATUS_DRAFT)

    def test_can_transition_to(self):
        event = make_event(self.proc_exec)
        self.assertTrue(event.can_transition_to(SourcingEvent.STATUS_CANCELLED))
        self.assertFalse(event.can_transition_to(SourcingEvent.STATUS_CLOSED))


class BidInviteModelTests(TestCase):

    def setUp(self):
        self.proc_exec = make_user("procexec2@test.com", Role.PROC_EXEC)
        self.vendor = make_vendor()
        self.event = make_event(self.proc_exec)

    def test_duplicate_invitation_not_created(self):
        invite_vendors_to_event_service(
            event=self.event, vendor_ids=[str(self.vendor.id)], invited_by=self.proc_exec
        )
        invite_vendors_to_event_service(
            event=self.event, vendor_ids=[str(self.vendor.id)], invited_by=self.proc_exec
        )
        self.assertEqual(BidInvite.objects.filter(event=self.event, vendor=self.vendor).count(), 1)

    def test_suspended_vendor_cannot_be_invited(self):
        suspended = make_vendor("Suspended Corp", status=Vendor.STATUS_SUSPENDED)
        with self.assertRaises(ValidationError):
            invite_vendors_to_event_service(
                event=self.event, vendor_ids=[str(suspended.id)], invited_by=self.proc_exec
            )


# ──────────────────────── Service Tests ──────────────────────────────────────

class PublishEventServiceTests(TestCase):

    def setUp(self):
        self.proc_exec = make_user("pe@test.com", Role.PROC_EXEC)
        self.vendor = make_vendor()

    def test_cannot_publish_without_invited_vendors(self):
        event = make_event(self.proc_exec)
        with self.assertRaises(ValidationError):
            publish_sourcing_event_service(event=event, user=self.proc_exec)

    def test_cannot_publish_non_draft(self):
        event = make_event(self.proc_exec)
        invite_vendors_to_event_service(
            event=event, vendor_ids=[str(self.vendor.id)], invited_by=self.proc_exec
        )
        publish_sourcing_event_service(event=event, user=self.proc_exec)
        with self.assertRaises(ValidationError):
            publish_sourcing_event_service(event=event, user=self.proc_exec)

    def test_publish_moves_status_to_bid_window(self):
        event = make_event(self.proc_exec)
        invite_vendors_to_event_service(
            event=event, vendor_ids=[str(self.vendor.id)], invited_by=self.proc_exec
        )
        publish_sourcing_event_service(event=event, user=self.proc_exec)
        event.refresh_from_db()
        self.assertEqual(event.status, SourcingEvent.STATUS_BID_WINDOW)
        self.assertIsNotNone(event.published_at)

    def test_publish_with_approved_vendor(self):
        approved_vendor = make_vendor("ApprovedVendor Ltd")
        approved_vendor.status = Vendor.STATUS_APPROVED
        approved_vendor.save(update_fields=["status"])
        event = make_event(self.proc_exec)
        invite_vendors_to_event_service(
            event=event, vendor_ids=[str(approved_vendor.id)], invited_by=self.proc_exec
        )
        publish_sourcing_event_service(event=event, user=self.proc_exec)
        event.refresh_from_db()
        self.assertEqual(event.status, SourcingEvent.STATUS_BID_WINDOW)


class CloseEventServiceTests(TestCase):

    def setUp(self):
        self.proc_exec = make_user("pec@test.com", Role.PROC_EXEC)
        self.vendor = make_vendor()

    def _open_event(self):
        event = make_event(self.proc_exec)
        invite_vendors_to_event_service(
            event=event, vendor_ids=[str(self.vendor.id)], invited_by=self.proc_exec
        )
        publish_sourcing_event_service(event=event, user=self.proc_exec)
        return event

    def test_close_bid_window_event(self):
        event = self._open_event()
        close_sourcing_event_service(event=event, user=self.proc_exec)
        event.refresh_from_db()
        self.assertEqual(event.status, SourcingEvent.STATUS_CLOSED)
        self.assertIsNotNone(event.closed_at)

    def test_cannot_close_draft_event(self):
        event = make_event(self.proc_exec)
        with self.assertRaises(ValidationError):
            close_sourcing_event_service(event=event, user=self.proc_exec)


class TechnicalRequirementTests(TestCase):

    def setUp(self):
        self.proc_exec = make_user("pe_tech@test.com", Role.PROC_EXEC)
        self.event = make_event(self.proc_exec)

    def test_add_tech_req_to_draft(self):
        req = add_technical_requirement_service(
            event=self.event, title="ISO Compliance", description="Must be ISO 27001",
            importance=TechnicalRequirement.IMPORTANCE_MANDATORY, created_by=self.proc_exec,
        )
        self.assertEqual(req.event, self.event)
        self.assertEqual(TechnicalRequirement.objects.filter(event=self.event).count(), 1)

    def test_cannot_add_tech_req_to_published_event(self):
        vendor = make_vendor()
        invite_vendors_to_event_service(
            event=self.event, vendor_ids=[str(vendor.id)], invited_by=self.proc_exec
        )
        publish_sourcing_event_service(event=self.event, user=self.proc_exec)
        with self.assertRaises(ValidationError):
            add_technical_requirement_service(
                event=self.event, title="T", description="D", created_by=self.proc_exec,
            )


class CommercialRequirementTests(TestCase):

    def setUp(self):
        self.proc_exec = make_user("pe_comm@test.com", Role.PROC_EXEC)
        self.event = make_event(self.proc_exec)

    def test_add_commercial_req(self):
        req = add_commercial_requirement_service(
            event=self.event, title="Server Units", description="24x Dell R750",
            requirement_type=CommercialRequirement.TYPE_UNIT_RATE,
            unit="each", quantity=24, created_by=self.proc_exec,
        )
        self.assertEqual(req.event, self.event)
        self.assertEqual(req.quantity, 24)

    def test_add_commercial_req_without_description_or_order(self):
        req = add_commercial_requirement_service(
            event=self.event, title="Server Units",
            requirement_type=CommercialRequirement.TYPE_UNIT_RATE,
            unit="ea", quantity=10, created_by=self.proc_exec,
        )
        self.assertEqual(req.description, "")
        self.assertEqual(req.order, 0)

    def test_commercial_req_form_validation(self):
        from apps.sourcing.forms import CommercialRequirementForm
        form = CommercialRequirementForm(data={
            "title": "Rack Rails",
            "requirement_type": CommercialRequirement.TYPE_UNIT_RATE,
            "unit": "set",
            "quantity": "24",
        })
        self.assertTrue(form.is_valid(), form.errors)
        self.assertEqual(form.cleaned_data["order"], 0)
        self.assertEqual(form.cleaned_data["description"], "")


class ClarificationTests(TestCase):

    def setUp(self):
        self.proc_exec = make_user("pe_clar@test.com", Role.PROC_EXEC)
        self.vendor = make_vendor("ClarVendor Corp")
        self.vendor_user = make_user("vuser@clarvendor.com", Role.VENDOR_USER, vendor=self.vendor)
        self.event = make_event(
            self.proc_exec,
            bid_start=timezone.now() - timedelta(hours=1),  # Already started
            bid_end=timezone.now() + timedelta(days=7),
        )
        invite_vendors_to_event_service(
            event=self.event, vendor_ids=[str(self.vendor.id)], invited_by=self.proc_exec
        )
        publish_sourcing_event_service(event=self.event, user=self.proc_exec)

    def test_invited_vendor_can_submit_clarification(self):
        clar = submit_clarification_service(
            event=self.event, vendor=self.vendor,
            question="What is the delivery timeline?",
            submitted_by=self.vendor_user,
        )
        self.assertEqual(clar.status, Clarification.STATUS_OPEN)
        self.assertEqual(clar.vendor, self.vendor)

    def test_uninvited_vendor_cannot_submit(self):
        other_vendor = make_vendor("OtherVendor Inc")
        with self.assertRaises(ValidationError):
            submit_clarification_service(
                event=self.event, vendor=other_vendor,
                question="Can I participate?",
                submitted_by=self.vendor_user,
            )

    def test_procurement_can_respond(self):
        clar = submit_clarification_service(
            event=self.event, vendor=self.vendor,
            question="Warranty period?",
            submitted_by=self.vendor_user,
        )
        respond_to_clarification_service(
            clarification=clar, response="2 years",
            visibility=Clarification.VISIBILITY_ALL_VENDORS,
            responded_by=self.proc_exec,
        )
        clar.refresh_from_db()
        self.assertEqual(clar.status, Clarification.STATUS_ANSWERED)
        self.assertEqual(clar.response, "2 years")
        self.assertEqual(clar.responded_by, self.proc_exec)


# ──────────────────────── Deadline Enforcement Tests ─────────────────────────

class DeadlineEnforcementTests(TestCase):

    def setUp(self):
        self.proc_exec = make_user("pe_dl@test.com", Role.PROC_EXEC)
        self.vendor = make_vendor("DeadlineVendor")
        self.vendor_user = make_user("vuser_dl@vendor.com", Role.VENDOR_USER, vendor=self.vendor)

    def test_bid_window_not_open_before_start(self):
        """Event in BID_WINDOW status but bid_start_date is in the future."""
        future_start = timezone.now() + timedelta(hours=2)
        event = make_event(
            self.proc_exec,
            bid_start=future_start,
            bid_end=future_start + timedelta(days=7),
        )
        invite_vendors_to_event_service(
            event=event, vendor_ids=[str(self.vendor.id)], invited_by=self.proc_exec
        )
        publish_sourcing_event_service(event=event, user=self.proc_exec)
        self.assertFalse(event.is_accepting_bids)

    def test_bid_window_open_during_window(self):
        """Event in BID_WINDOW with current time between start and end."""
        now = timezone.now()
        event = make_event(
            self.proc_exec,
            bid_start=now - timedelta(hours=1),
            bid_end=now + timedelta(days=7),
        )
        invite_vendors_to_event_service(
            event=event, vendor_ids=[str(self.vendor.id)], invited_by=self.proc_exec
        )
        publish_sourcing_event_service(event=event, user=self.proc_exec)
        self.assertTrue(event.is_accepting_bids)

    def test_bid_window_closed_after_deadline(self):
        """Event in BID_WINDOW but bid_end_date is in the past."""
        past_start = timezone.now() - timedelta(days=10)
        past_end = timezone.now() - timedelta(days=1)
        event = make_event(
            self.proc_exec,
            bid_start=past_start,
            bid_end=past_end,
        )
        invite_vendors_to_event_service(
            event=event, vendor_ids=[str(self.vendor.id)], invited_by=self.proc_exec
        )
        publish_sourcing_event_service(event=event, user=self.proc_exec)
        self.assertFalse(event.is_accepting_bids)
        self.assertTrue(event.is_past_deadline)


# ──────────────────────── View / RBAC Tests ──────────────────────────────────

class SourcingViewRBACTests(TestCase):

    def setUp(self):
        self.client = Client()
        self.proc_exec = make_user("view_pe@test.com", Role.PROC_EXEC)
        self.vendor = make_vendor("ViewVendor Corp")
        self.vendor_user = make_user("vview@vendor.com", Role.VENDOR_USER, vendor=self.vendor)
        self.other_vendor = make_vendor("OtherVendor Ltd")
        self.other_vendor_user = make_user("ov@vendor.com", Role.VENDOR_USER, vendor=self.other_vendor)
        self.event = make_event(self.proc_exec)

    def test_unauthenticated_user_redirected_from_list(self):
        response = self.client.get(reverse("sourcing:list"))
        self.assertIn(response.status_code, [302, 403])

    def test_procurement_exec_can_access_list(self):
        self.client.force_login(self.proc_exec)
        response = self.client.get(reverse("sourcing:list"))
        self.assertEqual(response.status_code, 200)

    def test_vendor_cannot_access_procurement_list(self):
        self.client.force_login(self.vendor_user)
        response = self.client.get(reverse("sourcing:list"))
        self.assertEqual(response.status_code, 403)

    def test_vendor_cannot_access_draft_event(self):
        """Draft events must not be visible to vendors."""
        invite_vendors_to_event_service(
            event=self.event, vendor_ids=[str(self.vendor.id)], invited_by=self.proc_exec
        )
        self.client.force_login(self.vendor_user)
        response = self.client.get(reverse("sourcing:detail", kwargs={"pk": self.event.id}))
        # Must be 403 (draft) not 200
        self.assertEqual(response.status_code, 403)

    def test_uninvited_vendor_cannot_access_event(self):
        """IDOR protection: vendor B cannot access event they were not invited to."""
        invite_vendors_to_event_service(
            event=self.event, vendor_ids=[str(self.vendor.id)], invited_by=self.proc_exec
        )
        publish_sourcing_event_service(event=self.event, user=self.proc_exec)

        # other_vendor_user was NOT invited
        self.client.force_login(self.other_vendor_user)
        response = self.client.get(reverse("sourcing:detail", kwargs={"pk": self.event.id}))
        self.assertEqual(response.status_code, 403)

    def test_invited_vendor_can_access_open_event(self):
        invite_vendors_to_event_service(
            event=self.event, vendor_ids=[str(self.vendor.id)], invited_by=self.proc_exec
        )
        publish_sourcing_event_service(event=self.event, user=self.proc_exec)

        self.client.force_login(self.vendor_user)
        response = self.client.get(reverse("sourcing:detail", kwargs={"pk": self.event.id}))
        self.assertEqual(response.status_code, 200)

    def test_csrf_required_for_publish(self):
        """CSRF enforcement: request without token must fail."""
        self.client.force_login(self.proc_exec)
        invite_vendors_to_event_service(
            event=self.event, vendor_ids=[str(self.vendor.id)], invited_by=self.proc_exec
        )
        # Django test client enforces CSRF when using enforce_csrf_checks=True
        csrf_client = Client(enforce_csrf_checks=True)
        csrf_client.force_login(self.proc_exec)
        response = csrf_client.post(
            reverse("sourcing:publish", kwargs={"pk": self.event.id})
        )
        self.assertEqual(response.status_code, 403)

    def test_vendor_cannot_publish_event(self):
        self.client.force_login(self.vendor_user)
        response = self.client.post(
            reverse("sourcing:publish", kwargs={"pk": self.event.id})
        )
        self.assertEqual(response.status_code, 403)

    def test_vendor_cannot_close_event(self):
        self.client.force_login(self.vendor_user)
        response = self.client.post(
            reverse("sourcing:close", kwargs={"pk": self.event.id})
        )
        self.assertEqual(response.status_code, 403)

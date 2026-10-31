"""Vendor APPROVED stage, KYC-bypass removal and blacklist control (audit group C)."""

import pytest
from django.core.exceptions import PermissionDenied, ValidationError
from django.core.files.uploadedfile import SimpleUploadedFile
from rest_framework.test import APIClient

from apps.accounts.models import Role, User
from apps.audit.models import AuditLog
from apps.orders.services import generate_purchase_order_service
from apps.vendors.models import Vendor, VendorDocument
from apps.vendors.services import (
    activate_vendor_service,
    approve_vendor_service,
    blacklist_vendor_service,
    lift_vendor_blacklist_service,
    set_vendor_status_governance_service,
    upload_vendor_document_service,
    verify_vendor_document_service,
)

pytestmark = pytest.mark.django_db

REASON = "Confirmed sanctions-list match during audit"


def _test_org():
    from apps.organization.models import Organization

    return Organization.objects.get_or_create(code="HPE-T", defaults={"name": "HPE Test Org"})[0]


@pytest.fixture
def w(procurement_world, db_roles):
    world = dict(procurement_world)
    world["legal"] = User.objects.create_user(
        email="legal@lc.test",
        password="Password123!",
        role=db_roles[Role.LEGAL_MGR],
        organization=_test_org(),
    )
    world["mgr2"] = User.objects.create_user(
        email="mgr2@lc.test",
        password="Password123!",
        role=db_roles[Role.PROC_MGR],
        organization=_test_org(),
    )
    world["admin"] = User.objects.create_superuser(
        email="admin@lc.test",
        password="Password123!",
        first_name="A",
        last_name="D",
        role=db_roles[Role.SUPER_ADMIN],
    )
    return world


def kyc_ready_vendor(w, verified=True, status=Vendor.STATUS_SUBMITTED):
    vendor = w["vendor_kyc"]
    Vendor.objects.filter(pk=vendor.pk).update(status=status)
    vendor.refresh_from_db()
    doc = upload_vendor_document_service(
        vendor=vendor,
        user=w["exec"],
        file=SimpleUploadedFile("t.pdf", b"%PDF-1.4 kyc", content_type="application/pdf"),
        document_type="TAX_CLEARANCE",
        title="Tax certificate",
    )
    if verified:
        verify_vendor_document_service(document=doc, verifier=w["legal"])
    return vendor, doc


def trail(vendor):
    return list(
        AuditLog.objects.filter(target_model="Vendor", target_object_id=str(vendor.id))
        .order_by("timestamp", "id")
        .values_list("previous_state", "new_state", "action")
    )


def transitions(vendor):
    """[(from, to)] for every status change recorded in the vendor's audit trail."""
    out = []
    for prev, new, _ in trail(vendor):
        if (
            prev
            and new
            and "status" in prev
            and "status" in new
            and prev["status"] != new["status"]
        ):
            out.append((prev["status"], new["status"]))
    return out


# ============================================================ APPROVED stage
def test_approval_records_both_approved_and_active_transitions(w):
    vendor, _ = kyc_ready_vendor(w)
    result = approve_vendor_service(vendor=vendor, manager=w["exec"], notes="all good")
    vendor.refresh_from_db()
    assert result.status == Vendor.STATUS_ACTIVE == vendor.status
    assert vendor.is_event_eligible and vendor.event_eligibility_status == "ELIGIBLE"
    assert transitions(vendor) == [("SUBMITTED", "APPROVED"), ("APPROVED", "ACTIVE")]


def test_approved_vendor_rests_in_approved_until_activated_and_cannot_trade(w):
    vendor, _ = kyc_ready_vendor(w)
    approve_vendor_service(vendor=vendor, manager=w["exec"], notes="kyc ok", activate=False)
    vendor.refresh_from_db()
    assert vendor.status == Vendor.STATUS_APPROVED
    assert not vendor.is_event_eligible

    # An APPROVED (not yet ACTIVE) vendor cannot be issued a PO.
    with pytest.raises(ValidationError, match="must be ACTIVE"):
        generate_purchase_order_service(
            vendor=vendor,
            cost_center=w["cost_center"],
            created_by_user=w["exec"],
            line_items=[{"item_description": "x", "quantity": 1, "unit_price": 10}],
        )

    activate_vendor_service(vendor=vendor, manager=w["mgr"], notes="go live")
    vendor.refresh_from_db()
    assert vendor.status == Vendor.STATUS_ACTIVE
    assert vendor.is_event_eligible and vendor.event_eligibility_status == "ELIGIBLE"
    assert transitions(vendor) == [("SUBMITTED", "APPROVED"), ("APPROVED", "ACTIVE")]
    assert trail(vendor)[-1][1]["notes"] == "go live"


def test_auto_activation_is_configurable(w, settings):
    settings.VENDOR_AUTO_ACTIVATE_ON_APPROVAL = False
    vendor, _ = kyc_ready_vendor(w)
    approve_vendor_service(vendor=vendor, manager=w["exec"])
    vendor.refresh_from_db()
    assert vendor.status == Vendor.STATUS_APPROVED


def test_activation_revalidates_documents(w):
    vendor, doc = kyc_ready_vendor(w)
    approve_vendor_service(vendor=vendor, manager=w["exec"], activate=False)
    VendorDocument.objects.filter(pk=doc.pk).update(review_status=VendorDocument.REVIEW_REJECTED)
    with pytest.raises(ValidationError, match="Cannot activate vendor"):
        activate_vendor_service(vendor=vendor, manager=w["exec"])
    vendor.refresh_from_db()
    assert vendor.status == Vendor.STATUS_APPROVED


def test_invalid_lifecycle_calls_are_rejected(w):
    vendor, _ = kyc_ready_vendor(w)
    with pytest.raises(ValidationError, match="Only an APPROVED vendor"):
        activate_vendor_service(vendor=vendor, manager=w["exec"])  # still SUBMITTED
    approve_vendor_service(vendor=vendor, manager=w["exec"], activate=False)
    with pytest.raises(ValidationError, match="already approved"):
        approve_vendor_service(vendor=vendor, manager=w["exec"])
    activate_vendor_service(vendor=vendor, manager=w["exec"])
    with pytest.raises(ValidationError, match="already active"):
        activate_vendor_service(vendor=vendor, manager=w["exec"])


def test_only_operators_may_approve_or_activate(w):
    vendor, _ = kyc_ready_vendor(w)
    for user in (w["requester"], w["legal"], w["finance"], w["stores"]):
        with pytest.raises(PermissionDenied):
            approve_vendor_service(vendor=vendor, manager=user)
    approve_vendor_service(vendor=vendor, manager=w["exec"], activate=False)
    for user in (w["requester"], w["legal"], w["finance"]):
        with pytest.raises(PermissionDenied):
            activate_vendor_service(vendor=vendor, manager=user)


# ============================================================ KYC bypass removal (API)
def _api(user):
    c = APIClient()
    c.force_authenticate(user)
    return c


def test_set_status_api_activates_an_approved_vendor_but_still_not_a_submitted_one(w):
    vendor, _ = kyc_ready_vendor(w)
    c = _api(w["exec"])
    res = c.post(f"/api/v1/vendors/{vendor.id}/set-status/", {"status": "ACTIVE", "notes": "x"})
    assert res.status_code == 400  # SUBMITTED -> ACTIVE is only the KYC service

    approve_vendor_service(vendor=vendor, manager=w["exec"], activate=False)
    res = c.post(f"/api/v1/vendors/{vendor.id}/set-status/", {"status": "ACTIVE", "notes": "go"})
    assert res.status_code == 200, res.data
    vendor.refresh_from_db()
    assert vendor.status == Vendor.STATUS_ACTIVE and vendor.is_event_eligible


def test_governance_status_api_can_no_longer_activate_a_draft_vendor(w):
    draft = w["vendor_draft"]  # no KYC documents at all
    res = _api(w["mgr"]).post(
        f"/api/v1/vendors/{draft.id}/governance-status/", {"status": "ACTIVE", "notes": "force"}
    )
    assert res.status_code == 400
    draft.refresh_from_db()
    assert draft.status == Vendor.STATUS_DRAFT
    assert not draft.is_event_eligible


def test_governance_status_api_activation_is_kyc_gated(w):
    vendor, doc = kyc_ready_vendor(w, verified=False)  # document still pending
    c = _api(w["mgr"])
    res = c.post(
        f"/api/v1/vendors/{vendor.id}/governance-status/", {"status": "ACTIVE", "notes": "force"}
    )
    assert res.status_code == 400
    vendor.refresh_from_db()
    assert vendor.status == Vendor.STATUS_SUBMITTED

    verify_vendor_document_service(document=doc, verifier=w["legal"])
    res = c.post(
        f"/api/v1/vendors/{vendor.id}/governance-status/", {"status": "ACTIVE", "notes": "ok"}
    )
    assert res.status_code == 200, res.data
    vendor.refresh_from_db()
    assert vendor.status == Vendor.STATUS_ACTIVE and vendor.is_event_eligible
    assert transitions(vendor) == [("SUBMITTED", "APPROVED"), ("APPROVED", "ACTIVE")]


def test_assess_risk_api_cannot_activate_an_unverified_vendor(w):
    vendor, _ = kyc_ready_vendor(w, verified=False)
    res = _api(w["exec"]).post(
        f"/api/v1/vendors/{vendor.id}/assess-risk/",
        {"risk_level": "LOW", "assessment_notes": "fine", "governance_status": "ACTIVE"},
        format="json",
    )
    vendor.refresh_from_db()
    assert vendor.status == Vendor.STATUS_SUBMITTED  # whatever the response, it did not activate
    assert not vendor.is_event_eligible
    assert res.status_code in (200, 400)


# ============================================================ blacklist
def active_vendor(w):
    return w["vendor_a"]


def test_blacklist_is_distinct_audited_and_blocks_trading(w):
    vendor = active_vendor(w)
    blacklist_vendor_service(vendor=vendor, actor=w["mgr"], reason=REASON)
    vendor.refresh_from_db()
    assert vendor.is_blacklisted and vendor.blacklist_reason == REASON
    assert vendor.blacklisted_by_id == w["mgr"].id and vendor.blacklisted_at
    assert vendor.status == Vendor.STATUS_SUSPENDED
    assert not vendor.is_event_eligible
    last = trail(vendor)[-1]
    assert last[1]["is_blacklisted"] is True and last[1]["reason"] == REASON

    with pytest.raises(ValidationError, match="must be ACTIVE"):
        generate_purchase_order_service(
            vendor=vendor,
            cost_center=w["cost_center"],
            created_by_user=w["exec"],
            line_items=[{"item_description": "x", "quantity": 1, "unit_price": 10}],
        )


def test_a_plain_suspension_is_not_a_blacklist_and_can_be_reinstated(w):
    vendor = active_vendor(w)
    set_vendor_status_governance_service(
        vendor=vendor, actor=w["mgr"], new_status=Vendor.STATUS_SUSPENDED, notes="Late documents"
    )
    vendor.refresh_from_db()
    assert vendor.status == Vendor.STATUS_SUSPENDED and not vendor.is_blacklisted
    set_vendor_status_governance_service(
        vendor=vendor, actor=w["mgr"], new_status=Vendor.STATUS_ACTIVE, notes="Documents supplied"
    )
    vendor.refresh_from_db()
    assert vendor.status == Vendor.STATUS_ACTIVE


@pytest.mark.parametrize("target", [Vendor.STATUS_ACTIVE, Vendor.STATUS_ON_HOLD])
def test_blacklisted_vendor_cannot_be_reinstated_until_the_blacklist_is_lifted(w, target):
    vendor = active_vendor(w)
    blacklist_vendor_service(vendor=vendor, actor=w["mgr"], reason=REASON)
    with pytest.raises(ValidationError, match="blacklist must be lifted"):
        set_vendor_status_governance_service(
            vendor=vendor, actor=w["mgr"], new_status=target, notes="please"
        )
    vendor.refresh_from_db()
    assert vendor.status == Vendor.STATUS_SUSPENDED and vendor.is_blacklisted


def test_blacklisted_vendor_cannot_be_approved_or_activated(w):
    vendor, _ = kyc_ready_vendor(w)
    blacklist_vendor_service(vendor=vendor, actor=w["mgr"], reason=REASON)
    with pytest.raises(ValidationError, match="blacklisted"):
        approve_vendor_service(vendor=vendor, manager=w["exec"])
    with pytest.raises(ValidationError, match="blacklisted"):
        activate_vendor_service(vendor=vendor, manager=w["exec"])


def test_blacklist_requires_authority_and_a_real_reason(w):
    vendor = active_vendor(w)
    with pytest.raises(PermissionDenied):
        blacklist_vendor_service(vendor=vendor, actor=w["exec"], reason=REASON)
    with pytest.raises(ValidationError, match="at least 10"):
        blacklist_vendor_service(vendor=vendor, actor=w["mgr"], reason="bad")
    blacklist_vendor_service(vendor=vendor, actor=w["mgr"], reason=REASON)
    with pytest.raises(ValidationError, match="already blacklisted"):
        blacklist_vendor_service(vendor=vendor, actor=w["mgr"], reason=REASON)


def test_lifting_enforces_separation_of_duties_and_leaves_vendor_suspended(w):
    vendor = active_vendor(w)
    blacklist_vendor_service(vendor=vendor, actor=w["mgr"], reason=REASON)
    with pytest.raises(ValidationError, match="Separation of duties"):
        lift_vendor_blacklist_service(
            vendor=vendor, actor=w["mgr"], reason="Appeal accepted after review"
        )
    with pytest.raises(PermissionDenied):
        lift_vendor_blacklist_service(
            vendor=vendor, actor=w["exec"], reason="Appeal accepted after review"
        )
    with pytest.raises(ValidationError, match="at least 10"):
        lift_vendor_blacklist_service(vendor=vendor, actor=w["mgr2"], reason="ok")

    lift_vendor_blacklist_service(
        vendor=vendor, actor=w["mgr2"], reason="Appeal accepted after review"
    )
    vendor.refresh_from_db()
    assert not vendor.is_blacklisted
    assert vendor.status == Vendor.STATUS_SUSPENDED  # still suspended: reinstatement is separate
    assert not vendor.is_event_eligible

    set_vendor_status_governance_service(
        vendor=vendor, actor=w["mgr2"], new_status=Vendor.STATUS_ACTIVE, notes="Cleared to trade"
    )
    vendor.refresh_from_db()
    assert vendor.status == Vendor.STATUS_ACTIVE


def test_a_second_manager_may_lift_a_blacklist(w):
    # PRD 3.6: Super Admin does not take vendor governance decisions, so the Procurement
    # Manager (who holds suspend/blacklist authority) imposes it; a different manager lifts it
    # (separation of duties - the imposer cannot lift their own blacklist).
    vendor = active_vendor(w)
    blacklist_vendor_service(vendor=vendor, actor=w["mgr"], reason=REASON)
    lift_vendor_blacklist_service(vendor=vendor, actor=w["mgr2"], reason="Imposed in error here")
    vendor.refresh_from_db()
    assert not vendor.is_blacklisted


def test_blacklist_api_endpoints_and_serializer_fields(w):
    vendor = active_vendor(w)
    res = _api(w["exec"]).post(f"/api/v1/vendors/{vendor.id}/blacklist/", {"reason": REASON})
    assert res.status_code == 403  # may operate governance, may not blacklist

    res = _api(w["mgr"]).post(f"/api/v1/vendors/{vendor.id}/blacklist/", {"reason": REASON})
    assert res.status_code == 200, res.data
    assert res.data["is_blacklisted"] is True and res.data["blacklist_reason"] == REASON
    assert res.data["status"] == "SUSPENDED"

    res = _api(w["mgr"]).post(f"/api/v1/vendors/{vendor.id}/blacklist/", {"reason": REASON})
    assert res.status_code == 400  # already blacklisted

    res = _api(w["mgr"]).patch(
        f"/api/v1/vendors/{vendor.id}/", {"is_blacklisted": False}, format="json"
    )
    vendor.refresh_from_db()
    assert vendor.is_blacklisted  # read-only on the serializer

    res = _api(w["mgr2"]).post(
        f"/api/v1/vendors/{vendor.id}/lift-blacklist/", {"reason": "Appeal accepted after review"}
    )
    assert res.status_code == 200, res.data
    assert res.data["is_blacklisted"] is False


def test_detail_page_shows_blacklist_banner_and_controls(w, client):
    from django.urls import reverse

    vendor = active_vendor(w)
    client.force_login(w["mgr"])
    page = client.get(reverse("vendor_detail", args=[vendor.id])).content.decode()
    assert "Blacklist reason" in page  # the control is offered
    assert "Lift blacklist" not in page

    blacklist_vendor_service(vendor=vendor, actor=w["mgr"], reason=REASON)
    page = client.get(reverse("vendor_detail", args=[vendor.id])).content.decode()
    assert "Blacklisted" in page and REASON in page
    assert "Lift blacklist" in page
    assert "Blacklist reason" not in page  # already blacklisted: no second blacklist control

    client.force_login(w["exec"])
    page = client.get(reverse("vendor_detail", args=[vendor.id])).content.decode()
    assert "Blacklist reason" not in page and "Lift blacklist" not in page


def test_kyc_action_endpoints_for_activate_and_blacklist(w, client):
    from django.urls import reverse

    vendor, _ = kyc_ready_vendor(w)
    approve_vendor_service(vendor=vendor, manager=w["exec"], activate=False)
    client.force_login(w["exec"])
    client.post(reverse("vendor_kyc_action", args=[vendor.id, "activate"]), {"notes": "live"})
    vendor.refresh_from_db()
    assert vendor.status == Vendor.STATUS_ACTIVE

    # an operator without suspend authority is refused the blacklist action
    client.post(reverse("vendor_kyc_action", args=[vendor.id, "blacklist"]), {"notes": REASON})
    vendor.refresh_from_db()
    assert not vendor.is_blacklisted

    client.force_login(w["mgr"])
    client.post(reverse("vendor_kyc_action", args=[vendor.id, "blacklist"]), {"notes": REASON})
    vendor.refresh_from_db()
    assert vendor.is_blacklisted

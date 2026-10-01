from decimal import Decimal

from django.core.exceptions import ValidationError
from django.db import transaction
from django.utils import timezone

from apps.accounts.models import User
from apps.audit.models import AuditLog
from apps.vendors.models import Vendor

from .models import (
    AwardDecision, BidAttachment, BidInvite, BidLine, BidTechnicalResponse, BidVersion,
    Clarification, CommercialRequirement, SourcingEvent, TechnicalRequirement, VendorBid,
)


def _write_audit(actor, action, model_name, object_id, previous=None, new=None):
    AuditLog.objects.create(
        actor=actor,
        action=action,
        target_model=model_name,
        target_object_id=str(object_id),
        previous_state=previous,
        new_state=new,
    )


@transaction.atomic
def create_sourcing_event_service(
    *,
    title: str,
    event_type: str,
    description: str,
    bid_start_date=None,
    bid_end_date=None,
    clarification_deadline=None,
    technical_section_enabled: bool = True,
    commercial_section_enabled: bool = True,
    clarifications_allowed: bool = True,
    requisition=None,
    created_by_user: User = None,
) -> SourcingEvent:
    """Creates a new RFQ/RFP sourcing event in DRAFT status."""
    if event_type not in [SourcingEvent.TYPE_RFQ, SourcingEvent.TYPE_RFP]:
        raise ValidationError(f"Invalid event type: {event_type}")

    if bid_start_date and bid_end_date and bid_start_date >= bid_end_date:
        raise ValidationError("Bid start date must be before bid end date.")

    event_count = SourcingEvent.objects.count() + 1
    prefix = event_type  # RFQ or RFP
    event_number = f"{prefix}-{timezone.now().strftime('%Y')}-{event_count:05d}"

    # Ensure uniqueness in edge cases
    while SourcingEvent.objects.filter(event_number=event_number).exists():
        event_count += 1
        event_number = f"{prefix}-{timezone.now().strftime('%Y')}-{event_count:05d}"

    event = SourcingEvent.objects.create(
        event_number=event_number,
        title=title,
        event_type=event_type,
        description=description,
        requisition=requisition,
        status=SourcingEvent.STATUS_DRAFT,
        bid_start_date=bid_start_date,
        bid_end_date=bid_end_date,
        clarification_deadline=clarification_deadline,
        technical_section_enabled=technical_section_enabled,
        commercial_section_enabled=commercial_section_enabled,
        clarifications_allowed=clarifications_allowed,
        is_sealed=True,
        created_by=created_by_user,
    )

    _write_audit(
        created_by_user, AuditLog.ACTION_CREATE, "SourcingEvent", event.id,
        new={"event_number": event.event_number, "status": event.status, "type": event_type},
    )
    return event


@transaction.atomic
def update_sourcing_event_service(
    *,
    event: SourcingEvent,
    user: User,
    title: str = None,
    description: str = None,
    bid_start_date=None,
    bid_end_date=None,
    clarification_deadline=None,
    technical_section_enabled: bool = None,
    commercial_section_enabled: bool = None,
    clarifications_allowed: bool = None,
) -> SourcingEvent:
    """Updates a DRAFT sourcing event."""
    if event.status != SourcingEvent.STATUS_DRAFT:
        raise ValidationError("Only DRAFT events can be edited.")

    if bid_start_date and bid_end_date and bid_start_date >= bid_end_date:
        raise ValidationError("Bid start date must be before bid end date.")

    update_fields = ["updated_at"]
    if title is not None:
        event.title = title
        update_fields.append("title")
    if description is not None:
        event.description = description
        update_fields.append("description")
    if bid_start_date is not None:
        event.bid_start_date = bid_start_date
        update_fields.append("bid_start_date")
    if bid_end_date is not None:
        event.bid_end_date = bid_end_date
        update_fields.append("bid_end_date")
    if clarification_deadline is not None:
        event.clarification_deadline = clarification_deadline
        update_fields.append("clarification_deadline")
    if technical_section_enabled is not None:
        event.technical_section_enabled = technical_section_enabled
        update_fields.append("technical_section_enabled")
    if commercial_section_enabled is not None:
        event.commercial_section_enabled = commercial_section_enabled
        update_fields.append("commercial_section_enabled")
    if clarifications_allowed is not None:
        event.clarifications_allowed = clarifications_allowed
        update_fields.append("clarifications_allowed")

    event.save(update_fields=update_fields)
    _write_audit(user, AuditLog.ACTION_UPDATE, "SourcingEvent", event.id,
                 new={"event_number": event.event_number, "updated_fields": update_fields})
    return event


@transaction.atomic
def publish_sourcing_event_service(*, event: SourcingEvent, user: User) -> SourcingEvent:
    """Publishes a DRAFT event → BID_WINDOW. Validates required fields."""
    if event.status != SourcingEvent.STATUS_DRAFT:
        raise ValidationError(f"Cannot publish event in status '{event.status}'. Must be DRAFT.")

    if not event.bid_start_date or not event.bid_end_date:
        raise ValidationError("Bid start and end dates are required before publishing.")

    if event.bid_start_date >= event.bid_end_date:
        raise ValidationError("Bid start must be before bid end.")

    if not event.invitations.filter(vendor__status__in=[Vendor.STATUS_ACTIVE, Vendor.STATUS_APPROVED]).exists():
        raise ValidationError("At least one active or approved vendor must be invited before publishing.")

    previous_status = event.status
    event.status = SourcingEvent.STATUS_BID_WINDOW
    event.published_at = timezone.now()
    event.save(update_fields=["status", "published_at", "updated_at"])

    _write_audit(user, AuditLog.ACTION_UPDATE, "SourcingEvent", event.id,
                 previous={"status": previous_status},
                 new={"status": event.status, "published_at": str(event.published_at)})
    return event


@transaction.atomic
def close_sourcing_event_service(*, event: SourcingEvent, user: User) -> SourcingEvent:
    """Closes an open sourcing event → CLOSED status."""
    if not event.can_transition_to(SourcingEvent.STATUS_CLOSED):
        raise ValidationError(
            f"Cannot close event in status '{event.status}'. "
            f"Event must be in BID_WINDOW status."
        )

    previous_status = event.status
    event.status = SourcingEvent.STATUS_CLOSED
    event.closed_at = timezone.now()
    event.save(update_fields=["status", "closed_at", "updated_at"])

    _write_audit(user, AuditLog.ACTION_UPDATE, "SourcingEvent", event.id,
                 previous={"status": previous_status},
                 new={"status": event.status, "closed_at": str(event.closed_at)})
    return event


@transaction.atomic
def invite_vendors_to_event_service(
    *, event: SourcingEvent, vendor_ids: list, invited_by: User
) -> list:
    """Invites eligible vendors. Blocks suspended/blacklisted vendors. Skips duplicates."""
    if event.status not in [SourcingEvent.STATUS_DRAFT, SourcingEvent.STATUS_PUBLISHED, SourcingEvent.STATUS_BID_WINDOW]:
        raise ValidationError(
            "Vendors can only be invited while the event is in DRAFT, PUBLISHED, or BID_WINDOW status."
        )

    invitations = []
    for vid in vendor_ids:
        try:
            vendor = Vendor.objects.get(id=vid)
        except Vendor.DoesNotExist:
            raise ValidationError(f"Vendor with ID '{vid}' not found.")

        if vendor.status == Vendor.STATUS_SUSPENDED:
            raise ValidationError(
                f"Vendor '{vendor.legal_name}' is suspended and cannot be invited."
            )
        if vendor.status not in [Vendor.STATUS_ACTIVE, Vendor.STATUS_APPROVED]:
            raise ValidationError(
                f"Vendor '{vendor.legal_name}' is not approved/active (status: {vendor.status}). "
                f"Only ACTIVE or APPROVED vendors can be invited."
            )

        invite, created = BidInvite.objects.get_or_create(
            event=event,
            vendor=vendor,
            defaults={"status": BidInvite.STATUS_INVITED, "created_by": invited_by},
        )
        invitations.append(invite)

        if created:
            _write_audit(invited_by, AuditLog.ACTION_CREATE, "BidInvite", invite.id,
                         new={"event": event.event_number, "vendor": vendor.legal_name})

    return invitations


@transaction.atomic
def remove_vendor_invitation_service(
    *, event: SourcingEvent, vendor: Vendor, removed_by: User
) -> None:
    """Removes a vendor invitation from a DRAFT event."""
    if event.status != SourcingEvent.STATUS_DRAFT:
        raise ValidationError("Vendor invitations can only be removed from DRAFT events.")

    invite = BidInvite.objects.filter(event=event, vendor=vendor).first()
    if not invite:
        raise ValidationError(f"No invitation found for vendor '{vendor.legal_name}'.")

    _write_audit(removed_by, AuditLog.ACTION_DELETE, "BidInvite", invite.id,
                 previous={"event": event.event_number, "vendor": vendor.legal_name})
    invite.delete()


@transaction.atomic
def add_technical_requirement_service(
    *,
    event: SourcingEvent,
    title: str,
    description: str = "",
    importance: str = TechnicalRequirement.IMPORTANCE_MANDATORY,
    order: int = 0,
    created_by: User = None,
) -> TechnicalRequirement:
    """Adds a technical requirement to a DRAFT sourcing event."""
    if event.status != SourcingEvent.STATUS_DRAFT:
        raise ValidationError("Technical requirements can only be added to DRAFT events.")

    req = TechnicalRequirement.objects.create(
        event=event,
        title=title,
        description=description or "",
        importance=importance,
        order=order or 0,
        created_by=created_by,
    )
    _write_audit(created_by, AuditLog.ACTION_CREATE, "TechnicalRequirement", req.id,
                 new={"event": event.event_number, "title": title})
    return req


@transaction.atomic
def add_commercial_requirement_service(
    *,
    event: SourcingEvent,
    title: str,
    description: str = "",
    requirement_type: str = CommercialRequirement.TYPE_UNIT_RATE,
    unit: str = "",
    quantity=None,
    order: int = 0,
    created_by: User = None,
) -> CommercialRequirement:
    """Adds a commercial requirement to a DRAFT sourcing event."""
    if event.status != SourcingEvent.STATUS_DRAFT:
        raise ValidationError("Commercial requirements can only be added to DRAFT events.")

    req = CommercialRequirement.objects.create(
        event=event,
        title=title,
        description=description or "",
        requirement_type=requirement_type,
        unit=unit or "",
        quantity=quantity,
        order=order or 0,
        created_by=created_by,
    )
    _write_audit(created_by, AuditLog.ACTION_CREATE, "CommercialRequirement", req.id,
                 new={"event": event.event_number, "title": title})
    return req


@transaction.atomic
def submit_clarification_service(
    *,
    event: SourcingEvent,
    vendor: Vendor,
    question: str,
    submitted_by: User,
) -> Clarification:
    """Vendor submits a clarification question during the allowed window."""
    # Check vendor is invited
    if not BidInvite.objects.filter(event=event, vendor=vendor).exists():
        raise ValidationError("Your vendor is not invited to this sourcing event.")

    # Enforce event state: must be BID_WINDOW
    if event.status != SourcingEvent.STATUS_BID_WINDOW:
        raise ValidationError("Clarifications can only be submitted while the bid window is open.")

    # Enforce clarification deadline
    if not event.is_open_for_clarifications:
        raise ValidationError(
            "The clarification period has ended. No further questions can be submitted."
        )

    clarification = Clarification.objects.create(
        event=event,
        vendor=vendor,
        question=question,
        status=Clarification.STATUS_OPEN,
        visibility=Clarification.VISIBILITY_PRIVATE,
        created_by=submitted_by,
    )
    _write_audit(submitted_by, AuditLog.ACTION_CREATE, "Clarification", clarification.id,
                 new={"event": event.event_number, "vendor": vendor.legal_name})
    return clarification


@transaction.atomic
def respond_to_clarification_service(
    *,
    clarification: Clarification,
    response: str,
    visibility: str,
    responded_by: User,
) -> Clarification:
    """Procurement Executive responds to a vendor clarification."""
    if clarification.status == Clarification.STATUS_CLOSED:
        raise ValidationError("This clarification is already closed.")

    clarification.response = response
    clarification.status = Clarification.STATUS_ANSWERED
    clarification.visibility = visibility
    clarification.responded_by = responded_by
    clarification.responded_at = timezone.now()
    clarification.save(
        update_fields=["response", "status", "visibility", "responded_by", "responded_at", "updated_at"]
    )

    _write_audit(responded_by, AuditLog.ACTION_UPDATE, "Clarification", clarification.id,
                 new={"status": clarification.status, "visibility": visibility})
    return clarification


def _generate_bid_number():
    bid_count = VendorBid.objects.count() + 1
    bid_number = f"BID-{timezone.now().strftime('%Y')}-{bid_count:05d}"
    while VendorBid.objects.filter(bid_number=bid_number).exists():
        bid_count += 1
        bid_number = f"BID-{timezone.now().strftime('%Y')}-{bid_count:05d}"
    return bid_number


def _build_bid_snapshot(bid: VendorBid) -> dict:
    """Serializes the complete current bid state for an immutable BidVersion snapshot."""
    return {
        "bid_number": bid.bid_number,
        "version_number": bid.current_version_number,
        "total_bid_amount": str(bid.total_bid_amount),
        "proposal_summary": bid.proposal_summary,
        "vendor": {
            "id": str(bid.vendor.id),
            "legal_name": bid.vendor.legal_name,
        },
        "event": {
            "id": str(bid.event.id),
            "event_number": bid.event.event_number,
            "title": bid.event.title,
        },
        "submitted_at": str(timezone.now()),
        "technical_responses": [
            {
                "requirement_id": str(resp.requirement.id),
                "requirement_title": resp.requirement.title,
                "importance": resp.requirement.importance,
                "compliance_status": resp.compliance_status,
                "compliance_display": resp.get_compliance_status_display(),
                "response_text": resp.response_text,
            }
            for resp in bid.technical_responses.select_related("requirement").all()
        ],
        "commercial_lines": [
            {
                "item_description": line.item_description,
                "quantity": str(line.quantity),
                "quoted_unit_price": str(line.quoted_unit_price),
                "quoted_total_price": str(line.quoted_total_price),
                "currency": line.currency,
                "lead_time_days": line.lead_time_days,
                "notes": line.notes,
                "commercial_requirement_id": str(line.commercial_requirement.id) if line.commercial_requirement else None,
            }
            for line in bid.lines.all()
        ],
        "attachments": [
            {
                "id": str(att.id),
                "title": att.title,
                "original_filename": att.original_filename,
                "file_size": att.file_size,
                "content_type": att.content_type,
                "doc_type": att.doc_type,
                "doc_type_display": att.get_doc_type_display(),
                "created_at": str(att.created_at),
            }
            for att in bid.attachments.all()
        ],
    }


@transaction.atomic
def get_or_create_vendor_bid_service(
    *, event: SourcingEvent, vendor: Vendor, user: User = None
) -> tuple[VendorBid, bool]:
    """Retrieves an existing bid or initializes a draft bid workspace for an invited vendor."""
    invite = BidInvite.objects.filter(event=event, vendor=vendor).first()
    if not invite:
        raise ValidationError("Vendor is not invited to this sourcing event.")

    if vendor.status == Vendor.STATUS_SUSPENDED:
        raise ValidationError(f"Vendor '{vendor.legal_name}' is suspended and cannot access or submit bids.")
    if vendor.status not in [Vendor.STATUS_ACTIVE, Vendor.STATUS_APPROVED]:
        raise ValidationError(f"Vendor '{vendor.legal_name}' is not in active or approved status.")

    if event.status == SourcingEvent.STATUS_DRAFT:
        raise ValidationError("Sourcing event is still in DRAFT and is not open for bidding.")

    bid, created = VendorBid.objects.get_or_create(
        event=event,
        vendor=vendor,
        defaults={
            "bid_number": _generate_bid_number(),
            "invitation": invite,
            "status": VendorBid.STATUS_DRAFT,
            "total_bid_amount": Decimal("0.00"),
        },
    )

    if created:
        # Pre-seed technical responses from event technical requirements
        for req in event.technical_requirements.all():
            BidTechnicalResponse.objects.get_or_create(
                bid=bid,
                requirement=req,
                defaults={
                    "compliance_status": BidTechnicalResponse.COMPLIANCE_COMPLIANT,
                    "response_text": "",
                },
            )

        # Pre-seed commercial lines from event commercial requirements
        for comm in event.commercial_requirements.all():
            BidLine.objects.get_or_create(
                bid=bid,
                commercial_requirement=comm,
                defaults={
                    "item_description": comm.title,
                    "quantity": comm.quantity or Decimal("1.00"),
                    "quoted_unit_price": Decimal("0.00"),
                    "quoted_total_price": Decimal("0.00"),
                },
            )

        _write_audit(
            user,
            AuditLog.ACTION_CREATE,
            "VendorBid",
            bid.id,
            new={"bid_number": bid.bid_number, "vendor": vendor.legal_name, "event": event.event_number},
        )

    return bid, created


@transaction.atomic
def save_bid_draft_service(
    *,
    bid: VendorBid,
    user: User,
    proposal_summary: str = None,
    technical_responses: list = None,
    line_items: list = None,
) -> VendorBid:
    """Saves work-in-progress draft responses for an invited vendor without final submission."""
    event = bid.event
    now = timezone.now()

    if not event.is_accepting_bids or (event.bid_end_date and now > event.bid_end_date):
        raise ValidationError(f"Bid deadline has passed ({event.bid_end_date}). Draft cannot be modified.")

    if bid.status not in [VendorBid.STATUS_DRAFT, VendorBid.STATUS_VALIDATED]:
        raise ValidationError(
            f"Bid in status '{bid.get_status_display()}' cannot be edited as draft. Use the amendment workflow."
        )

    if proposal_summary is not None:
        bid.proposal_summary = proposal_summary

    if technical_responses is not None:
        for item in technical_responses:
            req_id = item.get("requirement_id")
            compliance = item.get("compliance_status", BidTechnicalResponse.COMPLIANCE_COMPLIANT)
            text = item.get("response_text", "")
            try:
                req = TechnicalRequirement.objects.get(id=req_id, event=event)
                BidTechnicalResponse.objects.update_or_create(
                    bid=bid,
                    requirement=req,
                    defaults={"compliance_status": compliance, "response_text": text},
                )
            except TechnicalRequirement.DoesNotExist:
                continue

    if line_items is not None:
        bid.lines.all().delete()
        total = Decimal("0.00")
        for item in line_items:
            qty = Decimal(str(item.get("quantity", "1.00")))
            price = Decimal(str(item.get("quoted_unit_price", "0.00")))
            comm_req = item.get("commercial_requirement")
            line = BidLine.objects.create(
                bid=bid,
                commercial_requirement=comm_req,
                pr_line=item.get("pr_line"),
                item_description=item.get("item_description", comm_req.title if comm_req else "Line Item"),
                quantity=qty,
                quoted_unit_price=price,
                currency=item.get("currency", "USD"),
                lead_time_days=item.get("lead_time_days"),
                notes=item.get("notes", ""),
            )
            total += line.quoted_total_price
        bid.total_bid_amount = total

    # Revert to draft status if it was validated, since data has changed
    bid.is_valid = False
    bid.status = VendorBid.STATUS_DRAFT
    bid.save(update_fields=["proposal_summary", "total_bid_amount", "is_valid", "status", "updated_at"])

    _write_audit(
        user,
        AuditLog.ACTION_UPDATE,
        "VendorBid",
        bid.id,
        new={"action": "save_draft", "bid_number": bid.bid_number, "total_amount": str(bid.total_bid_amount)},
    )
    return bid


@transaction.atomic
def validate_bid_submission_service(*, bid: VendorBid, user: User = None) -> tuple[bool, list[str]]:
    """Strict server-side validation of a vendor bid prior to submission."""
    issues = []
    now = timezone.now()
    event = bid.event
    vendor = bid.vendor

    # 1. Event status check
    if event.status != SourcingEvent.STATUS_BID_WINDOW:
        issues.append(f"Event status is '{event.get_status_display()}'. Bids can only be submitted during the BID_WINDOW.")

    # 2. Server-side deadline check
    if event.bid_start_date and now < event.bid_start_date:
        issues.append(f"Bid window opens at {event.bid_start_date}. Early submissions are not permitted.")
    if event.bid_end_date and now > event.bid_end_date:
        issues.append(f"Bid deadline passed at {event.bid_end_date}. Submissions are closed.")

    # 3. Vendor status check
    if vendor.status == Vendor.STATUS_SUSPENDED:
        issues.append(f"Vendor '{vendor.legal_name}' is currently suspended and cannot participate.")
    if vendor.status not in [Vendor.STATUS_ACTIVE, Vendor.STATUS_APPROVED]:
        issues.append(f"Vendor '{vendor.legal_name}' is not in active or approved standing.")

    # 4. Invitation check
    if not BidInvite.objects.filter(event=event, vendor=vendor).exists():
        issues.append("Vendor does not have a valid invitation for this event.")

    # 5. Technical requirements validation
    if event.technical_section_enabled:
        mandatory_reqs = event.technical_requirements.filter(importance=TechnicalRequirement.IMPORTANCE_MANDATORY)
        for req in mandatory_reqs:
            resp = bid.technical_responses.filter(requirement=req).first()
            if not resp or not resp.response_text.strip():
                issues.append(f"Mandatory technical requirement '{req.title}' has no response text.")
            elif resp.compliance_status == BidTechnicalResponse.COMPLIANCE_NON_COMPLIANT:
                issues.append(f"Mandatory technical requirement '{req.title}' cannot be submitted as Non-Compliant.")

    # 6. Commercial requirements / lines validation
    if event.commercial_section_enabled:
        if event.commercial_requirements.exists():
            for comm_req in event.commercial_requirements.all():
                matching_line = bid.lines.filter(commercial_requirement=comm_req).first()
                if not matching_line:
                    issues.append(f"Commercial requirement '{comm_req.title}' has not been quoted.")
                elif matching_line.quoted_unit_price <= Decimal("0.00"):
                    issues.append(f"Commercial item '{comm_req.title}' unit price must be greater than zero.")
                elif matching_line.quantity <= Decimal("0.00"):
                    issues.append(f"Commercial item '{comm_req.title}' quantity must be greater than zero.")
        else:
            if not bid.lines.exists():
                issues.append("Bid contains no commercial quotation line items.")
            else:
                for line in bid.lines.all():
                    if line.quoted_unit_price <= Decimal("0.00"):
                        issues.append(f"Commercial line '{line.item_description}' requires a positive unit price.")
                    if line.quantity <= Decimal("0.00"):
                        issues.append(f"Commercial line '{line.item_description}' requires a quantity greater than zero.")

        if bid.total_bid_amount <= Decimal("0.00"):
            calculated_total = sum(l.quoted_total_price for l in bid.lines.all())
            if calculated_total <= Decimal("0.00"):
                issues.append("Total bid amount must be greater than zero.")

    is_valid = len(issues) == 0
    bid.is_valid = is_valid
    bid.validation_notes = {
        "is_valid": is_valid,
        "issues": issues,
        "validated_at": str(now),
    }
    if is_valid and bid.status == VendorBid.STATUS_DRAFT:
        bid.status = VendorBid.STATUS_VALIDATED

    bid.save(update_fields=["is_valid", "validation_notes", "status", "updated_at"])
    _write_audit(
        user,
        AuditLog.ACTION_UPDATE,
        "VendorBid",
        bid.id,
        new={"action": "validation", "is_valid": is_valid, "issues_count": len(issues)},
    )
    return is_valid, issues


@transaction.atomic
def submit_vendor_bid_service(
    *,
    event: SourcingEvent = None,
    vendor: Vendor = None,
    line_items: list = None,
    proposal_summary: str = "",
    submitted_by_user: User = None,
    bid: VendorBid = None,
) -> VendorBid:
    """Submits a sealed bid for a vendor during the BID_WINDOW.
    Server enforces deadline — browser timestamp not trusted.
    Creates an immutable frozen snapshot in BidVersion 1."""
    if bid is None:
        if not event or not vendor:
            raise ValidationError("Either bid instance or (event, vendor) pair is required.")
        bid, _ = get_or_create_vendor_bid_service(event=event, vendor=vendor, user=submitted_by_user)
    else:
        event = bid.event
        vendor = bid.vendor

    # Check authorization: vendor must be invited
    if not BidInvite.objects.filter(event=event, vendor=vendor).exists():
        raise ValidationError("This vendor is not invited to the sourcing event.")

    if vendor.status == Vendor.STATUS_SUSPENDED:
        raise ValidationError(f"Vendor '{vendor.legal_name}' is suspended and cannot submit bids.")

    # Server-side deadline enforcement
    now = timezone.now()
    if not event.is_accepting_bids:
        if event.status != SourcingEvent.STATUS_BID_WINDOW:
            raise ValidationError(f"Bidding is not open. Event status: {event.get_status_display()}")
        if event.bid_start_date and now < event.bid_start_date:
            raise ValidationError(f"Bid window has not started yet. Opens at {event.bid_start_date}.")
        if event.bid_end_date and now > event.bid_end_date:
            raise ValidationError(f"Bid deadline has passed ({event.bid_end_date}). No further submissions accepted.")
        raise ValidationError("Bidding window is not currently open.")

    if proposal_summary:
        bid.proposal_summary = proposal_summary

    if line_items is not None:
        bid.lines.all().delete()
        total = Decimal("0.00")
        for item in line_items:
            qty = Decimal(str(item.get("quantity", "1.00")))
            price = Decimal(str(item.get("quoted_unit_price", "0.00")))
            line = BidLine.objects.create(
                bid=bid,
                commercial_requirement=item.get("commercial_requirement"),
                pr_line=item.get("pr_line"),
                item_description=item.get("item_description", "Quoted Item"),
                quantity=qty,
                quoted_unit_price=price,
                currency=item.get("currency", "USD"),
                lead_time_days=item.get("lead_time_days"),
                notes=item.get("notes", ""),
            )
            total += line.quoted_total_price
        bid.total_bid_amount = total

    # Run validation
    is_valid, issues = validate_bid_submission_service(bid=bid, user=submitted_by_user)
    if not is_valid:
        raise ValidationError(f"Bid cannot be submitted. Issues: {'; '.join(issues)}")

    bid.status = VendorBid.STATUS_SUBMITTED
    bid.submitted_at = now
    bid.submitted_by = submitted_by_user
    bid.current_version_number = 1
    bid.save(
        update_fields=[
            "total_bid_amount",
            "status",
            "proposal_summary",
            "submitted_at",
            "submitted_by",
            "current_version_number",
            "is_valid",
            "validation_notes",
            "updated_at",
        ]
    )

    # Freeze snapshot in BidVersion 1
    snapshot = _build_bid_snapshot(bid)
    BidVersion.objects.create(
        bid=bid,
        version_number=1,
        total_bid_amount=bid.total_bid_amount,
        proposal_summary=bid.proposal_summary,
        change_summary="Initial bid submission",
        submitted_at=bid.submitted_at,
        submitted_by=submitted_by_user,
        status=BidVersion.STATUS_SUBMITTED,
        snapshot=snapshot,
    )

    # Update invite status to show vendor participated
    BidInvite.objects.filter(event=event, vendor=vendor).update(
        is_responded=True, status=BidInvite.STATUS_ACCEPTED
    )

    _write_audit(
        submitted_by_user,
        AuditLog.ACTION_CREATE,
        "VendorBid",
        bid.id,
        new={
            "bid_number": bid.bid_number,
            "total_amount": str(bid.total_bid_amount),
            "vendor": vendor.legal_name,
            "version": 1,
        },
    )
    return bid


@transaction.atomic
def amend_vendor_bid_service(
    *,
    bid: VendorBid,
    user: User,
    change_summary: str,
    proposal_summary: str = None,
    line_items: list = None,
    technical_responses: list = None,
) -> VendorBid:
    """Amends an already submitted bid before the event close deadline.
    Preserves all historical versions immutably in BidVersion records.
    Server enforces timezone-aware deadline."""
    event = bid.event
    now = timezone.now()

    if not event.is_accepting_bids or (event.bid_end_date and now > event.bid_end_date):
        raise ValidationError(
            f"Bid deadline has passed ({event.bid_end_date}). Amendments are strictly prohibited after deadline."
        )

    if bid.status not in [VendorBid.STATUS_SUBMITTED, VendorBid.STATUS_AMENDED]:
        raise ValidationError(f"Cannot amend bid in status '{bid.get_status_display()}'. Must be SUBMITTED or AMENDED.")

    if not change_summary or not change_summary.strip():
        raise ValidationError("Change summary is required when submitting an amendment.")

    if proposal_summary is not None:
        bid.proposal_summary = proposal_summary

    if technical_responses is not None:
        for resp_data in technical_responses:
            req_id = resp_data.get("requirement_id")
            compliance = resp_data.get("compliance_status", BidTechnicalResponse.COMPLIANCE_COMPLIANT)
            text = resp_data.get("response_text", "")
            try:
                req = TechnicalRequirement.objects.get(id=req_id, event=event)
                BidTechnicalResponse.objects.update_or_create(
                    bid=bid,
                    requirement=req,
                    defaults={"compliance_status": compliance, "response_text": text},
                )
            except TechnicalRequirement.DoesNotExist:
                continue

    if line_items is not None:
        bid.lines.all().delete()
        total = Decimal("0.00")
        for item in line_items:
            qty = Decimal(str(item.get("quantity", "1.00")))
            price = Decimal(str(item.get("quoted_unit_price", "0.00")))
            comm_req = item.get("commercial_requirement")
            line = BidLine.objects.create(
                bid=bid,
                commercial_requirement=comm_req,
                pr_line=item.get("pr_line"),
                item_description=item.get("item_description", comm_req.title if comm_req else "Line Item"),
                quantity=qty,
                quoted_unit_price=price,
                currency=item.get("currency", "USD"),
                lead_time_days=item.get("lead_time_days"),
                notes=item.get("notes", ""),
            )
            total += line.quoted_total_price
        bid.total_bid_amount = total

    is_valid, issues = validate_bid_submission_service(bid=bid, user=user)
    if not is_valid:
        raise ValidationError(f"Amendment validation failed: {'; '.join(issues)}")

    new_version_number = bid.current_version_number + 1
    bid.current_version_number = new_version_number
    bid.status = VendorBid.STATUS_AMENDED
    bid.last_amended_at = now
    bid.save(
        update_fields=[
            "current_version_number",
            "status",
            "last_amended_at",
            "total_bid_amount",
            "proposal_summary",
            "is_valid",
            "validation_notes",
            "updated_at",
        ]
    )

    snapshot = _build_bid_snapshot(bid)
    BidVersion.objects.create(
        bid=bid,
        version_number=new_version_number,
        total_bid_amount=bid.total_bid_amount,
        proposal_summary=bid.proposal_summary,
        change_summary=change_summary,
        submitted_at=now,
        submitted_by=user,
        status=BidVersion.STATUS_AMENDED,
        snapshot=snapshot,
    )

    _write_audit(
        user,
        AuditLog.ACTION_UPDATE,
        "VendorBid",
        bid.id,
        new={
            "action": "amend",
            "bid_number": bid.bid_number,
            "version": new_version_number,
            "total_amount": str(bid.total_bid_amount),
            "change_summary": change_summary,
        },
    )
    return bid


@transaction.atomic
def upload_bid_attachment_service(
    *,
    bid: VendorBid,
    file,
    title: str,
    doc_type: str = BidAttachment.DOC_TYPE_SUPPORTING,
    user: User,
) -> BidAttachment:
    """Uploads a validated supporting document for a vendor bid."""
    from apps.core.validators import validate_file_upload

    event = bid.event
    now = timezone.now()

    if not event.is_accepting_bids or (event.bid_end_date and now > event.bid_end_date):
        raise ValidationError("Cannot upload attachments after the bid deadline has passed.")

    if not bid.is_editable and not bid.can_be_amended:
        raise ValidationError(f"Cannot upload attachments to bid in status '{bid.get_status_display()}'.")

    if not user.is_superuser and getattr(user, "vendor_id", None) != bid.vendor_id:
        raise ValidationError("You are not authorized to upload attachments for this vendor bid.")

    validate_file_upload(file)

    attachment = BidAttachment.objects.create(
        bid=bid,
        title=title or file.name,
        file=file,
        original_filename=file.name,
        file_size=file.size,
        content_type=getattr(file, "content_type", "") or "application/octet-stream",
        doc_type=doc_type,
        uploaded_by=user,
    )

    _write_audit(
        user,
        AuditLog.ACTION_CREATE,
        "BidAttachment",
        attachment.id,
        new={
            "bid_number": bid.bid_number,
            "filename": attachment.original_filename,
            "file_size": attachment.file_size,
            "doc_type": doc_type,
        },
    )
    return attachment


@transaction.atomic
def delete_bid_attachment_service(*, attachment: BidAttachment, user: User) -> None:
    """Deletes an attachment from an editable or amendable bid."""
    bid = attachment.bid
    event = bid.event
    now = timezone.now()

    if not event.is_accepting_bids or (event.bid_end_date and now > event.bid_end_date):
        raise ValidationError("Cannot delete attachments after the bid deadline has passed.")

    if not bid.is_editable and not bid.can_be_amended:
        raise ValidationError(f"Cannot delete attachments from bid in status '{bid.get_status_display()}'.")

    if not user.is_superuser and getattr(user, "vendor_id", None) != bid.vendor_id:
        raise ValidationError("You are not authorized to delete attachments for this vendor bid.")

    filename = attachment.original_filename
    att_id = str(attachment.id)

    if attachment.file:
        attachment.file.delete(save=False)

    attachment.delete()

    _write_audit(
        user,
        AuditLog.ACTION_DELETE,
        "BidAttachment",
        att_id,
        previous={"bid_number": bid.bid_number, "filename": filename},
    )


@transaction.atomic
def withdraw_vendor_bid_service(*, bid: VendorBid, user: User, reason: str = "") -> VendorBid:
    """Allows vendor to withdraw their submitted bid before the event close deadline."""
    event = bid.event
    now = timezone.now()

    if not event.is_accepting_bids or (event.bid_end_date and now > event.bid_end_date):
        raise ValidationError("Bids cannot be withdrawn after the bid deadline.")

    if bid.status not in [VendorBid.STATUS_SUBMITTED, VendorBid.STATUS_AMENDED]:
        raise ValidationError(f"Cannot withdraw bid in status '{bid.get_status_display()}'.")

    if not user.is_superuser and getattr(user, "vendor_id", None) != bid.vendor_id:
        raise ValidationError("You are not authorized to withdraw this bid.")

    previous_status = bid.status
    bid.status = VendorBid.STATUS_WITHDRAWN
    bid.save(update_fields=["status", "updated_at"])

    _write_audit(
        user,
        AuditLog.ACTION_UPDATE,
        "VendorBid",
        bid.id,
        previous={"status": previous_status},
        new={"status": bid.status, "reason": reason},
    )
    return bid


@transaction.atomic
def evaluate_and_award_sourcing_event_service(
    *, event: SourcingEvent, winning_bid: VendorBid, award_reason: str, approved_by_user: User
) -> AwardDecision:
    """Records the award decision and transitions event to AWARDED."""
    if winning_bid.event_id != event.id:
        raise ValidationError("Winning bid does not belong to target sourcing event.")

    if event.status not in [
        SourcingEvent.STATUS_CLOSED,
        SourcingEvent.STATUS_TECHNICAL_REVIEW,
        SourcingEvent.STATUS_COMMERCIAL_REVIEW,
        SourcingEvent.STATUS_AWARD_APPROVAL,
    ]:
        raise ValidationError(f"Cannot award event in status '{event.status}'.")

    previous_status = event.status
    event.status = SourcingEvent.STATUS_AWARDED
    event.save(update_fields=["status", "updated_at"])

    decision = AwardDecision.objects.create(
        event=event,
        winning_bid=winning_bid,
        award_reason=award_reason,
        approved_by=approved_by_user,
    )

    _write_audit(
        approved_by_user, AuditLog.ACTION_APPROVE, "AwardDecision", decision.id,
        previous={"status": previous_status},
        new={"event_number": event.event_number, "winner": winning_bid.vendor.legal_name, "status": event.status},
    )
    return decision

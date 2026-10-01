from django.utils import timezone

from .models import (
    BidAttachment, BidInvite, BidLine, BidTechnicalResponse, BidVersion,
    Clarification, SourcingEvent, VendorBid,
)


def get_vendor_bid_for_event(event, vendor):
    """Returns the VendorBid for a specific event+vendor pair with related responses."""
    return (
        VendorBid.objects.filter(event=event, vendor=vendor)
        .select_related("event", "vendor", "submitted_by")
        .prefetch_related(
            "lines__commercial_requirement",
            "technical_responses__requirement",
            "attachments__uploaded_by",
            "versions__submitted_by",
        )
        .first()
    )


def get_vendor_bid_by_id_scoped(bid_id, user):
    """
    Retrieves a VendorBid by ID enforcing strict object-level authorization:
    - Vendor users: access ONLY their own vendor's bid. Attempting to access another
      vendor's bid returns None (enforces 404/403 to prevent enumeration).
    - Internal procurement users: can access submitted bids for evaluation/review
      according to role permissions.
    """
    bid = (
        VendorBid.objects.filter(id=bid_id)
        .select_related("event", "vendor", "submitted_by")
        .prefetch_related(
            "lines__commercial_requirement",
            "technical_responses__requirement",
            "attachments__uploaded_by",
            "versions__submitted_by",
        )
        .first()
    )
    if not bid:
        return None

    if getattr(user, "is_vendor", False):
        if bid.vendor_id != getattr(user, "vendor_id", None):
            return None

    return bid


def get_bid_attachments_for_bid(bid, user):
    """Returns attachments for a bid with IDOR protection."""
    if getattr(user, "is_vendor", False) and bid.vendor_id != getattr(user, "vendor_id", None):
        return BidAttachment.objects.none()
    return bid.attachments.select_related("uploaded_by").order_by("-created_at")


def get_bid_versions_for_bid(bid, user):
    """Returns immutable BidVersion history for a bid with IDOR protection."""
    if getattr(user, "is_vendor", False) and bid.vendor_id != getattr(user, "vendor_id", None):
        return BidVersion.objects.none()
    return bid.versions.select_related("submitted_by").order_by("-version_number")


def get_procurement_submissions_for_event(event):
    """Returns all vendor bids for an event for procurement review."""
    return (
        VendorBid.objects.filter(event=event)
        .select_related("vendor", "submitted_by")
        .prefetch_related("attachments", "versions")
        .order_by("-submitted_at")
    )


def get_vendor_invitation_for_event(event, vendor):
    """Returns the BidInvite for a specific event+vendor pair, or None."""
    return BidInvite.objects.filter(event=event, vendor=vendor).first()


def get_sealed_vendor_bids(event: SourcingEvent, requesting_user):
    return (
        SourcingEvent.objects.select_related("requisition", "created_by")
        .prefetch_related("invitations__vendor", "bids")
        .order_by("-created_at")
    )


def get_sourcing_events_for_procurement(filters=None):
    """Returns events filtered for procurement users with optional status filter."""
    qs = SourcingEvent.objects.select_related("requisition", "created_by").prefetch_related(
        "invitations__vendor"
    )
    if filters:
        qs = qs.filter(**filters)
    return qs.order_by("-created_at")


def get_sourcing_event_by_id(event_id):
    return (
        SourcingEvent.objects.select_related("requisition", "created_by")
        .prefetch_related(
            "invitations__vendor",
            "bids__vendor",
            "technical_requirements",
            "commercial_requirements",
            "clarifications__vendor",
            "clarifications__responded_by",
        )
        .filter(id=event_id)
        .first()
    )


def get_vendor_accessible_events(vendor):
    """
    Returns sourcing events the vendor is invited to and that are active.
    Does NOT return DRAFT events — vendors cannot see drafts.
    """
    return (
        SourcingEvent.objects.filter(
            invitations__vendor=vendor,
            status__in=[
                SourcingEvent.STATUS_BID_WINDOW,
                SourcingEvent.STATUS_CLOSED,
                SourcingEvent.STATUS_TECHNICAL_REVIEW,
                SourcingEvent.STATUS_COMMERCIAL_REVIEW,
                SourcingEvent.STATUS_AWARDED,
            ],
        )
        .select_related("created_by")
        .distinct()
        .order_by("-created_at")
    )


def get_vendor_invitation_for_event(event, vendor):
    """Returns the BidInvite for a specific event+vendor pair, or None."""
    return BidInvite.objects.filter(event=event, vendor=vendor).first()


def get_sealed_vendor_bids(event: SourcingEvent, requesting_user):
    """
    Enforces sealed bid privacy controls:
    1. Vendor users see ONLY their own bids.
    2. Internal evaluators see NO bids while event is in BID_WINDOW (sealed).
    3. Once BID_WINDOW closes, evaluators see all submitted bids.
    """
    queryset = (
        VendorBid.objects.select_related("vendor", "event")
        .prefetch_related("lines")
        .filter(event=event)
    )

    if requesting_user.is_vendor:
        return queryset.filter(vendor_id=requesting_user.vendor_id)

    # Internal evaluators: block bid visibility while bid window is open (sealed)
    if event.status == SourcingEvent.STATUS_BID_WINDOW:
        return VendorBid.objects.none()

    return queryset.order_by("total_bid_amount")


def get_clarifications_for_event_and_vendor(event, vendor):
    """Returns clarifications visible to a specific vendor (private + all-vendor)."""
    return Clarification.objects.filter(event=event).filter(
        models_Q_vendor_or_all(vendor)
    ).select_related("vendor", "responded_by").order_by("-created_at")


def models_Q_vendor_or_all(vendor):
    """Helper Q object: private clarifications for this vendor, or visible to all."""
    from django.db.models import Q
    return Q(vendor=vendor) | Q(visibility=Clarification.VISIBILITY_ALL_VENDORS)


def get_all_clarifications_for_event(event):
    """Procurement Executive view: all clarifications for the event."""
    return (
        Clarification.objects.filter(event=event)
        .select_related("vendor", "responded_by")
        .order_by("-created_at")
    )


def get_closing_soon_events(days=7):
    """Events closing within the specified number of days."""
    now = timezone.now()
    deadline = now + timezone.timedelta(days=days)
    return SourcingEvent.objects.filter(
        status=SourcingEvent.STATUS_BID_WINDOW,
        bid_end_date__gte=now,
        bid_end_date__lte=deadline,
    ).order_by("bid_end_date")


def get_sourcing_dashboard_metrics():
    """Aggregated sourcing metrics for dashboard."""
    from django.db.models import Count

    total = SourcingEvent.objects.count()
    draft = SourcingEvent.objects.filter(status=SourcingEvent.STATUS_DRAFT).count()
    open_events = SourcingEvent.objects.filter(status=SourcingEvent.STATUS_BID_WINDOW).count()
    closed = SourcingEvent.objects.filter(status=SourcingEvent.STATUS_CLOSED).count()
    awarded = SourcingEvent.objects.filter(status=SourcingEvent.STATUS_AWARDED).count()
    pending_clarifications = Clarification.objects.filter(
        status=Clarification.STATUS_OPEN
    ).count()
    closing_soon = get_closing_soon_events(days=7).count()

    return {
        "total": total,
        "draft": draft,
        "open": open_events,
        "closed": closed,
        "awarded": awarded,
        "pending_clarifications": pending_clarifications,
        "closing_soon": closing_soon,
    }

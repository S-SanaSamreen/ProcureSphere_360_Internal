from decimal import Decimal

from django.db import models
from django.core.exceptions import ValidationError
from django.utils import timezone

from apps.core.models import TimeStampedModel
from apps.core.validators import validate_file_upload


class SourcingEvent(TimeStampedModel):
    TYPE_RFQ = "RFQ"
    TYPE_RFP = "RFP"

    EVENT_TYPE_CHOICES = [
        (TYPE_RFQ, "Request For Quotation (RFQ)"),
        (TYPE_RFP, "Request For Proposal (RFP)"),
    ]

    STATUS_DRAFT = "DRAFT"
    STATUS_PUBLISHED = "PUBLISHED"
    STATUS_BID_WINDOW = "BID_WINDOW"
    STATUS_CLOSED = "CLOSED"
    STATUS_TECHNICAL_REVIEW = "TECHNICAL_REVIEW"
    STATUS_COMMERCIAL_REVIEW = "COMMERCIAL_REVIEW"
    STATUS_AWARD_APPROVAL = "AWARD_APPROVAL"
    STATUS_AWARDED = "AWARDED"
    STATUS_CANCELLED = "CANCELLED"

    STATUS_CHOICES = [
        (STATUS_DRAFT, "Draft"),
        (STATUS_PUBLISHED, "Published"),
        (STATUS_BID_WINDOW, "Bid Window Open"),
        (STATUS_CLOSED, "Closed"),
        (STATUS_TECHNICAL_REVIEW, "Technical Review"),
        (STATUS_COMMERCIAL_REVIEW, "Commercial Review"),
        (STATUS_AWARD_APPROVAL, "Award Approval"),
        (STATUS_AWARDED, "Awarded"),
        (STATUS_CANCELLED, "Cancelled"),
    ]

    # Valid transitions: {from_status: [allowed_to_statuses]}
    VALID_TRANSITIONS = {
        STATUS_DRAFT: [STATUS_PUBLISHED, STATUS_BID_WINDOW, STATUS_CANCELLED],
        STATUS_PUBLISHED: [STATUS_BID_WINDOW, STATUS_CANCELLED],
        STATUS_BID_WINDOW: [STATUS_CLOSED, STATUS_CANCELLED],
        STATUS_CLOSED: [STATUS_TECHNICAL_REVIEW],
        STATUS_TECHNICAL_REVIEW: [STATUS_COMMERCIAL_REVIEW],
        STATUS_COMMERCIAL_REVIEW: [STATUS_AWARD_APPROVAL],
        STATUS_AWARD_APPROVAL: [STATUS_AWARDED],
        STATUS_AWARDED: [],
        STATUS_CANCELLED: [],
    }

    event_number = models.CharField(max_length=50, unique=True, db_index=True)
    title = models.CharField(max_length=255)
    event_type = models.CharField(max_length=20, choices=EVENT_TYPE_CHOICES, default=TYPE_RFQ)
    description = models.TextField()
    requisition = models.ForeignKey(
        "requisitions.PurchaseRequisition",
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="sourcing_events",
    )
    status = models.CharField(
        max_length=50, choices=STATUS_CHOICES, default=STATUS_DRAFT, db_index=True
    )

    # Bid window (server-authoritative datetimes)
    bid_start_date = models.DateTimeField(null=True, blank=True)
    bid_end_date = models.DateTimeField(null=True, blank=True)

    # Sealed bid (vendors cannot see each other's bids)
    is_sealed = models.BooleanField(default=True)

    # Lifecycle timestamps
    published_at = models.DateTimeField(null=True, blank=True)
    closed_at = models.DateTimeField(null=True, blank=True)

    # Section flags
    technical_section_enabled = models.BooleanField(default=True)
    commercial_section_enabled = models.BooleanField(default=True)
    clarifications_allowed = models.BooleanField(default=True)

    # Clarification deadline (must submit questions before this time)
    clarification_deadline = models.DateTimeField(null=True, blank=True)

    class Meta:
        ordering = ["-created_at"]
        indexes = [
            models.Index(fields=["status", "event_type"]),
            models.Index(fields=["bid_end_date"]),
        ]

    def __str__(self):
        return f"{self.event_number} - {self.title} [{self.status}]"

    def clean(self):
        if self.bid_start_date and self.bid_end_date:
            if self.bid_start_date >= self.bid_end_date:
                raise ValidationError("Bid start date must be before bid end date.")

    @property
    def is_bid_window_open(self):
        """Server-authoritative bid window check."""
        now = timezone.now()
        return (
            self.status == self.STATUS_BID_WINDOW
            and self.bid_start_date is not None
            and self.bid_end_date is not None
            and self.bid_start_date <= now <= self.bid_end_date
        )

    @property
    def is_accepting_bids(self):
        return self.is_bid_window_open

    @property
    def is_past_deadline(self):
        if self.bid_end_date:
            return timezone.now() > self.bid_end_date
        return False

    @property
    def is_open_for_clarifications(self):
        if not self.clarifications_allowed:
            return False
        if self.clarification_deadline:
            return timezone.now() <= self.clarification_deadline
        # Default: allow clarifications while bid window is open
        return self.status == self.STATUS_BID_WINDOW

    def can_transition_to(self, new_status):
        return new_status in self.VALID_TRANSITIONS.get(self.status, [])


class BidInvite(TimeStampedModel):
    STATUS_INVITED = "INVITED"
    STATUS_ACCEPTED = "ACCEPTED"
    STATUS_DECLINED = "DECLINED"

    STATUS_CHOICES = [
        (STATUS_INVITED, "Invited"),
        (STATUS_ACCEPTED, "Accepted / Participating"),
        (STATUS_DECLINED, "Declined"),
    ]

    event = models.ForeignKey(SourcingEvent, on_delete=models.CASCADE, related_name="invitations")
    vendor = models.ForeignKey(
        "vendors.Vendor", on_delete=models.CASCADE, related_name="bid_invitations"
    )
    status = models.CharField(max_length=20, choices=STATUS_CHOICES, default=STATUS_INVITED)
    is_responded = models.BooleanField(default=False)
    invited_at = models.DateTimeField(auto_now_add=True)
    responded_at = models.DateTimeField(null=True, blank=True)

    class Meta:
        unique_together = ("event", "vendor")

    def __str__(self):
        return f"Invite for {self.vendor.legal_name} to {self.event.event_number}"


class TechnicalRequirement(TimeStampedModel):
    """Technical section requirement for a sourcing event."""

    IMPORTANCE_MANDATORY = "MANDATORY"
    IMPORTANCE_PREFERRED = "PREFERRED"
    IMPORTANCE_OPTIONAL = "OPTIONAL"

    IMPORTANCE_CHOICES = [
        (IMPORTANCE_MANDATORY, "Mandatory"),
        (IMPORTANCE_PREFERRED, "Preferred"),
        (IMPORTANCE_OPTIONAL, "Optional"),
    ]

    event = models.ForeignKey(
        SourcingEvent, on_delete=models.CASCADE, related_name="technical_requirements"
    )
    title = models.CharField(max_length=255)
    description = models.TextField(blank=True, default="")
    importance = models.CharField(
        max_length=20, choices=IMPORTANCE_CHOICES, default=IMPORTANCE_MANDATORY
    )
    order = models.PositiveIntegerField(default=0, blank=True)

    class Meta:
        ordering = ["order", "created_at"]

    def __str__(self):
        return f"[TECH] {self.title} ({self.event.event_number})"


class CommercialRequirement(TimeStampedModel):
    """Commercial section requirement for a sourcing event."""

    TYPE_FIXED_PRICE = "FIXED_PRICE"
    TYPE_UNIT_RATE = "UNIT_RATE"
    TYPE_MILESTONE = "MILESTONE"
    TYPE_OTHER = "OTHER"

    TYPE_CHOICES = [
        (TYPE_FIXED_PRICE, "Fixed Price"),
        (TYPE_UNIT_RATE, "Unit Rate / BOQ"),
        (TYPE_MILESTONE, "Milestone-Based"),
        (TYPE_OTHER, "Other"),
    ]

    event = models.ForeignKey(
        SourcingEvent, on_delete=models.CASCADE, related_name="commercial_requirements"
    )
    title = models.CharField(max_length=255)
    description = models.TextField(blank=True, default="")
    requirement_type = models.CharField(
        max_length=30, choices=TYPE_CHOICES, default=TYPE_UNIT_RATE
    )
    unit = models.CharField(max_length=100, blank=True, default="", help_text="E.g. 'each', 'kg', 'month'")
    quantity = models.DecimalField(
        max_digits=12, decimal_places=2, null=True, blank=True,
        help_text="Required quantity for this line item"
    )
    order = models.PositiveIntegerField(default=0, blank=True)

    class Meta:
        ordering = ["order", "created_at"]

    def __str__(self):
        return f"[COMM] {self.title} ({self.event.event_number})"


class Clarification(TimeStampedModel):
    """Vendor questions and Procurement Executive responses on a sourcing event."""

    STATUS_OPEN = "OPEN"
    STATUS_ANSWERED = "ANSWERED"
    STATUS_CLOSED = "CLOSED"

    STATUS_CHOICES = [
        (STATUS_OPEN, "Open / Pending Response"),
        (STATUS_ANSWERED, "Answered"),
        (STATUS_CLOSED, "Closed"),
    ]

    VISIBILITY_PRIVATE = "PRIVATE"
    VISIBILITY_ALL_VENDORS = "ALL_VENDORS"

    VISIBILITY_CHOICES = [
        (VISIBILITY_PRIVATE, "Private (This vendor only)"),
        (VISIBILITY_ALL_VENDORS, "Visible to all invited vendors"),
    ]

    event = models.ForeignKey(
        SourcingEvent, on_delete=models.CASCADE, related_name="clarifications"
    )
    vendor = models.ForeignKey(
        "vendors.Vendor", on_delete=models.CASCADE, related_name="sourcing_clarifications"
    )
    question = models.TextField()
    response = models.TextField(blank=True)
    status = models.CharField(max_length=20, choices=STATUS_CHOICES, default=STATUS_OPEN)
    visibility = models.CharField(
        max_length=20, choices=VISIBILITY_CHOICES, default=VISIBILITY_PRIVATE
    )
    responded_by = models.ForeignKey(
        "accounts.User",
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="clarification_responses",
    )
    responded_at = models.DateTimeField(null=True, blank=True)

    @property
    def answer(self):
        return self.response

    @answer.setter
    def answer(self, value):
        self.response = value

    @property
    def answered_by(self):
        return self.responded_by

    @answered_by.setter
    def answered_by(self, value):
        self.responded_by = value

    @property
    def answered_at(self):
        return self.responded_at

    @answered_at.setter
    def answered_at(self, value):
        self.responded_at = value


    class Meta:
        ordering = ["-created_at"]

    def __str__(self):
        return f"Clarification on {self.event.event_number} by {self.vendor.legal_name} [{self.status}]"


class VendorBid(TimeStampedModel):
    STATUS_DRAFT = "DRAFT"
    STATUS_VALIDATED = "VALIDATED"
    STATUS_SUBMITTED = "SUBMITTED"
    STATUS_AMENDED = "AMENDED"
    STATUS_CLOSED = "CLOSED"
    STATUS_WITHDRAWN = "WITHDRAWN"

    STATUS_CHOICES = [
        (STATUS_DRAFT, "Draft"),
        (STATUS_VALIDATED, "Validated"),
        (STATUS_SUBMITTED, "Submitted"),
        (STATUS_AMENDED, "Amended"),
        (STATUS_CLOSED, "Closed / Sealed"),
        (STATUS_WITHDRAWN, "Withdrawn"),
    ]

    event = models.ForeignKey(SourcingEvent, on_delete=models.CASCADE, related_name="bids")
    vendor = models.ForeignKey("vendors.Vendor", on_delete=models.CASCADE, related_name="bids")
    invitation = models.ForeignKey(
        "sourcing.BidInvite", on_delete=models.SET_NULL, null=True, blank=True, related_name="bids"
    )
    bid_number = models.CharField(max_length=50, unique=True, db_index=True)
    total_bid_amount = models.DecimalField(max_digits=14, decimal_places=2, default=Decimal("0.00"))
    status = models.CharField(max_length=30, choices=STATUS_CHOICES, default=STATUS_DRAFT, db_index=True)
    proposal_summary = models.TextField(blank=True)
    current_version_number = models.PositiveIntegerField(default=1)
    submitted_at = models.DateTimeField(null=True, blank=True)
    last_amended_at = models.DateTimeField(null=True, blank=True)
    submitted_by = models.ForeignKey(
        "accounts.User", on_delete=models.SET_NULL, null=True, blank=True, related_name="submitted_bids"
    )
    is_sealed = models.BooleanField(default=True)
    is_valid = models.BooleanField(default=False)
    validation_notes = models.JSONField(null=True, blank=True, default=dict)

    class Meta:
        unique_together = ("event", "vendor")
        ordering = ["-created_at"]

    def __str__(self):
        return f"Bid {self.bid_number} by {self.vendor.legal_name} (${self.total_bid_amount})"

    @property
    def is_editable(self):
        """Vendor can edit if event is open and deadline has not passed."""
        return self.event.is_accepting_bids and self.status in [
            self.STATUS_DRAFT, self.STATUS_VALIDATED, self.STATUS_SUBMITTED, self.STATUS_AMENDED
        ]

    @property
    def can_be_amended(self):
        """Vendor can amend if already submitted and event is still open before deadline."""
        return self.event.is_accepting_bids and self.status in [
            self.STATUS_SUBMITTED, self.STATUS_AMENDED
        ]


class BidTechnicalResponse(TimeStampedModel):
    """Vendor technical compliance response for a technical requirement."""
    COMPLIANCE_COMPLIANT = "COMPLIANT"
    COMPLIANCE_PARTIAL = "PARTIAL"
    COMPLIANCE_NON_COMPLIANT = "NON_COMPLIANT"

    COMPLIANCE_CHOICES = [
        (COMPLIANCE_COMPLIANT, "Compliant"),
        (COMPLIANCE_PARTIAL, "Partially Compliant"),
        (COMPLIANCE_NON_COMPLIANT, "Non-Compliant"),
    ]

    bid = models.ForeignKey(VendorBid, on_delete=models.CASCADE, related_name="technical_responses")
    requirement = models.ForeignKey(TechnicalRequirement, on_delete=models.CASCADE, related_name="bid_responses")
    compliance_status = models.CharField(max_length=30, choices=COMPLIANCE_CHOICES, default=COMPLIANCE_COMPLIANT)
    response_text = models.TextField(blank=True)

    class Meta:
        unique_together = ("bid", "requirement")
        ordering = ["requirement__order", "created_at"]

    def __str__(self):
        return f"Response for {self.requirement.title} on {self.bid.bid_number} [{self.compliance_status}]"


class BidLine(TimeStampedModel):
    bid = models.ForeignKey(VendorBid, on_delete=models.CASCADE, related_name="lines")
    commercial_requirement = models.ForeignKey(
        CommercialRequirement, on_delete=models.SET_NULL, null=True, blank=True,
        related_name="bid_lines"
    )
    pr_line = models.ForeignKey(
        "requisitions.PRLine", on_delete=models.SET_NULL, null=True, blank=True
    )
    item_description = models.CharField(max_length=255)
    quantity = models.DecimalField(max_digits=12, decimal_places=2)
    quoted_unit_price = models.DecimalField(max_digits=12, decimal_places=2)
    quoted_total_price = models.DecimalField(max_digits=14, decimal_places=2)
    currency = models.CharField(max_length=10, default="USD")
    notes = models.TextField(blank=True)
    lead_time_days = models.PositiveIntegerField(null=True, blank=True)

    def save(self, *args, **kwargs):
        self.quoted_total_price = self.quantity * self.quoted_unit_price
        super().save(*args, **kwargs)


class BidAttachment(TimeStampedModel):
    """Supporting document uploaded by the vendor for their bid."""
    DOC_TYPE_TECHNICAL = "TECHNICAL"
    DOC_TYPE_COMMERCIAL = "COMMERCIAL"
    DOC_TYPE_SUPPORTING = "SUPPORTING_DOC"
    DOC_TYPE_CERTIFICATE = "CERTIFICATE"

    DOC_TYPE_CHOICES = [
        (DOC_TYPE_TECHNICAL, "Technical Specification / Whitepaper"),
        (DOC_TYPE_COMMERCIAL, "Commercial Proposal / Quote Sheet"),
        (DOC_TYPE_SUPPORTING, "Supporting Document"),
        (DOC_TYPE_CERTIFICATE, "Compliance / ISO / Quality Certificate"),
    ]

    bid = models.ForeignKey(VendorBid, on_delete=models.CASCADE, related_name="attachments")
    title = models.CharField(max_length=255)
    file = models.FileField(upload_to="bid_attachments/%Y/%m/", validators=[validate_file_upload])
    original_filename = models.CharField(max_length=255)
    file_size = models.PositiveIntegerField(default=0)
    content_type = models.CharField(max_length=100, blank=True)
    doc_type = models.CharField(max_length=50, choices=DOC_TYPE_CHOICES, default=DOC_TYPE_SUPPORTING)
    uploaded_by = models.ForeignKey(
        "accounts.User", on_delete=models.SET_NULL, null=True, blank=True, related_name="uploaded_bid_attachments"
    )

    class Meta:
        ordering = ["-created_at"]

    def __str__(self):
        return f"{self.title} ({self.original_filename}) for {self.bid.bid_number}"


class BidVersion(TimeStampedModel):
    """Immutable snapshot of a submitted or amended vendor bid version."""
    STATUS_SUBMITTED = "SUBMITTED"
    STATUS_AMENDED = "AMENDED"

    STATUS_CHOICES = [
        (STATUS_SUBMITTED, "Submitted"),
        (STATUS_AMENDED, "Amended"),
    ]

    bid = models.ForeignKey(VendorBid, on_delete=models.CASCADE, related_name="versions")
    version_number = models.PositiveIntegerField()
    total_bid_amount = models.DecimalField(max_digits=14, decimal_places=2, default=Decimal("0.00"))
    proposal_summary = models.TextField(blank=True)
    submitted_at = models.DateTimeField()
    submitted_by = models.ForeignKey(
        "accounts.User", on_delete=models.SET_NULL, null=True, blank=True, related_name="created_bid_versions"
    )
    status = models.CharField(max_length=30, choices=STATUS_CHOICES, default=STATUS_SUBMITTED)
    change_summary = models.TextField(blank=True)
    snapshot = models.JSONField(default=dict, help_text="Frozen snapshot of technical, commercial, and attachment metadata")

    class Meta:
        unique_together = ("bid", "version_number")
        ordering = ["-version_number"]

    def __str__(self):
        return f"{self.bid.bid_number} v{self.version_number} (${self.total_bid_amount})"


class AwardDecision(TimeStampedModel):
    event = models.OneToOneField(
        SourcingEvent, on_delete=models.CASCADE, related_name="award_decision"
    )
    winning_bid = models.ForeignKey(VendorBid, on_delete=models.PROTECT, related_name="awards")
    award_reason = models.TextField()
    approved_by = models.ForeignKey(
        "accounts.User", on_delete=models.PROTECT, related_name="approved_awards"
    )

    def __str__(self):
        return f"Award for {self.event.event_number} -> {self.winning_bid.vendor.legal_name}"


class BidEvaluation(TimeStampedModel):
    event = models.ForeignKey(SourcingEvent, on_delete=models.CASCADE, related_name="evaluations")
    bid = models.ForeignKey(VendorBid, on_delete=models.CASCADE, related_name="evaluations")
    evaluator = models.ForeignKey("accounts.User", on_delete=models.PROTECT, related_name="evaluations")
    technical_score = models.DecimalField(max_digits=5, decimal_places=2, default=Decimal("0.00"))
    commercial_score = models.DecimalField(max_digits=5, decimal_places=2, default=Decimal("0.00"))
    weighted_total_score = models.DecimalField(max_digits=5, decimal_places=2, default=Decimal("0.00"))
    comments = models.TextField(blank=True)

    def save(self, *args, **kwargs):
        self.weighted_total_score = (self.technical_score * Decimal("0.50")) + (self.commercial_score * Decimal("0.50"))
        super().save(*args, **kwargs)

    def __str__(self):
        return f"Eval for Bid {self.bid.bid_number} by {self.evaluator.email}: {self.weighted_total_score}"



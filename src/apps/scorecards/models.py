from decimal import Decimal

from django.core.exceptions import ValidationError
from django.db import models

from apps.core.models import TimeStampedModel

DIMENSIONS = ("delivery", "quality", "price", "responsiveness", "compliance", "sla")
DIMENSION_LABELS = {
    "delivery": "Delivery",
    "quality": "Quality",
    "price": "Price",
    "responsiveness": "Responsiveness",
    "compliance": "Compliance",
    "sla": "SLA",
}
# Industry-typical split used when an organization has not configured its own weights.
DEFAULT_WEIGHTS = {
    "delivery": Decimal("25"),
    "quality": Decimal("25"),
    "price": Decimal("20"),
    "responsiveness": Decimal("10"),
    "compliance": Decimal("10"),
    "sla": Decimal("10"),
}
# (minimum composite, band) checked top-down.
RATING_BANDS = (
    (Decimal("90"), "A"),
    (Decimal("75"), "B"),
    (Decimal("60"), "C"),
    (Decimal("0"), "D"),
)


def rating_band_for(score):
    if score is None:
        return ""
    for minimum, band in RATING_BANDS:
        if Decimal(score) >= minimum:
            return band
    return "D"


def compute_composite(scores, weights):
    """
    Weighted average of the 0-100 dimension scores. Dimensions with no data (None) or a zero
    weight are excluded and the remaining weights renormalized (never counted as 0).
    Returns (composite or None, {dimension: effective weight %}).
    """
    available = {
        d: Decimal(weights.get(d, 0))
        for d in DIMENSIONS
        if scores.get(d) is not None and Decimal(weights.get(d, 0)) > 0
    }
    total = sum(available.values(), Decimal("0"))
    if not total:
        return None, {}
    weighted = sum((Decimal(scores[d]) * w for d, w in available.items()), Decimal("0"))
    composite = max(Decimal("0"), min(Decimal("100"), round(weighted / total, 2)))
    effective = {d: round(w / total * 100, 2) for d, w in available.items()}
    return composite, effective


class ScorecardWeight(TimeStampedModel):
    """Per-organization weights (percent, must total 100) for the six scorecard dimensions."""

    organization = models.OneToOneField(
        "organization.Organization", on_delete=models.CASCADE, related_name="scorecard_weight"
    )
    delivery_weight = models.DecimalField(max_digits=5, decimal_places=2, default=25)
    quality_weight = models.DecimalField(max_digits=5, decimal_places=2, default=25)
    price_weight = models.DecimalField(max_digits=5, decimal_places=2, default=20)
    responsiveness_weight = models.DecimalField(max_digits=5, decimal_places=2, default=10)
    compliance_weight = models.DecimalField(max_digits=5, decimal_places=2, default=10)
    sla_weight = models.DecimalField(max_digits=5, decimal_places=2, default=10)

    def as_dict(self):
        return {d: Decimal(getattr(self, f"{d}_weight")) for d in DIMENSIONS}

    def clean(self):
        super().clean()
        values = [getattr(self, f"{d}_weight") for d in DIMENSIONS]
        if any(v is None for v in values):
            return
        if any(v < 0 or v > 100 for v in values):
            raise ValidationError("Each weight must be between 0 and 100.")
        total = sum((Decimal(v) for v in values), Decimal("0"))
        if total != Decimal("100"):
            raise ValidationError(f"Weights must total 100% (currently {total}%).")

    def save(self, *args, **kwargs):
        self.full_clean(exclude=["organization", "created_by"])
        super().save(*args, **kwargs)

    def __str__(self):
        return f"Scorecard weights: {self.organization_id}"


def get_weights(organization):
    """Configured weights for the organization, or the industry defaults."""
    if organization is not None:
        row = ScorecardWeight.objects.filter(organization=organization).first()
        if row:
            return row.as_dict()
    return dict(DEFAULT_WEIGHTS)


def _score_field(help_text):
    return models.DecimalField(
        max_digits=5, decimal_places=2, null=True, blank=True, help_text=help_text
    )


class VendorScorecard(TimeStampedModel):
    vendor = models.ForeignKey(
        "vendors.Vendor", on_delete=models.CASCADE, related_name="scorecards"
    )
    evaluation_period = models.CharField(max_length=50)  # e.g., Q1-2026

    # All dimension scores are 0-100 and NULL when no transactional data exists (never
    # fabricated). PRD 3.3: delivery, quality, price, responsiveness, compliance and SLA.
    delivery_score = _score_field("0-100: share of GRNs received on/before the expected date")
    quality_score = _score_field("0-100: accepted quantity / received quantity")
    price_score = _score_field("0-100: invoiced value adherence to PO value")
    compliance_score = _score_field("0-100: KYC/governance status, document validity, risk")
    responsiveness_score = _score_field("0-100: RFQ response rate and PO acknowledgement rate")
    sla_score = _score_field("0-100: contract obligations fulfilled on/before due date")

    composite_score = _score_field("Weighted average of available dimensions (renormalized)")
    period_start = models.DateField(null=True, blank=True)
    period_end = models.DateField(null=True, blank=True)
    weights_used = models.JSONField(
        default=dict,
        blank=True,
        help_text="Configured dimension weights (%) in force when this scorecard was computed",
    )
    rating_band = models.CharField(max_length=1, blank=True, help_text="A>=90, B>=75, C>=60, D")
    evaluator_comments = models.TextField(blank=True)
    evaluated_by = models.ForeignKey(
        "accounts.User", on_delete=models.PROTECT, related_name="evaluated_scorecards"
    )

    class Meta:
        ordering = ["-created_at"]
        indexes = [models.Index(fields=["vendor", "created_at"], name="scorecard_vendor_ts_idx")]

    def save(self, *args, **kwargs):
        weights = self.weights_used or DEFAULT_WEIGHTS
        scores = {d: getattr(self, f"{d}_score") for d in DIMENSIONS}
        self.composite_score, _ = compute_composite(scores, weights)
        self.rating_band = rating_band_for(self.composite_score)
        if not self.weights_used:
            self.weights_used = {d: str(w) for d, w in DEFAULT_WEIGHTS.items()}
        super().save(*args, **kwargs)

    @property
    def dimension_rows(self):
        """[(label, score or None, configured weight %)] in PRD order."""
        weights = self.weights_used or DEFAULT_WEIGHTS
        return [
            (DIMENSION_LABELS[d], getattr(self, f"{d}_score"), Decimal(str(weights.get(d, 0))))
            for d in DIMENSIONS
        ]

    @property
    def indicator_rows(self):
        return [(label, score) for label, score, _ in self.dimension_rows]

    def __str__(self):
        return f"Scorecard: {self.vendor.legal_name} [{self.evaluation_period}] Score: {self.composite_score}"

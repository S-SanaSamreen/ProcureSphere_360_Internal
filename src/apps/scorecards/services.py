import calendar
import re
from datetime import date
from decimal import Decimal

from django.db import models, transaction
from django.db.models import Min, Q, Sum
from django.db.models.functions import Least
from django.utils import timezone

from apps.audit.models import AuditLog
from apps.audit.services import create_audit_log_service
from apps.receipts.models import GoodsReceipt, ReceiptLine
from apps.vendors.models import Vendor

from .models import VendorScorecard, get_weights

HUNDRED = Decimal("100.00")

# Compliance: start from the governance status, then deduct for KYC / risk findings.
COMPLIANCE_BY_STATUS = {
    Vendor.STATUS_ACTIVE: Decimal("100.00"),
    Vendor.STATUS_ON_HOLD: Decimal("60.00"),
    Vendor.STATUS_SUSPENDED: Decimal("0.00"),
}
COMPLIANCE_DEFAULT = Decimal("80.00")
EXPIRED_DOC_PENALTY = Decimal("15")
REJECTED_DOC_PENALTY = Decimal("5")
RISK_PENALTY = {"HIGH": Decimal("20"), "MEDIUM": Decimal("10")}


def _pct(numerator, denominator):
    if not denominator:
        return None
    return max(
        Decimal("0.00"),
        min(HUNDRED, round((Decimal(numerator) / Decimal(denominator)) * HUNDRED, 2)),
    )


_QUARTER_RE = re.compile(r"^(?:Q([1-4])[\s_-]*(\d{4})|(\d{4})[\s_-]*Q([1-4]))$", re.I)
_MONTH_RE = re.compile(r"^(\d{4})-(\d{2})$")
_YEAR_RE = re.compile(r"^(\d{4})$")


def resolve_period(label):
    """
    (start, end) dates for labels like "Q1-2026", "2026-Q1", "2026-03" or "2026"; (None, None)
    for free-text labels, which evaluate over all history.
    """
    label = (label or "").strip()
    m = _QUARTER_RE.match(label)
    if m:
        q = int(m.group(1) or m.group(4))
        year = int(m.group(2) or m.group(3))
        start = date(year, 3 * (q - 1) + 1, 1)
        end = date(year, 3 * q, calendar.monthrange(year, 3 * q)[1])
        return start, end
    m = _MONTH_RE.match(label)
    if m and 1 <= int(m.group(2)) <= 12:
        year, month = int(m.group(1)), int(m.group(2))
        return date(year, month, 1), date(year, month, calendar.monthrange(year, month)[1])
    m = _YEAR_RE.match(label)
    if m:
        year = int(m.group(1))
        return date(year, 1, 1), date(year, 12, 31)
    return None, None


def _in_range(qs, field, start, end):
    if start:
        qs = qs.filter(**{f"{field}__date__gte": start})
    if end:
        qs = qs.filter(**{f"{field}__date__lte": end})
    return qs


def compute_quality_score(vendor, start=None, end=None):
    """Accepted quantity / received quantity across the vendor's GRN lines (accepted capped)."""
    lines = _in_range(
        ReceiptLine.objects.filter(receipt__po__vendor=vendor), "receipt__received_date", start, end
    )
    totals = lines.aggregate(
        received=Sum("quantity_received"),
        accepted=Sum(Least("quantity_accepted", "quantity_received")),
    )
    return _pct(totals["accepted"] or 0, totals["received"] or 0)


def compute_delivery_score(vendor, start=None, end=None):
    """
    Share of GRNs received on/before the expected date. Expected date = earliest PO delivery
    schedule date, falling back to the requisition's requested delivery date. GRNs with no
    reference date are excluded rather than assumed on time.
    """
    receipts = _in_range(
        GoodsReceipt.objects.filter(po__vendor=vendor).select_related("po__requisition"),
        "received_date",
        start,
        end,
    )
    receipts = receipts.annotate(expected=Min("po__delivery_schedules__expected_delivery_date"))
    evaluated = on_time = 0
    for receipt in receipts:
        expected = receipt.expected or (
            receipt.po.requisition.requested_delivery_date if receipt.po.requisition_id else None
        )
        if expected is None:
            continue
        evaluated += 1
        if timezone.localdate(receipt.received_date) <= expected:
            on_time += 1
    return _pct(on_time, evaluated)


def compute_price_score(vendor, start=None, end=None):
    """
    Price adherence: invoiced value vs PO value per PO (non-rejected invoices). Billing at or
    below PO value scores 100; over-billing reduces the score by the over-billed percentage.
    """
    from apps.invoices.models import SupplierInvoice
    from apps.orders.models import PurchaseOrder

    invoices = SupplierInvoice.objects.filter(vendor=vendor, po__isnull=False, po__vendor=vendor)
    if start:
        invoices = invoices.filter(invoice_date__gte=start)
    if end:
        invoices = invoices.filter(invoice_date__lte=end)
    invoiced = (
        invoices.exclude(status=SupplierInvoice.STATUS_REJECTED)
        .values("po_id")
        .annotate(total=Sum("total_amount"))
    )
    po_totals = dict(
        PurchaseOrder.objects.filter(
            id__in=[row["po_id"] for row in invoiced], vendor=vendor
        ).values_list("id", "total_amount")
    )
    scores = []
    for row in invoiced:
        po_total = po_totals.get(row["po_id"])
        if not po_total:
            continue
        over = max(Decimal("0"), row["total"] - po_total) / po_total * HUNDRED
        scores.append(max(Decimal("0"), HUNDRED - over))
    if not scores:
        return None
    return round(sum(scores) / len(scores), 2)


def compute_responsiveness_score(vendor, start=None, end=None):
    """
    Average of (a) RFQ/RFP invitation response rate for events whose bid window has closed and
    (b) PO acknowledgement rate for POs issued to the vendor.
    """
    from apps.orders.models import PurchaseOrder
    from apps.sourcing.models import BidInvite, SourcingEvent

    closed_invites = _in_range(
        BidInvite.objects.filter(vendor=vendor).exclude(
            event__status__in=[
                SourcingEvent.STATUS_DRAFT,
                SourcingEvent.STATUS_PUBLISHED,
                SourcingEvent.STATUS_BID_WINDOW,
            ]
        ),
        "created_at",
        start,
        end,
    )
    components = []
    invite_rate = _pct(closed_invites.filter(is_responded=True).count(), closed_invites.count())
    if invite_rate is not None:
        components.append(invite_rate)

    issued_pos = _in_range(
        PurchaseOrder.objects.filter(vendor=vendor).exclude(
            status__in=[
                PurchaseOrder.STATUS_DRAFT,
                PurchaseOrder.STATUS_APPROVAL,
                PurchaseOrder.STATUS_CANCELLED,
            ]
        ),
        "created_at",
        start,
        end,
    )
    ack_rate = _pct(issued_pos.filter(acknowledged_at__isnull=False).count(), issued_pos.count())
    if ack_rate is not None:
        components.append(ack_rate)

    if not components:
        return None
    return round(sum(components) / len(components), 2)


def compute_sla_score(vendor, start=None, end=None):
    """
    Vendor-owned contract obligations already due: share fulfilled on/before the due date.
    Unfulfilled obligations count as missed only once the due date has passed.
    """
    from apps.contracts.models import ContractObligation

    today = timezone.localdate()
    due = ContractObligation.objects.filter(
        contract__vendor=vendor, responsible_party__iexact="VENDOR", due_date__lte=today
    )
    if start:
        due = due.filter(due_date__gte=start)
    if end:
        due = due.filter(due_date__lte=end)
    total = met = 0
    for ob in due.only("due_date", "is_fulfilled", "fulfilled_at"):
        if not ob.is_fulfilled and ob.due_date >= today:
            continue  # due today and still open: not yet missed
        total += 1
        if ob.is_fulfilled and (
            ob.fulfilled_at is None or timezone.localdate(ob.fulfilled_at) <= ob.due_date
        ):
            met += 1
    return _pct(met, total)


def compute_compliance_score(vendor, start=None, end=None):
    """
    Governance status baseline (Active 100 / On hold 60 / Suspended 0 / other 80), minus 15 per
    expired KYC document, 5 per rejected document and 20/10 for a HIGH/MEDIUM latest risk
    assessment. A blacklisted vendor scores 0. Bounded to 0-100 (point-in-time, not period-bound).
    """
    if vendor.is_blacklisted:
        return Decimal("0.00")
    score = COMPLIANCE_BY_STATUS.get(vendor.status, COMPLIANCE_DEFAULT)
    today = timezone.localdate()
    docs = vendor.documents.all()
    expired_q = Q(status="EXPIRED") | Q(expiry_date__lt=today)
    expired = docs.filter(expired_q).count()
    rejected = docs.filter(status="REJECTED").exclude(expired_q).count()
    score -= EXPIRED_DOC_PENALTY * expired + REJECTED_DOC_PENALTY * rejected
    latest_risk = vendor.risk_records.order_by("-created_at").first()
    if latest_risk:
        score -= RISK_PENALTY.get(latest_risk.risk_level, Decimal("0"))
    return max(Decimal("0.00"), min(HUNDRED, score))


@transaction.atomic
def calculate_vendor_scorecard_service(
    *,
    vendor: Vendor,
    evaluation_period: str,
    evaluated_by_user,
    comments: str = "",
) -> VendorScorecard:
    """
    Computes a supplier performance scorecard from transactional indicators (PRD 3.3:
    delivery, quality, price, responsiveness, compliance and SLA). An indicator with no
    underlying data is stored as NULL — never a fabricated score. Each run is a new record,
    so trend history is preserved.
    """
    evaluation_period = (evaluation_period or "").strip()
    if not evaluation_period:
        from django.core.exceptions import ValidationError

        raise ValidationError("Evaluation period is required.")

    start, end = resolve_period(evaluation_period)
    weights = get_weights(vendor.organization)
    scorecard = VendorScorecard.objects.create(
        vendor=vendor,
        evaluation_period=evaluation_period,
        period_start=start,
        period_end=end,
        weights_used={d: str(w) for d, w in weights.items()},
        delivery_score=compute_delivery_score(vendor, start, end),
        quality_score=compute_quality_score(vendor, start, end),
        price_score=compute_price_score(vendor, start, end),
        responsiveness_score=compute_responsiveness_score(vendor, start, end),
        compliance_score=compute_compliance_score(vendor, start, end),
        sla_score=compute_sla_score(vendor, start, end),
        evaluator_comments=comments
        or f"Automated performance evaluation for period {evaluation_period}",
        evaluated_by=evaluated_by_user,
        created_by=evaluated_by_user,
    )

    def _s(value):
        return None if value is None else str(value)

    create_audit_log_service(
        actor=evaluated_by_user,
        action=AuditLog.ACTION_CREATE,
        target_model="VendorScorecard",
        target_object_id=scorecard.id,
        new_state={
            "vendor": vendor.legal_name,
            "period": scorecard.evaluation_period,
            "composite_score": _s(scorecard.composite_score),
            "delivery_score": _s(scorecard.delivery_score),
            "quality_score": _s(scorecard.quality_score),
            "price_score": _s(scorecard.price_score),
            "responsiveness_score": _s(scorecard.responsiveness_score),
            "compliance_score": _s(scorecard.compliance_score),
            "sla_score": _s(scorecard.sla_score),
            "rating_band": scorecard.rating_band,
            "weights_used": scorecard.weights_used,
        },
    )

    return scorecard


def latest_scorecards_queryset():
    """One row per vendor: the most recent scorecard (PostgreSQL DISTINCT ON or DB fallback)."""
    from django.db import connection

    if connection.vendor == "postgresql":
        ids = (
            VendorScorecard.objects.order_by("vendor_id", "-created_at")
            .distinct("vendor_id")
            .values("id")
        )
    else:
        ids = (
            VendorScorecard.objects.values("vendor_id")
            .annotate(latest_id=models.Max("id"))
            .values("latest_id")
        )
    return VendorScorecard.objects.filter(id__in=models.Subquery(ids)).select_related(
        "vendor", "vendor__category"
    )


def current_period_label(today=None):
    today = today or timezone.localdate()
    return f"Q{(today.month - 1) // 3 + 1}-{today.year}"


def recalculate_scorecards_for_organization(organization, *, period=None, actor=None):
    """
    Computes a scorecard for every ACTIVE vendor of the organization for the period. Scheduled
    runs are idempotent per vendor + period (an existing "[Auto]" card is not duplicated).
    Returns the number of scorecards created.
    """
    from apps.accounts.models import User

    period = period or current_period_label()
    actor = (
        actor
        or User.objects.filter(is_active=True, organization=organization).first()
        or User.objects.filter(is_active=True, is_superuser=True).first()
    )
    if actor is None:
        return 0
    created = 0
    for vendor in Vendor.objects.filter(organization=organization, status=Vendor.STATUS_ACTIVE):
        if VendorScorecard.objects.filter(
            vendor=vendor, evaluation_period=period, evaluator_comments__startswith="[Auto]"
        ).exists():
            continue
        calculate_vendor_scorecard_service(
            vendor=vendor,
            evaluation_period=period,
            evaluated_by_user=actor,
            comments=f"[Auto] Scheduled performance evaluation for period {period}",
        )
        created += 1
    return created

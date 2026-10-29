from decimal import Decimal

from django.db import models
from django.utils import timezone

from apps.core.models import TimeStampedModel


class GoodsReceipt(TimeStampedModel):
    grn_number = models.CharField(max_length=50, unique=True)
    po = models.ForeignKey(
        "orders.PurchaseOrder", on_delete=models.PROTECT, related_name="receipts"
    )
    received_by = models.ForeignKey(
        "accounts.User", on_delete=models.PROTECT, related_name="received_grns"
    )
    received_date = models.DateTimeField()
    delivery_note_number = models.CharField(max_length=100, blank=True)
    remarks = models.TextField(blank=True)

    def __str__(self):
        return f"{self.grn_number} for {self.po.po_number}"

    @property
    def overall_inspection_status(self):
        lines = list(self.lines.all())
        if not lines:
            return "PENDING"
        statuses = [line.inspection_status for line in lines]
        if all(s == "PASSED" for s in statuses):
            return "PASSED"
        if any(s in ["FAILED", "REJECTED"] for s in statuses):
            return "FAILED"
        if any(s == "PASSED" for s in statuses):
            return "PARTIALLY_INSPECTED"
        return "PENDING"

    @property
    def stock_handoff_status(self):
        lines = list(self.lines.all())
        if not lines:
            return "NO_STOCK"
        accepted_lines = [line for line in lines if line.quantity_accepted > Decimal("0.00")]
        if not accepted_lines:
            return "ALL_REJECTED"
        handed_off_count = sum(1 for line in accepted_lines if line.is_handed_off)
        if handed_off_count == len(accepted_lines):
            return "HANDED_OFF"
        if handed_off_count > 0:
            return "PARTIALLY_HANDED_OFF"
        return "PENDING_HANDOFF"

    @property
    def stock_handoff_status_display(self):
        status_map = {
            "HANDED_OFF": "Handed Off to Stock",
            "PARTIALLY_HANDED_OFF": "Partially Handed Off",
            "PENDING_HANDOFF": "Pending Stock Handoff",
            "ALL_REJECTED": "All Items Rejected",
            "NO_STOCK": "No Stock",
        }
        return status_map.get(self.stock_handoff_status, "Pending Stock Handoff")

    @property
    def can_hand_off_to_stock(self):
        accepted_lines = [
            line for line in self.lines.all() if line.quantity_accepted > Decimal("0.00")
        ]
        return len(accepted_lines) > 0 and self.stock_handoff_status != "HANDED_OFF"

    @property
    def latest_acknowledgement(self):
        return self.acknowledgements.order_by("-version").first()


class ReceiptLine(TimeStampedModel):
    receipt = models.ForeignKey(GoodsReceipt, on_delete=models.CASCADE, related_name="lines")
    po_line = models.ForeignKey(
        "orders.POLine", on_delete=models.PROTECT, related_name="receipt_lines"
    )
    quantity_received = models.DecimalField(max_digits=12, decimal_places=2)
    quantity_accepted = models.DecimalField(max_digits=12, decimal_places=2)
    quantity_rejected = models.DecimalField(
        max_digits=12, decimal_places=2, default=Decimal("0.00")
    )
    notes = models.CharField(max_length=255, blank=True)

    def save(self, *args, **kwargs):
        self.quantity_rejected = self.quantity_received - self.quantity_accepted
        super().save(*args, **kwargs)

    @property
    def inspection_status(self):
        try:
            insp = self.inspection
        except Exception:
            insp = None
        if self.quantity_rejected > Decimal("0.00") or self.rejections.exists():
            return "REJECTED"
        if not insp:
            return "PENDING"
        if insp.passed:
            return "PASSED"
        return "FAILED"

    @property
    def inspection_status_display(self):
        status_map = {
            "PENDING": "Pending Inspection",
            "PASSED": "Passed",
            "FAILED": "Failed",
            "REJECTED": "Rejected",
            "PARTIALLY_INSPECTED": "Partially Inspected",
        }
        return status_map.get(self.inspection_status, "Pending Inspection")

    @property
    def latest_rejection(self):
        return self.rejections.order_by("-created_at").first()

    @property
    def is_handed_off(self):
        try:
            return self.stock_handoff is not None
        except Exception:
            return False

    @property
    def stock_handoff_record(self):
        try:
            return self.stock_handoff
        except Exception:
            return None

    def __str__(self):
        return f"GRN Line: Recv {self.quantity_received}, Accept {self.quantity_accepted}, Reject {self.quantity_rejected}"


class InspectionRecord(TimeStampedModel):
    receipt_line = models.OneToOneField(
        ReceiptLine, on_delete=models.CASCADE, related_name="inspection"
    )
    inspected_by = models.ForeignKey(
        "accounts.User", on_delete=models.PROTECT, related_name="inspections"
    )
    passed = models.BooleanField(default=True)
    inspection_notes = models.TextField()

    def __str__(self):
        return f"Inspection {'PASSED' if self.passed else 'FAILED'} by {self.inspected_by.email}"


class RejectionRecord(TimeStampedModel):
    receipt_line = models.ForeignKey(
        ReceiptLine, on_delete=models.CASCADE, related_name="rejections"
    )
    rejected_quantity = models.DecimalField(max_digits=12, decimal_places=2)
    rejection_reason = models.TextField()
    returned_to_vendor = models.BooleanField(default=False)

    def __str__(self):
        return f"Rejection: {self.rejected_quantity} units - {self.rejection_reason[:50]}"


class StockHandoffRecord(TimeStampedModel):
    """
    Formal record of accepted GRN materials handed over into warehouse inventory/stock.
    Provides complete end-to-end traceability from PO -> Receipt -> Inspection -> Stock.
    """

    receipt_line = models.OneToOneField(
        ReceiptLine, on_delete=models.CASCADE, related_name="stock_handoff"
    )
    handed_off_by = models.ForeignKey(
        "accounts.User", on_delete=models.PROTECT, related_name="stock_handoffs"
    )
    quantity_handed_off = models.DecimalField(max_digits=12, decimal_places=2)
    storage_location = models.CharField(max_length=100, default="MAIN-WH")
    handoff_notes = models.TextField(blank=True, default="")
    handed_off_at = models.DateTimeField(auto_now_add=True)

    def __str__(self):
        return f"Stock Handoff: {self.quantity_handed_off} units to {self.storage_location} ({self.receipt_line.receipt.grn_number})"


class GRNAcknowledgement(TimeStampedModel):
    """
    Traceable Goods Receipt Note (GRN) Acknowledgement generated after quality inspection.
    Maintains full version history, outcome status (full, partial, rejection), recipient delivery status,
    and idempotency fingerprint to prevent duplicate events.
    """

    OUTCOME_FULL = "FULL_ACCEPTANCE"
    OUTCOME_PARTIAL = "PARTIAL_ACCEPTANCE"
    OUTCOME_REJECTION = "REJECTION"

    OUTCOME_CHOICES = [
        (OUTCOME_FULL, "Full Acceptance"),
        (OUTCOME_PARTIAL, "Partial Acceptance"),
        (OUTCOME_REJECTION, "Quality Rejection"),
    ]

    STATUS_PENDING = "PENDING"
    STATUS_SENT = "SENT"
    STATUS_PARTIAL = "PARTIALLY_SENT"
    STATUS_FAILED = "FAILED"
    STATUS_NO_EMAIL = "NO_RECIPIENTS"

    STATUS_CHOICES = [
        (STATUS_PENDING, "Pending Delivery"),
        (STATUS_SENT, "Sent Successfully"),
        (STATUS_PARTIAL, "Partially Sent"),
        (STATUS_FAILED, "Delivery Failed"),
        (STATUS_NO_EMAIL, "Vendor Email Unavailable"),
    ]

    receipt = models.ForeignKey(
        GoodsReceipt, on_delete=models.CASCADE, related_name="acknowledgements"
    )
    acknowledgement_number = models.CharField(max_length=60, unique=True, db_index=True)
    version = models.PositiveIntegerField(default=1)
    outcome = models.CharField(max_length=30, choices=OUTCOME_CHOICES, db_index=True)
    inspection_fingerprint = models.CharField(max_length=64, db_index=True)
    idempotency_key = models.CharField(max_length=255, unique=True, db_index=True)

    generated_by = models.ForeignKey(
        "accounts.User", on_delete=models.PROTECT, related_name="generated_acknowledgements"
    )
    generated_at = models.DateTimeField(default=timezone.now)

    total_received_qty = models.DecimalField(
        max_digits=12, decimal_places=2, default=Decimal("0.00")
    )
    total_accepted_qty = models.DecimalField(
        max_digits=12, decimal_places=2, default=Decimal("0.00")
    )
    total_rejected_qty = models.DecimalField(
        max_digits=12, decimal_places=2, default=Decimal("0.00")
    )

    # Tracking delivery
    delivery_status = models.CharField(
        max_length=30, choices=STATUS_CHOICES, default=STATUS_PENDING, db_index=True
    )
    delivery_error = models.TextField(blank=True, default="")
    delivered_at = models.DateTimeField(null=True, blank=True)
    recipient_summary = models.JSONField(default=dict, blank=True)
    in_app_notified_count = models.PositiveIntegerField(default=0)
    email_sent_count = models.PositiveIntegerField(default=0)

    # Document details
    document_notes = models.TextField(blank=True, default="")
    pdf_file_path = models.CharField(max_length=500, blank=True, default="")

    prior_acknowledgement = models.ForeignKey(
        "self", on_delete=models.SET_NULL, null=True, blank=True, related_name="revisions"
    )

    class Meta:
        ordering = ["-version", "-created_at"]
        indexes = [
            models.Index(fields=["receipt", "-version"], name="grn_ack_receipt_ver_idx"),
            models.Index(fields=["outcome", "delivery_status"], name="grn_ack_outc_stat_idx"),
        ]

    def __str__(self):
        return (
            f"{self.acknowledgement_number} ({self.get_outcome_display()}) - {self.receipt.grn_number}"
        )

    @property
    def has_rejections(self) -> bool:
        return self.total_rejected_qty > Decimal("0.00")

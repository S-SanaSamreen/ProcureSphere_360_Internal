from decimal import Decimal

from django.core.exceptions import ValidationError
from django.db import models

from apps.core.models import TimeStampedModel


class Budget(TimeStampedModel):
    cost_center = models.ForeignKey(
        "organization.CostCenter", on_delete=models.CASCADE, related_name="budgets"
    )
    fiscal_period = models.ForeignKey(
        "organization.FiscalPeriod", on_delete=models.CASCADE, related_name="budgets"
    )

    allocated_amount = models.DecimalField(max_digits=14, decimal_places=2)
    reserved_amount = models.DecimalField(max_digits=14, decimal_places=2, default=Decimal("0.00"))
    committed_amount = models.DecimalField(max_digits=14, decimal_places=2, default=Decimal("0.00"))
    actual_amount = models.DecimalField(max_digits=14, decimal_places=2, default=Decimal("0.00"))

    allow_overspend = models.BooleanField(default=False)

    class Meta:
        unique_together = ("cost_center", "fiscal_period")

    def __str__(self):
        return f"Budget: {self.cost_center.code} [{self.fiscal_period.name}] - Alloc: ${self.allocated_amount}"

    @property
    def available_amount(self):
        diff = self.allocated_amount - (
            self.reserved_amount + self.committed_amount + self.actual_amount
        )
        if not self.allow_overspend:
            return max(Decimal("0.00"), diff)
        return diff


class BudgetReservation(TimeStampedModel):
    STATUS_RESERVED = "RESERVED"
    STATUS_RELEASED = "RELEASED"
    STATUS_COMMITTED = "COMMITTED"

    STATUS_CHOICES = [
        (STATUS_RESERVED, "Reserved"),
        (STATUS_RELEASED, "Released"),
        (STATUS_COMMITTED, "Converted to Commitment"),
    ]

    budget = models.ForeignKey(Budget, on_delete=models.CASCADE, related_name="reservations")
    requisition = models.ForeignKey(
        "requisitions.PurchaseRequisition",
        on_delete=models.CASCADE,
        related_name="budget_reservations",
    )
    amount = models.DecimalField(max_digits=14, decimal_places=2)
    converted_amount = models.DecimalField(
        max_digits=14,
        decimal_places=2,
        default=Decimal("0.00"),
        help_text="Portion already converted into PO commitments (reservation is consumed, not double counted).",
    )
    status = models.CharField(max_length=30, choices=STATUS_CHOICES, default=STATUS_RESERVED)

    @property
    def open_amount(self):
        return self.amount - self.converted_amount

    def __str__(self):
        return f"Reservation: ${self.amount} for PR {self.requisition.pr_number} [{self.status}]"


class SpendLedgerQuerySet(models.QuerySet):
    """The ledger is append-only: corrections are new (signed) entries, never edits."""

    def update(self, **kwargs):
        raise ValidationError("Spend ledger entries are immutable and cannot be updated.")

    def delete(self):
        raise ValidationError("Spend ledger entries are immutable and cannot be deleted.")


class SpendLedger(TimeStampedModel):
    """
    Append-only record of every budget movement. Amounts are SIGNED per entry type:
    reserved = sum(RESERVATION), committed = sum(COMMITMENT), actual = sum(ACTUAL).
    The counters on Budget are a cached projection maintained only by
    budgets.services.post_ledger_entry_service and verifiable with reconcile_budget().
    """

    objects = SpendLedgerQuerySet.as_manager()

    ENTRY_RESERVATION = "RESERVATION"
    ENTRY_COMMITMENT = "COMMITMENT"
    ENTRY_ACTUAL = "ACTUAL"
    # Approved transfer of allocated budget between cost centers (signed: -source / +destination).
    ENTRY_TRANSFER = "TRANSFER"

    ENTRY_CHOICES = [
        (ENTRY_RESERVATION, "Budget Reservation"),
        (ENTRY_COMMITMENT, "PO Commitment"),
        (ENTRY_ACTUAL, "Invoice Actual Spend"),
        (ENTRY_TRANSFER, "Budget Transfer (allocation)"),
    ]

    budget = models.ForeignKey(Budget, on_delete=models.PROTECT, related_name="ledger_entries")
    entry_type = models.CharField(max_length=30, choices=ENTRY_CHOICES)
    amount = models.DecimalField(max_digits=14, decimal_places=2)
    reference_number = models.CharField(max_length=100)
    description = models.CharField(max_length=255)

    def save(self, *args, **kwargs):
        if not self._state.adding:
            raise ValidationError("Spend ledger entries are immutable and cannot be modified.")
        super().save(*args, **kwargs)

    def delete(self, *args, **kwargs):
        raise ValidationError("Spend ledger entries are immutable and cannot be deleted.")

    def __str__(self):
        return f"{self.entry_type}: ${self.amount} ({self.reference_number})"


class BudgetTransfer(TimeStampedModel):
    """
    Request to move allocated budget between two cost centers of the same organization and
    fiscal period. Takes effect only when the approval engine approves it; the movement is then
    posted as two signed TRANSFER ledger entries.
    """

    STATUS_PENDING = "PENDING"
    STATUS_APPROVED = "APPROVED"
    STATUS_REJECTED = "REJECTED"
    STATUS_CHOICES = [
        (STATUS_PENDING, "Pending Approval"),
        (STATUS_APPROVED, "Approved"),
        (STATUS_REJECTED, "Rejected"),
    ]

    source = models.ForeignKey(Budget, on_delete=models.PROTECT, related_name="transfers_out")
    destination = models.ForeignKey(Budget, on_delete=models.PROTECT, related_name="transfers_in")
    amount = models.DecimalField(max_digits=14, decimal_places=2)
    reason = models.TextField()
    status = models.CharField(
        max_length=20, choices=STATUS_CHOICES, default=STATUS_PENDING, db_index=True
    )
    requested_by = models.ForeignKey(
        "accounts.User", on_delete=models.PROTECT, related_name="budget_transfers_requested"
    )
    decided_by = models.ForeignKey(
        "accounts.User",
        on_delete=models.PROTECT,
        null=True,
        blank=True,
        related_name="budget_transfers_decided",
    )
    decided_at = models.DateTimeField(null=True, blank=True)
    decision_comments = models.TextField(blank=True, default="")

    class Meta:
        ordering = ["-created_at"]

    def __str__(self):
        return f"Transfer {self.amount}: {self.source.cost_center.code} -> {self.destination.cost_center.code} [{self.status}]"

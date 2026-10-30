import uuid
from decimal import Decimal

from django.core.exceptions import ValidationError
from django.db import models
from django.db.models import Q

from apps.core.models import TimeStampedModel


class ApprovalPolicy(TimeStampedModel):
    MODULE_PR = "PR"
    MODULE_PO = "PO"
    MODULE_SOURCING = "SOURCING"
    MODULE_INVOICE = "INVOICE"
    MODULE_VENDOR = "VENDOR"
    MODULE_PO_AMENDMENT = "PO_AMENDMENT"
    MODULE_CONTRACT = "CONTRACT"
    MODULE_BUDGET_EXCEPTION = "BUDGET_EXCEPTION"

    MODULE_CHOICES = [
        (MODULE_PR, "Purchase Requisition"),
        (MODULE_PO, "Purchase Order"),
        (MODULE_SOURCING, "Sourcing Award"),
        (MODULE_INVOICE, "Invoice Match Exception"),
        (MODULE_VENDOR, "Vendor KYC Approval"),

        (MODULE_PO_AMENDMENT, "Purchase Order Amendment"),
        (MODULE_CONTRACT, "Contract Approval"),

        (MODULE_BUDGET_EXCEPTION, "Budget Exception"),
    ]

    name = models.CharField(max_length=200)
    module = models.CharField(max_length=50, choices=MODULE_CHOICES)
    department = models.ForeignKey(
        "organization.Department",
        on_delete=models.CASCADE,
        null=True,
        blank=True,
        related_name="approval_policies",
    )
    cost_center = models.ForeignKey(
        "organization.CostCenter",
        on_delete=models.CASCADE,
        null=True,
        blank=True,
        related_name="approval_policies",
    )

    min_amount = models.DecimalField(
        max_digits=14, decimal_places=2, default=Decimal("0.00")
    )
    max_amount = models.DecimalField(
        max_digits=14, decimal_places=2, null=True, blank=True
    )

    is_active = models.BooleanField(default=True)

    # Versioning: a policy is never edited in place. A change creates the next version (same
    # policy_key) and supersedes the old row, so every request keeps the exact rules it was
    # routed under.
    policy_key = models.UUIDField(default=uuid.uuid4, db_index=True, editable=False)
    version = models.PositiveIntegerField(default=1)
    superseded_by = models.ForeignKey(
        "self", null=True, blank=True, on_delete=models.SET_NULL, related_name="supersedes"
    )
    change_note = models.CharField(max_length=255, blank=True)

    # Exception routing: a policy flagged as an exception route is never chosen as the normal
    # chain. When a request is raised as an exception (budget overrun, price above estimate,
    # non-lowest award ...), its steps are appended after the normal chain.
    is_exception_route = models.BooleanField(default=False)

    # Routing fields that must never change once a request was routed under this row. The only
    # permitted changes are retiring it (is_active), superseding it, and renaming.
    ROUTING_FIELDS = (
        "module",
        "min_amount",
        "max_amount",
        "department_id",
        "cost_center_id",
        "is_exception_route",
    )

    @property
    def is_in_use(self):
        return self.pk is not None and (
            self.requests.exists() or self.exception_requests.exists()
        )

    def save(self, *args, **kwargs):
        if not self._state.adding and self.is_in_use:
            stored = type(self).objects.get(pk=self.pk)
            changed = [f for f in self.ROUTING_FIELDS if getattr(stored, f) != getattr(self, f)]
            if changed:
                raise ValidationError(
                    "This policy version has already routed approval requests and cannot be "
                    f"edited in place ({', '.join(changed)}). Create a new version instead."
                )
        super().save(*args, **kwargs)

    def __str__(self):
        flag = " EXCEPTION" if self.is_exception_route else ""
        return f"{self.name} v{self.version} [{self.module}{flag}] (Min: ${self.min_amount})"


class ApprovalStep(TimeStampedModel):
    policy = models.ForeignKey(
        ApprovalPolicy, on_delete=models.CASCADE, related_name="steps"
    )
    step_number = models.PositiveIntegerField()
    approver_role = models.ForeignKey(
        "accounts.Role", on_delete=models.PROTECT, related_name="approval_steps"
    )
    specific_approver = models.ForeignKey(
        "accounts.User",
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="assigned_approval_steps",
    )
    description = models.CharField(max_length=255, blank=True)

    class Meta:
        ordering = ["step_number"]
        unique_together = ("policy", "step_number")

    def _guard_in_use(self):
        if self.policy_id and self.policy.is_in_use:
            raise ValidationError(
                "The steps of a policy version that has routed approval requests cannot be "
                "changed. Create a new version of the policy instead."
            )

    def save(self, *args, **kwargs):
        self._guard_in_use()
        super().save(*args, **kwargs)

    def delete(self, *args, **kwargs):
        self._guard_in_use()
        return super().delete(*args, **kwargs)

    def __str__(self):
        return (
            f"Step {self.step_number}: {self.approver_role.name} for {self.policy.name}"
        )


class ApprovalRequest(TimeStampedModel):
    """
    One approval cycle for one target object. Created when the target is submitted, bound to the
    policy VERSION selected at that moment, with a snapshot of its steps. Later policy edits,
    new versions or deactivation never re-route a request that is already in flight.
    """

    STATUS_PENDING = "PENDING"
    STATUS_APPROVED = "APPROVED"
    STATUS_REJECTED = "REJECTED"
    STATUS_CANCELLED = "CANCELLED"

    STATUS_CHOICES = [
        (STATUS_PENDING, "Pending"),
        (STATUS_APPROVED, "Approved"),
        (STATUS_REJECTED, "Rejected"),
        (STATUS_CANCELLED, "Cancelled"),
    ]

    module = models.CharField(max_length=50, choices=ApprovalPolicy.MODULE_CHOICES, db_index=True)
    target_model_name = models.CharField(max_length=100)
    target_object_id = models.UUIDField(db_index=True)
    policy = models.ForeignKey(ApprovalPolicy, on_delete=models.PROTECT, related_name="requests")
    exception_policy = models.ForeignKey(
        ApprovalPolicy,
        null=True,
        blank=True,
        on_delete=models.PROTECT,
        related_name="exception_requests",
    )
    amount = models.DecimalField(max_digits=14, decimal_places=2, default=Decimal("0.00"))
    department = models.ForeignKey(
        "organization.Department", null=True, blank=True, on_delete=models.SET_NULL, related_name="+"
    )
    cost_center = models.ForeignKey(
        "organization.CostCenter", null=True, blank=True, on_delete=models.SET_NULL, related_name="+"
    )
    exception_reason = models.TextField(blank=True)
    steps = models.JSONField(default=list)
    current_step = models.PositiveIntegerField(default=0)
    status = models.CharField(
        max_length=20, choices=STATUS_CHOICES, default=STATUS_PENDING, db_index=True
    )
    decided_at = models.DateTimeField(null=True, blank=True)
    # A granted budget exception is a one-time permission; consuming it stamps this field.
    consumed_at = models.DateTimeField(null=True, blank=True)

    class Meta:
        ordering = ["-created_at"]
        constraints = [
            models.UniqueConstraint(
                fields=["module", "target_model_name", "target_object_id"],
                condition=Q(status="PENDING"),
                name="uniq_pending_approval_request",
            )
        ]
        indexes = [models.Index(fields=["target_model_name", "target_object_id", "module"])]

    def __str__(self):
        return f"{self.module} request for {self.target_model_name} {self.target_object_id} [{self.status}]"

    @property
    def is_pending(self):
        return self.status == self.STATUS_PENDING

    @property
    def active_step(self):
        """The step awaiting a decision, or None when the request is decided."""
        if self.status != self.STATUS_PENDING or self.current_step >= len(self.steps):
            return None
        return self.steps[self.current_step]


class ApprovalAction(TimeStampedModel):
    ACTION_SUBMIT = "SUBMITTED"
    ACTION_APPROVE = "APPROVED"
    ACTION_REJECT = "REJECTED"
    ACTION_DELEGATE = "DELEGATED"
    ACTION_COMMENT = "COMMENTED"

    ACTION_CHOICES = [
        (ACTION_SUBMIT, "Submitted"),
        (ACTION_APPROVE, "Approved"),
        (ACTION_REJECT, "Rejected"),
        (ACTION_DELEGATE, "Delegated"),
        (ACTION_COMMENT, "Commented"),
    ]

    approval_request = models.ForeignKey(
        ApprovalRequest, null=True, blank=True, on_delete=models.SET_NULL, related_name="actions"
    )

    policy_step = models.ForeignKey(
        ApprovalStep, on_delete=models.SET_NULL, null=True, blank=True
    )
    target_object_id = models.UUIDField(db_index=True)
    target_model_name = models.CharField(max_length=100)
    actor = models.ForeignKey(
        "accounts.User", on_delete=models.PROTECT, related_name="approval_actions"
    )
    action = models.CharField(max_length=50, choices=ACTION_CHOICES)
    comments = models.TextField(blank=True)
    previous_state = models.CharField(max_length=100)
    new_state = models.CharField(max_length=100)

    def __str__(self):
        return f"{self.action} by {self.actor.email} on {self.target_model_name} ({self.target_object_id})"


class ApprovalDelegate(TimeStampedModel):
    approver = models.ForeignKey(
        "accounts.User", on_delete=models.CASCADE, related_name="delegator_records"
    )
    delegate = models.ForeignKey(
        "accounts.User", on_delete=models.CASCADE, related_name="delegated_records"
    )
    start_date = models.DateField()
    end_date = models.DateField()
    is_active = models.BooleanField(default=True)
    reason = models.TextField(blank=True)

    def __str__(self):
        return f"Delegate {self.delegate.email} for {self.approver.email} ({self.start_date} to {self.end_date})"

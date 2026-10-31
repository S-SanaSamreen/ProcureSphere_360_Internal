from decimal import Decimal

from django.db import migrations, models

MODULE_CHOICES = [
    ("PR", "Purchase Requisition"),
    ("PO", "Purchase Order"),
    ("SOURCING", "Sourcing Award"),
    ("INVOICE", "Invoice Match Exception"),
    ("VENDOR", "Vendor KYC Approval"),
    ("PO_AMENDMENT", "Purchase Order Amendment"),
    ("CONTRACT", "Contract Approval"),
    ("BUDGET_EXCEPTION", "Budget Exception"),
    ("BUDGET_TRANSFER", "Budget Transfer"),
]


def seed(apps, schema_editor):
    ApprovalPolicy = apps.get_model("approvals", "ApprovalPolicy")
    ApprovalStep = apps.get_model("approvals", "ApprovalStep")
    Role = apps.get_model("accounts", "Role")
    role, _ = Role.objects.get_or_create(code="PROC_MGR", defaults={"name": "Procurement Manager"})
    policy = ApprovalPolicy.objects.filter(
        module="BUDGET_TRANSFER",
        department__isnull=True,
        cost_center__isnull=True,
        is_exception_route=False,
    ).first()
    if not policy:
        policy = ApprovalPolicy.objects.create(
            name="Budget Transfer Approval - Global",
            module="BUDGET_TRANSFER",
            min_amount=Decimal("0.00"),
            max_amount=None,
            is_active=True,
        )
    if not policy.steps.filter(step_number=1).exists():
        ApprovalStep.objects.create(
            policy=policy,
            step_number=1,
            approver_role=role,
            description="Procurement Manager Approval",
        )


class Migration(migrations.Migration):

    dependencies = [
        ("approvals", "0007_pr_default_finance_budget_review"),
        ("accounts", "0008_approval_engine"),
    ]

    operations = [
        migrations.AlterField(
            model_name="approvalpolicy",
            name="module",
            field=models.CharField(choices=MODULE_CHOICES, max_length=50),
        ),
        migrations.AlterField(
            model_name="approvalrequest",
            name="module",
            field=models.CharField(choices=MODULE_CHOICES, db_index=True, max_length=50),
        ),
        migrations.RunPython(seed, migrations.RunPython.noop),
    ]

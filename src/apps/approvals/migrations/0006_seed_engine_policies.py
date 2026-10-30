from decimal import Decimal

from django.db import migrations

# Modules whose approval used to be a hard-coded role check. Seeding a global one-step policy for
# each reproduces today's behaviour exactly, but as configuration an administrator can version
# (cost center / department / amount tiers, extra steps, exception routes).
#   module            role          description
DEFAULTS = [
    ("SOURCING", "PROC_MGR", "Procurement Manager", "Sourcing Award Approval — Global"),
    ("CONTRACT", "PROC_MGR", "Procurement Manager", "Contract Business Approval — Global"),
    (
        "INVOICE",
        "FINANCE_AP",
        "Finance / AP Specialist",
        "Invoice Match Exception Resolution — Global",
    ),
    (
        "BUDGET_EXCEPTION",
        "FINANCE_AP",
        "Finance / AP Specialist",
        "Budget Exception Approval — Global",
    ),
]


def seed(apps, schema_editor):
    ApprovalPolicy = apps.get_model("approvals", "ApprovalPolicy")
    ApprovalStep = apps.get_model("approvals", "ApprovalStep")
    Role = apps.get_model("accounts", "Role")

    for module, role_code, role_name, policy_name in DEFAULTS:
        role, _ = Role.objects.get_or_create(code=role_code, defaults={"name": role_name})
        policy = ApprovalPolicy.objects.filter(
            module=module,
            department__isnull=True,
            cost_center__isnull=True,
            is_exception_route=False,
        ).first()
        if not policy:
            policy = ApprovalPolicy.objects.create(
                name=policy_name,
                module=module,
                department=None,
                cost_center=None,
                min_amount=Decimal("0.00"),
                max_amount=None,
                is_active=True,
            )
        if not policy.steps.filter(step_number=1).exists():
            ApprovalStep.objects.create(
                policy=policy,
                step_number=1,
                approver_role=role,
                description=role_name + " Approval",
            )


class Migration(migrations.Migration):

    dependencies = [
        ("approvals", "0005_approval_engine"),
        ("accounts", "0008_approval_engine"),
    ]

    operations = [migrations.RunPython(seed, migrations.RunPython.noop)]

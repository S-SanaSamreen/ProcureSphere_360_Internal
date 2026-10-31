from django.db import migrations


def switch_default_pr_chain(apps, schema_editor):
    """
    PRD 4.2: the default PR chain is Department Approver -> Finance/AP (Budget Review), not
    Procurement Manager. Only the untouched seeded global policy is changed: edited in place if
    no request was ever routed under it, otherwise superseded by a new version so in-flight
    requests keep the steps they were routed under.
    """
    ApprovalPolicy = apps.get_model("approvals", "ApprovalPolicy")
    ApprovalStep = apps.get_model("approvals", "ApprovalStep")
    Role = apps.get_model("accounts", "Role")

    finance, _ = Role.objects.get_or_create(
        code="FINANCE_AP", defaults={"name": "Finance / AP Specialist"}
    )
    policies = ApprovalPolicy.objects.filter(
        module="PR",
        name="Standard PR Approval — Global",
        department__isnull=True,
        cost_center__isnull=True,
        is_active=True,
        superseded_by__isnull=True,
    )
    for policy in policies:
        steps = list(policy.steps.select_related("approver_role").order_by("step_number"))
        if [s.approver_role.code for s in steps] != ["DEPT_APPROVER", "PROC_MGR"]:
            continue  # customised by an administrator: leave it alone
        if not (policy.requests.exists() or policy.exception_requests.exists()):
            steps[1].approver_role = finance
            steps[1].description = "Budget Review"
            steps[1].save()
            continue
        new = ApprovalPolicy.objects.create(
            name=policy.name,
            module=policy.module,
            min_amount=policy.min_amount,
            max_amount=policy.max_amount,
            is_active=True,
            policy_key=policy.policy_key,
            version=policy.version + 1,
            change_note="PRD 4.2: default PR chain is Department Approver then Finance/AP",
        )
        ApprovalStep.objects.create(
            policy=new,
            step_number=1,
            approver_role=steps[0].approver_role,
            description=steps[0].description,
        )
        ApprovalStep.objects.create(
            policy=new, step_number=2, approver_role=finance, description="Budget Review"
        )
        policy.superseded_by = new
        policy.is_active = False
        policy.save(update_fields=["superseded_by", "is_active"])


class Migration(migrations.Migration):

    dependencies = [("approvals", "0006_seed_engine_policies")]

    operations = [migrations.RunPython(switch_default_pr_chain, migrations.RunPython.noop)]

# Generated for ProcureSphere 360 multi-organization isolation

from django.db import migrations, models
import django.db.models.deletion


def populate_user_organizations(apps, schema_editor):
    User = apps.get_model("accounts", "User")
    Organization = apps.get_model("organization", "Organization")

    hpe_org = Organization.objects.filter(code="HPE-GLOBAL").first()
    vpd_org = Organization.objects.filter(code="VPD-01").first()

    for user in User.objects.all():
        if user.department_id:
            dept = getattr(user, "department", None)
            if dept and getattr(dept, "organization_id", None):
                user.organization_id = dept.organization_id
                user.save(update_fields=["organization"])
                continue

        email = (user.email or "").lower()
        if hpe_org and (email.endswith("@hpe.com") or email.endswith("@procuresphere.com") or email.endswith("@procuresphere.local")):
            user.organization_id = hpe_org.id
            user.save(update_fields=["organization"])
        elif vpd_org and ("vpd" in email or "john" in email):
            user.organization_id = vpd_org.id
            user.save(update_fields=["organization"])
        elif hpe_org and not user.organization_id:
            user.organization_id = hpe_org.id
            user.save(update_fields=["organization"])


def reverse_populate(apps, schema_editor):
    pass


class Migration(migrations.Migration):

    dependencies = [
        ("organization", "0001_initial"),
        ("accounts", "0005_alter_user_must_change_password"),
    ]

    operations = [
        migrations.AddField(
            model_name="user",
            name="organization",
            field=models.ForeignKey(
                blank=True,
                help_text="Organization / legal entity this user belongs to.",
                null=True,
                on_delete=django.db.models.deletion.SET_NULL,
                related_name="users",
                to="organization.organization",
            ),
        ),
        migrations.RunPython(populate_user_organizations, reverse_populate),
    ]

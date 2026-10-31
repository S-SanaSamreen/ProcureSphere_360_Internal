"""Platform tenancy rules: one platform superuser, one Super Admin per organization, and a
Django admin that does not expose tenant business data."""

import pytest
from django.core.exceptions import ValidationError
from django.test import override_settings
from django.urls import reverse

from apps.accounts.models import Role, User
from apps.organization.models import Organization

pytestmark = pytest.mark.django_db


def _super_admin(email, org, roles, **extra):
    return User.objects.create_user(
        email=email,
        password="Password123!",
        role=roles[Role.SUPER_ADMIN],
        organization=org,
        **extra,
    )


def test_each_organization_has_exactly_one_super_admin(db_roles):
    org_a = Organization.objects.create(name="A", code="A-1")
    org_b = Organization.objects.create(name="B", code="B-1")
    _super_admin("a1@a.test", org_a, db_roles)
    with pytest.raises(ValidationError, match="already has a Super Admin"):
        _super_admin("a2@a.test", org_a, db_roles)
    # another organization gets its own Super Admin independently
    _super_admin("b1@b.test", org_b, db_roles)


def test_inactive_super_admin_can_be_replaced(db_roles):
    org = Organization.objects.create(name="A", code="A-1")
    old = _super_admin("old@a.test", org, db_roles)
    old.is_active = False
    old.save()
    _super_admin("new@a.test", org, db_roles)


def test_routine_saves_never_trip_the_guard(db_roles):
    org = Organization.objects.create(name="A", code="A-1")
    admin = _super_admin("a1@a.test", org, db_roles)
    admin.save(update_fields=["last_login"])  # what a login does


@override_settings(ENFORCE_SINGLE_PLATFORM_SUPERUSER=True)
def test_only_one_platform_superuser(db_roles):
    User.objects.create_superuser(email="root@x.test", password="Password123!")
    with pytest.raises(ValidationError, match="only one platform administrator"):
        User.objects.create_superuser(email="root2@x.test", password="Password123!")


def test_django_admin_hides_tenant_business_data_from_the_superuser(client, super_admin_user):
    client.force_login(super_admin_user)
    index = client.get(reverse("admin:index")).content.decode()
    assert "Organizations" in index or "Organization" in index  # platform concerns stay
    assert client.get(reverse("admin:vendors_vendor_changelist")).status_code == 403
    assert client.get(reverse("admin:orders_purchaseorder_changelist")).status_code == 403
    assert "/admin/vendors/" not in index and "/admin/orders/" not in index


def test_internal_users_must_belong_to_an_organization(db_roles):
    org = Organization.objects.create(name="A", code="A-1")
    orphan = User(email="req@a.test", role=db_roles[Role.REQUESTER])
    with pytest.raises(ValidationError, match="must belong to an organization"):
        orphan.clean()
    orphan.organization = org
    orphan.clean()  # fine once attached
    # vendors and the platform superuser are the only exceptions
    User(email="v@x.test", role=db_roles[Role.VENDOR_USER]).clean()
    User(email="root@x.test", role=db_roles[Role.SUPER_ADMIN], is_superuser=True).clean()

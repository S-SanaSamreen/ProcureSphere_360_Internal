import pytest
from django.test import Client
from apps.accounts.models import Role, User
from apps.organization.models import Organization, Department, CostCenter


@pytest.fixture
def superadmin_setup(db):
    org = Organization.objects.create(name="Hewlett Packard Enterprise", code="HPE-GLOBAL", is_active=True)
    super_admin_role, _ = Role.objects.get_or_create(code=Role.SUPER_ADMIN, defaults={"name": "Super Admin"})
    admin_user = User.objects.create_user(
        email="superadmin@hpe.com",
        password="TestPassword123!",
        role=super_admin_role,
        organization=org,
    )
    client = Client()
    client.force_login(admin_user)
    return client, admin_user


@pytest.mark.django_db
def test_superadmin_requisitions_renders_superadmin_layout(superadmin_setup):
    client, _ = superadmin_setup
    resp = client.get("/requisitions/")
    assert resp.status_code == 200
    content = resp.content.decode("utf-8")
    assert 'id="superadminSidebar"' in content
    assert 'id="requesterSidebar"' not in content


@pytest.mark.django_db
def test_superadmin_audit_logs_redirects_to_superadmin_audit(superadmin_setup):
    client, _ = superadmin_setup
    resp = client.get("/audit/logs/", follow=True)
    assert resp.status_code == 200
    content = resp.content.decode("utf-8")
    assert 'id="superadminSidebar"' in content
    assert "Immutable Audit Logs" in content
    assert 'id="auditorSidebar"' not in content


@pytest.mark.django_db
def test_superadmin_invoices_renders_superadmin_layout(superadmin_setup):
    client, _ = superadmin_setup
    resp = client.get("/invoices/")
    assert resp.status_code == 200
    content = resp.content.decode("utf-8")
    assert 'id="superadminSidebar"' in content
    assert 'id="financeSidebar"' not in content


@pytest.mark.django_db
def test_superadmin_approvals_inbox_renders_superadmin_layout(superadmin_setup):
    client, _ = superadmin_setup
    resp = client.get("/approvals/inbox/")
    assert resp.status_code == 200
    content = resp.content.decode("utf-8")
    assert 'id="superadminSidebar"' in content
    assert 'id="requesterSidebar"' not in content


@pytest.mark.django_db
def test_superadmin_contracts_renders_superadmin_layout(superadmin_setup):
    client, _ = superadmin_setup
    resp = client.get("/contracts/")
    assert resp.status_code == 200
    content = resp.content.decode("utf-8")
    assert 'id="superadminSidebar"' in content
    assert 'id="legalSidebar"' not in content

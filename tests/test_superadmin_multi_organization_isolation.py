import pytest
from django.urls import reverse
from django.test import Client

from apps.accounts.models import Role, User
from apps.organization.models import CostCenter, Department, Organization


@pytest.fixture
def org_ecosystem(db):
    """
    Creates two distinct organizations: HPE and VPD, each with their own departments,
    cost centers, superadmins, and requesters.
    """
    role_sa, _ = Role.objects.get_or_create(code=Role.SUPER_ADMIN, defaults={"name": "Super Admin"})
    role_req, _ = Role.objects.get_or_create(code=Role.REQUESTER, defaults={"name": "Requester"})

    # Organization 1: HPE
    hpe_org = Organization.objects.create(name="Hewlett Packard Enterprise", code="HPE-GLOBAL", is_active=True)
    hpe_dept = Department.objects.create(name="IT Infrastructure Operations", code="IT-OPS", organization=hpe_org)
    hpe_cc = CostCenter.objects.create(name="Compute & Cloud Center", code="CC-IT-101", department=hpe_dept)

    hpe_admin = User.objects.create_superuser(
        email="admin@hpe.com",
        password="Password123!",
        role=role_sa,
        organization=hpe_org,
    )
    hpe_requester = User.objects.create_user(
        email="requester@hpe.com",
        password="Password123!",
        role=role_req,
        organization=hpe_org,
        department=hpe_dept,
        cost_center=hpe_cc,
    )

    # Organization 2: VPD
    vpd_org = Organization.objects.create(name="VPD Technologies", code="VPD-01", is_active=True)
    vpd_dept = Department.objects.create(name="HR Dept", code="VPD-HR", organization=vpd_org)
    vpd_cc = CostCenter.objects.create(name="VPD HR Center", code="CC-VPD-HR", department=vpd_dept)

    vpd_admin = User.objects.create_user(
        email="admin@vpdtechnologies.com",
        password="Password123!",
        role=role_sa,
        organization=vpd_org,
        is_staff=True,
    )
    vpd_requester = User.objects.create_user(
        email="johnalexander2706@gmail.com",
        password="Password123!",
        role=role_req,
        organization=vpd_org,
        department=vpd_dept,
        cost_center=vpd_cc,
    )

    return {
        "hpe_org": hpe_org,
        "hpe_dept": hpe_dept,
        "hpe_cc": hpe_cc,
        "hpe_admin": hpe_admin,
        "hpe_requester": hpe_requester,
        "vpd_org": vpd_org,
        "vpd_dept": vpd_dept,
        "vpd_cc": vpd_cc,
        "vpd_admin": vpd_admin,
        "vpd_requester": vpd_requester,
    }


@pytest.mark.django_db
def test_superadmin_dashboard_cannot_create_organizations(client, org_ecosystem):
    """
    Verify:
    1. '+ Add Organization' button is NOT present in Super Admin dashboard.
    2. Directly posting or getting /superadmin/organizations/create/ returns 403 Forbidden.
    """
    hpe_admin = org_ecosystem["hpe_admin"]
    client.force_login(hpe_admin)

    # GET /superadmin/organizations/
    response = client.get(reverse("superadmin:organizations"))
    assert response.status_code == 200
    content = response.content.decode("utf-8")
    assert "Add Organization" not in content
    assert "Add Department" in content
    assert "Add Cost Center" in content

    # GET and POST /superadmin/organizations/create/ should be forbidden
    get_res = client.get(reverse("superadmin:org_create"))
    assert get_res.status_code == 403
    assert "restricted to Django Admin" in get_res.content.decode("utf-8")

    post_res = client.post(reverse("superadmin:org_create"), {"name": "Malicious Org", "code": "MAL"})
    assert post_res.status_code == 403


@pytest.mark.django_db
def test_django_admin_retains_organization_creation(client, org_ecosystem):
    """
    Verify that Django Admin (/admin/organization/organization/add/) can still create organizations.
    """
    hpe_admin = org_ecosystem["hpe_admin"]
    client.force_login(hpe_admin)

    response = client.get("/admin/organization/organization/add/")
    assert response.status_code == 200


@pytest.mark.django_db
def test_superadmin_user_list_multi_tenant_isolation(client, org_ecosystem):
    """
    Verify multi-tenant isolation on /superadmin/users/:
    - HPE Super Admin sees HPE Requester, NOT VPD Requester (johnalexander2706@gmail.com).
    - VPD Super Admin sees VPD Requester, NOT HPE Requester.
    """
    # 1. HPE Super Admin perspective
    client.force_login(org_ecosystem["hpe_admin"])
    response = client.get(reverse("superadmin:users"))
    assert response.status_code == 200
    content = response.content.decode("utf-8")
    assert "admin@hpe.com" in content
    assert "requester@hpe.com" in content
    assert "johnalexander2706@gmail.com" not in content
    assert "admin@vpdtechnologies.com" not in content

    # 2. VPD Super Admin perspective
    client.force_login(org_ecosystem["vpd_admin"])
    response_vpd = client.get(reverse("superadmin:users"))
    assert response_vpd.status_code == 200
    content_vpd = response_vpd.content.decode("utf-8")
    assert "johnalexander2706@gmail.com" in content_vpd
    assert "admin@vpdtechnologies.com" in content_vpd
    assert "requester@hpe.com" not in content_vpd
    assert "admin@hpe.com" not in content_vpd


@pytest.mark.django_db
def test_superadmin_organizations_page_multi_tenant_isolation(client, org_ecosystem):
    """
    Verify multi-tenant isolation on /superadmin/organizations/:
    - HPE Super Admin sees only HPE Legal Entity, HPE Departments, HPE Cost Centers.
    - VPD Legal Entity, VPD Departments, VPD Cost Centers are completely hidden.
    """
    client.force_login(org_ecosystem["hpe_admin"])
    response = client.get(reverse("superadmin:organizations"))
    assert response.status_code == 200
    content = response.content.decode("utf-8")

    # HPE data present
    assert "HPE-GLOBAL" in content
    assert "Hewlett Packard Enterprise" in content
    assert "IT-OPS" in content
    assert "CC-IT-101" in content

    # VPD data strictly absent
    assert "VPD-01" not in content
    assert "VPD Technologies" not in content
    assert "VPD-HR" not in content
    assert "CC-VPD-HR" not in content


@pytest.mark.django_db
def test_superadmin_can_create_departments_and_cost_centers_bound_to_own_org(client, org_ecosystem):
    """
    Verify Super Admin can create departments and cost centers, and they are
    automatically bound to their own organization.
    """
    client.force_login(org_ecosystem["hpe_admin"])

    # 1. Create new department
    dept_res = client.post(
        reverse("superadmin:dept_create"),
        {
            "name": "Finance & Analytics",
            "code": "FIN-ANALYTICS",
            "description": "Financial planning unit",
        },
    )
    assert dept_res.status_code == 200
    new_dept = Department.objects.get(code="FIN-ANALYTICS")
    assert new_dept.organization == org_ecosystem["hpe_org"]

    # 2. Create new cost center under this department
    cc_res = client.post(
        reverse("superadmin:cost_center_create"),
        {
            "code": "CC-FIN-201",
            "name": "Treasury & Tax",
            "department": str(new_dept.pk),
        },
    )
    assert cc_res.status_code == 200
    new_cc = CostCenter.objects.get(code="CC-FIN-201")
    assert new_cc.department == new_dept
    assert new_cc.department.organization == org_ecosystem["hpe_org"]

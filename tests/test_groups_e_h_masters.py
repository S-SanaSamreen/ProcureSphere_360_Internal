from decimal import Decimal

import pytest
from django.core.exceptions import ValidationError
from django.urls import reverse
from django.utils import timezone

from apps.accounts.models import Role, User
from apps.audit.models import AuditLog
from apps.budgets.models import Budget, SpendLedger
from apps.budgets.services import allocate_budget_service, post_ledger_entry_service
from apps.organization.models import (
    BusinessUnit,
    CostCenter,
    Department,
    FiscalPeriod,
    Location,
    Organization,
)

PW = "TenantPassword123!"


@pytest.fixture
def two_orgs(db, db_roles):
    out = {}
    for tag in ("A", "B"):
        org = Organization.objects.create(name=f"Org {tag}", code=f"ORG-{tag}", is_active=True)
        admin = User.objects.create_user(
            email=f"admin{tag.lower()}@example.com",
            password=PW,
            first_name="Admin",
            last_name=tag,
            role=db_roles[Role.SUPER_ADMIN],
            organization=org,
        )
        out[tag] = (org, admin)
    return out


def _period(org, closed=False, name="P1"):
    return FiscalPeriod.objects.create(
        organization=org,
        year=2026,
        period_number=1,
        name=name,
        start_date=timezone.localdate().replace(month=1, day=1),
        end_date=timezone.localdate().replace(month=12, day=31),
        is_closed=closed,
    )


def _budget(org, period):
    dept = Department.objects.create(organization=org, name="Dept", code=f"D-{org.code}")
    cc = CostCenter.objects.create(department=dept, code=f"CC-{org.code}", name="CC")
    return Budget.objects.create(
        cost_center=cc, fiscal_period=period, allocated_amount=Decimal("1000.00")
    )


# --- Location / Business Unit CRUD and tenant scoping ---------------------------------


@pytest.mark.django_db
def test_location_create_edit_and_audit(client, two_orgs):
    org_a, admin_a = two_orgs["A"]
    client.force_login(admin_a)
    resp = client.post(
        reverse("superadmin:location_create"),
        {
            "organization": org_a.pk,
            "name": "HQ",
            "code": "hq",
            "address": "1 Main",
            "is_active": "on",
        },
    )
    assert resp.status_code == 200
    loc = Location.objects.get(organization=org_a, code="HQ")
    assert AuditLog.objects.filter(target_model="Location", target_object_id=str(loc.id)).exists()

    resp = client.post(
        reverse("superadmin:location_edit", args=[loc.pk]),
        {"organization": org_a.pk, "name": "HQ Renamed", "code": "HQ", "address": "2 Main"},
    )
    assert resp.status_code == 200
    loc.refresh_from_db()
    assert loc.name == "HQ Renamed" and loc.is_active is False


@pytest.mark.django_db
def test_location_code_unique_per_org_not_global(client, two_orgs):
    org_a, admin_a = two_orgs["A"]
    org_b, _ = two_orgs["B"]
    Location.objects.create(organization=org_b, name="B site", code="HQ")
    client.force_login(admin_a)
    ok = client.post(
        reverse("superadmin:location_create"),
        {"organization": org_a.pk, "name": "A site", "code": "HQ", "is_active": "on"},
    )
    assert "<script>" in ok.content.decode()
    dup = client.post(
        reverse("superadmin:location_create"),
        {"organization": org_a.pk, "name": "Dup", "code": "HQ", "is_active": "on"},
    )
    assert "already exists" in dup.content.decode()
    assert Location.objects.filter(organization=org_a).count() == 1


@pytest.mark.django_db
def test_org_a_admin_cannot_see_or_edit_org_b_masters(client, two_orgs):
    org_a, admin_a = two_orgs["A"]
    org_b, _ = two_orgs["B"]
    loc_b = Location.objects.create(organization=org_b, name="B Secret Site", code="BSITE")
    bu_b = BusinessUnit.objects.create(organization=org_b, name="B Secret BU", code="BBU")
    client.force_login(admin_a)

    page = client.get(reverse("superadmin:locations")).content.decode()
    assert "BSITE" not in page and "BBU" not in page

    assert client.get(reverse("superadmin:location_edit", args=[loc_b.pk])).status_code == 403
    assert (
        client.post(
            reverse("superadmin:location_edit", args=[loc_b.pk]),
            {"organization": org_b.pk, "name": "Hacked", "code": "BSITE"},
        ).status_code
        == 403
    )
    assert client.get(reverse("superadmin:business_unit_edit", args=[bu_b.pk])).status_code == 403
    loc_b.refresh_from_db()
    assert loc_b.name == "B Secret Site"


@pytest.mark.django_db
def test_create_cannot_target_other_org(client, two_orgs):
    org_a, admin_a = two_orgs["A"]
    org_b, _ = two_orgs["B"]
    client.post(
        reverse("superadmin:business_unit_create"),
        {"organization": org_b.pk, "name": "Sneaky", "code": "SN", "is_active": "on"},
    )
    assert not BusinessUnit.objects.filter(code="SN").exists()
    client.force_login(admin_a)
    resp = client.post(
        reverse("superadmin:business_unit_create"),
        {"organization": org_b.pk, "name": "Sneaky", "code": "SN", "is_active": "on"},
    )
    assert "valid choice" in resp.content.decode()
    assert not BusinessUnit.objects.filter(code="SN").exists()


@pytest.mark.django_db
def test_business_unit_parent_must_be_same_org_and_no_cycles(client, two_orgs):
    org_a, admin_a = two_orgs["A"]
    org_b, _ = two_orgs["B"]
    foreign = BusinessUnit.objects.create(organization=org_b, name="Foreign", code="F")
    parent = BusinessUnit.objects.create(organization=org_a, name="Parent", code="P")
    child = BusinessUnit.objects.create(organization=org_a, name="Child", code="C", parent=parent)
    client.force_login(admin_a)

    resp = client.post(
        reverse("superadmin:business_unit_create"),
        {"organization": org_a.pk, "name": "X", "code": "X", "parent": foreign.pk},
    )
    assert not BusinessUnit.objects.filter(code="X").exists()
    assert "Select a valid choice" in resp.content.decode()

    resp = client.post(
        reverse("superadmin:business_unit_edit", args=[parent.pk]),
        {"organization": org_a.pk, "name": "Parent", "code": "P", "parent": child.pk},
    )
    assert "ancestor" in resp.content.decode()
    parent.refresh_from_db()
    assert parent.parent_id is None


@pytest.mark.django_db
def test_orgless_non_superuser_sees_nothing_on_new_screens(client, db_roles, two_orgs):
    org_a, _ = two_orgs["A"]
    Location.objects.create(organization=org_a, name="A site", code="ASITE")
    orphan = User.objects.create_user(
        email="orphan@example.com", password=PW, role=db_roles[Role.SUPER_ADMIN]
    )
    client.force_login(orphan)
    assert "ASITE" not in client.get(reverse("superadmin:locations")).content.decode()
    assert client.get(reverse("superadmin:location_create")).status_code == 403


@pytest.mark.django_db
def test_platform_superuser_sees_all_and_non_admin_denied(client, super_admin_user, two_orgs):
    org_a, _ = two_orgs["A"]
    org_b, _ = two_orgs["B"]
    Location.objects.create(organization=org_a, name="A site", code="ASITE")
    Location.objects.create(organization=org_b, name="B site", code="BSITE")
    client.force_login(super_admin_user)
    page = client.get(reverse("superadmin:locations")).content.decode()
    assert "ASITE" in page and "BSITE" in page

    client.logout()
    plain = User.objects.create_user(email="plain@example.com", password=PW, organization=org_a)
    client.force_login(plain)
    assert client.get(reverse("superadmin:locations")).status_code == 403


@pytest.mark.django_db
def test_sidebar_links_to_locations_screen(client, two_orgs):
    _, admin_a = two_orgs["A"]
    client.force_login(admin_a)
    page = client.get(reverse("superadmin:organizations")).content.decode()
    assert "/superadmin/locations/" in page


# --- Fiscal period close / reopen ---------------------------------------------------------


@pytest.mark.django_db
def test_closed_period_blocks_ledger_posting_and_allocation_reopen_allows():
    org = Organization.objects.create(name="Org", code="ORG-X")
    period = _period(org)
    budget = _budget(org, period)

    post_ledger_entry_service(
        budget=budget,
        entry_type=SpendLedger.ENTRY_RESERVATION,
        amount=Decimal("10.00"),
        reference_number="R1",
        description="ok",
    )
    period.is_closed = True
    period.save()
    with pytest.raises(ValidationError, match="closed"):
        post_ledger_entry_service(
            budget=budget,
            entry_type=SpendLedger.ENTRY_RESERVATION,
            amount=Decimal("5.00"),
            reference_number="R2",
            description="blocked",
        )
    with pytest.raises(ValidationError, match="closed"):
        allocate_budget_service(
            cost_center=budget.cost_center,
            fiscal_period=period,
            allocated_amount=Decimal("2000.00"),
        )
    budget.refresh_from_db()
    assert budget.reserved_amount == Decimal("10.00")
    assert budget.allocated_amount == Decimal("1000.00")

    period.is_closed = False
    period.save()
    post_ledger_entry_service(
        budget=budget,
        entry_type=SpendLedger.ENTRY_RESERVATION,
        amount=Decimal("5.00"),
        reference_number="R3",
        description="ok again",
    )
    budget.refresh_from_db()
    assert budget.reserved_amount == Decimal("15.00")


@pytest.mark.django_db
def test_superadmin_can_close_and_reopen_period_with_audit(client, two_orgs):
    org_a, admin_a = two_orgs["A"]
    period = _period(org_a)
    client.force_login(admin_a)
    url = reverse("superadmin:fiscal_period_toggle", args=[period.pk])

    assert client.get(url).status_code == 405
    client.post(url, {"action": "close"})
    period.refresh_from_db()
    assert period.is_closed is True
    client.post(url, {"action": "open"})
    period.refresh_from_db()
    assert period.is_closed is False
    logs = AuditLog.objects.filter(target_model="FiscalPeriod", target_object_id=str(period.id))
    assert logs.count() == 2
    page = client.get("/superadmin/budgets/").content.decode()
    assert f"/superadmin/fiscal-periods/{period.pk}/toggle/" in page


@pytest.mark.django_db
def test_period_toggle_is_tenant_scoped(client, two_orgs):
    org_a, admin_a = two_orgs["A"]
    org_b, _ = two_orgs["B"]
    period_b = _period(org_b)
    client.force_login(admin_a)
    resp = client.post(
        reverse("superadmin:fiscal_period_toggle", args=[period_b.pk]), {"action": "close"}
    )
    assert resp.status_code == 403
    period_b.refresh_from_db()
    assert period_b.is_closed is False


# --- Part H: dead links now resolve -------------------------------------------------------


def _template_source(rel):
    from pathlib import Path

    return (Path(__file__).resolve().parent.parent / "templates" / rel).read_text(encoding="utf-8")


@pytest.mark.django_db
def test_super_admin_template_manage_links_resolve(client, super_admin_user):
    src = _template_source("pages/dashboards/super_admin.html")
    assert 'href="#" class="btn btn-sm btn-outline' not in src
    for link in ("/vendors/", "/superadmin/users/"):
        assert f'href="{link}" class="btn btn-sm btn-outline' in src
    client.force_login(super_admin_user)
    for link in ("/vendors/", "/superadmin/users/"):
        assert client.get(link).status_code == 200


def test_exception_detail_template_links_to_po_detail():
    src = _template_source("pages/invoices/exception_detail.html")
    assert "{% url 'order_detail' exception.invoice.po.pk %}" in src
    assert reverse("order_detail", args=["11111111-1111-1111-1111-111111111111"])


def test_sourcing_template_has_no_static_dead_pagination():
    src = _template_source("manager/sourcing.html")
    assert 'class="page-link" href="#"' not in src
    assert "renderSourcingPagination" in src


def test_poamendment_declares_fields_once():
    from apps.orders.models import POAmendment

    names = [f.name for f in POAmendment._meta.get_fields()]
    assert len(names) == len(set(names))
    assert POAmendment._meta.get_field("status").max_length == 30

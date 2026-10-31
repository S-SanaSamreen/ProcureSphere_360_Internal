"""Two organisations must never see each other's core records (web + /api/v1)."""

import datetime
from decimal import Decimal

import pytest
from django.utils import timezone

from apps.accounts.models import Role, User
from apps.orders.models import POLine, PurchaseOrder
from apps.organization.models import CostCenter, Department, Organization
from apps.receipts.models import GoodsReceipt
from apps.requisitions.models import PurchaseRequisition
from apps.vendors.models import Vendor, VendorCategory

pytestmark = pytest.mark.django_db


def make_org(db_roles, tag, vendor):
    org = Organization.objects.create(name=f"Org {tag}", code=f"ISO-{tag}")
    dept = Department.objects.create(name=f"Dept {tag}", code=f"ID{tag}", organization=org)
    cc = CostCenter.objects.create(name=f"CC {tag}", code=f"ICC-{tag}", department=dept)

    def user(local, role):
        return User.objects.create_user(
            email=f"{local}.{tag.lower()}@iso.test",
            password="Password123!",
            role=db_roles[role],
            organization=org,
            department=dept,
        )

    req = user("req", Role.REQUESTER)
    pr = PurchaseRequisition.objects.create(
        pr_number=f"PR-ISO-{tag}",
        title="t",
        justification="j",
        requester=req,
        department=dept,
        cost_center=cc,
        status=PurchaseRequisition.STATUS_APPROVED,
        total_amount=Decimal("10"),
        requested_delivery_date=timezone.localdate() + datetime.timedelta(days=5),
    )
    po = PurchaseOrder.objects.create(
        po_number=f"PO-ISO-{tag}",
        vendor=vendor,
        cost_center=cc,
        requisition=pr,
        status=PurchaseOrder.STATUS_ISSUED,
        total_amount=Decimal("10"),
    )
    POLine.objects.create(
        po=po, item_description="x", quantity=Decimal("1"), unit_price=Decimal("10")
    )
    stores = user("st", Role.STORES_RECEIVER)
    grn = GoodsReceipt.objects.create(
        grn_number=f"GRN-ISO-{tag}", po=po, received_by=stores, received_date=timezone.now()
    )
    return dict(
        org=org,
        pr=pr,
        po=po,
        grn=grn,
        pm=user("pm", Role.PROC_MGR),
        fin=user("fin", Role.FINANCE_AP),
        stores=stores,
    )


@pytest.fixture
def two(db_roles):
    cat = VendorCategory.objects.create(name="C", code="CAT-ISO")
    vendor = Vendor.objects.create(
        legal_name="Shared Vendor",
        tax_identification_number="TIN-ISO",
        category=cat,
        status=Vendor.STATUS_ACTIVE,
    )
    return make_org(db_roles, "A", vendor), make_org(db_roles, "B", vendor)


@pytest.mark.parametrize(
    "path,key,role",
    [
        ("/requisitions/{}/", "pr", "pm"),
        ("/purchase-orders/{}/", "po", "pm"),
        ("/receipts/{}/", "grn", "stores"),
        ("/receipts/{}/", "grn", "fin"),
    ],
)
def test_web_detail_not_visible_cross_org(client, two, path, key, role):
    a, b = two
    client.force_login(a[role])
    own = client.get(path.format(a[key].pk))
    other = client.get(path.format(b[key].pk))
    assert own.status_code == 200
    assert other.status_code in (302, 403, 404)
    if other.status_code == 302:
        assert str(b[key].pk) not in other.url


@pytest.mark.parametrize(
    "path,key,role",
    [
        ("/api/v1/requisitions/", "pr", "pm"),
        ("/api/v1/purchase-orders/", "po", "pm"),
        ("/api/v1/receipts/", "grn", "stores"),
        ("/api/v1/receipts/", "grn", "fin"),
    ],
)
def test_api_lists_and_details_not_cross_org(api_client, two, path, key, role):
    a, b = two
    api_client.force_authenticate(a[role])
    resp = api_client.get(path)
    assert resp.status_code == 200
    data = resp.json()
    rows = data.get("results", data) if isinstance(data, dict) else data
    ids = {str(r["id"]) for r in rows}
    assert str(b[key].pk) not in ids
    assert str(a[key].pk) in ids
    assert api_client.get(f"{path}{b[key].pk}/").status_code == 404

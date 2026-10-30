"""Manual PO screen: multi-line orders, cost center follows requisition, errors handled (group C)."""

import datetime
from decimal import Decimal

import pytest
from django.contrib.messages import get_messages
from django.urls import reverse

from apps.budgets.models import Budget
from apps.orders.models import PurchaseOrder
from apps.organization.models import CostCenter
from apps.requisitions.models import PurchaseRequisition
from tests.test_group_c_lifecycle import D, make_po, make_pr, world  # noqa: F401  (shared fixture)

pytestmark = pytest.mark.django_db

URL = "/purchase-orders/create/"


def post_lines(w, client, lines, **extra):
    data = {
        "vendor_id": str(w["vendor"].pk),
        "cost_center_id": str(w["cc"].pk),
        "item_name": [row[0] for row in lines],
        "quantity": [str(row[1]) for row in lines],
        "unit_price": [str(row[2]) for row in lines],
        "unit_of_measure": ["EA" for _ in lines],
        "terms_and_conditions": "Net 30",
    }
    data.update(extra)
    return client.post(URL, data)


def msgs(resp):
    return [str(m) for m in get_messages(resp.wsgi_request)]


@pytest.fixture
def pe_client(client, world):  # noqa: F811
    client.force_login(world["pe"])
    return client


def test_modal_offers_multiple_lines_tax_default_and_orderable_requisitions(
    pe_client, world
):  # noqa: F811
    w = world
    make_pr(w, 10000)
    page = pe_client.get(URL).content.decode()
    assert 'id="po-add-line"' in page and 'name="item_name"' in page
    assert 'name="tax_rate_pct"' in page and 'value="10"' in page  # settings default, not 1E+1
    assert "still orderable" in page and "CC-L" in page


def test_multi_line_po_is_created_with_correct_totals_and_commitment(
    pe_client, world
):  # noqa: F811
    w = world
    resp = post_lines(
        w, pe_client, [("Server", 2, "5000"), ("Rack", 1, "1500.50"), ("Cable", 10, "12")]
    )
    po = PurchaseOrder.objects.get()
    assert resp.status_code == 302 and resp.url == reverse("order_detail", args=[po.pk])
    assert po.lines.count() == 3
    assert po.subtotal == D("11620.50")
    assert po.tax_rate == D("0.10") and po.total_amount == D("12782.55")
    assert Budget.objects.get(cost_center=w["cc"]).committed_amount == D("12782.55")
    assert "3 line(s)" in msgs(resp)[0]


def test_cost_center_follows_the_requisition_whatever_is_posted(pe_client, world):  # noqa: F811
    w = world
    other_cc = CostCenter.objects.create(name="Other", code="CC-OTHER", department=w["dept"])
    pr = make_pr(w, 10000)
    resp = post_lines(
        w,
        pe_client,
        [("Thing", 1, "10000")],
        requisition_id=str(pr.pk),
        cost_center_id=str(other_cc.pk),  # tampered / stale selection
    )
    assert resp.status_code == 302
    po = PurchaseOrder.objects.get()
    assert po.cost_center_id == w["cc"].pk  # the requisition's, not the posted one
    pr.refresh_from_db()
    assert pr.status == PurchaseRequisition.STATUS_PO_ISSUED
    budget = Budget.objects.get(cost_center=w["cc"])
    assert budget.reserved_amount == D("0.00") and budget.committed_amount == D("11000.00")


def test_direct_po_requires_a_cost_center(pe_client, world):  # noqa: F811
    w = world
    resp = pe_client.post(
        URL,
        {
            "vendor_id": str(w["vendor"].pk),
            "item_name": ["x"],
            "quantity": ["1"],
            "unit_price": ["10"],
        },
    )
    assert resp.status_code == 404  # no cost center and no requisition to take it from
    assert not PurchaseOrder.objects.exists()


def test_blank_rows_are_ignored_but_partial_rows_are_rejected_by_line_number(
    pe_client, world
):  # noqa: F811
    w = world
    ok = post_lines(w, pe_client, [("Server", 1, "100"), ("", "", ""), ("Cable", 2, "5")])
    assert ok.status_code == 302
    assert PurchaseOrder.objects.get().lines.count() == 2

    PurchaseOrder.objects.all().delete()
    bad = post_lines(w, pe_client, [("Server", 1, "100"), ("", 3, "5")])
    assert bad.status_code == 302 and bad.url == reverse("orders_list")
    assert any("Line 2" in m and "description is required" in m for m in msgs(bad))
    assert not PurchaseOrder.objects.exists()


@pytest.mark.parametrize(
    "line,fragment",
    [
        (("Item", "abc", "5"), "must be numbers"),
        (("Item", 0, "5"), "greater than zero"),
        (("Item", -2, "5"), "greater than zero"),
        (("Item", 1, "-5"), "cannot be negative"),
        (("Item", 1, "NaN"), "must be numbers"),
        (("Item", 99999999999, "1"), "too large"),
    ],
)
def test_invalid_numbers_are_rejected_without_creating_anything(
    pe_client, world, line, fragment
):  # noqa: F811
    resp = post_lines(world, pe_client, [line])
    assert resp.status_code == 302
    assert any(fragment in m for m in msgs(resp)), msgs(resp)
    assert not PurchaseOrder.objects.exists()


def test_business_rule_failures_are_shown_not_a_500_and_leave_no_po(pe_client, world):  # noqa: F811
    resp = post_lines(world, pe_client, [("Huge", 1, "99999999")])  # overspends the budget
    assert resp.status_code == 302 and resp.url == reverse("orders_list")
    assert any("Insufficient budget" in m for m in msgs(resp))
    assert not PurchaseOrder.objects.exists()


def test_tax_rate_override_and_validation(pe_client, world):  # noqa: F811
    w = world
    resp = post_lines(w, pe_client, [("Item", 1, "1000")], tax_rate_pct="18")
    assert resp.status_code == 302
    po = PurchaseOrder.objects.get()
    assert po.tax_rate == D("0.18") and po.total_amount == D("1180.00")

    PurchaseOrder.objects.all().delete()
    for bad in ("abc", "150", "-1"):
        r = post_lines(w, pe_client, [("Item", 1, "1000")], tax_rate_pct=bad)
        assert any("ax rate" in m for m in msgs(r)), (bad, msgs(r))
    assert not PurchaseOrder.objects.exists()


def test_fully_covered_requisition_disappears_from_the_form(
    pe_client, world, settings
):  # noqa: F811
    settings.PO_OVER_REQUISITION_TOLERANCE_PCT = "0"
    w = world
    pr = make_pr(w, 10000)
    make_po(w, pr, 6000)
    page = pe_client.get(URL).content.decode()
    assert (
        pr.pr_number in page and "up to $4,000.00 still orderable" in page
    )  # PO_ISSUED, room left
    make_po(w, pr, 4000)
    page = pe_client.get(URL).content.decode()
    assert pr.pr_number not in page


def test_single_field_post_still_works_for_backward_compatibility(pe_client, world):  # noqa: F811
    w = world
    resp = pe_client.post(
        URL,
        {
            "vendor_id": str(w["vendor"].pk),
            "cost_center_id": str(w["cc"].pk),
            "item_name": "Legacy single line",
            "quantity": "3",
            "unit_price": "100",
        },
    )
    assert resp.status_code == 302
    po = PurchaseOrder.objects.get()
    assert po.lines.count() == 1 and po.subtotal == D("300.00")


@pytest.mark.parametrize("who", ["requester", "legal", "finance"])
def test_only_procurement_roles_can_use_the_form(client, world, who):  # noqa: F811
    client.force_login(world[who])
    assert client.get(URL).status_code == 403
    resp = client.post(URL, {"vendor_id": str(world["vendor"].pk)})
    assert resp.status_code == 403
    assert not PurchaseOrder.objects.exists()

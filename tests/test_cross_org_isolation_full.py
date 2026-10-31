"""Full cross-organisation / cross-vendor isolation sweep.

Org A owns data in every module. Org B users (one per role), org-less internal users and an
unrelated vendor are then pointed at every GET route of the project (parameterless routes and
routes parameterised with A's real identifiers). No 2xx response may disclose A's data, and
parameterless JSON/HTML responses must be identical before and after A's data exists.
"""

import datetime
import json
import re
from decimal import Decimal

import pytest
from django.core.files.base import ContentFile
from django.test import Client
from django.urls import get_resolver
from django.utils import timezone

from apps.accounts.models import Role, User
from apps.approvals.models import ApprovalDelegate, ApprovalPolicy, ApprovalRequest, ApprovalStep
from apps.audit.models import AuditLog
from apps.budgets.models import Budget, BudgetTransfer
from apps.contracts.models import (
    Contract,
    ContractAlert,
    ContractDocument,
    ContractMilestone,
    ContractObligation,
)
from apps.invoices.models import MatchException, SupplierInvoice
from apps.notifications.models import Notification
from apps.orders.models import POLine, PurchaseOrder, ServiceEntry
from apps.organization.models import (
    BusinessUnit,
    CostCenter,
    Department,
    FiscalPeriod,
    Location,
    Organization,
)
from apps.receipts.models import GoodsReceipt
from apps.reports.models import ExportJob
from apps.requisitions.models import PRAttachment, PurchaseRequisition
from apps.scorecards.models import ScorecardWeight, VendorScorecard
from apps.sourcing.models import (
    AwardDecision,
    BidAttachment,
    BidInvite,
    Clarification,
    SourcingDeviation,
    SourcingEvent,
    VendorBid,
)
from apps.vendors.models import Vendor, VendorCategory, VendorChangeRequest, VendorDocument

MARK = "ZQXA"
SECRET_BYTES = b"ZQXA-FILE-SECRET-CONTENT"
INTERNAL_ROLES = [
    Role.REQUESTER,
    Role.DEPT_APPROVER,
    Role.PROC_EXEC,
    Role.PROC_MGR,
    Role.FINANCE_AP,
    Role.STORES_RECEIVER,
    Role.LEGAL_MGR,
    Role.AUDITOR,
]
SKIP_PREFIXES = (
    "admin/",
    "login/",
    "logout/",
    "api/v1/auth/",
    "api/schema",
    "api/docs",
    "media/",
    "^media",
    "^.*",
    "^api/.*",
    "health",
    "readyz",
    "password-change",
    "accounts/change-password",
    "accounts/mfa",
)
# action-style URLs that mutate state on GET in some views are still fine to request: every
# request is made by an intruder and must be refused, so no state of A may change.

PK_BY_PREFIX = [
    ("api/v1/accounts/users", "user"),
    ("api/v1/accounts/roles", "role"),
    ("api/v1/organization/entities", "org"),
    ("api/v1/organization/departments", "dept"),
    ("api/v1/organization/cost-centers", "cc"),
    ("api/v1/approvals/policies", "policy"),
    ("api/v1/approvals/actions", "approval"),
    ("api/v1/vendors/categories", "vcat"),
    ("api/v1/vendors/risk-records", "risk"),
    ("api/v1/vendors", "vendor"),
    ("vendors/", "vendor"),
    ("api/v1/requisitions", "pr"),
    ("requisitions", "pr"),
    ("api/v1/sourcing-events/events", "event"),
    ("api/v1/sourcing-events/bids", "bid"),
    ("api/v1/sourcing-events/clarifications", "clar"),
    ("api/v1/sourcing-events/evaluations", "bid"),
    ("api/v1/purchase-orders", "po"),
    ("purchase-orders", "po"),
    ("orders", "po"),
    ("api/v1/receipts", "grn"),
    ("api/v1/invoices/exceptions", "mexc"),
    ("api/v1/invoices", "invoice"),
    ("api/v1/contracts", "contract"),
    ("api/v1/budgets/ledger", "ledger"),
    ("api/v1/budgets", "budget"),
    ("api/v1/scorecards", "scorecard"),
    ("api/v1/notifications", "notif"),
    ("api/v1/reports/exports", "export"),
    ("api/v1/audit/logs", "auditlog"),
]
PARAM_KEYS = {
    "user_id": "user",
    "org_id": "org",
    "dept_id": "dept",
    "cc_id": "cc",
    "period_id": "period",
    "location_id": "location",
    "bu_id": "bu",
    "budget_id": "budget",
    "policy_id": "policy",
    "event_id": "event",
    "bid_id": "bid",
    "po_id": "po",
    "grn_id": "grn",
    "invoice_id": "invoice",
    "exception_id": "mexc",
    "contract_id": "contract",
    "milestone_id": "milestone",
    "obligation_id": "obligation",
    "document_id": "cdoc",
    "dispatch_id": "dispatch",
    "doc_id": "vdoc",
    "notif_id": "notif",
    "notification_id": "notif",
    "delegation_id": "delegation",
    "request_id": "approval",
    "vendor_id": "vendor",
    "attachment_id": "attachment",
    "line_id": "grnline",
    "rejection_id": "rejection",
    "amendment_id": "amendment",
    "pk": None,
}


def _mk(model, **kw):
    obj = model(**kw)
    obj.save()
    return obj


def _file(name, data=SECRET_BYTES):
    return ContentFile(data, name=name)


def _user(tag, role, roles, org=None, dept=None, vendor=None, cc=None):
    return User.objects.create_user(
        email=f"{tag}@iso-full.test",
        password="Password123!",
        role=roles[role],
        organization=org,
        department=dept,
        vendor=vendor,
        cost_center=cc,
        first_name=tag.split(".")[0].title(),
        last_name="Tester",
    )


def _walk(patterns, prefix=""):
    for p in patterns:
        if hasattr(p, "url_patterns"):
            yield from _walk(p.url_patterns, prefix + str(p.pattern))
        else:
            yield prefix + str(p.pattern)


def _routes():
    out = []
    for r in _walk(get_resolver().url_patterns):
        if r.startswith(SKIP_PREFIXES) or r.startswith("^.*") or "\\." in r:
            continue
        if "<drf_format_suffix" in r or r in ("api/v1/accounts/", "api/v1/organization/"):
            continue
        out.append(r)
    return sorted(set(out))


@pytest.fixture
def world_b(db, settings, tmp_path):
    settings.MEDIA_ROOT = str(tmp_path)
    return _build_b()


@pytest.fixture
def world(world_b):
    return _add_org_a(world_b)


def _build_b():
    roles = {}
    for code, name in Role.ROLE_CHOICES:
        roles[code] = Role.objects.get_or_create(code=code, defaults={"name": name})[0]
    cat = _mk(VendorCategory, name="Cat", code="CAT-ISOF")
    today = timezone.localdate()

    # ---- org B (intruders), no business data
    org_b = _mk(Organization, name="Org B Full", code="ISOF-B")
    dept_b = _mk(Department, name="Dept B", code="DB-F", organization=org_b)
    cc_b = _mk(CostCenter, name="CC B", code="CCB-F", department=dept_b)
    vendor_b = _mk(
        Vendor,
        legal_name="Other Vendor B",
        vendor_number="V-ISOF-B",
        tax_identification_number="TIN-ISOF-B",
        category=cat,
        status=Vendor.STATUS_ACTIVE,
        email="vb@iso-full.test",
        address="b",
    )
    intruders = {}
    for role in INTERNAL_ROLES:
        intruders[f"orgB.{role}"] = _user(
            f"b.{role.lower()}", role, roles, org=org_b, dept=dept_b, cc=cc_b
        )
        intruders[f"noorg.{role}"] = _user(f"n.{role.lower()}", role, roles)
    intruders["orgB.vendor"] = _user("b.vendor", Role.VENDOR_USER, roles, vendor=vendor_b)
    intruders["orgB.admin"] = _user("b.admin", Role.SUPER_ADMIN, roles, org=org_b, dept=dept_b)
    intruders["noorg.SUPER_ADMIN"] = _user("n.superadmin", Role.SUPER_ADMIN, roles)

    routes = _routes()
    plain = [
        r
        for r in routes
        if "<" not in r and "(" not in r and not r.startswith("^") and "export" not in r
    ]
    snap_users = [f"orgB.{r}" for r in INTERNAL_ROLES] + ["orgB.vendor", "orgB.admin"]
    return dict(
        roles=roles,
        cat=cat,
        intruders=intruders,
        routes=routes,
        plain=plain,
        snap_users=snap_users,
    )


def _add_org_a(w):
    roles, cat, intruders = w["roles"], w["cat"], w["intruders"]
    today = timezone.localdate()
    org_a = _mk(Organization, name=f"{MARK} Org", code="ISOF-A")
    dept_a = _mk(Department, name=f"{MARK} Dept", code="DA-F", organization=org_a)
    loc_a = _mk(Location, organization=org_a, name=f"{MARK} Loc", code="LOC-ZQXA")
    bu_a = _mk(BusinessUnit, organization=org_a, name=f"{MARK} BU", code="BU-ZQXA")
    cc_a = _mk(CostCenter, name=f"{MARK} CC", code="CC-ZQXA", department=dept_a, location=loc_a)
    period_a = _mk(
        FiscalPeriod,
        organization=org_a,
        year=2026,
        period_number=1,
        name=f"{MARK}-FY26",
        start_date=today - datetime.timedelta(days=100),
        end_date=today + datetime.timedelta(days=200),
    )
    vendor_a = _mk(
        Vendor,
        legal_name=f"{MARK} Vendor Ltd",
        vendor_number="V-ZQXA-1",
        tax_identification_number="TIN-ZQXA-1",
        category=cat,
        organization=org_a,
        status=Vendor.STATUS_ACTIVE,
        email="va@iso-full.test",
        address=f"{MARK} street",
    )
    a = {}
    for role in INTERNAL_ROLES + [Role.SUPER_ADMIN]:
        a[role] = _user(f"a.{role.lower()}", role, roles, org=org_a, dept=dept_a, cc=cc_a)
    a["VENDOR"] = _user("a.vendor", Role.VENDOR_USER, roles, vendor=vendor_a)
    req, pm, fin = a[Role.REQUESTER], a[Role.PROC_MGR], a[Role.FINANCE_AP]

    pr = _mk(
        PurchaseRequisition,
        pr_number="PR-ZQXA-1",
        title=f"{MARK} requisition",
        justification=f"{MARK} just",
        requester=req,
        department=dept_a,
        cost_center=cc_a,
        status=PurchaseRequisition.STATUS_APPROVED,
        total_amount=Decimal("7100001.00"),
        requested_delivery_date=today + datetime.timedelta(days=9),
    )
    po = _mk(
        PurchaseOrder,
        po_number="PO-ZQXA-1",
        vendor=vendor_a,
        cost_center=cc_a,
        requisition=pr,
        status=PurchaseOrder.STATUS_ISSUED,
        total_amount=Decimal("7200002.00"),
    )
    line = _mk(
        POLine,
        po=po,
        item_description=f"{MARK} widget",
        quantity=Decimal("2"),
        unit_price=Decimal("3600001.00"),
    )
    grn = _mk(
        GoodsReceipt,
        grn_number="GRN-ZQXA-1",
        po=po,
        received_by=a[Role.STORES_RECEIVER],
        received_date=timezone.now(),
    )
    invoice = _mk(
        SupplierInvoice,
        invoice_number="INV-ZQXA-1",
        vendor=vendor_a,
        po=po,
        invoice_date=today,
        due_date=today + datetime.timedelta(days=30),
        status=(
            SupplierInvoice.STATUS_EXCEPTION
            if hasattr(SupplierInvoice, "STATUS_EXCEPTION")
            else SupplierInvoice._meta.get_field("status").default
        ),
        subtotal=Decimal("7300003.00"),
        total_amount=Decimal("7300003.00"),
        notes=f"{MARK} note",
    )
    mexc = _mk(
        MatchException,
        invoice=invoice,
        exception_type=MatchException._meta.get_field("exception_type").choices[0][0],
        description=f"{MARK} exception",
        variance_amount=Decimal("1.00"),
    )
    contract = _mk(
        Contract,
        contract_number="CT-ZQXA-1",
        title=f"{MARK} contract",
        vendor=vendor_a,
        po=po,
        status=Contract.STATUS_ACTIVE,
        contract_value=Decimal("7400004.00"),
        start_date=today - datetime.timedelta(days=5),
        end_date=today + datetime.timedelta(days=20),
        contract_owner=a[Role.LEGAL_MGR],
    )
    milestone = _mk(
        ContractMilestone,
        contract=contract,
        title=f"{MARK} milestone",
        due_date=today + datetime.timedelta(days=3),
        amount=Decimal("1.00"),
    )
    obligation = _mk(
        ContractObligation,
        contract=contract,
        title=f"{MARK} obligation",
        due_date=today + datetime.timedelta(days=3),
    )
    cdoc = _mk(
        ContractDocument,
        contract=contract,
        title=f"{MARK} doc",
        file=_file("zqxa_contract.pdf"),
        uploaded_by=a[Role.LEGAL_MGR],
    )
    _mk(
        ContractAlert,
        contract=contract,
        alert_type=ContractAlert._meta.get_field("alert_type").choices[0][0],
        message=f"{MARK} alert",
    )
    event = _mk(
        SourcingEvent,
        event_number="EV-ZQXA-1",
        title=f"{MARK} event",
        requisition=pr,
        status=SourcingEvent.STATUS_BID_WINDOW,
        bid_start_date=timezone.now() - datetime.timedelta(days=1),
        bid_end_date=timezone.now() + datetime.timedelta(days=5),
        description=f"{MARK} desc",
        created_by=a[Role.PROC_MGR],
    )
    _mk(BidInvite, event=event, vendor=vendor_a)
    bid = _mk(
        VendorBid,
        event=event,
        vendor=vendor_a,
        bid_number="BID-ZQXA-1",
        total_bid_amount=Decimal("7600006.00"),
        proposal_summary=f"{MARK} proposal",
        submitted_at=timezone.now(),
    )
    pr_att = _mk(
        PRAttachment,
        requisition=pr,
        title=f"{MARK} att",
        file=_file("zqxa_pr_att.pdf"),
        uploaded_by=req,
    )
    bid_att = _mk(BidAttachment, bid=bid, title=f"{MARK} batt", file=_file("zqxa_bid_att.pdf"))
    clar = _mk(Clarification, event=event, vendor=vendor_a, question=f"{MARK} question?")
    _mk(
        AwardDecision,
        event=event,
        winning_bid=bid,
        award_reason=f"{MARK} award",
        recommended_by=pm,
    )
    _mk(
        SourcingDeviation,
        event=event,
        kind=SourcingDeviation._meta.get_field("kind").choices[0][0],
        reason=f"{MARK} deviation",
        requested_by=pm,
    )
    budget = _mk(
        Budget, cost_center=cc_a, fiscal_period=period_a, allocated_amount=Decimal("7500005.00")
    )
    cc_a2 = _mk(CostCenter, name=f"{MARK} CC2", code="CC-ZQXA-2", department=dept_a)
    budget2 = _mk(
        Budget, cost_center=cc_a2, fiscal_period=period_a, allocated_amount=Decimal("7500006.00")
    )
    _mk(
        BudgetTransfer,
        source=budget,
        destination=budget2,
        amount=Decimal("1234599.00"),
        reason=f"{MARK} transfer",
        requested_by=fin,
    )
    _mk(
        ServiceEntry,
        po_line=line,
        milestone=f"{MARK} svc",
        percent_complete=Decimal("10"),
        entry_date=today,
        recorded_by=a[Role.STORES_RECEIVER],
    )
    _mk(
        VendorChangeRequest,
        vendor=vendor_a,
        changes={"legal_name": f"{MARK} new name"},
        requested_by=pm,
    )
    vdoc = _mk(
        VendorDocument,
        vendor=vendor_a,
        document_type=VendorDocument._meta.get_field("document_type").choices[0][0],
        title=f"{MARK} vdoc",
        file=_file("zqxa_vendor_doc.pdf"),
    )
    _mk(ScorecardWeight, organization=org_a)
    scorecard = _mk(
        VendorScorecard,
        vendor=vendor_a,
        evaluation_period="Q1-ZQXA",
        evaluated_by=pm,
        evaluator_comments=f"{MARK} comment",
    )
    policy = _mk(
        ApprovalPolicy,
        name=f"{MARK} policy",
        module=ApprovalPolicy.MODULE_PR,
        department=dept_a,
        cost_center=cc_a,
    )
    _mk(ApprovalStep, policy=policy, step_number=1, approver_role=roles[Role.DEPT_APPROVER])
    approval = _mk(
        ApprovalRequest,
        module=ApprovalPolicy.MODULE_PR,
        target_model_name="PurchaseRequisition",
        target_object_id=pr.pk,
        policy=policy,
        amount=Decimal("7100001.00"),
        department=dept_a,
        cost_center=cc_a,
        steps=[
            {
                "step_number": 1,
                "role_id": str(roles[Role.DEPT_APPROVER].pk),
                "role_code": Role.DEPT_APPROVER,
                "specific_approver_id": None,
                "description": "",
            }
        ],
    )
    delegation = _mk(
        ApprovalDelegate,
        approver=a[Role.DEPT_APPROVER],
        delegate=a[Role.PROC_MGR],
        start_date=today,
        end_date=today + datetime.timedelta(days=3),
        reason=f"{MARK} delegation",
    )
    notif = None
    for u in a.values():
        n = _mk(
            Notification,
            recipient=u,
            notification_type=Notification.TYPE_APPROVAL_REQUIRED,
            title=f"{MARK} notification",
            message=f"{MARK} message",
        )
        notif = notif or n
    export = _mk(
        ExportJob,
        report_type="po_status",
        requested_by=pm,
        status=ExportJob.STATUS_COMPLETED,
        result_file=_file("zqxa_export.csv"),
    )
    alog = _mk(
        AuditLog,
        actor=pm,
        action=AuditLog.ACTION_UPDATE,
        target_model="PurchaseOrder",
        target_object_id=str(po.pk),
        new_state={"note": f"{MARK} audit"},
    )

    ids = {
        "user": [u.pk for u in a.values()],
        "org": [org_a.pk],
        "dept": [dept_a.pk],
        "cc": [cc_a.pk],
        "period": [period_a.pk],
        "location": [loc_a.pk],
        "bu": [bu_a.pk],
        "budget": [budget.pk],
        "policy": [policy.pk],
        "event": [event.pk],
        "bid": [bid.pk],
        "po": [po.pk],
        "grn": [grn.pk],
        "invoice": [invoice.pk],
        "mexc": [mexc.pk],
        "contract": [contract.pk],
        "milestone": [milestone.pk],
        "obligation": [obligation.pk],
        "cdoc": [cdoc.pk],
        "vdoc": [vdoc.pk],
        "dispatch": [pr.pk],
        "notif": [notif.pk],
        "delegation": [delegation.pk],
        "approval": [approval.pk],
        "vendor": [vendor_a.pk],
        "pr": [pr.pk],
        "clar": [clar.pk],
        "scorecard": [scorecard.pk],
        "export": [export.pk],
        "auditlog": [alog.pk],
        "attachment": [pr_att.pk, bid_att.pk],
        "grnline": [grn.pk],
        "rejection": [grn.pk],
        "amendment": [po.pk],
        "risk": [vendor_a.pk],
        "ledger": [budget.pk],
    }
    all_ids = {str(i) for v in ids.values() for i in v}
    return dict(
        **w,
        a=a,
        ids=ids,
        all_ids=all_ids,
        files={
            "contract_doc": cdoc.file.name,
            "vendor_doc": vdoc.file.name,
            "export": export.result_file.name,
            "pr_att": pr_att.file.name,
            "bid_att": bid_att.file.name,
            "cdoc": cdoc,
            "vdoc": vdoc,
        },
    )


def _snapshot(user, urls):
    c = Client()
    c.force_login(user)
    out = {}
    for u in urls:
        resp = c.get("/" + u)
        out[u] = (resp.status_code, _norm(resp))
    return out


_VOLATILE = re.compile(
    r"csrfmiddlewaretoken\" value=\"[^\"]+\"|nonce=\"[^\"]+\"|\d{1,2}:\d{2}(:\d{2})?"
    r"|\d{4}-\d{2}-\d{2}T[\d:.+Z-]+|\d+ (second|minute|hour)s?"
)


def _norm(resp):
    if resp.status_code in (301, 302):
        return resp.headers.get("Location", "")
    try:
        body = resp.content.decode("utf-8", "replace")
    except Exception:  # pragma: no cover
        return ""
    return _VOLATILE.sub("", body)


def _fill(route, ids):
    """Yield concrete URLs for a route, substituting A's identifiers."""
    params = re.findall(r"<(?:\w+:)?(\w+)>|\(\?P<(\w+)>[^)]*\)", route)
    names = [x or y for x, y in params]
    if not names:
        yield route
        return
    cand = []
    for n in names:
        key = PARAM_KEYS.get(n)
        if n == "pk" or key is None and n != "pk":
            key = key if n != "pk" else None
        if n == "pk":
            key = next((k for p, k in PK_BY_PREFIX if route.startswith(p)), None)
            lst = ids.get(key, []) if key else ids["pr"] + ids["po"] + ids["grn"] + ids["event"]
        elif n in ("action", "stage", "kyc_action", "doc_type", "format"):
            lst = ["publish"] if n == "action" else ["technical"] if n == "stage" else ["approve"]
            if n == "doc_type":
                lst = ["PAN"]
        else:
            lst = ids.get(key, []) if key else []
        cand.append([str(x) for x in lst] or ["00000000-0000-0000-0000-000000000000"])
    # one pass per candidate index (avoid cartesian blow-up)
    for i in range(max(len(c) for c in cand)):
        url = route
        for n, c in zip(names, cand):
            val = c[min(i, len(c) - 1)]
            url = re.sub(r"<(?:\w+:)?%s>|\(\?P<%s>[^)]*\)" % (n, n), val, url, count=1)
        yield url.replace("^", "").replace("$", "")


def _leaks(resp, all_ids):
    if resp.status_code in (301, 302):
        return []
    if not 200 <= resp.status_code < 300:
        return []
    ctype = resp.headers.get("Content-Type", "")
    raw = (
        resp.content if not getattr(resp, "streaming", False) else b"".join(resp.streaming_content)
    )
    if SECRET_BYTES in raw:
        return ["file bytes served"]
    if not ctype.startswith(("text/", "application/json", "application/xml", "text/csv")):
        return [] if not raw else [f"binary {ctype} served ({len(raw)}b)"]
    body = raw.decode("utf-8", "replace")
    found = []
    if MARK.lower() in body.lower():
        m = re.search(r".{0,40}%s.{0,40}" % MARK, body, re.I | re.S)
        found.append("marker: " + (m.group(0).replace("\n", " ") if m else MARK))
    flat = body.replace(",", "")
    for amt in ("7100001", "7200002", "7300003", "7400004", "7500005", "7600006", "1234599"):
        if amt in flat:
            found.append(f"amount {amt}")
    for i in all_ids:
        if i in body:
            found.append(f"id {i}")
            break
    return found


def _get_all(client, world):
    leaks = []
    for route in world["routes"]:
        for url in _fill(route, world["ids"]):
            try:
                resp = client.get("/" + url.lstrip("/"), follow=False, HTTP_HX_REQUEST="true")
            except Exception as exc:  # a crash is a bug but not a leak; report it
                leaks.append(f"{url}: EXC {type(exc).__name__}: {exc}"[:200])
                continue
            for lk in _leaks(resp, world["all_ids"]):
                leaks.append(f"{url} [{resp.status_code}] {lk}")
    return leaks


INTRUDER_KEYS = (
    [f"orgB.{r}" for r in INTERNAL_ROLES]
    + [f"noorg.{r}" for r in INTERNAL_ROLES]
    + ["orgB.vendor", "orgB.admin", "noorg.SUPER_ADMIN"]
)


@pytest.mark.parametrize("who", INTRUDER_KEYS)
def test_intruder_sees_nothing_of_org_a(world, who):
    client = Client()
    client.force_login(world["intruders"][who])
    leaks = _get_all(client, world)
    assert not leaks, f"{who} leaks:\n" + "\n".join(leaks[:60])


@pytest.mark.parametrize("who", ["orgB.PROC_MGR", "orgB.FINANCE_AP", "orgB.AUDITOR"])
def test_intruder_post_does_not_mutate_org_a(world, who):
    """Mutating endpoints addressed at A's objects are refused for outsiders."""
    client = Client()
    client.force_login(world["intruders"][who])
    ids = world["ids"]
    before = (
        PurchaseOrder.objects.get(pk=ids["po"][0]).status,
        Contract.objects.get(pk=ids["contract"][0]).status,
        SourcingEvent.objects.get(pk=ids["event"][0]).status,
        SupplierInvoice.objects.get(pk=ids["invoice"][0]).status,
        PurchaseRequisition.objects.get(pk=ids["pr"][0]).status,
    )
    for route in world["routes"]:
        if "<" not in route and "(" not in route:
            continue
        if route.startswith(("api/v1/auth", "admin")):
            continue
        for url in _fill(route, ids):
            for method in (client.post, client.delete):
                try:
                    method("/" + url.lstrip("/"), {"comments": "x", "reason": "x"})
                except Exception:
                    pass
    after = (
        PurchaseOrder.objects.get(pk=ids["po"][0]).status,
        Contract.objects.get(pk=ids["contract"][0]).status,
        SourcingEvent.objects.get(pk=ids["event"][0]).status,
        SupplierInvoice.objects.get(pk=ids["invoice"][0]).status,
        PurchaseRequisition.objects.get(pk=ids["pr"][0]).status,
    )
    assert before == after
    assert Notification.objects.filter(title__startswith=MARK, is_read=True).count() == 0
    assert Contract.objects.filter(pk=ids["contract"][0]).exists()
    assert PurchaseOrder.objects.filter(pk=ids["po"][0]).exists()


def test_parameterless_pages_unchanged_by_org_a_data(world_b):
    """Aggregates/dashboards/JSON of org B users must be identical with and without org A."""
    before = {
        u: _snapshot(world_b["intruders"][u], world_b["plain"]) for u in world_b["snap_users"]
    }
    world = _add_org_a(world_b)
    diffs = []
    for who in world["snap_users"]:
        after = _snapshot(world["intruders"][who], world["plain"])
        for url, (code, body) in after.items():
            b_code, b_body = before[who][url]
            if (code, body) != (b_code, b_body):
                import difflib

                delta = [
                    ln
                    for ln in difflib.unified_diff(
                        b_body.splitlines(), body.splitlines(), lineterm="", n=0
                    )
                    if ln[:1] in "+-" and ln[:3] not in ("+++", "---")
                ]
                diffs.append(f"{who} /{url}: {b_code}->{code} {' | '.join(delta[:4])[:300]}")
    assert not diffs, "A's data altered B's view:\n" + "\n".join(diffs[:40])


@pytest.mark.parametrize("kind", ["contract_doc", "vendor_doc", "export", "pr_att", "bid_att"])
@pytest.mark.parametrize(
    "who", ["orgB.PROC_MGR", "orgB.REQUESTER", "noorg.LEGAL_MGR", "orgB.vendor"]
)
def test_media_download_blocked(world, who, kind):
    client = Client()
    client.force_login(world["intruders"][who])
    resp = client.get("/media/" + world["files"][kind])
    body = b"".join(resp.streaming_content) if getattr(resp, "streaming", False) else resp.content
    assert SECRET_BYTES not in body
    assert resp.status_code in (301, 302, 403, 404)


def test_org_a_users_still_see_their_data(world):
    """No over-blocking: A's own staff still reach their records."""
    a = world["a"]
    checks = [
        (Role.PROC_MGR, "/purchase-orders/", "PO-ZQXA-1"),
        (Role.PROC_MGR, "/requisitions/", "PR-ZQXA-1"),
        (Role.PROC_MGR, "/sourcing-events/", "EV-ZQXA-1"),
        (Role.PROC_MGR, "/vendors/", "ZQXA Vendor"),
        (Role.LEGAL_MGR, "/contracts/", "CT-ZQXA-1"),
        (Role.FINANCE_AP, "/invoices/", "INV-ZQXA-1"),
        (Role.STORES_RECEIVER, "/receipts/", "GRN-ZQXA-1"),
        (Role.AUDITOR, "/audit/logs/", "PurchaseOrder"),
    ]
    missing = []
    for role, url, text in checks:
        c = Client()
        c.force_login(a[role])
        resp = c.get(url, HTTP_HX_REQUEST="true")
        if resp.status_code != 200 or text not in resp.content.decode("utf-8", "replace"):
            missing.append(f"{role} {url} -> {resp.status_code} lacks {text}")
    # contract detail + notifications + vendor portal
    c = Client()
    c.force_login(a[Role.LEGAL_MGR])
    r = c.get(f"/contracts/{world['ids']['contract'][0]}/")
    if r.status_code != 200:
        missing.append(f"legal contract detail {r.status_code}")
    c = Client()
    c.force_login(a["VENDOR"])
    for url in ("/vendor/purchase-orders/", "/vendor/contracts/", "/vendor/invoices/"):
        r = c.get(url)
        if r.status_code != 200:
            missing.append(f"vendor {url} {r.status_code}")
    assert not missing, "\n".join(missing)


def test_json_has_no_foreign_numbers_for_org_b(world):
    """JSON API output for B never contains numbers as large as A's distinctive amounts."""
    c = Client()
    c.force_login(world["intruders"]["orgB.PROC_MGR"])
    bad = []
    for url in world["plain"]:
        if not url.startswith("api/"):
            continue
        r = c.get("/" + url)
        if r.status_code != 200 or "json" not in r.headers.get("Content-Type", ""):
            continue
        try:
            data = json.loads(r.content)
        except ValueError:
            continue
        stack = [data]
        while stack:
            x = stack.pop()
            if isinstance(x, dict):
                stack.extend(x.values())
            elif isinstance(x, list):
                stack.extend(x)
            elif isinstance(x, (int, float)) and not isinstance(x, bool) and abs(x) >= 1000000:
                bad.append(f"{url}: {x}")
            elif isinstance(x, str) and re.fullmatch(r"-?\d{7,}(\.\d+)?", x):
                bad.append(f"{url}: {x}")
    assert not bad, "\n".join(bad[:30])


def _dummy():  # keep Decimal import used for linters
    return Decimal("0")

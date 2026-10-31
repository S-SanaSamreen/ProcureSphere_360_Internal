from django.contrib import messages
from django.contrib.auth.decorators import login_required
from django.core.exceptions import PermissionDenied, ValidationError
from django.shortcuts import get_object_or_404, redirect, render
from django.views.decorators.http import require_POST

from apps.accounts.models import Role
from apps.orders.models import POAmendment, PurchaseOrder
from apps.orders.selectors import scope_purchase_orders_queryset
from apps.requisitions.selectors import scope_requisitions_to_org
from apps.vendors.selectors import scope_vendors_queryset

from .forms import POAmendForm


def _po_or_404(request, pk, qs=None):
    """Purchase order lookup limited to what the requesting user may see (404 otherwise)."""
    base = PurchaseOrder.objects.all() if qs is None else qs
    return get_object_or_404(scope_purchase_orders_queryset(base, request.user), pk=pk)


RECEIVABLE_PO_STATUSES = [
    PurchaseOrder.STATUS_ISSUED,
    PurchaseOrder.STATUS_ACKNOWLEDGED,
    PurchaseOrder.STATUS_PARTIAL_RECEIPT,
]


@login_required(login_url="/login/")
def list_view(request):
    user = request.user
    role_code = getattr(user, "role_code", None) or (
        user.role.code if hasattr(user, "role") and user.role else None
    )

    if role_code == Role.VENDOR_USER:
        raise PermissionDenied(
            "Vendors must access purchase orders via the Vendor Portal (/vendor/purchase-orders/)."
        )

    scoped_qs = scope_purchase_orders_queryset(PurchaseOrder.objects.all(), user)
    if role_code == Role.STORES_RECEIVER:
        # Stores / Receiver sees only POs eligible for receiving
        items = (
            scoped_qs.filter(status__in=RECEIVABLE_PO_STATUSES)
            .select_related("vendor", "cost_center")
            .prefetch_related("lines")
            .order_by("-created_at")
        )
    else:
        items = (
            scoped_qs.select_related("vendor", "cost_center")
            .prefetch_related("lines")
            .order_by("-created_at")
        )

    context = {
        "items": items,
        "role_code": role_code,
        "is_stores_receiver": role_code == Role.STORES_RECEIVER,
        "receivable_statuses": RECEIVABLE_PO_STATUSES,
    }
    if role_code == Role.SUPER_ADMIN or getattr(user, "is_superuser", False):
        context["base_layout"] = "base.html"

    return render(
        request,
        "pages/orders/list.html",
        context,
    )


@login_required(login_url="/login/")
def detail_view(request, pk):
    from django.core.exceptions import PermissionDenied

    user = request.user
    role_code = getattr(user, "role_code", None) or (
        user.role.code if hasattr(user, "role") and user.role else None
    )

    if role_code is None and not user.is_superuser:
        raise PermissionDenied("No role assigned. Contact your administrator.")

    po = get_object_or_404(
        scope_purchase_orders_queryset(PurchaseOrder.objects.all(), user)
        .select_related("vendor", "cost_center", "requisition", "acknowledged_by")
        .prefetch_related(
            "lines",
            "delivery_schedules",
            "amendments",
            "receipts__received_by",
            "receipts__lines__inspection",
        ),
        pk=pk,
    )

    # Scoping check: Stores Receiver only views receivable POs
    if role_code == Role.STORES_RECEIVER and po.status not in RECEIVABLE_PO_STATUSES:
        messages.warning(
            request,
            f"Purchase Order {po.po_number} is in '{po.get_status_display()}' status and is not currently eligible for goods receiving.",
        )
        return redirect("orders_list")

    if role_code == Role.VENDOR_USER:
        raise PermissionDenied(
            "Vendors must access purchase orders via the Vendor Portal (/vendor/purchase-orders/)."
        )

    lines = po.lines.all()
    delivery_schedules = po.delivery_schedules.select_related("po_line").all()
    amendments = po.amendments.select_related("requested_by").order_by("-amendment_number")
    can_amend = role_code in (Role.PROC_EXEC, Role.PROC_MGR) and not user.is_superuser

    return render(
        request,
        "pages/orders/detail.html",
        {
            "po": po,
            "lines": lines,
            "delivery_schedules": delivery_schedules,
            "amendments": amendments,
            "can_amend": can_amend,
            "amend_form": POAmendForm(po=po) if can_amend else None,
            "role_code": role_code,
            "is_stores_receiver": role_code == Role.STORES_RECEIVER and not user.is_superuser,
            "is_receivable": po.status in RECEIVABLE_PO_STATUSES,
        },
    )


def _require_po_execute(user):
    """Procurement Executive PO execution RBAC (backend enforcement).

    PROC_EXEC owns PO execution; PROC_MGR governs amendments. Super Admin and the platform
    superuser (PRD 3.6), Vendor, Stores, Finance, Legal and Auditor roles are denied.
    """
    role_code = getattr(user, "role_code", None)
    if user.is_superuser or role_code not in (Role.PROC_EXEC, Role.PROC_MGR):
        raise PermissionDenied("Purchase order execution is restricted to procurement roles.")


def _is_htmx(request):
    return request.headers.get("HX-Request") == "true"


@login_required(login_url="/login/")
def awards_pending_po_tab_view(request):
    """Pillar 4 HTMX tab: approved awards awaiting PO generation (real DB rows)."""
    _require_po_execute(request.user)
    from apps.core.tenancy import scope_sourcing_events
    from apps.sourcing.models import SourcingEvent

    pending = (
        scope_sourcing_events(SourcingEvent.objects.all(), request.user)
        .filter(
            status=SourcingEvent.STATUS_AWARDED,
            award_decisions__status="APPROVED",
        )
        .exclude(
            purchase_orders__status__in=[
                PurchaseOrder.STATUS_DRAFT,
                PurchaseOrder.STATUS_APPROVAL,
                PurchaseOrder.STATUS_ISSUED,
                PurchaseOrder.STATUS_ACKNOWLEDGED,
                PurchaseOrder.STATUS_PARTIAL_RECEIPT,
                PurchaseOrder.STATUS_COMPLETED,
            ]
        )
        .select_related("requisition")
        .prefetch_related("award_decisions__winning_bid__vendor")
        .order_by("-updated_at")
    )
    return render(request, "orders/partials/htmx_awards_pending.html", {"pending": pending})


@login_required(login_url="/login/")
@require_POST
def po_generate_from_award_htmx_view(request, event_id):
    """Wire button -> generate_po_from_award_service -> generate_purchase_order_service."""
    _require_po_execute(request.user)
    from apps.core.tenancy import scope_sourcing_events
    from apps.sourcing import services as sourcing_services
    from apps.sourcing.models import SourcingEvent

    event = get_object_or_404(
        scope_sourcing_events(SourcingEvent.objects.all(), request.user), pk=event_id
    )
    try:
        po = sourcing_services.generate_po_from_award_service(event=event, user=request.user)
    except ValidationError as exc:
        messages.error(request, " ".join(exc.messages))
        if _is_htmx(request):
            return render(
                request,
                "orders/partials/htmx_action_result.html",
                {"success": False, "errors": exc.messages},
                status=400,
            )
        return redirect("evaluation_event", event_id=event.id)
    messages.success(request, f"Purchase Order {po.po_number} generated from the award.")
    if _is_htmx(request):
        response = render(
            request,
            "orders/partials/htmx_action_result.html",
            {"success": True, "po": po},
        )
        response["HX-Trigger"] = "po-generated"
        return response
    return redirect("order_detail", pk=po.id)


@login_required(login_url="/login/")
def po_amend_modal_view(request, pk):
    """HTMX modal: GET shows reason + line edits; POST calls amend_purchase_order_service."""
    _require_po_execute(request.user)
    po = _po_or_404(
        request,
        pk,
        PurchaseOrder.objects.select_related("vendor", "cost_center").prefetch_related("lines"),
    )
    if po.status == PurchaseOrder.STATUS_CANCELLED:
        raise PermissionDenied("Cancelled purchase orders cannot be amended.")

    def _modal_context(bound_form):
        rows = []
        for line in po.lines.all():
            rows.append(
                {
                    "line": line,
                    "qty_field": bound_form[f"quantity_{line.id}"],
                    "price_field": bound_form[f"unit_price_{line.id}"],
                }
            )
        return {"po": po, "form": bound_form, "line_rows": rows}

    if request.method == "POST":
        form = POAmendForm(request.POST, po=po)
        if form.is_valid():
            from .services import amend_purchase_order_service

            try:
                amendment = amend_purchase_order_service(
                    po=po,
                    reason=form.cleaned_data["reason"],
                    updated_line_items=form.build_updated_line_items(),
                    requested_by_user=request.user,
                )
            except ValidationError as exc:
                form.add_error(None, exc)
                return render(
                    request,
                    "orders/partials/htmx_amend_modal.html",
                    _modal_context(form),
                    status=400,
                )
            messages.success(
                request,
                f"PO Amendment #{amendment.amendment_number} requested for {po.po_number}. "
                "The order is in AMENDMENT_PENDING status awaiting manager approval.",
            )
            if _is_htmx(request):
                response = render(
                    request,
                    "orders/partials/htmx_action_result.html",
                    {"success": True, "po": po, "amendment": amendment},
                )
                response["HX-Trigger"] = "po-amended"
                return response
            return redirect("order_detail", pk=po.id)
        return render(
            request,
            "orders/partials/htmx_amend_modal.html",
            _modal_context(form),
            status=400,
        )
    return render(
        request,
        "orders/partials/htmx_amend_modal.html",
        _modal_context(POAmendForm(po=po)),
    )


@login_required(login_url="/login/")
@require_POST
def po_approve_amendment_view(request, pk, amendment_id):
    """Approve (a step of) a pending PO amendment; the approval engine decides who may."""
    _require_po_decider(request.user, "PO_AMENDMENT")
    from .models import POAmendment
    from .services import approve_po_amendment_service, reject_po_amendment_service

    po = _po_or_404(request, pk)
    amendment = get_object_or_404(POAmendment, pk=amendment_id, po=po)
    action = request.POST.get("action", "approve")
    comments = request.POST.get("comments", "").strip()
    try:
        if action == "reject":
            reject_po_amendment_service(
                amendment=amendment, approver=request.user, reason=comments or "Rejected"
            )
            messages.success(request, f"Amendment #{amendment.amendment_number} rejected.")
        else:
            approve_po_amendment_service(
                amendment=amendment, approver=request.user, comments=comments
            )
            messages.success(
                request,
                f"Amendment #{amendment.amendment_number} approved. PO {po.po_number} updated to V{po.version}.",
            )
    except ValidationError as exc:
        messages.error(request, " ".join(exc.messages) if hasattr(exc, "messages") else str(exc))
    return redirect("order_detail", pk=po.id)


@login_required(login_url="/login/")
def po_amendments_partial_view(request, pk):
    """HTMX fragment: immutable amendment/version history for a PO."""
    _require_po_execute(request.user)
    po = _po_or_404(request, pk)
    amendments = po.amendments.select_related("requested_by").order_by("-amendment_number")
    return render(
        request,
        "orders/partials/htmx_amendments.html",
        {"po": po, "amendments": amendments},
    )


def _parse_po_form_lines(post):
    """
    Reads the repeated item_name / quantity / unit_price / unit_of_measure fields into line
    dicts. Fully blank rows are ignored; a partly filled or invalid row is a validation error
    that names the row, so nothing is silently dropped or defaulted.
    """
    from decimal import Decimal, InvalidOperation

    names = post.getlist("item_name")
    quantities = post.getlist("quantity")
    prices = post.getlist("unit_price")
    uoms = post.getlist("unit_of_measure")
    lines = []
    for idx, name in enumerate(names):
        qty_raw = (quantities[idx] if idx < len(quantities) else "").strip()
        price_raw = (prices[idx] if idx < len(prices) else "").strip()
        name = (name or "").strip()
        if not (name or qty_raw or price_raw):
            continue  # untouched row
        row = idx + 1
        if not name:
            raise ValidationError(f"Line {row}: item description is required.")
        try:
            qty = Decimal(qty_raw)
            price = Decimal(price_raw)
        except (InvalidOperation, ValueError):
            raise ValidationError(
                f"Line {row} ('{name}'): quantity and unit price must be numbers."
            )
        if not qty.is_finite() or not price.is_finite():
            raise ValidationError(
                f"Line {row} ('{name}'): quantity and unit price must be numbers."
            )
        if qty <= 0:
            raise ValidationError(f"Line {row} ('{name}'): quantity must be greater than zero.")
        if price < 0:
            raise ValidationError(f"Line {row} ('{name}'): unit price cannot be negative.")
        if qty > Decimal("9999999") or price > Decimal("9999999999.99"):
            raise ValidationError(f"Line {row} ('{name}'): quantity or unit price is too large.")
        lines.append(
            {
                "item_description": name,
                "quantity": qty,
                "unit_price": price,
                "unit_of_measure": (
                    uoms[idx].strip() if idx < len(uoms) and uoms[idx].strip() else "EA"
                ),
            }
        )
    if not lines:
        raise ValidationError("Add at least one order line.")
    return lines


def _require_po_decider(user, module):
    """Entry gate for PO / amendment approval screens: anyone the module's policy may involve."""
    from apps.approvals import engine as approval_engine

    if approval_engine.user_in_policy_roles(module, user):
        return
    raise PermissionDenied("You are not an approver for this kind of transaction.")


def _po_form_context(user):
    """Vendors, cost centers and requisitions a procurement user may raise a PO against."""
    from decimal import Decimal

    from django.conf import settings

    from apps.budgets.services import requisition_po_capacity
    from apps.organization.models import CostCenter
    from apps.requisitions.models import PurchaseRequisition
    from apps.vendors.models import Vendor
    from apps.vendors.selectors import scope_vendors_queryset

    cost_centers = CostCenter.objects.select_related("department").order_by("code")
    scoped_org = getattr(user, "effective_organization", None)
    if scoped_org and not user.is_superuser:
        cost_centers = cost_centers.filter(department__organization=scoped_org)
    elif not user.is_superuser:
        cost_centers = cost_centers.none()

    vendors = scope_vendors_queryset(
        Vendor.objects.filter(status=Vendor.STATUS_ACTIVE).order_by("legal_name"), user
    )

    requisitions = []
    candidates = PurchaseRequisition.objects.filter(
        status__in=[PurchaseRequisition.STATUS_APPROVED, PurchaseRequisition.STATUS_PO_ISSUED]
    ).select_related("cost_center", "department")
    if scoped_org and not user.is_superuser:
        candidates = candidates.filter(department__organization=scoped_org)
    elif not user.is_superuser:
        candidates = candidates.none()
    for pr in candidates.order_by("-created_at"):
        capacity = requisition_po_capacity(pr)
        if capacity["remaining"] > Decimal("0.00"):  # a fully covered PR cannot take another PO
            pr.remaining_amount = capacity["remaining"]
            requisitions.append(pr)

    default_rate = Decimal(str(getattr(settings, "DEFAULT_PO_TAX_RATE", "0.10"))) * 100
    return {
        "vendors": vendors,
        "cost_centers": cost_centers,
        "requisitions": requisitions,
        "default_tax_pct": format(default_rate.quantize(Decimal("0.01")).normalize(), "f"),
    }


@login_required(login_url="/login/")
def po_create_view(request):
    """View & modal to issue a purchase order (one or many lines)."""
    from decimal import Decimal, InvalidOperation

    user = request.user
    role_code = getattr(user, "role_code", None) or (
        user.role.code if hasattr(user, "role") and user.role else None
    )
    if user.is_superuser or role_code not in (Role.PROC_EXEC, Role.PROC_MGR):
        raise PermissionDenied("Only Procurement users can issue purchase orders.")

    if request.method == "POST":
        from apps.organization.models import CostCenter
        from apps.requisitions.models import PurchaseRequisition
        from apps.vendors.models import Vendor

        from .services import generate_purchase_order_service

        try:
            vendor = get_object_or_404(
                scope_vendors_queryset(Vendor.objects.all(), request.user),
                pk=request.POST.get("vendor_id"),
            )
            req_id = request.POST.get("requisition_id")
            requisition = (
                get_object_or_404(
                    scope_requisitions_to_org(PurchaseRequisition.objects.all(), request.user),
                    pk=req_id,
                )
                if req_id
                else None
            )

            # The cost center follows the requisition: the PO commits against the same budget
            # the requisition reserved. Only a PO with no requisition picks its own.
            if requisition:
                cost_center = requisition.cost_center
            else:
                cost_center = get_object_or_404(
                    CostCenter.objects.filter(
                        department__organization=getattr(
                            request.user, "effective_organization", None
                        )
                    ),
                    pk=request.POST.get("cost_center_id"),
                )

            tax_rate = None
            tax_raw = (request.POST.get("tax_rate_pct") or "").strip()
            if tax_raw:
                try:
                    tax_rate = Decimal(tax_raw) / Decimal("100")
                except (InvalidOperation, ValueError):
                    raise ValidationError("Tax rate must be a number between 0 and 100.")

            po = generate_purchase_order_service(
                vendor=vendor,
                cost_center=cost_center,
                line_items=_parse_po_form_lines(request.POST),
                created_by_user=user,
                requisition=requisition,
                terms_and_conditions=request.POST.get(
                    "terms_and_conditions",
                    "Standard HPE Enterprise Procurement Terms & Conditions",
                ),
                tax_rate=tax_rate,
            )
        except ValidationError as exc:
            messages.error(
                request,
                "Purchase order not issued: "
                + (" ".join(exc.messages) if hasattr(exc, "messages") else str(exc)),
            )
            return redirect("orders_list")
        messages.success(
            request, f"Purchase Order {po.po_number} issued with {po.lines.count()} line(s)."
        )
        return redirect("order_detail", pk=po.pk)

    return render(request, "orders/partials/po_create_modal.html", _po_form_context(user))


@login_required(login_url="/login/")
@require_POST
def po_approve_action_view(request, pk):
    """Approval of a PO in APPROVAL or DRAFT status (engine-enforced; never Super Admin)."""
    user = request.user
    role_code = getattr(user, "role_code", None) or (
        user.role.code if hasattr(user, "role") and user.role else None
    )
    if role_code != Role.PROC_MGR or user.is_superuser:
        _require_po_decider(user, "PO")  # the PO policy may name other roles

    po = _po_or_404(request, pk)
    comments = request.POST.get("comments", "Approved").strip()
    try:
        from .services import approve_purchase_order_service

        approve_purchase_order_service(po=po, approver=user, comments=comments)
        messages.success(
            request,
            f"Purchase Order {po.po_number} approved and issued to {po.vendor.legal_name}.",
        )
    except ValidationError as exc:
        messages.error(request, " ".join(exc.messages) if hasattr(exc, "messages") else str(exc))
    return redirect("order_detail", pk=po.id)


@login_required(login_url="/login/")
@require_POST
def po_submit_approval_action_view(request, pk):
    """Procurement Executive action to submit draft PO for approval."""
    user = request.user
    _require_po_execute(user)
    po = _po_or_404(request, pk)
    try:
        from .services import submit_purchase_order_for_approval_service

        submit_purchase_order_for_approval_service(po=po, actor=user)
        messages.success(request, f"Purchase Order {po.po_number} submitted for manager review.")
    except ValidationError as exc:
        messages.error(request, " ".join(exc.messages) if hasattr(exc, "messages") else str(exc))
    return redirect("order_detail", pk=po.id)


@login_required(login_url="/login/")
def po_amendment_approve_view(request, pk, amendment_id):
    """Action to approve (a step of) a pending PO amendment; the approval engine decides who may."""
    _require_po_decider(request.user, "PO_AMENDMENT")
    po = _po_or_404(request, pk)
    amendment = get_object_or_404(POAmendment, pk=amendment_id, po=po)

    if request.method == "POST":
        comments = request.POST.get("comments", "").strip()
        from .services import approve_po_amendment_service

        try:
            approve_po_amendment_service(
                amendment=amendment,
                approver=request.user,
                comments=comments,
            )
            messages.success(
                request,
                f"PO Amendment #{amendment.amendment_number} approved! PO {po.po_number} is now V{po.version} (requires supplier re-acknowledgement).",
            )
        except ValidationError as exc:
            msg = exc.messages[0] if hasattr(exc, "messages") else str(exc)
            messages.error(request, msg)

    if _is_htmx(request):
        amendments = po.amendments.select_related("requested_by", "approved_by").order_by(
            "-amendment_number"
        )
        response = render(
            request,
            "orders/partials/htmx_amendments.html",
            {
                "po": po,
                "amendments": amendments,
                "role_code": getattr(request.user, "role_code", None)
                or (request.user.role.code if getattr(request.user, "role", None) else None),
            },
        )
        response["HX-Trigger"] = "po-amended"
        return response
    return redirect("order_detail", pk=po.id)


@login_required(login_url="/login/")
def po_amendment_reject_view(request, pk, amendment_id):
    """Action to reject a pending PO amendment; the approval engine decides who may."""
    _require_po_decider(request.user, "PO_AMENDMENT")
    po = _po_or_404(request, pk)
    amendment = get_object_or_404(POAmendment, pk=amendment_id, po=po)

    if request.method == "POST":
        reason = request.POST.get("reason", "").strip()
        if not reason:
            messages.error(request, "Rejection justification reason is mandatory.")
        else:
            from .services import reject_po_amendment_service

            try:
                reject_po_amendment_service(
                    amendment=amendment,
                    rejector=request.user,
                    reason=reason,
                )
                messages.success(
                    request,
                    f"PO Amendment #{amendment.amendment_number} rejected. PO {po.po_number} reverted to operational status.",
                )
            except ValidationError as exc:
                msg = exc.messages[0] if hasattr(exc, "messages") else str(exc)
                messages.error(request, msg)

    if _is_htmx(request):
        amendments = po.amendments.select_related("requested_by", "approved_by").order_by(
            "-amendment_number"
        )
        response = render(
            request,
            "orders/partials/htmx_amendments.html",
            {
                "po": po,
                "amendments": amendments,
                "role_code": getattr(request.user, "role_code", None)
                or (request.user.role.code if getattr(request.user, "role", None) else None),
            },
        )
        response["HX-Trigger"] = "po-amended"
        return response
    return redirect("order_detail", pk=po.id)


@login_required(login_url="/login/")
def service_entries_view(request, pk):
    """Stores records service milestones; the business owner confirms them."""
    from django.core.exceptions import PermissionDenied, ValidationError

    from . import service_entries as se
    from .models import ServiceEntry

    user = request.user
    po = get_object_or_404(
        scope_purchase_orders_queryset(PurchaseOrder.objects.all(), user).select_related(
            "vendor", "requisition"
        ),
        pk=pk,
    )
    role_code = getattr(user, "role_code", None)
    if request.method == "POST":
        action = request.POST.get("action")
        try:
            if action == "record":
                line = get_object_or_404(po.lines.all(), pk=request.POST.get("po_line"))
                se.record_service_entry_service(
                    po_line=line,
                    user=user,
                    milestone=request.POST.get("milestone", ""),
                    percent_complete=request.POST.get("percent_complete"),
                    notes=request.POST.get("notes", ""),
                )
                messages.success(
                    request, "Service entry recorded; awaiting business-owner confirmation."
                )
            elif action in ("confirm", "reject"):
                entry = get_object_or_404(
                    ServiceEntry.objects.filter(po_line__po=po), pk=request.POST.get("entry_id")
                )
                se.decide_service_entry_service(
                    entry=entry,
                    user=user,
                    confirm=action == "confirm",
                    comments=request.POST.get("comments", ""),
                )
                messages.success(request, f"Service entry {action}ed.")
            else:
                raise ValidationError("Unknown action.")
        except (ValidationError, PermissionDenied) as exc:
            messages.error(request, " ".join(getattr(exc, "messages", [str(exc)])))
        return redirect("order_service_entries", pk=po.pk)

    entries = ServiceEntry.objects.filter(po_line__po=po).select_related(
        "po_line", "recorded_by", "confirmed_by"
    )
    return render(
        request,
        "pages/orders/service_entries.html",
        {
            "po": po,
            "service_lines": [
                {"line": ln, "confirmed_pct": se.confirmed_percent(ln)}
                for ln in po.lines.filter(is_service=True)
            ],
            "entries": entries,
            "can_record": role_code == Role.STORES_RECEIVER,
            "confirmable_ids": {
                e.pk
                for e in entries
                if e.status == ServiceEntry.STATUS_RECORDED
                and se.can_confirm_service_entry(user, e)
            },
        },
    )

from decimal import Decimal

from django.conf import settings
from django.contrib.auth.decorators import login_required
from django.db import connection
from django.db.models import Avg, Count, Q, Sum
from django.http import JsonResponse
from django.shortcuts import render
from django.utils import timezone
from redis import Redis
from rest_framework.permissions import AllowAny
from rest_framework.views import APIView

from apps.accounts.models import Role, User
from apps.core import tenancy
from apps.orders.models import PurchaseOrder
from apps.orders.selectors import scope_purchase_orders_queryset
from apps.requisitions.models import PurchaseRequisition
from apps.requisitions.selectors import scope_requisitions_to_org
from apps.scorecards.models import VendorScorecard
from apps.sourcing.models import Clarification, SourcingEvent
from apps.vendors.models import Vendor


def health_check_view(request):
    """
    Health check endpoint returning DB and Redis status.
    """
    health_status = {
        "status": "healthy",
        "service": "ProcureSphere 360",
        "database": "unknown",
        "redis": "unknown",
    }

    # Check Database connection
    try:
        with connection.cursor() as cursor:
            cursor.execute("SELECT 1;")
            health_status["database"] = "ok"
    except Exception as e:
        health_status["database"] = f"error: {str(e)}"
        health_status["status"] = "unhealthy"

    # Check Redis connection
    try:
        redis_client = Redis.from_url(settings.REDIS_URL, socket_timeout=2)
        if redis_client.ping():
            health_status["redis"] = "ok"
    except Exception as e:
        health_status["redis"] = f"offline (local mode): {str(e)}"

    http_status = 200 if health_status["status"] == "healthy" else 503
    return JsonResponse(health_status, status=http_status)


class HealthAPIView(APIView):
    permission_classes = [AllowAny]

    def get(self, request):
        return health_check_view(request)


def _pr_ids_with_pending_budget_exception():
    from apps.approvals.models import ApprovalPolicy, ApprovalRequest

    return ApprovalRequest.objects.filter(
        module=ApprovalPolicy.MODULE_BUDGET_EXCEPTION,
        status=ApprovalRequest.STATUS_PENDING,
        target_model_name="PurchaseRequisition",
    ).values_list("target_object_id", flat=True)


@login_required(login_url="/login/")
def home_view(request):  # noqa: C901
    """
    Role-tailored Dashboard page view with live aggregated ERP metrics.
    Dispatches to custom workspace per user role (Requester, Approver, Procurement, Finance, Vendor, Legal, Auditor, Admin).
    """
    user = request.user
    role_code = getattr(user, "role_code", None) or (
        user.role.code if hasattr(user, "role") and user.role else None
    )

    if role_code is None and not user.is_superuser:
        return render(request, "pages/dashboards/no_role.html", status=403)

    # 1. REQUESTER ROLE DASHBOARD
    if role_code == Role.REQUESTER:
        my_prs = PurchaseRequisition.objects.filter(requester=user)
        context = {
            "metrics": {
                "total_my_prs": my_prs.count(),
                "pending_prs": my_prs.filter(
                    status__in=["SUBMITTED", "MANAGER_REVIEW", "BUDGET_REVIEW"]
                ).count(),
                "approved_prs": my_prs.filter(status="APPROVED").count(),
            },
            "my_recent_prs": my_prs.order_by("-created_at")[:10],
        }
        return render(request, "pages/dashboards/requester_dashboard.html", context)

    elif role_code == Role.PROC_MGR:
        from apps.core.manager_views import manager_dashboard_view

        return manager_dashboard_view(request)

    # 4. STORES / RECEIVER DASHBOARD
    elif role_code == Role.STORES_RECEIVER:
        from apps.receipts.views import get_stores_dashboard_data

        data = get_stores_dashboard_data(user)
        return render(
            request,
            "pages/dashboards/stores_dashboard.html",
            {
                "metrics": data["metrics"],
                "recent_grns": data["recent_grns"],
                "pending_deliveries": data.get("pending_deliveries", []),
                "pending_inspections": data.get("pending_inspections", []),
                "pending_handoffs": data.get("pending_handoffs", []),
                "pending_rejections": data.get("pending_rejections", []),
            },
        )

    # 5. VENDOR PORTAL USER DASHBOARD
    elif role_code == Role.VENDOR_USER:
        from apps.vendors.vendor_dashboard_views import vendor_dashboard_overview_view

        return vendor_dashboard_overview_view(request)

    # 6. FINANCE / AP SPECIALIST DASHBOARD
    elif role_code == Role.FINANCE_AP:
        from apps.budgets.models import SpendLedger
        from apps.invoices.models import MatchException, SupplierInvoice
        from apps.organization.models import CostCenter, Department
        from apps.requisitions.selectors import get_pr_budget_review_queue

        selected_dept_id = request.GET.get("dept") or ""
        selected_cc_id = request.GET.get("cost_center") or ""

        departments = Department.objects.all().order_by("name")
        cost_centers = CostCenter.objects.select_related("department").all().order_by("code")
        fin_org = getattr(user, "effective_organization", None)
        if not user.is_superuser:
            if fin_org:
                departments = departments.filter(organization=fin_org)
                cost_centers = cost_centers.filter(department__organization=fin_org)
            else:
                departments = departments.none()
                cost_centers = cost_centers.none()

        all_cost_centers = list(cost_centers)
        if selected_dept_id:
            filtered_cost_centers = [
                cc for cc in all_cost_centers if str(cc.department_id) == str(selected_dept_id)
            ]
        else:
            filtered_cost_centers = all_cost_centers

        open_match_exceptions = (
            tenancy.scope_match_exceptions(MatchException.objects.all(), user)
            .filter(status=MatchException.STATUS_OPEN)
            .select_related(
                "invoice__vendor",
                "invoice__po__requisition__department",
                "invoice__po__requisition__cost_center",
            )
            .order_by("-created_at")
        )
        # PRs awaiting a budget exception (Finance already approved; out of budget). Scoped to
        # the user's organization - an org-less user sees nothing.
        open_budget_exceptions = (
            PurchaseRequisition.objects.filter(
                status=PurchaseRequisition.STATUS_BUDGET_REVIEW,
                department__organization_id=user.organization_id,
            )
            if user.organization_id
            else PurchaseRequisition.objects.none()
        )
        open_budget_exceptions = open_budget_exceptions.filter(
            id__in=_pr_ids_with_pending_budget_exception()
        )
        open_budget_exceptions = open_budget_exceptions.select_related(
            "department", "cost_center", "requester"
        ).order_by("-updated_at")
        from apps.budgets.selectors import scope_spend_ledger_queryset
        from apps.invoices.selectors import scope_invoices_queryset

        ready_for_payment_qs = scope_invoices_queryset(
            SupplierInvoice.objects.filter(status=SupplierInvoice.STATUS_READY_FOR_PAYMENT), user
        )
        ledger_qs = scope_spend_ledger_queryset(SpendLedger.objects.all(), user)
        committed_spend_qs = ledger_qs.filter(entry_type=SpendLedger.ENTRY_COMMITMENT)
        actual_spend_qs = ledger_qs.filter(entry_type=SpendLedger.ENTRY_ACTUAL)

        if selected_dept_id:
            open_budget_exceptions = open_budget_exceptions.filter(department_id=selected_dept_id)
            open_match_exceptions = open_match_exceptions.filter(
                invoice__po__requisition__department_id=selected_dept_id
            )
            ready_for_payment_qs = ready_for_payment_qs.filter(
                po__requisition__department_id=selected_dept_id
            )
            committed_spend_qs = committed_spend_qs.filter(
                budget__cost_center__department_id=selected_dept_id
            )
            actual_spend_qs = actual_spend_qs.filter(
                budget__cost_center__department_id=selected_dept_id
            )

        if selected_cc_id:
            open_budget_exceptions = open_budget_exceptions.filter(cost_center_id=selected_cc_id)
            open_match_exceptions = open_match_exceptions.filter(
                invoice__po__requisition__cost_center_id=selected_cc_id
            )
            ready_for_payment_qs = ready_for_payment_qs.filter(
                po__requisition__cost_center_id=selected_cc_id
            )
            committed_spend_qs = committed_spend_qs.filter(budget__cost_center_id=selected_cc_id)
            actual_spend_qs = actual_spend_qs.filter(budget__cost_center_id=selected_cc_id)

        committed_spend = committed_spend_qs.aggregate(total=Sum("amount"))["total"] or 0
        actual_spend = actual_spend_qs.aggregate(total=Sum("amount"))["total"] or 0

        selected_dept_obj = None
        if selected_dept_id:
            selected_dept_obj = next(
                (d for d in departments if str(d.id) == str(selected_dept_id)), None
            )

        selected_cc_obj = None
        if selected_cc_id:
            selected_cc_obj = next(
                (c for c in all_cost_centers if str(c.id) == str(selected_cc_id)), None
            )

        pr_budget_queue = get_pr_budget_review_queue(
            user,
            department_id=selected_dept_id or None,
            cost_center_id=selected_cc_id or None,
        )
        context = {
            "metrics": {
                "pr_budget_review": len(pr_budget_queue),
                "pending_match_exceptions": open_match_exceptions.count(),
                "pending_budget_exceptions": open_budget_exceptions.count(),
                "ready_for_payment": ready_for_payment_qs.count(),
                "committed_spend": float(committed_spend),
                "actual_spend": float(actual_spend),
            },
            "open_match_exceptions": open_match_exceptions[:10],
            "open_budget_exceptions": open_budget_exceptions[:10],
            "pr_budget_queue": pr_budget_queue,
            "departments": departments,
            "cost_centers": filtered_cost_centers,
            "all_cost_centers": all_cost_centers,
            "selected_dept_id": selected_dept_id,
            "selected_cc_id": selected_cc_id,
            "selected_dept_obj": selected_dept_obj,
            "selected_cc_obj": selected_cc_obj,
        }
        return render(request, "pages/dashboards/finance_dashboard.html", context)

    # 7. PROCUREMENT EXECUTIVE DASHBOARD
    elif role_code == Role.PROC_EXEC:
        from apps.notifications.models import Notification
        from apps.sourcing.selectors import get_sourcing_dashboard_metrics
        from apps.sourcing.services import sync_all_event_windows
        from apps.vendors.selectors import get_governance_metrics, get_governance_vendor_queryset

        sync_all_event_windows()
        now = timezone.now()
        org_events = tenancy.scope_sourcing_events(SourcingEvent.objects.all(), user)
        closing_soon = (
            org_events.filter(
                status=SourcingEvent.STATUS_BID_WINDOW,
                bid_end_date__gt=now,
                bid_end_date__lte=now + timezone.timedelta(days=3),
            )
            .annotate(invite_count=Count("invitations"))
            .order_by("bid_end_date")[:8]
        )
        action_queue = org_events.filter(
            status__in=[
                SourcingEvent.STATUS_DRAFT,
                SourcingEvent.STATUS_TECHNICAL_REVIEW,
                SourcingEvent.STATUS_COMMERCIAL_REVIEW,
                SourcingEvent.STATUS_AWARD_APPROVAL,
            ]
        ).order_by("-updated_at")[:10]
        attention_vendors = (
            get_governance_vendor_queryset(user=request.user)
            .filter(
                Q(status=Vendor.STATUS_ON_HOLD)
                | Q(latest_risk_level="HIGH")
                | Q(latest_composite_score__lt=70)
            )
            .order_by("-updated_at")[:8]
        )
        awarded_without_po = (
            org_events.filter(status=SourcingEvent.STATUS_AWARDED)
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
            .order_by("-updated_at")[:8]
        )
        avg_scorecard = tenancy.scope_scorecards(VendorScorecard.objects.all(), user).aggregate(
            avg=Avg("composite_score")
        )["avg"]
        ready_prs_count = scope_requisitions_to_org(
            PurchaseRequisition.objects.filter(status=PurchaseRequisition.STATUS_APPROVED), user
        ).count()
        context = {
            "sourcing": get_sourcing_dashboard_metrics(user),
            "governance": get_governance_metrics(user=user),
            "ready_prs_count": ready_prs_count,
            "open_pos": scope_purchase_orders_queryset(PurchaseOrder.objects.all(), user)
            .filter(
                status__in=[
                    PurchaseOrder.STATUS_ISSUED,
                    PurchaseOrder.STATUS_ACKNOWLEDGED,
                    PurchaseOrder.STATUS_PARTIAL_RECEIPT,
                ]
            )
            .count(),
            "pending_clarifications": Clarification.objects.filter(
                status="PENDING", event__in=org_events.values("pk")
            )
            .exclude(event__status__in=["AWARDED", "CANCELLED"])
            .count(),
            "avg_scorecard": round(float(avg_scorecard), 1) if avg_scorecard is not None else None,
            "unread_notifications": Notification.objects.filter(
                recipient=user, is_read=False
            ).count(),
            "closing_soon": closing_soon,
            "action_queue": action_queue,
            "attention_vendors": attention_vendors,
            "awarded_without_po": awarded_without_po,
        }
        return render(request, "pages/dashboards/procurement_dashboard.html", context)

    # 8. LEGAL / CONTRACT MANAGER DASHBOARD
    elif role_code == Role.LEGAL_MGR:
        from apps.contracts.views import dashboard_view as legal_dashboard_view

        return legal_dashboard_view(request)

    # 9. DEPARTMENT APPROVER DASHBOARD
    elif role_code == Role.DEPT_APPROVER:
        user_department = getattr(user, "department", None)
        from apps.budgets.models import Budget
        from apps.organization.models import CostCenter, Department, FiscalPeriod

        if not user_department:
            managed_cc = (
                CostCenter.objects.filter(manager=user).select_related("department").first()
            )
            if managed_cc and managed_cc.department:
                user_department = managed_cc.department
            elif getattr(user, "organization", None):
                user_department = Department.objects.filter(organization=user.organization).first()

        if user_department:
            dept_prs = PurchaseRequisition.objects.filter(
                Q(department=user_department) | Q(cost_center__manager=user)
            ).distinct()
        elif getattr(user, "organization", None):
            dept_prs = PurchaseRequisition.objects.filter(
                Q(department__organization=user.organization) | Q(cost_center__manager=user)
            ).distinct()
        else:
            dept_prs = PurchaseRequisition.objects.none()
        dept_prs = scope_requisitions_to_org(dept_prs, user)

        from apps.approvals.models import ApprovalAction

        user_approved_pr_ids = ApprovalAction.objects.filter(
            actor=user,
            target_model_name="PurchaseRequisition",
            action=ApprovalAction.ACTION_APPROVE,
        ).values_list("target_object_id", flat=True)

        pending_prs_qs = (
            dept_prs.filter(status=PurchaseRequisition.STATUS_MANAGER_REVIEW)
            .exclude(id__in=user_approved_pr_ids)
            .order_by("-updated_at")
            .prefetch_related("attachments")
        )
        budget_overrun_qs = pending_prs_qs.filter(status=PurchaseRequisition.STATUS_BUDGET_REVIEW)
        has_budget_overrun = budget_overrun_qs.exists()
        budget_overrun_count = budget_overrun_qs.count()

        now = timezone.now()
        current_period = FiscalPeriod.objects.filter(
            start_date__lte=now,
            end_date__gte=now,
            is_closed=False,
            organization=getattr(user, "effective_organization", None),
        ).first()

        available_budget = 0
        if current_period:
            if user_department:
                dept_ccs = CostCenter.objects.filter(department=user_department)
            elif getattr(user, "organization", None):
                dept_ccs = CostCenter.objects.filter(department__organization=user.organization)
            else:
                dept_ccs = CostCenter.objects.filter(manager=user)
            budgets = Budget.objects.filter(cost_center__in=dept_ccs, fiscal_period=current_period)
            available_budget = sum([b.available_amount for b in budgets])

        context = {
            "user_department": user_department,
            "has_budget_overrun": has_budget_overrun,
            "budget_overrun_count": budget_overrun_count,
            "metrics": {
                "total_prs_count": dept_prs.count(),
                "pending_count": pending_prs_qs.count(),
                "approved_count": dept_prs.filter(status="APPROVED").count(),
                "available_budget": float(available_budget),
                "budget_overrun_count": budget_overrun_count,
            },
            "pending_prs": pending_prs_qs[:10],
        }
        return render(request, "pages/dashboards/approver_dashboard.html", context)

    # 10. SUPER ADMIN DASHBOARD
    elif role_code == Role.SUPER_ADMIN:
        from apps.audit.models import AuditLog
        from apps.budgets.models import Budget
        from apps.invoices.models import MatchException
        from apps.organization.models import Organization

        scoped_org = (
            getattr(user, "effective_organization", None) if not user.is_superuser else None
        )

        today = timezone.now().date()
        users_qs = User.objects.all()
        budgets_qs = Budget.objects.all()
        prs_qs = PurchaseRequisition.objects.filter(
            status__in=[
                PurchaseRequisition.STATUS_SUBMITTED,
                PurchaseRequisition.STATUS_MANAGER_REVIEW,
                PurchaseRequisition.STATUS_BUDGET_REVIEW,
            ]
        )
        exceptions_qs = MatchException.objects.filter(status=MatchException.STATUS_OPEN)
        audits_qs = AuditLog.objects.filter(timestamp__date=today)
        orgs_qs = Organization.objects.select_related("admin_user").order_by("code")

        if not scoped_org and not user.is_superuser:
            # Fail closed: an org-less account sees nothing (only the platform superuser is global).
            users_qs = users_qs.none()
            budgets_qs = budgets_qs.none()
            prs_qs = prs_qs.none()
            exceptions_qs = exceptions_qs.none()
            audits_qs = audits_qs.none()
            orgs_qs = orgs_qs.none()
        if scoped_org:
            users_qs = users_qs.filter(
                Q(organization=scoped_org) | Q(department__organization=scoped_org)
            )
            budgets_qs = budgets_qs.filter(cost_center__department__organization=scoped_org)
            prs_qs = prs_qs.filter(department__organization=scoped_org)
            exceptions_qs = exceptions_qs.filter(
                invoice__po__requisition__department__organization=scoped_org
            )
            audits_qs = audits_qs.filter(
                Q(actor__organization=scoped_org) | Q(actor__department__organization=scoped_org)
            )
            orgs_qs = orgs_qs.filter(id=scoped_org.id)

        total_users = users_qs.count()
        budget_agg = budgets_qs.aggregate(total=Sum("allocated_amount"))["total"]
        allocated_budget = budget_agg if budget_agg is not None else Decimal("0.00")
        pending_pr_count = prs_qs.count()
        pending_exceptions = exceptions_qs.count()
        audits_today = audits_qs.count()

        is_global_superadmin = bool(not scoped_org and user.is_superuser)

        context = {
            "organizations": orgs_qs,
            "scoped_org": scoped_org,
            "is_global_superadmin": is_global_superadmin,
            "can_add_organization": is_global_superadmin,
            "total_organizations": (
                Organization.objects.count() if is_global_superadmin else (1 if scoped_org else 0)
            ),
            "active_organizations": (
                Organization.objects.filter(is_active=True).count()
                if is_global_superadmin
                else (1 if (scoped_org and scoped_org.is_active) else 0)
            ),
            "provisioned_admins": (
                Organization.objects.filter(admin_user__isnull=False).count()
                if is_global_superadmin
                else (1 if (scoped_org and scoped_org.admin_user) else 0)
            ),
            "metrics": {
                "total_users": total_users,
                "allocated_budget": allocated_budget,
                "pending_pr_count": pending_pr_count,
                "pending_exceptions": pending_exceptions,
                "audits_today": audits_today,
            },
            "kpi_total_users": total_users,
            "kpi_total_budget": allocated_budget,
            "kpi_pending_transactions": pending_pr_count + pending_exceptions,
            "kpi_audit_logs_today": audits_today,
        }
        return render(request, "pages/dashboards/superadmin_dashboard.html", context)

    # 11. COMPLIANCE AUDITOR DASHBOARD
    elif role_code == Role.AUDITOR:
        from apps.audit.views import auditor_dashboard_view

        return auditor_dashboard_view(request)

    # 12. VENDOR PORTAL DASHBOARD
    elif role_code == Role.VENDOR_USER:
        from apps.vendors.vendor_dashboard_views import vendor_dashboard_overview_view

        return vendor_dashboard_overview_view(request)

    # Fallback for all other unknown roles
    return render(request, "pages/dashboards/requester_dashboard.html")


def _is_api_request(request):
    accept = request.headers.get("Accept", "").lower()
    return request.path.startswith("/api/") or "application/json" in accept or "json" in accept


def custom_404_view(request, exception=None):
    if _is_api_request(request):
        return JsonResponse(
            {
                "error": {
                    "code": "NOT_FOUND",
                    "message": "The requested resource was not found.",
                    "details": {"path": request.path},
                }
            },
            status=404,
        )
    return render(request, "404.html", status=404)


def custom_500_view(request):
    if _is_api_request(request):
        return JsonResponse(
            {
                "error": {
                    "code": "INTERNAL_SERVER_ERROR",
                    "message": "An unexpected server error occurred.",
                    "details": {},
                }
            },
            status=500,
        )
    return render(request, "500.html", status=500)


def custom_403_view(request, exception=None):
    if _is_api_request(request):
        return JsonResponse(
            {
                "error": {
                    "code": "PERMISSION_DENIED",
                    "message": "You do not have permission to access this resource.",
                    "details": {"detail": str(exception) if exception else "Permission denied."},
                }
            },
            status=403,
        )
    return render(request, "403.html", status=403)


def _media_file_visible_to(user, rel_path):
    """True when a stored file belongs to a record visible to ``user`` (org / vendor scoped)."""
    from apps.contracts.models import Contract, ContractDocument
    from apps.core import tenancy
    from apps.reports.models import ExportJob
    from apps.requisitions.models import PRAttachment, PurchaseRequisition
    from apps.requisitions.selectors import scope_requisitions_to_org
    from apps.sourcing.models import BidAttachment, SourcingEvent
    from apps.vendors.models import Vendor, VendorDocument
    from apps.vendors.selectors import scope_vendors_queryset

    vendors = scope_vendors_queryset(Vendor.objects.all(), user)
    if VendorDocument.objects.filter(file=rel_path, vendor__in=vendors).exists():
        return True
    contracts = tenancy.scope_contracts(Contract.objects.all(), user)
    if ContractDocument.objects.filter(file=rel_path, contract__in=contracts).exists():
        return True
    bids = BidAttachment.objects.filter(file=rel_path)
    if tenancy.is_vendor_user(user):
        return bids.filter(bid__vendor_id=getattr(user, "vendor_id", None)).exists()
    events = tenancy.scope_sourcing_events(SourcingEvent.objects.all(), user)
    if bids.filter(bid__event__in=events).exists():
        return True
    prs = scope_requisitions_to_org(PurchaseRequisition.objects.all(), user)
    if PRAttachment.objects.filter(file=rel_path, requisition__in=prs).exists():
        return True
    jobs = tenancy.scope_to_org(
        ExportJob.objects.filter(result_file=rel_path),
        user,
        "requested_by__organization",
        "requested_by__department__organization",
    )
    return jobs.exists()


def protected_media_view(request, file_path):
    """
    Secure media delivery endpoint enforcing authentication, directory traversal
    protection, and object-level/role-based access control (DEF-002).
    """
    import mimetypes
    import os

    from django.conf import settings
    from django.http import FileResponse, Http404, HttpResponseForbidden

    if not request.user or not request.user.is_authenticated:
        if _is_api_request(request):
            return JsonResponse(
                {
                    "error": {
                        "code": "AUTHENTICATION_REQUIRED",
                        "message": "Authentication required to access media documents.",
                    }
                },
                status=401,
            )
        return HttpResponseForbidden("Authentication required to access media documents.")

    user = request.user
    role_code = getattr(user, "role_code", None) or (
        user.role.code if hasattr(user, "role") and user.role else None
    )

    # 1. Path traversal hardening
    normalized_path = os.path.normpath(file_path).lstrip("/\\")
    if normalized_path.startswith("..") or ".." in normalized_path.split(os.path.sep):
        return HttpResponseForbidden("Access denied: Invalid file path.")

    full_path = os.path.join(settings.MEDIA_ROOT, normalized_path)
    if not os.path.exists(full_path) or not os.path.isfile(full_path):
        raise Http404("Document not found.")

    # 2. RBAC and Ownership Enforcement
    if not user.is_superuser:
        norm_lower = normalized_path.lower()
        if role_code in [Role.REQUESTER, Role.DEPT_APPROVER, Role.STORES_RECEIVER]:
            if "contracts" in norm_lower or "contract_docs" in norm_lower:
                return HttpResponseForbidden(
                    "Access denied: Legal contracts are restricted to Legal and Procurement."
                )
            if "vendor_docs" in norm_lower or "kyc" in norm_lower:
                return HttpResponseForbidden(
                    "Access denied: Vendor KYC records are restricted to Procurement and Legal."
                )
        # Tenant isolation: the file must belong to a record the caller may see. Files that are
        # not attached to any record are served to the platform superuser only.
        if not _media_file_visible_to(user, normalized_path.replace("\\", "/")):
            return HttpResponseForbidden("Access denied: You may only access your own documents.")

    content_type, _ = mimetypes.guess_type(full_path)
    filename = os.path.basename(full_path)
    response = FileResponse(
        open(full_path, "rb"), content_type=content_type or "application/octet-stream"
    )
    response["Content-Disposition"] = f'inline; filename="{filename}"'
    return response


def protected_vendor_document_view(request, file_path):
    """Alias for backwards compatibility with vendor_documents specific route."""
    import os

    if not file_path.startswith("vendor_documents"):
        file_path = os.path.join("vendor_documents", file_path)
    return protected_media_view(request, file_path)

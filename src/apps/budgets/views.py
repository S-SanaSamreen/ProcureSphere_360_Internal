from django.contrib.auth.decorators import login_required
from django.shortcuts import render

from apps.budgets.models import Budget, SpendLedger
from apps.organization.models import CostCenter, Department, FiscalPeriod
from apps.requisitions.models import PurchaseRequisition


@login_required(login_url="/login/")
def list_view(request):
    dept_id = request.GET.get("department", "").strip()
    cc_id = request.GET.get("cost_center", "").strip()
    fp_id = request.GET.get("fiscal_period", "").strip()

    budgets = Budget.objects.select_related("cost_center__department", "fiscal_period").all()

    if dept_id:
        budgets = budgets.filter(cost_center__department_id=dept_id)
    if cc_id:
        budgets = budgets.filter(cost_center_id=cc_id)
    if fp_id:
        budgets = budgets.filter(fiscal_period_id=fp_id)

    departments = Department.objects.all().order_by("name")
    cost_centers = CostCenter.objects.select_related("department").all().order_by("code")
    fiscal_periods = FiscalPeriod.objects.all().order_by("-start_date")

    return render(
        request,
        "pages/budgets/list.html",
        {
            "budgets": budgets,
            "departments": departments,
            "cost_centers": cost_centers,
            "fiscal_periods": fiscal_periods,
            "selected_dept": dept_id,
            "selected_cc": cc_id,
            "selected_fp": fp_id,
        },
    )


@login_required(login_url="/login/")
def ledger_view(request):
    dept_id = request.GET.get("department", "").strip()
    cc_id = request.GET.get("cost_center", "").strip()
    fp_id = request.GET.get("fiscal_period", "").strip()

    ledger = SpendLedger.objects.select_related(
        "budget__cost_center__department", "budget__fiscal_period"
    ).order_by("-created_at")

    if dept_id:
        ledger = ledger.filter(budget__cost_center__department_id=dept_id)
    if cc_id:
        ledger = ledger.filter(budget__cost_center_id=cc_id)
    if fp_id:
        ledger = ledger.filter(budget__fiscal_period_id=fp_id)

    departments = Department.objects.all().order_by("name")
    cost_centers = CostCenter.objects.select_related("department").all().order_by("code")
    fiscal_periods = FiscalPeriod.objects.all().order_by("-start_date")

    return render(
        request,
        "pages/budgets/ledger.html",
        {
            "ledger": ledger,
            "departments": departments,
            "cost_centers": cost_centers,
            "fiscal_periods": fiscal_periods,
            "selected_dept": dept_id,
            "selected_cc": cc_id,
            "selected_fp": fp_id,
        },
    )


@login_required(login_url="/login/")
def budget_exceptions_view(request):
    dept_id = request.GET.get("department", "").strip()
    cc_id = request.GET.get("cost_center", "").strip()

    exceptions = (
        PurchaseRequisition.objects.filter(status=PurchaseRequisition.STATUS_BUDGET_REVIEW)
        .select_related("department", "cost_center", "requester")
        .order_by("-updated_at")
    )

    if dept_id:
        exceptions = exceptions.filter(department_id=dept_id)
    if cc_id:
        exceptions = exceptions.filter(cost_center_id=cc_id)

    departments = Department.objects.all().order_by("name")
    cost_centers = CostCenter.objects.select_related("department").all().order_by("code")

    return render(
        request,
        "pages/budgets/exceptions.html",
        {
            "exceptions": exceptions,
            "departments": departments,
            "cost_centers": cost_centers,
            "selected_dept": dept_id,
            "selected_cc": cc_id,
        },
    )

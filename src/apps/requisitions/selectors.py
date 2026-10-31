from .models import PurchaseRequisition


def get_all_requisitions():
    return (
        PurchaseRequisition.objects.select_related("requester", "department", "cost_center")
        .prefetch_related("lines", "attachments")
        .order_by("-created_at")
    )


def get_requisition_by_id(pr_id):
    return (
        PurchaseRequisition.objects.select_related("requester", "department", "cost_center")
        .prefetch_related("lines", "attachments")
        .filter(id=pr_id)
        .first()
    )


def get_requisitions_by_department(department_id):
    return (
        PurchaseRequisition.objects.select_related("requester", "cost_center")
        .filter(department_id=department_id)
        .order_by("-created_at")
    )


def get_user_requisitions(user):
    if not user or not user.is_authenticated:
        return PurchaseRequisition.objects.none()
    if user.is_superuser or getattr(user, "role_code", "") in [
        "SUPER_ADMIN",
        "PROCUREMENT_MANAGER",
        "FINANCE_AP",
    ]:
        return get_all_requisitions()
    return (
        PurchaseRequisition.objects.select_related("requester", "department", "cost_center")
        .prefetch_related("lines", "attachments")
        .filter(requester=user)
        .order_by("-created_at")
    )


def get_pr_budget_review_queue(user, *, department_id=None, cost_center_id=None):
    """
    Finance/AP "PR Budget Review" queue: this organization's PRs at the BUDGET_REVIEW stage whose
    Finance step is still open, each with its live budget position (allocated / reserved /
    committed / available, Under / Out of budget and shortfall). A user without an organization
    sees nothing.
    """
    from apps.approvals.models import ApprovalPolicy, ApprovalRequest
    from apps.budgets.services import pr_budget_position

    org_id = getattr(user, "organization_id", None)
    if not getattr(user, "is_authenticated", False) or not org_id:
        return []
    prs = PurchaseRequisition.objects.filter(
        status=PurchaseRequisition.STATUS_BUDGET_REVIEW, department__organization_id=org_id
    ).select_related("department", "cost_center", "requester")
    if department_id:
        prs = prs.filter(department_id=department_id)
    if cost_center_id:
        prs = prs.filter(cost_center_id=cost_center_id)
    requests = {
        r.target_object_id: r
        for r in ApprovalRequest.objects.filter(
            module=ApprovalPolicy.MODULE_PR,
            status=ApprovalRequest.STATUS_PENDING,
            target_model_name="PurchaseRequisition",
            target_object_id__in=[p.id for p in prs],
        )
    }
    rows = []
    for pr in prs.order_by("updated_at"):
        if pr.id in requests:
            rows.append({"pr": pr, "request": requests[pr.id], "position": pr_budget_position(pr)})
    return rows


def scope_requisitions_to_org(qs, user):
    """
    Organisation isolation for requisition querysets: only the platform superuser spans
    organisations; every other user sees just their own organisation's requisitions
    (an internal user without an organisation sees nothing).
    """
    if not user or not user.is_authenticated:
        return qs.none()
    if user.is_superuser:
        return qs
    org = getattr(user, "effective_organization", None)
    if org is None:
        return qs.none()
    return qs.filter(department__organization=org)

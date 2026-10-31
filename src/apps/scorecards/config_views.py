"""Super Admin screen: per-organization supplier scorecard dimension weights."""

from decimal import Decimal, InvalidOperation

from django.contrib import messages
from django.contrib.auth.decorators import login_required
from django.http import HttpResponse
from django.shortcuts import redirect, render

from apps.audit.models import AuditLog
from apps.audit.services import create_audit_log_service
from apps.core.superadmin_views import get_user_scoped_org, super_admin_required
from apps.organization.models import Organization

from .models import DEFAULT_WEIGHTS, DIMENSION_LABELS, DIMENSIONS, ScorecardWeight, get_weights

_DENIED = "<div class='alert alert-danger m-3'>Permission Denied: {}</div>"


def _resolve_org(request):
    """The user's own organization; a global superuser (no org) must pick one via ?org=."""
    org = get_user_scoped_org(request.user)
    if org is not None:
        return org
    if request.user.is_superuser:
        org_id = request.POST.get("org") or request.GET.get("org")
        if org_id:
            return Organization.objects.filter(pk=org_id).first()
    return None


@login_required
@super_admin_required
def scorecard_weights_view(request):
    org = _resolve_org(request)
    if org is None:
        if request.user.is_superuser and not get_user_scoped_org(request.user):
            return render(
                request,
                "pages/dashboards/superadmin/scorecard_weights.html",
                {"organizations": Organization.objects.order_by("name"), "org": None},
            )
        return HttpResponse(_DENIED.format("Organization assignment required."), status=403)

    existing = ScorecardWeight.objects.filter(organization=org).first()
    current = get_weights(org)
    errors = []
    if request.method == "POST":
        new = {}
        for d in DIMENSIONS:
            raw = (request.POST.get(f"{d}_weight") or "").strip()
            try:
                new[d] = Decimal(raw)
            except InvalidOperation:
                errors.append(f"{DIMENSION_LABELS[d]} weight must be a number.")
        if not errors:
            row = existing or ScorecardWeight(organization=org, created_by=request.user)
            for d, v in new.items():
                setattr(row, f"{d}_weight", v)
            try:
                row.save()
            except Exception as exc:  # ValidationError: message list
                errors.extend(getattr(exc, "messages", [str(exc)]))
            else:
                create_audit_log_service(
                    actor=request.user,
                    action=AuditLog.ACTION_UPDATE if existing else AuditLog.ACTION_CREATE,
                    target_model="ScorecardWeight",
                    target_object_id=str(row.id),
                    previous_state={d: str(w) for d, w in current.items()} if existing else None,
                    new_state={"organization": org.code, **{d: str(w) for d, w in new.items()}},
                    ip_address=request.META.get("REMOTE_ADDR"),
                    user_agent=request.META.get("HTTP_USER_AGENT", ""),
                )
                messages.success(request, "Scorecard weights saved.")
                url = "/superadmin/scorecard-weights/"
                return redirect(f"{url}?org={org.pk}" if request.user.is_superuser else url)
        current = {d: request.POST.get(f"{d}_weight", "") for d in DIMENSIONS}
    rows = [
        {
            "key": d,
            "label": DIMENSION_LABELS[d],
            "value": current[d],
            "default": DEFAULT_WEIGHTS[d],
        }
        for d in DIMENSIONS
    ]
    return render(
        request,
        "pages/dashboards/superadmin/scorecard_weights.html",
        {"org": org, "rows": rows, "errors": errors, "is_default": existing is None},
    )

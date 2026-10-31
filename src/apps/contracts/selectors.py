from django.db.models import Q, Sum
from django.utils import timezone

from apps.core import tenancy

from .models import Contract, ContractAlert, ContractMilestone, ContractObligation


def _scope_children(qs, user):
    """Scope obligations / milestones / alerts / documents through their parent contract."""
    parents = tenancy.scope_contracts(Contract.objects.all(), user).values("pk")
    return qs.filter(contract_id__in=parents)


def get_contracts_qs(user=None, status=None, search=None):
    """
    Returns filtered and scoped contracts QuerySet.
    """
    qs = Contract.objects.select_related(
        "vendor", "contract_owner", "sourcing_event", "po"
    ).prefetch_related("milestones", "obligations", "documents", "alerts", "versions")

    if user:
        qs = tenancy.scope_contracts(qs, user)

    if status:
        qs = qs.filter(status=status)

    if search:
        qs = qs.filter(
            Q(contract_number__icontains=search)
            | Q(title__icontains=search)
            | Q(vendor__legal_name__icontains=search)
        )

    return qs.order_by("-created_at")


def get_all_contracts():
    return get_contracts_qs()


def get_contract_by_id(contract_id, user=None):
    qs = (
        Contract.objects.select_related("vendor", "contract_owner", "sourcing_event", "po")
        .prefetch_related(
            "milestones",
            "obligations",
            "documents",
            "alerts",
            "versions",
            "versions__approved_by",
            "documents__uploaded_by",
        )
        .filter(id=contract_id)
    )
    if user is not None:
        qs = tenancy.scope_contracts(qs, user)
    return qs.first()


def get_contracts_pending_legal_review():
    return get_contracts_qs(status=Contract.STATUS_LEGAL_REVIEW)


def get_expiring_contracts(days=30, user=None):
    today = timezone.now().date()
    future_date = today + timezone.timedelta(days=days)
    qs = Contract.objects.select_related("vendor", "contract_owner").filter(
        status__in=[Contract.STATUS_ACTIVE, Contract.STATUS_RENEWAL_DUE],
        end_date__lte=future_date,
    )
    if user:
        qs = tenancy.scope_contracts(qs, user)
    return qs.order_by("end_date")


def get_pending_obligations(user=None):
    qs = ContractObligation.objects.select_related("contract", "contract__vendor").filter(
        is_fulfilled=False
    )
    if user:
        qs = _scope_children(qs, user)
    return qs.order_by("due_date")


def get_contract_obligations_qs(
    user=None,
    due_status=None,
    responsible_party=None,
    start_date=None,
    end_date=None,
    search=None,
):
    """
    Returns filtered and scoped ContractObligation QuerySet.
    """
    today = timezone.now().date()
    qs = ContractObligation.objects.select_related("contract", "contract__vendor")

    if user:
        qs = _scope_children(qs, user)

    if due_status:
        st = str(due_status).upper().strip()
        if st == "OVERDUE":
            qs = qs.filter(is_fulfilled=False, due_date__lt=today)
        elif st == "UPCOMING":
            qs = qs.filter(is_fulfilled=False, due_date__gte=today)
        elif st == "FULFILLED":
            qs = qs.filter(is_fulfilled=True)

    if responsible_party:
        qs = qs.filter(responsible_party__iexact=str(responsible_party).strip())

    if start_date:
        qs = qs.filter(due_date__gte=start_date)

    if end_date:
        qs = qs.filter(due_date__lte=end_date)

    if search:
        qs = qs.filter(
            Q(title__icontains=search)
            | Q(contract__contract_number__icontains=search)
            | Q(contract__title__icontains=search)
            | Q(contract__vendor__legal_name__icontains=search)
        )

    return qs.order_by("due_date")


def get_pending_milestones(user=None):
    qs = ContractMilestone.objects.select_related("contract", "contract__vendor").filter(
        is_completed=False
    )
    if user:
        qs = _scope_children(qs, user)
    return qs.order_by("due_date")


def get_active_contract_alerts(user=None):
    qs = ContractAlert.objects.select_related("contract", "contract__vendor").filter(
        is_processed=False
    )
    if user:
        qs = _scope_children(qs, user)
    return qs.order_by("-triggered_at")


def get_legal_dashboard_metrics(user=None):
    today = timezone.now().date()
    qs = Contract.objects.all()
    if user:
        qs = tenancy.scope_contracts(qs, user)

    total_contracts = qs.count()
    active_contracts = qs.filter(
        status__in=[Contract.STATUS_ACTIVE, Contract.STATUS_RENEWED]
    ).count()
    pending_legal_review = qs.filter(status=Contract.STATUS_LEGAL_REVIEW).count()
    expiring_soon = qs.filter(
        status__in=[Contract.STATUS_ACTIVE, Contract.STATUS_RENEWAL_DUE],
        end_date__lte=today + timezone.timedelta(days=30),
    ).count()

    total_value = qs.aggregate(total=Sum("contract_value"))["total"] or 0

    obligations_qs = ContractObligation.objects.all()
    milestones_qs = ContractMilestone.objects.all()
    if user:
        obligations_qs = _scope_children(obligations_qs, user)
        milestones_qs = _scope_children(milestones_qs, user)

    open_obligations = obligations_qs.filter(is_fulfilled=False).count()
    overdue_obligations = obligations_qs.filter(is_fulfilled=False, due_date__lt=today).count()
    open_milestones = milestones_qs.filter(is_completed=False).count()

    from apps.vendors.models import VendorCredentialDispatch

    pending_dispatches = VendorCredentialDispatch.objects.filter(
        legal_verification_status=VendorCredentialDispatch.LEGAL_STATUS_PENDING
    )
    if user:
        from apps.vendors.models import Vendor
        from apps.vendors.selectors import scope_vendors_queryset

        pending_dispatches = pending_dispatches.filter(
            vendor__in=scope_vendors_queryset(Vendor.objects.all(), user)
        )
    pending_vendor_verifications = pending_dispatches.count()

    return {
        "total_contracts": total_contracts,
        "active_contracts": active_contracts,
        "pending_legal_review": pending_legal_review,
        "expiring_soon": expiring_soon,
        "total_contract_value": float(total_value),
        "open_obligations": open_obligations,
        "overdue_obligations": overdue_obligations,
        "open_milestones": open_milestones,
        "pending_vendor_verifications": pending_vendor_verifications,
    }


def get_pending_vendor_verifications_qs(user=None):
    """
    Returns list of records pending Legal Manager verification with pre-evaluated KYC compliance reports.
    Includes:
    1. VendorCredentialDispatch records pending legal verification.
    2. Vendors with replacement/renewed KYC documents in PENDING status,
       ensuring replacement documents enter the Legal review queue even when no credential dispatch is pending.
    """
    from django.db.models import Prefetch

    from apps.vendors.kyc_policy import evaluate_vendor_kyc_compliance
    from apps.vendors.models import Vendor, VendorCredentialDispatch, VendorDocument

    doc_prefetch = Prefetch(
        "vendor__documents",
        queryset=VendorDocument.objects.select_related(
            "verified_by", "replaces_document"
        ).prefetch_related("review_history"),
    )

    from apps.vendors.selectors import scope_vendors_queryset

    visible_vendors = (
        scope_vendors_queryset(Vendor.objects.all(), user) if user is not None else None
    )
    dispatches = list(
        VendorCredentialDispatch.objects.filter(
            legal_verification_status=VendorCredentialDispatch.LEGAL_STATUS_PENDING
        )
        .filter(**({"vendor__in": visible_vendors} if visible_vendors is not None else {}))
        .select_related("vendor", "vendor__category", "user", "created_by")
        .prefetch_related(doc_prefetch)
        .order_by("-created_at")
    )
    seen_vendor_ids = {d.vendor_id for d in dispatches if d.vendor_id}

    vendor_doc_prefetch = Prefetch(
        "documents",
        queryset=VendorDocument.objects.select_related(
            "verified_by", "replaces_document"
        ).prefetch_related("review_history"),
    )
    vendor_dispatch_prefetch = Prefetch(
        "credential_dispatches",
        queryset=VendorCredentialDispatch.objects.select_related("user", "created_by").order_by(
            "-created_at"
        ),
    )

    # Include vendors that have pending documents but no pending credential dispatch
    vendors_with_pending_docs = (
        (Vendor.objects.all() if visible_vendors is None else visible_vendors)
        .filter(documents__status=VendorDocument.STATUS_PENDING)
        .exclude(id__in=seen_vendor_ids)
        .select_related("category", "created_by")
        .prefetch_related(vendor_doc_prefetch, vendor_dispatch_prefetch)
        .distinct()
    )

    additional_items = []
    for vendor in vendors_with_pending_docs:
        # Use cached prefetched list instead of issuing a new SQL query per vendor
        dispatches_list = list(vendor.credential_dispatches.all())
        dispatch = dispatches_list[0] if dispatches_list else None
        if dispatch:
            additional_items.append(dispatch)
        else:

            class _VendorDocReviewItem:
                def __init__(self, v):
                    self.id = v.id
                    self.vendor = v
                    self.user = None
                    self.sent_to = v.email
                    self.created_by = v.created_by
                    self.created_at = v.created_at
                    self.legal_verification_status = "DOCUMENTS_PENDING"

            additional_items.append(_VendorDocReviewItem(vendor))

    all_items = dispatches + additional_items
    for item in all_items:
        item.kyc_compliance = evaluate_vendor_kyc_compliance(item.vendor)
    return all_items


def get_pending_documents_for_review_qs(user=None):
    """
    Returns QuerySet of VendorDocument records with status STATUS_PENDING awaiting Legal review.
    """
    from apps.vendors.models import Vendor, VendorDocument
    from apps.vendors.selectors import scope_vendors_queryset

    qs = VendorDocument.objects.filter(status=VendorDocument.STATUS_PENDING)
    if user is not None:
        qs = qs.filter(vendor__in=scope_vendors_queryset(Vendor.objects.all(), user))
    return qs.select_related("vendor", "vendor__category", "replaces_document").order_by(
        "-created_at"
    )

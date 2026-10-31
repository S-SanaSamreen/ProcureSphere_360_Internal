"""Organization-level procurement controls with deployment-wide defaults from settings."""

from dataclasses import dataclass
from decimal import Decimal

from django.conf import settings


@dataclass(frozen=True)
class Controls:
    min_bidders: int
    enforce_min_bidders: bool
    publish_approval_threshold: Decimal | None
    over_delivery_tolerance_pct: Decimal


def get_controls(organization) -> Controls:
    """Controls for an organization (row if configured, else settings defaults)."""
    row = None
    if organization is not None:
        from .models import ProcurementControlSetting

        row = ProcurementControlSetting.objects.filter(organization=organization).first()
    if row is not None:
        return Controls(
            min_bidders=row.min_bidders,
            enforce_min_bidders=row.enforce_min_bidders,
            publish_approval_threshold=row.publish_approval_threshold,
            over_delivery_tolerance_pct=row.over_delivery_tolerance_pct,
        )
    threshold = getattr(settings, "SOURCING_PUBLISH_APPROVAL_THRESHOLD", None)
    return Controls(
        min_bidders=int(getattr(settings, "SOURCING_MIN_BIDDERS", 3)),
        enforce_min_bidders=bool(getattr(settings, "SOURCING_ENFORCE_MIN_BIDDERS", False)),
        publish_approval_threshold=None if threshold is None else Decimal(str(threshold)),
        over_delivery_tolerance_pct=Decimal(
            str(getattr(settings, "OVER_DELIVERY_TOLERANCE_PCT", "0"))
        ),
    )


def po_organization(po):
    """Organization a purchase order belongs to (cost center, else requisition, else vendor)."""
    cc = getattr(po, "cost_center", None)
    if cc is not None and cc.department_id:
        return cc.department.organization
    req = getattr(po, "requisition", None)
    if req is not None and req.department_id:
        return req.department.organization
    vendor = getattr(po, "vendor", None)
    return getattr(vendor, "organization", None)


def event_organization(event):
    req = event.requisition
    if req is not None and req.department_id:
        return req.department.organization
    creator = event.created_by
    return getattr(creator, "effective_organization", None) if creator else None

from rest_framework import permissions
from apps.accounts.models import Role


def is_procurement_user(user) -> bool:
    """Returns True if the user is authenticated and has a procurement / admin role."""
    return user.is_authenticated and (
        user.is_superuser or
        user.role_code in [Role.PROC_EXEC, Role.PROC_MGR, Role.SUPER_ADMIN]
    )


def is_vendor_user(user) -> bool:
    """Returns True if the user is authenticated and is a vendor user with a linked vendor."""
    return (
        user.is_authenticated
        and getattr(user, "is_vendor", False)
        and getattr(user, "vendor_id", None) is not None
    )


def can_vendor_access_event(user, event) -> bool:
    """Checks if a vendor user is invited and the event is published/open."""
    if not is_vendor_user(user):
        return False
    from .models import BidInvite, SourcingEvent
    if event.status == SourcingEvent.STATUS_DRAFT:
        return False
    return BidInvite.objects.filter(event=event, vendor=user.vendor).exists()


def can_vendor_modify_bid(user, bid) -> bool:
    """Checks if a vendor user can create, edit draft, or amend this bid."""
    if not is_vendor_user(user):
        return False
    if bid.vendor_id != user.vendor_id:
        return False
    return bid.is_editable or bid.can_be_amended


class IsVendorUser(permissions.BasePermission):
    """DRF permission allowing only authenticated vendor users."""
    def has_permission(self, request, view):
        return is_vendor_user(request.user)


class IsProcurementUser(permissions.BasePermission):
    """DRF permission allowing only procurement executives, managers, or superadmins."""
    def has_permission(self, request, view):
        return is_procurement_user(request.user)


class IsBidOwnerOrProcurement(permissions.BasePermission):
    """
    Object-level DRF permission:
    - Vendor users can ONLY access their own vendor's bid.
    - Procurement users can view bids once submitted, respecting sealed privacy.
    """
    def has_object_permission(self, request, view, obj):
        # Handle VendorBid or related child objects (BidAttachment, BidVersion, BidLine)
        bid = getattr(obj, "bid", obj)
        user = request.user

        if is_vendor_user(user):
            return bid.vendor_id == user.vendor_id

        if is_procurement_user(user):
            # In safe methods (GET, HEAD, OPTIONS), procurement can view
            # For state-changing operations, only vendor owner can submit/amend/attach
            if request.method in permissions.SAFE_METHODS:
                return True
            return False

        return False

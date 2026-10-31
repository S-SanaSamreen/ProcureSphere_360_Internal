from .models import GoodsReceipt


def get_all_goods_receipts():
    return (
        GoodsReceipt.objects.select_related("po", "received_by")
        .prefetch_related("lines")
        .order_by("-received_date")
    )


def get_goods_receipt_by_id(grn_id):
    return (
        GoodsReceipt.objects.select_related("po", "received_by")
        .prefetch_related("lines__inspection", "lines__rejections")
        .filter(id=grn_id)
        .first()
    )


def get_receipts_for_po(po_id):
    return (
        GoodsReceipt.objects.select_related("received_by")
        .prefetch_related("lines")
        .filter(po_id=po_id)
        .order_by("-received_date")
    )


# ----------------------------------------------------------------------------- org scoping
def _visible_pos(user):
    from apps.orders.models import PurchaseOrder
    from apps.orders.selectors import scope_purchase_orders_queryset

    return scope_purchase_orders_queryset(PurchaseOrder.objects.all(), user)


def scoped_purchase_orders(user):
    """Purchase orders this user may see (``None`` = internal call, unscoped)."""
    from apps.orders.models import PurchaseOrder

    if user is None:
        return PurchaseOrder.objects.all()
    return _visible_pos(user)


def scoped_receipts(user):
    """Goods receipts of the purchase orders this user may see (org / vendor isolated)."""
    if user is None:
        return GoodsReceipt.objects.all()
    return GoodsReceipt.objects.filter(po__in=_visible_pos(user))


def scoped_receipt_lines(user):
    from .models import ReceiptLine

    if user is None:
        return ReceiptLine.objects.all()
    return ReceiptLine.objects.filter(receipt__po__in=_visible_pos(user))


def scoped_rejections(user):
    from .models import RejectionRecord

    if user is None:
        return RejectionRecord.objects.all()
    return RejectionRecord.objects.filter(receipt_line__receipt__po__in=_visible_pos(user))

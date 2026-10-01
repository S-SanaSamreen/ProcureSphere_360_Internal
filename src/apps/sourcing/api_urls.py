from rest_framework.routers import DefaultRouter

from .api_views import SourcingEventViewSet, VendorBidViewSet

router = DefaultRouter()
router.register(r"events", SourcingEventViewSet, basename="sourcing-event")
router.register(r"bids", VendorBidViewSet, basename="vendor-bid")

urlpatterns = router.urls

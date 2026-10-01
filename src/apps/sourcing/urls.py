from django.urls import path
from . import views

app_name = "sourcing"

urlpatterns = [
    # Procurement Executive views
    path("", views.sourcing_list_view, name="list"),
    path("create/", views.sourcing_create_view, name="create"),
    path("<uuid:pk>/", views.sourcing_detail_view, name="detail"),
    path("<uuid:pk>/edit/", views.sourcing_edit_view, name="edit"),
    path("<uuid:pk>/invite-vendors/", views.sourcing_invite_vendors_view, name="invite-vendors"),
    path("<uuid:pk>/remove-vendor/<uuid:vendor_id>/", views.sourcing_remove_vendor_view, name="remove-vendor"),
    path("<uuid:pk>/add-technical-req/", views.sourcing_add_technical_req_view, name="add-tech-req"),
    path("<uuid:pk>/add-commercial-req/", views.sourcing_add_commercial_req_view, name="add-comm-req"),
    path("<uuid:pk>/publish/", views.sourcing_publish_view, name="publish"),
    path("<uuid:pk>/close/", views.sourcing_close_view, name="close"),
    path("<uuid:pk>/clarifications/<uuid:clarification_id>/respond/", views.sourcing_respond_clarification_view, name="respond-clarification"),

    # Vendor views
    path("<uuid:pk>/submit-clarification/", views.sourcing_submit_clarification_view, name="submit-clarification"),
    path("my-invitations/", views.vendor_my_invitations_view, name="my-invitations"),

    # Vendor Bid Portal endpoints
    path("<uuid:pk>/bid/", views.vendor_bid_workspace_view, name="vendor-bid-workspace"),
    path("bids/<uuid:bid_id>/upload-attachment/", views.vendor_bid_upload_attachment_view, name="bid-upload-attachment"),
    path("bids/attachments/<uuid:attachment_id>/delete/", views.vendor_bid_delete_attachment_view, name="bid-delete-attachment"),
    path("bids/attachments/<uuid:attachment_id>/download/", views.vendor_bid_download_attachment_view, name="bid-download-attachment"),
    path("bids/<uuid:bid_id>/amend/", views.vendor_bid_amend_view, name="bid-amend"),
    path("bids/<uuid:bid_id>/versions/<int:version_number>/", views.vendor_bid_version_detail_view, name="bid-version-detail"),

    # Procurement Executive Review endpoints
    path("<uuid:pk>/submissions/", views.procurement_event_bids_view, name="event-submissions"),
    path("submissions/<uuid:bid_id>/", views.procurement_bid_detail_view, name="procurement-bid-detail"),
]

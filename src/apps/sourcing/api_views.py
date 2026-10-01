from django.core.exceptions import ValidationError as DjangoValidationError
from rest_framework import status, viewsets
from rest_framework.decorators import action
from rest_framework.exceptions import PermissionDenied, ValidationError
from rest_framework.permissions import IsAuthenticated
from rest_framework.response import Response

from .models import BidAttachment, BidVersion, SourcingEvent, VendorBid
from .permissions import IsBidOwnerOrProcurement
from .serializers import (
    BidAttachmentSerializer, BidVersionSerializer,
    SourcingEventSerializer, VendorBidSerializer,
)
from .services import (
    amend_vendor_bid_service, delete_bid_attachment_service,
    save_bid_draft_service, submit_vendor_bid_service,
    upload_bid_attachment_service, validate_bid_submission_service,
)


class SourcingEventViewSet(viewsets.ReadOnlyModelViewSet):
    """API endpoint for retrieving sourcing events."""
    queryset = SourcingEvent.objects.prefetch_related(
        "technical_requirements", "commercial_requirements", "invitations"
    ).all()
    serializer_class = SourcingEventSerializer
    permission_classes = [IsAuthenticated]

    def get_queryset(self):
        user = self.request.user
        if getattr(user, "is_vendor", False) and getattr(user, "vendor_id", None):
            # Vendors see only events they are invited to and that are not DRAFT
            return self.queryset.filter(
                invitations__vendor_id=user.vendor_id
            ).exclude(status=SourcingEvent.STATUS_DRAFT).distinct()
        return self.queryset.all()


class VendorBidViewSet(viewsets.ModelViewSet):
    """
    API endpoint for vendor bid management with sealed bid privacy:
    - Vendors can only access and modify their own bids.
    - Procurement users can view submitted bids after BID_WINDOW closes.
    """
    queryset = (
        VendorBid.objects.select_related("event", "vendor", "submitted_by")
        .prefetch_related(
            "lines__commercial_requirement",
            "technical_responses__requirement",
            "attachments__uploaded_by",
            "versions__submitted_by",
        )
        .all()
    )
    serializer_class = VendorBidSerializer
    permission_classes = [IsAuthenticated, IsBidOwnerOrProcurement]

    def get_queryset(self):
        user = self.request.user
        if getattr(user, "is_vendor", False) and getattr(user, "vendor_id", None):
            return self.queryset.filter(vendor_id=user.vendor_id)

        # Procurement users: if event is in BID_WINDOW, evaluate sealed privacy
        return self.queryset.all()

    @action(detail=True, methods=["post"], url_path="save-draft")
    def save_draft(self, request, pk=None):
        """Saves WIP responses without final submission."""
        bid = self.get_object()
        if getattr(request.user, "is_vendor", False) and bid.vendor_id != request.user.vendor_id:
            raise PermissionDenied("You are not authorized to modify this bid.")

        proposal_summary = request.data.get("proposal_summary")
        technical_responses = request.data.get("technical_responses")
        line_items = request.data.get("line_items")

        try:
            bid = save_bid_draft_service(
                bid=bid,
                user=request.user,
                proposal_summary=proposal_summary,
                technical_responses=technical_responses,
                line_items=line_items,
            )
            return Response(self.get_serializer(bid).data)
        except DjangoValidationError as e:
            return Response({"error": {"code": "VALIDATION_ERROR", "message": str(e)}}, status=status.HTTP_400_BAD_REQUEST)

    @action(detail=True, methods=["post"], url_path="validate")
    def validate_bid(self, request, pk=None):
        """Performs server-side validation against all sourcing rules."""
        bid = self.get_object()
        is_valid, issues = validate_bid_submission_service(bid=bid, user=request.user)
        return Response({
            "is_valid": is_valid,
            "issues": issues,
            "status": bid.status,
        })

    @action(detail=True, methods=["post"], url_path="submit")
    def submit_bid(self, request, pk=None):
        """Submits the validated bid atomically, sealing it and creating BidVersion 1."""
        bid = self.get_object()
        if getattr(request.user, "is_vendor", False) and bid.vendor_id != request.user.vendor_id:
            raise PermissionDenied("You are not authorized to submit this bid.")

        try:
            bid = submit_vendor_bid_service(bid=bid, submitted_by_user=request.user)
            return Response(self.get_serializer(bid).data, status=status.HTTP_200_OK)
        except DjangoValidationError as e:
            return Response({"error": {"code": "SUBMISSION_REJECTED", "message": str(e)}}, status=status.HTTP_400_BAD_REQUEST)

    @action(detail=True, methods=["post"], url_path="amend")
    def amend_bid(self, request, pk=None):
        """Amends a submitted bid before deadline, archiving current version into BidVersion."""
        bid = self.get_object()
        if getattr(request.user, "is_vendor", False) and bid.vendor_id != request.user.vendor_id:
            raise PermissionDenied("You are not authorized to amend this bid.")

        change_summary = request.data.get("change_summary")
        if not change_summary:
            return Response({"error": {"code": "REQUIRED_FIELD", "message": "change_summary is required."}}, status=status.HTTP_400_BAD_REQUEST)

        proposal_summary = request.data.get("proposal_summary")
        line_items = request.data.get("line_items")
        technical_responses = request.data.get("technical_responses")

        try:
            bid = amend_vendor_bid_service(
                bid=bid,
                user=request.user,
                change_summary=change_summary,
                proposal_summary=proposal_summary,
                line_items=line_items,
                technical_responses=technical_responses,
            )
            return Response(self.get_serializer(bid).data, status=status.HTTP_200_OK)
        except DjangoValidationError as e:
            return Response({"error": {"code": "AMENDMENT_REJECTED", "message": str(e)}}, status=status.HTTP_400_BAD_REQUEST)

    @action(detail=True, methods=["get", "post"], url_path="attachments")
    def attachments(self, request, pk=None):
        """List or upload attachments for a vendor bid."""
        bid = self.get_object()
        if getattr(request.user, "is_vendor", False) and bid.vendor_id != request.user.vendor_id:
            raise PermissionDenied("Access denied.")

        if request.method == "GET":
            serializer = BidAttachmentSerializer(bid.attachments.all(), many=True)
            return Response(serializer.data)

        # POST upload
        file = request.FILES.get("file")
        if not file:
            return Response({"error": {"code": "FILE_REQUIRED", "message": "No file uploaded."}}, status=status.HTTP_400_BAD_REQUEST)

        title = request.data.get("title", file.name)
        doc_type = request.data.get("doc_type", BidAttachment.DOC_TYPE_SUPPORTING)

        try:
            attachment = upload_bid_attachment_service(
                bid=bid,
                file=file,
                title=title,
                doc_type=doc_type,
                user=request.user,
            )
            return Response(BidAttachmentSerializer(attachment).data, status=status.HTTP_201_CREATED)
        except DjangoValidationError as e:
            return Response({"error": {"code": "UPLOAD_FAILED", "message": str(e)}}, status=status.HTTP_400_BAD_REQUEST)

    @action(detail=True, methods=["get"], url_path="versions")
    def versions(self, request, pk=None):
        """Retrieve frozen historical versions for a vendor bid."""
        bid = self.get_object()
        if getattr(request.user, "is_vendor", False) and bid.vendor_id != request.user.vendor_id:
            raise PermissionDenied("Access denied.")

        serializer = BidVersionSerializer(bid.versions.all(), many=True)
        return Response(serializer.data)

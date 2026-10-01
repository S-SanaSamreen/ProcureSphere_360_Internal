from rest_framework import serializers

from .models import (
    BidAttachment, BidInvite, BidLine, BidTechnicalResponse, BidVersion,
    CommercialRequirement, SourcingEvent, TechnicalRequirement, VendorBid,
)


class TechnicalRequirementSerializer(serializers.ModelSerializer):
    importance_display = serializers.CharField(source="get_importance_display", read_only=True)

    class Meta:
        model = TechnicalRequirement
        fields = ["id", "title", "description", "importance", "importance_display", "order"]


class CommercialRequirementSerializer(serializers.ModelSerializer):
    type_display = serializers.CharField(source="get_requirement_type_display", read_only=True)

    class Meta:
        model = CommercialRequirement
        fields = ["id", "title", "description", "requirement_type", "type_display", "unit", "quantity", "order"]


class BidTechnicalResponseSerializer(serializers.ModelSerializer):
    requirement_title = serializers.CharField(source="requirement.title", read_only=True)
    compliance_display = serializers.CharField(source="get_compliance_status_display", read_only=True)

    class Meta:
        model = BidTechnicalResponse
        fields = [
            "id", "requirement", "requirement_title",
            "compliance_status", "compliance_display", "response_text",
        ]


class BidLineSerializer(serializers.ModelSerializer):
    quoted_total_price = serializers.DecimalField(max_digits=14, decimal_places=2, read_only=True)

    class Meta:
        model = BidLine
        fields = [
            "id", "commercial_requirement", "pr_line", "item_description",
            "quantity", "quoted_unit_price", "quoted_total_price", "currency",
            "lead_time_days", "notes",
        ]


class BidAttachmentSerializer(serializers.ModelSerializer):
    uploaded_by_email = serializers.CharField(source="uploaded_by.email", read_only=True)
    doc_type_display = serializers.CharField(source="get_doc_type_display", read_only=True)

    class Meta:
        model = BidAttachment
        fields = [
            "id", "bid", "title", "file", "original_filename",
            "file_size", "content_type", "doc_type", "doc_type_display",
            "uploaded_by", "uploaded_by_email", "created_at",
        ]
        read_only_fields = ["original_filename", "file_size", "content_type", "uploaded_by", "created_at"]


class BidVersionSerializer(serializers.ModelSerializer):
    submitted_by_email = serializers.CharField(source="submitted_by.email", read_only=True)

    class Meta:
        model = BidVersion
        fields = [
            "id", "bid", "version_number", "total_bid_amount",
            "proposal_summary", "change_summary", "status",
            "submitted_at", "submitted_by", "submitted_by_email", "snapshot",
        ]


class VendorBidSerializer(serializers.ModelSerializer):
    vendor_name = serializers.CharField(source="vendor.legal_name", read_only=True)
    status_display = serializers.CharField(source="get_status_display", read_only=True)
    lines = BidLineSerializer(many=True, read_only=True)
    technical_responses = BidTechnicalResponseSerializer(many=True, read_only=True)
    attachments = BidAttachmentSerializer(many=True, read_only=True)
    versions = BidVersionSerializer(many=True, read_only=True)

    class Meta:
        model = VendorBid
        fields = [
            "id", "bid_number", "event", "vendor", "vendor_name",
            "status", "status_display", "total_bid_amount", "proposal_summary",
            "current_version_number", "submitted_at", "last_amended_at",
            "is_sealed", "is_valid", "validation_notes",
            "lines", "technical_responses", "attachments", "versions",
            "created_at", "updated_at",
        ]
        read_only_fields = [
            "bid_number", "total_bid_amount", "status",
            "current_version_number", "submitted_at", "last_amended_at",
            "is_sealed", "is_valid", "validation_notes", "created_at", "updated_at",
        ]


class SourcingEventSerializer(serializers.ModelSerializer):
    status_display = serializers.CharField(source="get_status_display", read_only=True)
    event_type_display = serializers.CharField(source="get_event_type_display", read_only=True)
    technical_requirements = TechnicalRequirementSerializer(many=True, read_only=True)
    commercial_requirements = CommercialRequirementSerializer(many=True, read_only=True)

    class Meta:
        model = SourcingEvent
        fields = [
            "id", "event_number", "event_type", "event_type_display",
            "title", "description", "status", "status_display",
            "bid_start_date", "bid_end_date", "clarification_deadline",
            "technical_section_enabled", "commercial_section_enabled",
            "clarifications_allowed", "is_sealed", "published_at", "closed_at",
            "technical_requirements", "commercial_requirements", "created_at",
        ]
        read_only_fields = ["event_number", "status", "published_at", "closed_at", "created_at"]

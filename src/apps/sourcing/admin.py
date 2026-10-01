from django.contrib import admin
from .models import (
    AwardDecision, BidInvite, BidLine, Clarification,
    CommercialRequirement, SourcingEvent, TechnicalRequirement, VendorBid,
)


@admin.register(SourcingEvent)
class SourcingEventAdmin(admin.ModelAdmin):
    list_display = ["event_number", "title", "event_type", "status", "bid_start_date", "bid_end_date", "created_by"]
    list_filter = ["status", "event_type"]
    search_fields = ["event_number", "title"]
    readonly_fields = ["event_number", "created_at", "updated_at", "published_at", "closed_at"]


@admin.register(BidInvite)
class BidInviteAdmin(admin.ModelAdmin):
    list_display = ["event", "vendor", "status", "is_responded", "invited_at"]
    list_filter = ["status", "is_responded"]
    search_fields = ["event__event_number", "vendor__legal_name"]


@admin.register(TechnicalRequirement)
class TechnicalRequirementAdmin(admin.ModelAdmin):
    list_display = ["title", "event", "importance", "order"]
    list_filter = ["importance"]
    search_fields = ["title", "event__event_number"]


@admin.register(CommercialRequirement)
class CommercialRequirementAdmin(admin.ModelAdmin):
    list_display = ["title", "event", "requirement_type", "unit", "quantity", "order"]
    list_filter = ["requirement_type"]
    search_fields = ["title", "event__event_number"]


@admin.register(Clarification)
class ClarificationAdmin(admin.ModelAdmin):
    list_display = ["event", "vendor", "status", "visibility", "created_at", "responded_at"]
    list_filter = ["status", "visibility"]
    search_fields = ["event__event_number", "vendor__legal_name"]
    readonly_fields = ["created_at", "updated_at"]


@admin.register(VendorBid)
class VendorBidAdmin(admin.ModelAdmin):
    list_display = ["bid_number", "event", "vendor", "status", "total_bid_amount", "submitted_at"]
    list_filter = ["status"]
    search_fields = ["bid_number", "event__event_number", "vendor__legal_name"]
    readonly_fields = ["bid_number", "submitted_at", "created_at", "updated_at"]


@admin.register(BidLine)
class BidLineAdmin(admin.ModelAdmin):
    list_display = ["bid", "item_description", "quantity", "quoted_unit_price", "quoted_total_price"]


@admin.register(AwardDecision)
class AwardDecisionAdmin(admin.ModelAdmin):
    list_display = ["event", "winning_bid", "approved_by", "created_at"]
    readonly_fields = ["created_at", "updated_at"]

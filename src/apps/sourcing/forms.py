from django import forms
from django.utils import timezone

from apps.vendors.models import Vendor

from .models import (
    Clarification, CommercialRequirement, SourcingEvent, TechnicalRequirement,
)


class SourcingEventForm(forms.ModelForm):
    """Form for creating and editing a sourcing event."""

    bid_start_date = forms.DateTimeField(
        widget=forms.DateTimeInput(attrs={"type": "datetime-local", "class": "form-control"}),
        required=False,
        input_formats=["%Y-%m-%dT%H:%M", "%Y-%m-%d %H:%M"],
    )
    bid_end_date = forms.DateTimeField(
        widget=forms.DateTimeInput(attrs={"type": "datetime-local", "class": "form-control"}),
        required=False,
        input_formats=["%Y-%m-%dT%H:%M", "%Y-%m-%d %H:%M"],
    )
    clarification_deadline = forms.DateTimeField(
        widget=forms.DateTimeInput(attrs={"type": "datetime-local", "class": "form-control"}),
        required=False,
        input_formats=["%Y-%m-%dT%H:%M", "%Y-%m-%d %H:%M"],
    )

    class Meta:
        model = SourcingEvent
        fields = [
            "event_type",
            "title",
            "description",
            "bid_start_date",
            "bid_end_date",
            "clarification_deadline",
            "technical_section_enabled",
            "commercial_section_enabled",
            "clarifications_allowed",
        ]
        widgets = {
            "event_type": forms.Select(attrs={"class": "form-select"}),
            "title": forms.TextInput(attrs={"class": "form-control", "placeholder": "e.g. Enterprise Networking Equipment RFQ"}),
            "description": forms.Textarea(attrs={"class": "form-control", "rows": 4}),
            "technical_section_enabled": forms.CheckboxInput(attrs={"class": "form-check-input"}),
            "commercial_section_enabled": forms.CheckboxInput(attrs={"class": "form-check-input"}),
            "clarifications_allowed": forms.CheckboxInput(attrs={"class": "form-check-input"}),
        }

    def clean(self):
        cleaned = super().clean()
        start = cleaned.get("bid_start_date")
        end = cleaned.get("bid_end_date")
        if start and end and start >= end:
            raise forms.ValidationError("Bid start date must be before bid end date.")
        return cleaned


class VendorInviteForm(forms.Form):
    """Form for selecting vendors to invite to a sourcing event."""
    vendors = forms.ModelMultipleChoiceField(
        queryset=Vendor.objects.filter(status__in=["ACTIVE", "APPROVED"]).order_by("legal_name"),
        widget=forms.CheckboxSelectMultiple(attrs={"class": "vendor-checkbox"}),
        label="Select eligible vendors to invite",
        error_messages={"required": "Please select at least one vendor checkbox to invite."},
    )


class TechnicalRequirementForm(forms.ModelForm):
    description = forms.CharField(
        required=False,
        widget=forms.Textarea(attrs={"class": "form-control", "rows": 2, "placeholder": "Requirement description / details"}),
    )
    order = forms.IntegerField(
        required=False,
        initial=0,
        widget=forms.NumberInput(attrs={"class": "form-control", "placeholder": "0"}),
    )

    class Meta:
        model = TechnicalRequirement
        fields = ["title", "description", "importance", "order"]
        widgets = {
            "title": forms.TextInput(attrs={"class": "form-control", "placeholder": "Requirement title"}),
            "importance": forms.Select(attrs={"class": "form-select"}),
        }

    def clean_description(self):
        return self.cleaned_data.get("description") or ""

    def clean_order(self):
        val = self.cleaned_data.get("order")
        return 0 if val is None else val


class CommercialRequirementForm(forms.ModelForm):
    description = forms.CharField(
        required=False,
        widget=forms.Textarea(attrs={"class": "form-control", "rows": 2, "placeholder": "Optional description or specifications"}),
    )
    unit = forms.CharField(
        required=False,
        widget=forms.TextInput(attrs={"class": "form-control", "placeholder": "e.g. each, kg, month"}),
    )
    quantity = forms.DecimalField(
        required=False,
        widget=forms.NumberInput(attrs={"class": "form-control", "step": "0.01", "placeholder": "Qty"}),
    )
    order = forms.IntegerField(
        required=False,
        initial=0,
        widget=forms.NumberInput(attrs={"class": "form-control", "placeholder": "0"}),
    )

    class Meta:
        model = CommercialRequirement
        fields = ["title", "requirement_type", "unit", "quantity", "order", "description"]
        widgets = {
            "title": forms.TextInput(attrs={"class": "form-control", "placeholder": "Commercial line item title"}),
            "requirement_type": forms.Select(attrs={"class": "form-select"}),
        }

    def clean_description(self):
        return self.cleaned_data.get("description") or ""

    def clean_order(self):
        val = self.cleaned_data.get("order")
        return 0 if val is None else val

    def clean_unit(self):
        return self.cleaned_data.get("unit") or ""


class ClarificationSubmitForm(forms.Form):
    """For vendors submitting clarification questions."""
    question = forms.CharField(
        widget=forms.Textarea(attrs={"class": "form-control", "rows": 4, "placeholder": "Enter your question here..."}),
        label="Your Question / Clarification Request",
    )


class ClarificationResponseForm(forms.Form):
    """For procurement executive responding to clarifications."""
    response = forms.CharField(
        widget=forms.Textarea(attrs={"class": "form-control", "rows": 4}),
        label="Your Response",
    )
    visibility = forms.ChoiceField(
        choices=Clarification.VISIBILITY_CHOICES,
        widget=forms.Select(attrs={"class": "form-select"}),
        initial=Clarification.VISIBILITY_PRIVATE,
        label="Visibility",
        help_text="'Private' = only the asking vendor sees it. 'All Vendors' = all invited vendors see the answer.",
    )


class BidDraftForm(forms.Form):
    """General bid metadata draft form."""
    proposal_summary = forms.CharField(
        required=False,
        widget=forms.Textarea(attrs={
            "class": "form-control",
            "rows": 4,
            "placeholder": "Provide an executive summary of your commercial & technical proposal...",
        }),
        label="Executive Proposal Summary",
    )


class BidAttachmentForm(forms.Form):
    """Upload supporting documentation for a vendor bid."""
    title = forms.CharField(
        max_length=255,
        widget=forms.TextInput(attrs={
            "class": "form-control",
            "placeholder": "e.g. Technical Architecture Whitepaper v2.0",
        }),
        label="Document Title",
    )
    doc_type = forms.ChoiceField(
        choices=[
            ("TECHNICAL", "Technical Specification / Whitepaper"),
            ("COMMERCIAL", "Commercial Proposal / Quote Sheet"),
            ("SUPPORTING_DOC", "Supporting Document"),
            ("CERTIFICATE", "Compliance / ISO / Quality Certificate"),
        ],
        widget=forms.Select(attrs={"class": "form-select"}),
        label="Document Type",
    )
    file = forms.FileField(
        widget=forms.FileInput(attrs={"class": "form-control"}),
        label="Choose File (Max 10MB; PDF, PNG, JPG, DOCX, XLSX, CSV)",
    )


class BidAmendmentForm(forms.Form):
    """Vendor amendment form for resubmitting updated bids before close."""
    change_summary = forms.CharField(
        required=True,
        widget=forms.Textarea(attrs={
            "class": "form-control",
            "rows": 3,
            "placeholder": "Provide justification and summary of changes made in this amendment...",
        }),
        label="Reason for Amendment / Change Summary",
    )
    proposal_summary = forms.CharField(
        required=False,
        widget=forms.Textarea(attrs={
            "class": "form-control",
            "rows": 4,
            "placeholder": "Updated executive proposal summary...",
        }),
        label="Updated Executive Summary",
    )


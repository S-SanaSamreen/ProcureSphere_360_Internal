from django.contrib.auth.decorators import login_required
from django.core.exceptions import ValidationError
from django.core.paginator import Paginator
from django.http import FileResponse, Http404, HttpResponseForbidden
from django.shortcuts import get_object_or_404, redirect, render
from django.contrib import messages
from django.utils import timezone

from apps.accounts.models import Role
from apps.vendors.models import Vendor

from .forms import (
    BidAmendmentForm, BidAttachmentForm, BidDraftForm,
    ClarificationResponseForm, ClarificationSubmitForm, CommercialRequirementForm,
    SourcingEventForm, TechnicalRequirementForm, VendorInviteForm,
)
from .models import (
    BidAttachment, BidInvite, BidLine, BidTechnicalResponse, BidVersion,
    Clarification, SourcingEvent, VendorBid,
)
from .selectors import (
    get_all_clarifications_for_event, get_bid_attachments_for_bid,
    get_bid_versions_for_bid, get_clarifications_for_event_and_vendor,
    get_procurement_submissions_for_event, get_sourcing_dashboard_metrics,
    get_sourcing_event_by_id, get_sourcing_events_for_procurement,
    get_vendor_accessible_events, get_vendor_bid_by_id_scoped,
    get_vendor_bid_for_event, get_vendor_invitation_for_event,
)
from .services import (
    add_commercial_requirement_service, add_technical_requirement_service,
    amend_vendor_bid_service, close_sourcing_event_service,
    create_sourcing_event_service, delete_bid_attachment_service,
    get_or_create_vendor_bid_service, invite_vendors_to_event_service,
    publish_sourcing_event_service, remove_vendor_invitation_service,
    respond_to_clarification_service, save_bid_draft_service,
    submit_clarification_service, submit_vendor_bid_service,
    update_sourcing_event_service, upload_bid_attachment_service,
    validate_bid_submission_service, withdraw_vendor_bid_service,
)


# ─────────────────────────── Access Helpers ──────────────────────────────────

def _is_procurement_user(user):
    return user.is_authenticated and (
        user.is_superuser or
        user.role_code in [Role.PROC_EXEC, Role.PROC_MGR, Role.SUPER_ADMIN]
    )


def _is_vendor_user(user):
    return user.is_authenticated and user.role_code == Role.VENDOR_USER


def _require_procurement(request):
    if not _is_procurement_user(request.user):
        return HttpResponseForbidden(
            "Access denied. Procurement Executive or Manager role required."
        )
    return None


# ──────────────────────────── List View ──────────────────────────────────────

@login_required(login_url="/admin/login/")
def sourcing_list_view(request):
    """Event list for procurement users."""
    denied = _require_procurement(request)
    if denied:
        return denied

    status_filter = request.GET.get("status", "")
    type_filter = request.GET.get("type", "")
    search = request.GET.get("q", "").strip()

    filters = {}
    if status_filter:
        filters["status"] = status_filter
    if type_filter:
        filters["event_type"] = type_filter

    qs = get_sourcing_events_for_procurement(filters=filters if filters else None)

    if search:
        qs = qs.filter(title__icontains=search) | qs.filter(event_number__icontains=search)
        qs = qs.distinct()

    paginator = Paginator(qs.order_by("-created_at"), 20)
    page_obj = paginator.get_page(request.GET.get("page"))

    metrics = get_sourcing_dashboard_metrics()

    return render(request, "pages/sourcing/list.html", {
        "page_obj": page_obj,
        "metrics": metrics,
        "status_filter": status_filter,
        "type_filter": type_filter,
        "search": search,
        "status_choices": SourcingEvent.STATUS_CHOICES,
        "type_choices": SourcingEvent.EVENT_TYPE_CHOICES,
    })


# ──────────────────────────── Create View ────────────────────────────────────

@login_required(login_url="/admin/login/")
def sourcing_create_view(request):
    """Create a new RFQ/RFP sourcing event."""
    denied = _require_procurement(request)
    if denied:
        return denied

    if request.method == "POST":
        form = SourcingEventForm(request.POST)
        if form.is_valid():
            try:
                event = create_sourcing_event_service(
                    title=form.cleaned_data["title"],
                    event_type=form.cleaned_data["event_type"],
                    description=form.cleaned_data["description"],
                    bid_start_date=form.cleaned_data.get("bid_start_date"),
                    bid_end_date=form.cleaned_data.get("bid_end_date"),
                    clarification_deadline=form.cleaned_data.get("clarification_deadline"),
                    technical_section_enabled=form.cleaned_data.get("technical_section_enabled", True),
                    commercial_section_enabled=form.cleaned_data.get("commercial_section_enabled", True),
                    clarifications_allowed=form.cleaned_data.get("clarifications_allowed", True),
                    created_by_user=request.user,
                )
                messages.success(request, f"Sourcing event {event.event_number} created as draft.")
                return redirect("sourcing:detail", pk=event.id)
            except ValidationError as e:
                messages.error(request, str(e))
    else:
        form = SourcingEventForm()

    return render(request, "pages/sourcing/create.html", {"form": form})


# ──────────────────────────── Detail View ────────────────────────────────────

@login_required(login_url="/admin/login/")
def sourcing_detail_view(request, pk):
    """Detailed view of a sourcing event — access controlled."""
    event = get_object_or_404(SourcingEvent, pk=pk)

    # Vendor access: must be invited and event must not be DRAFT
    if _is_vendor_user(request.user):
        if not request.user.vendor:
            return HttpResponseForbidden("No vendor account linked to your profile.")
        invite = get_vendor_invitation_for_event(event, request.user.vendor)
        if not invite:
            return HttpResponseForbidden("You are not invited to this sourcing event.")
        if event.status == SourcingEvent.STATUS_DRAFT:
            return HttpResponseForbidden("This event is not yet published.")

        # Vendor-specific clarifications only
        clarifications = get_clarifications_for_event_and_vendor(event, request.user.vendor)
        return render(request, "pages/sourcing/detail_vendor.html", {
            "event": event,
            "invite": invite,
            "clarifications": clarifications,
            "clarification_form": ClarificationSubmitForm(),
            "now": timezone.now(),
        })

    # Procurement / Admin access
    if not _is_procurement_user(request.user) and not request.user.is_superuser:
        return HttpResponseForbidden("Access denied.")

    clarifications = get_all_clarifications_for_event(event)
    clarification_form = ClarificationResponseForm()

    event_full = get_sourcing_event_by_id(pk)
    return render(request, "pages/sourcing/detail.html", {
        "event": event_full,
        "clarifications": clarifications,
        "clarification_form": clarification_form,
        "tech_form": TechnicalRequirementForm(),
        "comm_form": CommercialRequirementForm(),
        "now": timezone.now(),
    })


# ──────────────────────────── Edit View ──────────────────────────────────────

@login_required(login_url="/admin/login/")
def sourcing_edit_view(request, pk):
    """Edit a DRAFT sourcing event."""
    denied = _require_procurement(request)
    if denied:
        return denied

    event = get_object_or_404(SourcingEvent, pk=pk)
    if event.status != SourcingEvent.STATUS_DRAFT:
        messages.error(request, "Only DRAFT events can be edited.")
        return redirect("sourcing:detail", pk=pk)

    if request.method == "POST":
        form = SourcingEventForm(request.POST, instance=event)
        if form.is_valid():
            try:
                update_sourcing_event_service(
                    event=event,
                    user=request.user,
                    title=form.cleaned_data["title"],
                    description=form.cleaned_data["description"],
                    bid_start_date=form.cleaned_data.get("bid_start_date"),
                    bid_end_date=form.cleaned_data.get("bid_end_date"),
                    clarification_deadline=form.cleaned_data.get("clarification_deadline"),
                    technical_section_enabled=form.cleaned_data.get("technical_section_enabled"),
                    commercial_section_enabled=form.cleaned_data.get("commercial_section_enabled"),
                    clarifications_allowed=form.cleaned_data.get("clarifications_allowed"),
                )
                messages.success(request, "Event updated successfully.")
                return redirect("sourcing:detail", pk=pk)
            except ValidationError as e:
                messages.error(request, str(e))
    else:
        # Pre-format datetimes for the datetime-local input
        initial = {}
        if event.bid_start_date:
            initial["bid_start_date"] = event.bid_start_date.strftime("%Y-%m-%dT%H:%M")
        if event.bid_end_date:
            initial["bid_end_date"] = event.bid_end_date.strftime("%Y-%m-%dT%H:%M")
        if event.clarification_deadline:
            initial["clarification_deadline"] = event.clarification_deadline.strftime("%Y-%m-%dT%H:%M")
        form = SourcingEventForm(instance=event, initial=initial)

    return render(request, "pages/sourcing/edit.html", {"form": form, "event": event})


# ──────────────────────────── Vendor Management ──────────────────────────────

@login_required(login_url="/admin/login/")
def sourcing_invite_vendors_view(request, pk):
    """Invite vendors to a sourcing event."""
    denied = _require_procurement(request)
    if denied:
        return denied

    event = get_object_or_404(SourcingEvent, pk=pk)
    if event.status not in [SourcingEvent.STATUS_DRAFT, SourcingEvent.STATUS_BID_WINDOW]:
        messages.error(request, "Vendors can only be invited while the event is in DRAFT or BID_WINDOW.")
        return redirect("sourcing:detail", pk=pk)

    already_invited_ids = list(
        event.invitations.values_list("vendor_id", flat=True)
    )

    eligible_vendors = Vendor.objects.filter(
        status__in=[Vendor.STATUS_ACTIVE, Vendor.STATUS_APPROVED]
    ).exclude(id__in=already_invited_ids).order_by("legal_name")

    if request.method == "POST":
        form = VendorInviteForm(request.POST)
        form.fields["vendors"].queryset = eligible_vendors
        if form.is_valid():
            vendor_ids = [str(v.id) for v in form.cleaned_data["vendors"]]
            try:
                invitations = invite_vendors_to_event_service(
                    event=event, vendor_ids=vendor_ids, invited_by=request.user
                )
                messages.success(request, f"{len(invitations)} vendor(s) invited successfully.")
                return redirect("sourcing:detail", pk=pk)
            except ValidationError as e:
                err_msg = ", ".join(e.messages) if hasattr(e, "messages") else str(e)
                messages.error(request, err_msg)
        else:
            messages.error(request, "Please select at least one vendor checkbox to invite.")
    else:
        form = VendorInviteForm()
        form.fields["vendors"].queryset = eligible_vendors

    return render(request, "pages/sourcing/invite_vendors.html", {
        "event": event,
        "form": form,
        "already_invited": event.invitations.select_related("vendor").all(),
    })


@login_required(login_url="/admin/login/")
def sourcing_remove_vendor_view(request, pk, vendor_id):
    """Remove a vendor invitation from a DRAFT event."""
    denied = _require_procurement(request)
    if denied:
        return denied

    if request.method != "POST":
        return HttpResponseForbidden("Method not allowed.")

    event = get_object_or_404(SourcingEvent, pk=pk)
    vendor = get_object_or_404(Vendor, pk=vendor_id)

    try:
        remove_vendor_invitation_service(event=event, vendor=vendor, removed_by=request.user)
        messages.success(request, f"Invitation for {vendor.legal_name} removed.")
    except ValidationError as e:
        messages.error(request, str(e))

    return redirect("sourcing:invite-vendors", pk=pk)


# ──────────────────────────── Technical Requirements ─────────────────────────

@login_required(login_url="/admin/login/")
def sourcing_add_technical_req_view(request, pk):
    """Add a technical requirement to a DRAFT event."""
    denied = _require_procurement(request)
    if denied:
        return denied

    event = get_object_or_404(SourcingEvent, pk=pk)

    if request.method == "POST":
        form = TechnicalRequirementForm(request.POST)
        if form.is_valid():
            try:
                add_technical_requirement_service(
                    event=event,
                    title=form.cleaned_data["title"],
                    description=form.cleaned_data.get("description", "") or "",
                    importance=form.cleaned_data["importance"],
                    order=form.cleaned_data.get("order") or 0,
                    created_by=request.user,
                )
                messages.success(request, "Technical requirement added successfully.")
            except ValidationError as e:
                err_msg = ", ".join(e.messages) if hasattr(e, "messages") else str(e)
                messages.error(request, err_msg)
        else:
            err_list = [f"{field}: {', '.join(errs)}" for field, errs in form.errors.items()]
            messages.error(request, f"Could not add technical requirement: {'; '.join(err_list)}")

    return redirect("sourcing:detail", pk=pk)


# ──────────────────────────── Commercial Requirements ────────────────────────

@login_required(login_url="/admin/login/")
def sourcing_add_commercial_req_view(request, pk):
    """Add a commercial requirement to a DRAFT event."""
    denied = _require_procurement(request)
    if denied:
        return denied

    event = get_object_or_404(SourcingEvent, pk=pk)

    if request.method == "POST":
        form = CommercialRequirementForm(request.POST)
        if form.is_valid():
            try:
                add_commercial_requirement_service(
                    event=event,
                    title=form.cleaned_data["title"],
                    description=form.cleaned_data.get("description", "") or "",
                    requirement_type=form.cleaned_data["requirement_type"],
                    unit=form.cleaned_data.get("unit", "") or "",
                    quantity=form.cleaned_data.get("quantity"),
                    order=form.cleaned_data.get("order") or 0,
                    created_by=request.user,
                )
                messages.success(request, "Commercial requirement added successfully.")
            except ValidationError as e:
                err_msg = ", ".join(e.messages) if hasattr(e, "messages") else str(e)
                messages.error(request, err_msg)
        else:
            err_list = [f"{field}: {', '.join(errs)}" for field, errs in form.errors.items()]
            messages.error(request, f"Could not add commercial requirement: {'; '.join(err_list)}")

    return redirect("sourcing:detail", pk=pk)


# ──────────────────────────── Publish ────────────────────────────────────────

@login_required(login_url="/admin/login/")
def sourcing_publish_view(request, pk):
    """Publish a DRAFT sourcing event."""
    denied = _require_procurement(request)
    if denied:
        return denied

    if request.method != "POST":
        return HttpResponseForbidden("Method not allowed.")

    event = get_object_or_404(SourcingEvent, pk=pk)
    try:
        publish_sourcing_event_service(event=event, user=request.user)
        messages.success(request, f"Event {event.event_number} published successfully. Bid window is now open.")
    except ValidationError as e:
        err_msg = ", ".join(e.messages) if hasattr(e, "messages") else str(e)
        messages.error(request, f"Cannot publish event: {err_msg}")

    return redirect("sourcing:detail", pk=pk)


# ──────────────────────────── Close ──────────────────────────────────────────

@login_required(login_url="/admin/login/")
def sourcing_close_view(request, pk):
    """Close a BID_WINDOW sourcing event."""
    denied = _require_procurement(request)
    if denied:
        return denied

    if request.method != "POST":
        return HttpResponseForbidden("Method not allowed.")

    event = get_object_or_404(SourcingEvent, pk=pk)
    try:
        close_sourcing_event_service(event=event, user=request.user)
        messages.success(request, f"Event {event.event_number} has been closed.")
    except ValidationError as e:
        messages.error(request, str(e))

    return redirect("sourcing:detail", pk=pk)


# ──────────────────────────── Clarifications ─────────────────────────────────

@login_required(login_url="/admin/login/")
def sourcing_submit_clarification_view(request, pk):
    """Vendor submits a clarification question."""
    if not _is_vendor_user(request.user):
        return HttpResponseForbidden("Only vendor users can submit clarifications.")

    if request.method != "POST":
        return HttpResponseForbidden("Method not allowed.")

    event = get_object_or_404(SourcingEvent, pk=pk)
    vendor = request.user.vendor

    if not vendor:
        return HttpResponseForbidden("No vendor account linked to your profile.")

    form = ClarificationSubmitForm(request.POST)
    if form.is_valid():
        try:
            submit_clarification_service(
                event=event,
                vendor=vendor,
                question=form.cleaned_data["question"],
                submitted_by=request.user,
            )
            messages.success(request, "Your clarification has been submitted.")
        except ValidationError as e:
            messages.error(request, str(e))

    return redirect("sourcing:detail", pk=pk)


@login_required(login_url="/admin/login/")
def sourcing_respond_clarification_view(request, pk, clarification_id):
    """Procurement Executive responds to a clarification."""
    denied = _require_procurement(request)
    if denied:
        return denied

    if request.method != "POST":
        return HttpResponseForbidden("Method not allowed.")

    event = get_object_or_404(SourcingEvent, pk=pk)
    clarification = get_object_or_404(Clarification, pk=clarification_id, event=event)

    form = ClarificationResponseForm(request.POST)
    if form.is_valid():
        try:
            respond_to_clarification_service(
                clarification=clarification,
                response=form.cleaned_data["response"],
                visibility=form.cleaned_data["visibility"],
                responded_by=request.user,
            )
            messages.success(request, "Clarification response submitted.")
        except ValidationError as e:
            messages.error(request, str(e))

    return redirect("sourcing:detail", pk=pk)


# ──────────────────────── Vendor: My Invitations ─────────────────────────────

@login_required(login_url="/admin/login/")
def vendor_my_invitations_view(request):
    """Vendor dashboard: shows all events the vendor is invited to."""
    if not _is_vendor_user(request.user):
        return HttpResponseForbidden("Only vendor users can access this view.")

    vendor = request.user.vendor
    if not vendor:
        return HttpResponseForbidden("No vendor account linked to your profile.")

    events = get_vendor_accessible_events(vendor)

    return render(request, "pages/sourcing/vendor_invitations.html", {
        "events": events,
        "vendor": vendor,
    })


# ─────────────────────── Vendor Bid Portal Views ─────────────────────────────

@login_required(login_url="/admin/login/")
def vendor_bid_workspace_view(request, pk):
    """
    Unified Vendor Bid Workspace:
    - Authorized vendors access ONLY sourcing events they are invited to.
    - Prepare draft responses, technical compliance, commercial BOQ line items.
    - Upload / manage attachments.
    - Perform server-side validation.
    - Submit sealed bid atomically.
    - Amend bid before deadline.
    """
    if not _is_vendor_user(request.user):
        return HttpResponseForbidden("Only vendor users can access the bid workspace.")

    vendor = request.user.vendor
    if not vendor:
        return HttpResponseForbidden("No vendor account linked to your profile.")

    event = get_object_or_404(SourcingEvent, pk=pk)

    # Vendor access check: must be invited and event must not be DRAFT
    if event.status == SourcingEvent.STATUS_DRAFT:
        return HttpResponseForbidden("This sourcing event is not yet published.")

    invite = get_vendor_invitation_for_event(event, vendor)
    if not invite:
        return HttpResponseForbidden("You are not invited to this sourcing event.")

    try:
        bid, _ = get_or_create_vendor_bid_service(event=event, vendor=vendor, user=request.user)
    except ValidationError as e:
        messages.error(request, str(e))
        return redirect("sourcing:my-invitations")

    now = timezone.now()
    is_deadline_passed = bool(event.bid_end_date and now > event.bid_end_date)

    if request.method == "POST":
        action = request.POST.get("action", "")

        # 1. Save Draft
        if action == "save_draft":
            if not bid.is_editable:
                messages.error(request, "This bid cannot be edited as a draft in its current status or after the deadline.")
                return redirect("sourcing:vendor-bid-workspace", pk=pk)

            proposal_summary = request.POST.get("proposal_summary", "").strip()

            # Parse technical responses
            tech_responses = []
            for req in event.technical_requirements.all():
                comp_key = f"tech_compliance_{req.id}"
                text_key = f"tech_response_{req.id}"
                tech_responses.append({
                    "requirement_id": req.id,
                    "compliance_status": request.POST.get(comp_key, BidTechnicalResponse.COMPLIANCE_COMPLIANT),
                    "response_text": request.POST.get(text_key, "").strip(),
                })

            # Parse commercial lines
            line_items = []
            if event.commercial_requirements.exists():
                for comm in event.commercial_requirements.all():
                    qty_key = f"comm_qty_{comm.id}"
                    price_key = f"comm_price_{comm.id}"
                    notes_key = f"comm_notes_{comm.id}"
                    lead_key = f"comm_lead_{comm.id}"
                    qty = request.POST.get(qty_key, str(comm.quantity or 1.0))
                    price = request.POST.get(price_key, "0.00")
                    lead = request.POST.get(lead_key)
                    lead_days = int(lead) if lead and lead.isdigit() else None
                    line_items.append({
                        "commercial_requirement": comm,
                        "item_description": comm.title,
                        "quantity": qty,
                        "quoted_unit_price": price,
                        "lead_time_days": lead_days,
                        "notes": request.POST.get(notes_key, ""),
                    })
            else:
                item_descs = request.POST.getlist("custom_item_desc[]")
                item_qtys = request.POST.getlist("custom_item_qty[]")
                item_prices = request.POST.getlist("custom_item_price[]")
                for i in range(len(item_descs)):
                    if item_descs[i].strip():
                        line_items.append({
                            "item_description": item_descs[i].strip(),
                            "quantity": item_qtys[i] if i < len(item_qtys) and item_qtys[i] else "1.0",
                            "quoted_unit_price": item_prices[i] if i < len(item_prices) and item_prices[i] else "0.00",
                        })

            try:
                save_bid_draft_service(
                    bid=bid,
                    user=request.user,
                    proposal_summary=proposal_summary,
                    technical_responses=tech_responses,
                    line_items=line_items,
                )
                messages.success(request, f"Draft saved successfully. Total quote: ${bid.total_bid_amount:,.2f}")
            except ValidationError as e:
                messages.error(request, str(e))
            return redirect("sourcing:vendor-bid-workspace", pk=pk)

        # 2. Validate Bid
        elif action == "validate":
            is_valid, issues = validate_bid_submission_service(bid=bid, user=request.user)
            if is_valid:
                messages.success(request, "Bid validation passed! All mandatory technical and commercial requirements are satisfied.")
            else:
                messages.warning(request, f"Validation issues detected ({len(issues)}): " + "; ".join(issues))
            return redirect("sourcing:vendor-bid-workspace", pk=pk)

        # 3. Submit Bid
        elif action == "submit":
            try:
                submit_vendor_bid_service(bid=bid, submitted_by_user=request.user)
                messages.success(request, f"Bid {bid.bid_number} submitted successfully! Your submission is sealed until deadline.")
            except ValidationError as e:
                messages.error(request, str(e))
            return redirect("sourcing:vendor-bid-workspace", pk=pk)

        # 4. Withdraw Bid
        elif action == "withdraw":
            reason = request.POST.get("withdraw_reason", "Withdrawn by vendor")
            try:
                withdraw_vendor_bid_service(bid=bid, user=request.user, reason=reason)
                messages.warning(request, f"Bid {bid.bid_number} has been withdrawn.")
            except ValidationError as e:
                messages.error(request, str(e))
            return redirect("sourcing:vendor-bid-workspace", pk=pk)

    bid.refresh_from_db()
    tech_requirements = event.technical_requirements.all()
    comm_requirements = event.commercial_requirements.all()
    technical_responses_dict = {
        resp.requirement_id: resp for resp in bid.technical_responses.all()
    }
    commercial_lines_dict = {
        line.commercial_requirement_id: line for line in bid.lines.filter(commercial_requirement__isnull=False)
    }

    attachments = bid.attachments.select_related("uploaded_by").all()
    versions = bid.versions.select_related("submitted_by").all()

    attachment_form = BidAttachmentForm()
    amendment_form = BidAmendmentForm(initial={"proposal_summary": bid.proposal_summary})

    return render(request, "pages/sourcing/vendor_bid_workspace.html", {
        "event": event,
        "invite": invite,
        "bid": bid,
        "tech_requirements": tech_requirements,
        "comm_requirements": comm_requirements,
        "technical_responses_dict": technical_responses_dict,
        "commercial_lines_dict": commercial_lines_dict,
        "attachments": attachments,
        "versions": versions,
        "attachment_form": attachment_form,
        "amendment_form": amendment_form,
        "now": now,
        "is_deadline_passed": is_deadline_passed,
    })


@login_required(login_url="/admin/login/")
def vendor_bid_upload_attachment_view(request, bid_id):
    """Uploads a supporting document for a vendor bid."""
    if not _is_vendor_user(request.user):
        return HttpResponseForbidden("Only vendor users can upload attachments.")

    if request.method != "POST":
        return HttpResponseForbidden("Method not allowed.")

    bid = get_vendor_bid_by_id_scoped(bid_id, request.user)
    if not bid:
        return HttpResponseForbidden("Bid not found or access denied.")

    form = BidAttachmentForm(request.POST, request.FILES)
    if form.is_valid():
        try:
            upload_bid_attachment_service(
                bid=bid,
                file=form.cleaned_data["file"],
                title=form.cleaned_data["title"],
                doc_type=form.cleaned_data["doc_type"],
                user=request.user,
            )
            messages.success(request, f"Document '{form.cleaned_data['title']}' uploaded successfully.")
        except ValidationError as e:
            messages.error(request, str(e))
    else:
        err_msg = "; ".join([f"{f}: {', '.join(e)}" for f, e in form.errors.items()])
        messages.error(request, f"Attachment upload failed: {err_msg}")

    return redirect("sourcing:vendor-bid-workspace", pk=bid.event_id)


@login_required(login_url="/admin/login/")
def vendor_bid_delete_attachment_view(request, attachment_id):
    """Deletes an attachment from a vendor bid."""
    if not _is_vendor_user(request.user):
        return HttpResponseForbidden("Only vendor users can delete attachments.")

    if request.method != "POST":
        return HttpResponseForbidden("Method not allowed.")

    attachment = get_object_or_404(BidAttachment, id=attachment_id)
    if attachment.bid.vendor_id != getattr(request.user, "vendor_id", None):
        return HttpResponseForbidden("Access denied. You do not own this attachment.")

    event_id = attachment.bid.event_id
    try:
        delete_bid_attachment_service(attachment=attachment, user=request.user)
        messages.success(request, "Attachment removed successfully.")
    except ValidationError as e:
        messages.error(request, str(e))

    return redirect("sourcing:vendor-bid-workspace", pk=event_id)


@login_required(login_url="/admin/login/")
def vendor_bid_download_attachment_view(request, attachment_id):
    """
    Secure file download endpoint:
    - Vendor user: can download ONLY attachments belonging to their own vendor's bid.
    - Procurement user: can download attachments once submitted and unsealed.
    - Returns 404 / 403 on unauthorized access to prevent enumeration.
    """
    attachment = get_object_or_404(BidAttachment, id=attachment_id)
    bid = attachment.bid
    user = request.user

    # Vendor isolation check
    if _is_vendor_user(user):
        if bid.vendor_id != getattr(user, "vendor_id", None):
            return HttpResponseForbidden("Access denied. You do not own this attachment.")
    elif _is_procurement_user(user) or user.is_superuser:
        if bid.event.status == SourcingEvent.STATUS_BID_WINDOW and not user.is_superuser:
            return HttpResponseForbidden("Sealed bid attachments are locked until the bid window closes.")
    else:
        return HttpResponseForbidden("Access denied.")

    if not attachment.file:
        raise Http404("File does not exist.")

    try:
        response = FileResponse(attachment.file.open("rb"), content_type=attachment.content_type or "application/octet-stream")
        response["Content-Disposition"] = f'attachment; filename="{attachment.original_filename}"'
        return response
    except (FileNotFoundError, ValueError):
        raise Http404("File not found on storage.")


@login_required(login_url="/admin/login/")
def vendor_bid_amend_view(request, bid_id):
    """
    Submits a formal bid amendment creating a new immutable BidVersion.
    Requires change justification and respects deadline.
    """
    if not _is_vendor_user(request.user):
        return HttpResponseForbidden("Only vendor users can amend bids.")

    bid = get_vendor_bid_by_id_scoped(bid_id, request.user)
    if not bid:
        return HttpResponseForbidden("Bid not found or access denied.")

    if request.method != "POST":
        return HttpResponseForbidden("Method not allowed.")

    change_summary = request.POST.get("change_summary", "").strip()
    proposal_summary = request.POST.get("proposal_summary", "").strip()

    tech_responses = []
    for req in bid.event.technical_requirements.all():
        comp_key = f"amend_tech_compliance_{req.id}"
        text_key = f"amend_tech_response_{req.id}"
        if comp_key in request.POST or text_key in request.POST:
            tech_responses.append({
                "requirement_id": req.id,
                "compliance_status": request.POST.get(comp_key, BidTechnicalResponse.COMPLIANCE_COMPLIANT),
                "response_text": request.POST.get(text_key, "").strip(),
            })

    line_items = []
    if bid.event.commercial_requirements.exists():
        for comm in bid.event.commercial_requirements.all():
            qty_key = f"amend_comm_qty_{comm.id}"
            price_key = f"amend_comm_price_{comm.id}"
            notes_key = f"amend_comm_notes_{comm.id}"
            lead_key = f"amend_comm_lead_{comm.id}"
            qty = request.POST.get(qty_key, str(comm.quantity or 1.0))
            price = request.POST.get(price_key, "0.00")
            lead = request.POST.get(lead_key)
            lead_days = int(lead) if lead and lead.isdigit() else None
            line_items.append({
                "commercial_requirement": comm,
                "item_description": comm.title,
                "quantity": qty,
                "quoted_unit_price": price,
                "lead_time_days": lead_days,
                "notes": request.POST.get(notes_key, ""),
            })

    try:
        amended_bid = amend_vendor_bid_service(
            bid=bid,
            user=request.user,
            change_summary=change_summary,
            proposal_summary=proposal_summary or None,
            line_items=line_items if line_items else None,
            technical_responses=tech_responses if tech_responses else None,
        )
        messages.success(
            request,
            f"Bid amendment v{amended_bid.current_version_number} submitted successfully! Version snapshot archived.",
        )
    except ValidationError as e:
        messages.error(request, str(e))

    return redirect("sourcing:vendor-bid-workspace", pk=bid.event_id)


@login_required(login_url="/admin/login/")
def vendor_bid_version_detail_view(request, bid_id, version_number):
    """
    Displays an immutable frozen snapshot of a historical or current BidVersion.
    IDOR protected: vendors can only view their own versions.
    Procurement can view after BID_WINDOW.
    """
    bid = get_vendor_bid_by_id_scoped(bid_id, request.user)
    if not bid:
        return HttpResponseForbidden("Bid not found or access denied.")

    version = get_object_or_404(BidVersion, bid=bid, version_number=version_number)

    return render(request, "pages/sourcing/bid_version_detail.html", {
        "bid": bid,
        "version": version,
        "snapshot": version.snapshot,
        "is_vendor": _is_vendor_user(request.user),
    })


@login_required(login_url="/admin/login/")
def procurement_event_bids_view(request, pk):
    """
    Procurement Executive view: manages and reviews submissions for an event.
    Enforces sealed bid privacy: line items / prices are hidden while event is in BID_WINDOW.
    """
    denied = _require_procurement(request)
    if denied:
        return denied

    event = get_object_or_404(SourcingEvent, pk=pk)
    submissions = get_procurement_submissions_for_event(event)

    is_sealed = event.status == SourcingEvent.STATUS_BID_WINDOW

    return render(request, "pages/sourcing/procurement_bids_list.html", {
        "event": event,
        "submissions": submissions,
        "is_sealed": is_sealed,
        "now": timezone.now(),
    })


@login_required(login_url="/admin/login/")
def procurement_bid_detail_view(request, bid_id):
    """
    Procurement Executive view: review individual vendor bid submission.
    Sealed privacy: if event is in BID_WINDOW, commercial line pricing is locked.
    """
    denied = _require_procurement(request)
    if denied:
        return denied

    bid = get_object_or_404(VendorBid.objects.select_related("event", "vendor", "submitted_by"), pk=bid_id)
    event = bid.event

    is_sealed = event.status == SourcingEvent.STATUS_BID_WINDOW and not request.user.is_superuser
    if is_sealed:
        messages.info(request, "This bid is sealed until the bid window deadline has passed.")

    lines = bid.lines.select_related("commercial_requirement").all() if not is_sealed else []
    technical_responses = bid.technical_responses.select_related("requirement").all()
    attachments = bid.attachments.all() if not is_sealed else []
    versions = bid.versions.all()

    return render(request, "pages/sourcing/procurement_bid_detail.html", {
        "event": event,
        "bid": bid,
        "lines": lines,
        "technical_responses": technical_responses,
        "attachments": attachments,
        "versions": versions,
        "is_sealed": is_sealed,
        "now": timezone.now(),
    })

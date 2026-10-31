from decimal import Decimal, InvalidOperation

from django.core.exceptions import ValidationError
from django.db import transaction
from django.utils import timezone

from apps.accounts.models import Role, User
from apps.accounts.permissions import forbid_non_transacting_actor
from apps.audit.models import AuditLog
from apps.notifications.models import Notification
from apps.notifications.services import notify_role_users, notify_users
from apps.vendors.models import Vendor

from .models import (
    Contract,
    ContractAlert,
    ContractDocument,
    ContractMilestone,
    ContractObligation,
    ContractVersion,
)


def _get_user_role_code(user):
    if not user:
        return None
    return getattr(user, "role_code", None) or (
        user.role.code if getattr(user, "role", None) else None
    )


@transaction.atomic
def create_contract_service(
    *,
    title: str,
    vendor: Vendor,
    contract_value: Decimal,
    start_date,
    end_date,
    contract_owner: User,
    renewal_notice_days: int = 30,
    sourcing_event=None,
    po=None,
) -> Contract:
    """
    Creates a new Contract in DRAFT status after strict validation of mandatory fields and term dates.
    """
    forbid_non_transacting_actor(contract_owner, "create contracts")
    if vendor.status == Vendor.STATUS_SUSPENDED:
        raise ValidationError(
            f"Vendor '{vendor.legal_name}' is suspended and cannot enter into contracts."
        )

    if not title or not str(title).strip():
        raise ValidationError("Contract title is required.")
    if end_date and start_date and end_date < start_date:
        raise ValidationError("Contract end date cannot be earlier than start date.")
    if contract_value is not None:
        try:
            val = Decimal(str(contract_value))
            if val < Decimal("0.00"):
                raise ValidationError("Contract value cannot be negative.")
        except (ValueError, TypeError, InvalidOperation):
            raise ValidationError("Invalid contract value format.")
    if renewal_notice_days is not None and renewal_notice_days <= 0:
        raise ValidationError("Renewal notice period must be a positive number of days.")

    from apps.core.services import generate_document_number

    contract_number = generate_document_number(Contract, "contract_number", "CON")

    contract = Contract.objects.create(
        contract_number=contract_number,
        title=title.strip(),
        version=1,
        vendor=vendor,
        sourcing_event=sourcing_event,
        po=po,
        status=Contract.STATUS_DRAFT,
        contract_value=contract_value,
        start_date=start_date,
        end_date=end_date,
        renewal_notice_days=renewal_notice_days,
        contract_owner=contract_owner,
    )

    # Initial Version 1 Snapshot
    ContractVersion.objects.create(
        contract=contract,
        version_number=1,
        amendment_summary="Initial Contract Execution Draft",
        contract_value=contract_value,
        start_date=start_date,
        end_date=end_date,
        approved_by=contract_owner,
    )

    AuditLog.objects.create(
        actor=contract_owner,
        action=AuditLog.ACTION_CREATE,
        target_model="Contract",
        target_object_id=str(contract.id),
        new_state={
            "contract_number": contract.contract_number,
            "vendor": vendor.legal_name,
            "value": str(contract_value),
            "status": contract.status,
        },
    )

    return contract


@transaction.atomic
def submit_for_legal_review_service(*, contract: Contract, user: User, notes: str = "") -> Contract:
    """
    Submits contract from DRAFT to LEGAL_REVIEW state.
    """
    forbid_non_transacting_actor(user, "submit contracts")
    if contract.status != Contract.STATUS_DRAFT:
        raise ValidationError(
            f"Cannot submit contract in status '{contract.status}' for legal review."
        )

    previous_status = contract.status
    contract.status = Contract.STATUS_LEGAL_REVIEW
    contract.save(update_fields=["status", "updated_at"])

    AuditLog.objects.create(
        actor=user,
        action=AuditLog.ACTION_UPDATE,
        target_model="Contract",
        target_object_id=str(contract.id),
        previous_state={"status": previous_status},
        new_state={"status": contract.status, "notes": notes},
    )

    notify_role_users(
        role_codes=[Role.LEGAL_MGR],
        notification_type=Notification.TYPE_APPROVAL_REQUIRED,
        title=f"Legal Review Required: {contract.contract_number}",
        message=f"Contract '{contract.contract_number}' ({contract.title}) submitted for legal review.",
        target_url=f"/contracts/{contract.id}/",
    )

    return contract


@transaction.atomic
def approve_legal_review_service(*, contract: Contract, user: User, notes: str = "") -> Contract:
    """
    Approves legal review, moving contract to BUSINESS_APPROVAL state.
    """
    role_code = _get_user_role_code(user)
    if getattr(user, "is_superuser", False) or role_code != Role.LEGAL_MGR:
        raise ValidationError("Permission Denied: Only Legal Managers can approve legal reviews.")

    if contract.status != Contract.STATUS_LEGAL_REVIEW:
        raise ValidationError(
            f"Cannot perform legal approval on contract in status '{contract.status}'."
        )

    previous_status = contract.status
    contract.status = Contract.STATUS_BUSINESS_APPROVAL
    contract.save(update_fields=["status", "updated_at"])
    # Business approval is routed by the CONTRACT policy (fail closed): value tiers, optional
    # purchase-order cost center / department scope, multi-step chains, approval limits.
    business_request = _open_business_request(contract, maker=user)

    AuditLog.objects.create(
        actor=user,
        action=AuditLog.ACTION_APPROVE,
        target_model="Contract",
        target_object_id=str(contract.id),
        previous_state={"status": previous_status},
        new_state={"status": contract.status, "legal_approval_notes": notes},
    )

    from apps.approvals import engine

    approvers = engine.step_approvers(business_request)
    if approvers:
        notify_users(
            users=approvers,
            notification_type=Notification.TYPE_APPROVAL_REQUIRED,
            title=f"Legal Review Approved: {contract.contract_number}",
            message=f"Contract '{contract.contract_number}' passed legal review and requires business approval.",
            target_url=f"/contracts/{contract.id}/",
        )
    notify_users(
        users=[contract.contract_owner],
        notification_type=Notification.TYPE_APPROVAL_REQUIRED,
        title=f"Legal Review Approved: {contract.contract_number}",
        message=f"Contract '{contract.contract_number}' passed legal review.",
        target_url=f"/contracts/{contract.id}/",
    )

    return contract


@transaction.atomic
def reject_legal_review_service(*, contract: Contract, user: User, reason: str) -> Contract:
    """
    Rejects legal review, returning contract to DRAFT state for corrections.
    """
    role_code = _get_user_role_code(user)
    if getattr(user, "is_superuser", False) or role_code != Role.LEGAL_MGR:
        raise ValidationError("Permission Denied: Only Legal Managers can reject legal reviews.")

    if not reason or not str(reason).strip():
        raise ValidationError("Rejection reason is required.")
    if contract.status != Contract.STATUS_LEGAL_REVIEW:
        raise ValidationError(
            f"Cannot reject legal review for contract in status '{contract.status}'."
        )

    previous_status = contract.status
    contract.status = Contract.STATUS_DRAFT
    contract.save(update_fields=["status", "updated_at"])

    AuditLog.objects.create(
        actor=user,
        action=AuditLog.ACTION_REJECT,
        target_model="Contract",
        target_object_id=str(contract.id),
        previous_state={"status": previous_status},
        new_state={"status": contract.status, "rejection_reason": reason.strip()},
    )

    notify_users(
        users=[contract.contract_owner],
        notification_type=Notification.TYPE_APPROVAL_REQUIRED,
        title=f"Legal Review Rejected: {contract.contract_number}",
        message=f"Contract '{contract.contract_number}' legal review was rejected. Reason: {reason.strip()}",
        target_url=f"/contracts/{contract.id}/",
    )

    return contract


def _open_business_request(contract: Contract, *, maker):
    from apps.approvals import engine
    from apps.approvals.models import ApprovalPolicy

    cost_center = contract.po.cost_center if contract.po_id else None
    return engine.open_approval_request(
        module=ApprovalPolicy.MODULE_CONTRACT,
        target=contract,
        amount=contract.contract_value,
        maker=maker,
        department=cost_center.department if cost_center else None,
        cost_center=cost_center,
        required=True,
        record_submission=maker is not None,
    )


@transaction.atomic
def approve_business_service(*, contract: Contract, user: User, notes: str = "") -> Contract:
    """
    Business approval of a contract (BUSINESS_APPROVAL -> ACTIVE once the last step of the
    CONTRACT approval chain is approved). Authorisation, separation of duties, approval limits
    and multi-step chains are enforced by the approval engine.
    """
    forbid_non_transacting_actor(user, "approve contracts")
    from apps.approvals import engine

    if contract.vendor.status == Vendor.STATUS_SUSPENDED:
        raise ValidationError(
            f"Cannot activate contract: Vendor '{contract.vendor.legal_name}' is suspended."
        )

    if contract.status != Contract.STATUS_BUSINESS_APPROVAL:
        raise ValidationError(
            f"Cannot perform business approval on contract in status '{contract.status}'. Mandatory legal review must be completed first."
        )

    request = engine.get_pending_request("CONTRACT", contract) or _open_business_request(
        contract, maker=None
    )
    is_final_step = request.current_step + 1 >= len(request.steps)
    request = engine.approve_step(
        request=request,
        actor=user,
        comments=notes,
        states=("BUSINESS_APPROVAL", "ACTIVE" if is_final_step else "BUSINESS_APPROVAL"),
    )
    if request.status != "APPROVED":
        step = request.active_step
        approvers = engine.step_approvers(request)
        if approvers:
            notify_users(
                users=approvers,
                notification_type=Notification.TYPE_APPROVAL_REQUIRED,
                title=f"Contract approval required: {contract.contract_number} (Step {step['step_number']})",
                message=f"Contract '{contract.contract_number}' awaits {step['role_name']} approval.",
                target_url=f"/contracts/{contract.id}/",
            )
        return contract  # partially approved: still BUSINESS_APPROVAL

    previous_status = contract.status
    contract.status = Contract.STATUS_ACTIVE
    contract.save(update_fields=["status", "updated_at"])

    AuditLog.objects.create(
        actor=user,
        action=AuditLog.ACTION_APPROVE,
        target_model="Contract",
        target_object_id=str(contract.id),
        previous_state={"status": previous_status},
        new_state={"status": contract.status, "business_approval_notes": notes},
    )

    notify_users(
        users=[contract.contract_owner],
        notification_type=Notification.TYPE_APPROVAL_REQUIRED,
        title=f"Contract Activated: {contract.contract_number}",
        message=f"Contract '{contract.contract_number}' ({contract.title}) has been approved and is now ACTIVE.",
        target_url=f"/contracts/{contract.id}/",
    )

    return contract


@transaction.atomic
def reject_business_service(*, contract: Contract, user: User, reason: str) -> Contract:
    """Business approver rejects the contract, returning it to DRAFT for rework."""
    forbid_non_transacting_actor(user, "reject contracts")
    from apps.approvals import engine

    if not reason or not str(reason).strip():
        raise ValidationError("Rejection reason is required.")
    if contract.status != Contract.STATUS_BUSINESS_APPROVAL:
        raise ValidationError(
            f"Cannot reject business approval for contract in status '{contract.status}'."
        )
    request = engine.get_pending_request("CONTRACT", contract) or _open_business_request(
        contract, maker=None
    )
    engine.reject_request(
        request=request,
        actor=user,
        comments=reason,
        states=("BUSINESS_APPROVAL", "DRAFT"),
    )
    previous_status = contract.status
    contract.status = Contract.STATUS_DRAFT
    contract.save(update_fields=["status", "updated_at"])
    AuditLog.objects.create(
        actor=user,
        action=AuditLog.ACTION_REJECT,
        target_model="Contract",
        target_object_id=str(contract.id),
        previous_state={"status": previous_status},
        new_state={"status": contract.status, "rejection_reason": reason.strip()},
    )
    notify_users(
        users=[contract.contract_owner],
        notification_type=Notification.TYPE_APPROVAL_REQUIRED,
        title=f"Business Approval Rejected: {contract.contract_number}",
        message=f"Contract '{contract.contract_number}' was rejected at business approval. Reason: {reason.strip()}",
        target_url=f"/contracts/{contract.id}/",
    )
    return contract


@transaction.atomic
def activate_contract_service(*, contract: Contract, user: User) -> Contract:
    """
    Activates contract state after verifying mandatory workflow approvals.
    Direct activation from DRAFT or LEGAL_REVIEW without completing legal and business approval is forbidden.
    """
    forbid_non_transacting_actor(user, "activate contracts")
    if contract.status != Contract.STATUS_BUSINESS_APPROVAL:
        raise ValidationError(
            f"Cannot activate contract in status '{contract.status}'. "
            f"Mandatory legal review and business approval must be completed first."
        )
    return approve_business_service(contract=contract, user=user)


@transaction.atomic
def create_contract_version_service(
    *,
    contract: Contract,
    user: User,
    amendment_summary: str,
    contract_value: Decimal,
    start_date,
    end_date,
) -> ContractVersion:
    """
    Amends contract by incrementing version number, preserving prior version values,
    and updating active contract values.
    """
    forbid_non_transacting_actor(user, "amend contracts")
    role_code = _get_user_role_code(user)
    if getattr(user, "is_superuser", False) or role_code not in [Role.LEGAL_MGR, Role.PROC_MGR]:
        raise ValidationError(
            "Permission Denied: Only Legal Managers or Procurement Managers can amend contracts."
        )

    if not amendment_summary or not str(amendment_summary).strip():
        raise ValidationError("Amendment summary is required.")
    if end_date and start_date and end_date < start_date:
        raise ValidationError("Contract end date cannot be earlier than start date.")
    if contract_value is not None:
        try:
            val = Decimal(str(contract_value))
            if val < Decimal("0.00"):
                raise ValidationError("Contract value cannot be negative.")
        except (ValueError, TypeError, InvalidOperation):
            raise ValidationError("Invalid contract value format.")

    previous_version = contract.version
    previous_value = contract.contract_value
    previous_start = contract.start_date
    previous_end = contract.end_date

    new_version_number = contract.version + 1

    # Record historic version snapshot
    version_record = ContractVersion.objects.create(
        contract=contract,
        version_number=new_version_number,
        amendment_summary=amendment_summary.strip(),
        contract_value=contract_value,
        start_date=start_date,
        end_date=end_date,
        approved_by=user,
    )

    # Update active Contract instance
    contract.version = new_version_number
    contract.contract_value = contract_value
    contract.start_date = start_date
    contract.end_date = end_date
    contract.save(
        update_fields=["version", "contract_value", "start_date", "end_date", "updated_at"]
    )

    AuditLog.objects.create(
        actor=user,
        action=AuditLog.ACTION_UPDATE,
        target_model="Contract",
        target_object_id=str(contract.id),
        previous_state={
            "version": previous_version,
            "contract_value": str(previous_value),
            "start_date": str(previous_start),
            "end_date": str(previous_end),
        },
        new_state={
            "version": new_version_number,
            "amendment_summary": amendment_summary.strip(),
            "contract_value": str(contract_value),
            "start_date": str(start_date),
            "end_date": str(end_date),
        },
    )

    return version_record


@transaction.atomic
def add_contract_milestone_service(
    *,
    contract: Contract,
    title: str,
    due_date,
    amount: Decimal = Decimal("0.00"),
    user: User = None,
) -> ContractMilestone:
    """
    Adds a tracked milestone to a contract with audit logging.
    """
    forbid_non_transacting_actor(user, "edit contract milestones")
    if not title or not str(title).strip():
        raise ValidationError("Milestone title is required.")
    if not due_date:
        raise ValidationError("Milestone due date is required.")
    if amount is not None and Decimal(str(amount)) < Decimal("0.00"):
        raise ValidationError("Milestone amount cannot be negative.")

    milestone = ContractMilestone.objects.create(
        contract=contract,
        title=title.strip(),
        due_date=due_date,
        amount=amount,
    )

    actor = user or contract.contract_owner
    if actor:
        AuditLog.objects.create(
            actor=actor,
            action=AuditLog.ACTION_CREATE,
            target_model="ContractMilestone",
            target_object_id=str(milestone.id),
            new_state={
                "contract_number": contract.contract_number,
                "title": milestone.title,
                "due_date": str(milestone.due_date),
                "amount": str(milestone.amount),
            },
        )

    return milestone


@transaction.atomic
def complete_contract_milestone_service(
    *, milestone: ContractMilestone, user: User
) -> ContractMilestone:
    """
    Marks a milestone as completed.
    """
    forbid_non_transacting_actor(user, "edit contract milestones")
    if milestone.is_completed:
        raise ValidationError("Milestone is already completed.")

    milestone.is_completed = True
    milestone.completed_at = timezone.now()
    milestone.save(update_fields=["is_completed", "completed_at", "updated_at"])

    AuditLog.objects.create(
        actor=user,
        action=AuditLog.ACTION_UPDATE,
        target_model="ContractMilestone",
        target_object_id=str(milestone.id),
        new_state={"title": milestone.title, "is_completed": True},
    )

    return milestone


@transaction.atomic
def add_contract_obligation_service(
    *, contract: Contract, title: str, responsible_party: str, due_date, user: User = None
) -> ContractObligation:
    """
    Adds a legal obligation to a contract with audit logging.
    """
    forbid_non_transacting_actor(user, "edit contract obligations")
    if not title or not str(title).strip():
        raise ValidationError("Obligation title is required.")
    if not due_date:
        raise ValidationError("Obligation due date is required.")
    if not responsible_party or not str(responsible_party).strip():
        raise ValidationError("Responsible party is required.")

    obligation = ContractObligation.objects.create(
        contract=contract,
        title=title.strip(),
        responsible_party=responsible_party.strip(),
        due_date=due_date,
    )

    actor = user or contract.contract_owner
    if actor:
        AuditLog.objects.create(
            actor=actor,
            action=AuditLog.ACTION_CREATE,
            target_model="ContractObligation",
            target_object_id=str(obligation.id),
            new_state={
                "contract_number": contract.contract_number,
                "title": obligation.title,
                "responsible_party": obligation.responsible_party,
                "due_date": str(obligation.due_date),
            },
        )

    return obligation


@transaction.atomic
def fulfill_contract_obligation_service(
    *, obligation: ContractObligation, user: User
) -> ContractObligation:
    """
    Marks an obligation as fulfilled.
    """
    forbid_non_transacting_actor(user, "edit contract obligations")
    if obligation.is_fulfilled:
        raise ValidationError("Obligation is already fulfilled.")

    obligation.is_fulfilled = True
    obligation.fulfilled_at = timezone.now()
    obligation.save(update_fields=["is_fulfilled", "fulfilled_at", "updated_at"])

    AuditLog.objects.create(
        actor=user,
        action=AuditLog.ACTION_UPDATE,
        target_model="ContractObligation",
        target_object_id=str(obligation.id),
        new_state={"title": obligation.title, "is_fulfilled": True},
    )

    return obligation


@transaction.atomic
def upload_contract_document_service(
    *, contract: Contract, user: User, title: str, file
) -> ContractDocument:
    """
    Uploads a signed contract document or appendix after title and format/size validation.
    """
    forbid_non_transacting_actor(user, "upload contract documents")
    role_code = getattr(user, "role_code", None) or (
        user.role.code if getattr(user, "role", None) else None
    )
    if role_code == Role.AUDITOR:
        raise ValidationError(
            "Permission Denied: Compliance Auditors hold strictly read-only permissions and cannot upload contract documents."
        )

    if not title or not str(title).strip():
        raise ValidationError("Document title is required.")
    if not file:
        raise ValidationError("Document file is required.")

    if hasattr(file, "read") or hasattr(file, "size"):
        from apps.core.validators import validate_file_upload

        validate_file_upload(file)

    file_name = ""
    if hasattr(file, "name"):
        file_name = file.name
    elif isinstance(file, str):
        file_name = file

    if not file_name or "." not in file_name or file_name.startswith("."):
        raise ValidationError("File must have a valid extension.")

    clean_name = file_name.split()[0]
    ext = clean_name.split(".")[-1].lower().rstrip(")")
    allowed_extensions = ["pdf", "docx", "doc", "xlsx", "xls", "png", "jpg", "jpeg", "txt"]
    if ext not in allowed_extensions:
        raise ValidationError(
            f"Unsupported file extension '.{ext}'. Allowed formats: PDF, DOCX, XLSX, PNG, JPG, TXT."
        )

    if hasattr(file, "size") and file.size > 10 * 1024 * 1024:
        raise ValidationError("File size exceeds 10MB upload limit.")

    from django.core.files.base import ContentFile

    if isinstance(file, str):
        file = ContentFile(
            b"document payload", name=file_name.split()[0] if file_name else "document.pdf"
        )

    document = ContractDocument.objects.create(
        contract=contract,
        title=title.strip(),
        file=file,
        uploaded_by=user,
    )

    AuditLog.objects.create(
        actor=user,
        action=AuditLog.ACTION_CREATE,
        target_model="ContractDocument",
        target_object_id=str(document.id),
        new_state={"title": title.strip(), "file_name": str(file)},
    )

    return document


@transaction.atomic
def renew_contract_service(
    *, contract: Contract, user: User, new_end_date, new_value: Decimal = None, notes: str = ""
) -> Contract:
    """
    Renews an active or renewal-due contract, creating a new version.
    """
    role_code = _get_user_role_code(user)
    if getattr(user, "is_superuser", False) or role_code not in [Role.LEGAL_MGR, Role.PROC_MGR]:
        raise ValidationError(
            "Permission Denied: Only Legal Managers or Procurement Managers can renew contracts."
        )

    if contract.status not in [
        Contract.STATUS_ACTIVE,
        Contract.STATUS_RENEWED,
        Contract.STATUS_RENEWAL_DUE,
        Contract.STATUS_EXPIRED,
    ]:
        raise ValidationError(f"Cannot renew contract in status '{contract.status}'.")

    if isinstance(new_end_date, str):
        from datetime import datetime

        try:
            new_end_date = datetime.strptime(new_end_date, "%Y-%m-%d").date()
        except ValueError:
            raise ValidationError("Invalid new end date format. Expected YYYY-MM-DD.")

    if new_end_date and contract.start_date and new_end_date <= contract.start_date:
        raise ValidationError("Renewal end date must be after contract start date.")
    if new_end_date:
        if new_end_date <= timezone.localdate():
            raise ValidationError("Renewal end date must be in the future.")
        if contract.end_date and new_end_date <= contract.end_date:
            raise ValidationError(
                "Renewal end date must be later than the current contract end date "
                f"({contract.end_date})."
            )
    if new_value is not None:
        try:
            new_value = Decimal(str(new_value))
        except Exception:
            raise ValidationError("Invalid contract value.")
        if new_value < Decimal("0.00"):
            raise ValidationError("Contract value cannot be negative.")

    value = new_value if new_value is not None else contract.contract_value
    previous_status = contract.status
    contract.status = Contract.STATUS_RENEWED
    contract.end_date = new_end_date
    contract.contract_value = value
    contract.save(update_fields=["status", "end_date", "contract_value", "updated_at"])

    create_contract_version_service(
        contract=contract,
        user=user,
        amendment_summary=f"Contract Renewal Extension to {new_end_date}. {notes}".strip(),
        contract_value=value,
        start_date=contract.start_date,
        end_date=new_end_date,
    )

    AuditLog.objects.create(
        actor=user,
        action=AuditLog.ACTION_APPROVE,
        target_model="Contract",
        target_object_id=str(contract.id),
        previous_state={"status": previous_status, "end_date": str(contract.end_date)},
        new_state={
            "status": contract.status,
            "new_end_date": str(new_end_date),
            "value": str(value),
        },
    )

    notify_role_users(
        role_codes=[Role.LEGAL_MGR, Role.PROC_MGR],
        notification_type=Notification.TYPE_CONTRACT_EXPIRATION,
        title=f"Contract Renewed: {contract.contract_number}",
        message=f"Contract '{contract.contract_number}' ({contract.title}) has been renewed until {new_end_date}.",
        target_url=f"/contracts/{contract.id}/",
    )

    return contract


@transaction.atomic
def terminate_contract_service(*, contract: Contract, user: User, reason: str) -> Contract:
    """
    Terminates a contract.
    """
    role_code = _get_user_role_code(user)
    if getattr(user, "is_superuser", False) or role_code not in [Role.LEGAL_MGR, Role.PROC_MGR]:
        raise ValidationError(
            "Permission Denied: Only Legal Managers or Procurement Managers can terminate contracts."
        )

    if not reason or not str(reason).strip():
        raise ValidationError("Termination reason is required.")
    if contract.status == Contract.STATUS_TERMINATED:
        raise ValidationError("Contract is already terminated.")

    previous_status = contract.status
    contract.status = Contract.STATUS_TERMINATED
    contract.save(update_fields=["status", "updated_at"])

    AuditLog.objects.create(
        actor=user,
        action=AuditLog.ACTION_UPDATE,
        target_model="Contract",
        target_object_id=str(contract.id),
        previous_state={"status": previous_status},
        new_state={"status": Contract.STATUS_TERMINATED, "reason": reason.strip()},
    )

    notify_role_users(
        role_codes=[Role.LEGAL_MGR, Role.PROC_MGR],
        notification_type=Notification.TYPE_CONTRACT_EXPIRATION,
        title=f"Contract Terminated: {contract.contract_number}",
        message=f"Contract '{contract.contract_number}' ({contract.title}) was terminated. Reason: {reason.strip()}",
        target_url=f"/contracts/{contract.id}/",
    )

    return contract


@transaction.atomic
def _set_contract_status_by_scheduler(contract: Contract, new_status: str, reason: str):
    """
    Status change made by the scheduled scan (no human actor). It is written to the audit log
    like any other contract transition, tagged as a system action, so lifecycle changes driven
    by Celery Beat are as traceable as manual ones.
    """
    previous = contract.status
    contract.status = new_status
    contract.save(update_fields=["status", "updated_at"])
    AuditLog.objects.create(
        actor=None,
        action=AuditLog.ACTION_UPDATE,
        target_model="Contract",
        target_object_id=str(contract.id),
        previous_state={"status": previous},
        new_state={
            "status": new_status,
            "reason": reason,
            "end_date": str(contract.end_date),
            "source": "scheduled scan_contract_expirations_and_milestones",
        },
        user_agent="celery-beat/scan_contract_expirations_and_milestones",
    )


def scan_contract_expirations_and_milestones_service() -> int:
    """
    Scans active contracts for upcoming expirations, milestones, and legal obligations.
    Executes scheduled Celery Beat task logic, creates ContractAlert records,
    and dispatches in-app Notifications to Legal / Contract Managers & Procurement Managers.
    Returns count of generated alerts.
    """
    today = timezone.localdate()
    alerts_created = 0

    # 1. Expiration notice scan. One alert per contract *cycle* (end date): a renewed contract
    #    gets a fresh renewal alert when its new notice window opens.
    active_contracts = Contract.objects.filter(
        status__in=[Contract.STATUS_ACTIVE, Contract.STATUS_RENEWED]
    )
    for contract in active_contracts:
        notice_date = contract.end_date - timezone.timedelta(days=contract.renewal_notice_days)
        if (
            today >= notice_date
            and not ContractAlert.objects.filter(
                contract=contract,
                alert_type=ContractAlert.ALERT_RENEWAL,
                target_date=contract.end_date,
            ).exists()
        ):
            msg = f"Contract '{contract.contract_number}' is reaching renewal notice period (End date: {contract.end_date})."
            ContractAlert.objects.create(
                contract=contract,
                alert_type=ContractAlert.ALERT_RENEWAL,
                message=msg,
                target_date=contract.end_date,
            )
            _set_contract_status_by_scheduler(
                contract, Contract.STATUS_RENEWAL_DUE, "renewal notice window reached"
            )
            alerts_created += 1

            notify_role_users(
                role_codes=[Role.LEGAL_MGR, Role.PROC_MGR],
                notification_type=Notification.TYPE_CONTRACT_EXPIRATION,
                title=f"Renewal Notice Due: {contract.contract_number}",
                message=msg,
                target_url=f"/contracts/{contract.id}/",
            )

    # 2. Milestone due scan
    pending_milestones = ContractMilestone.objects.select_related("contract").filter(
        is_completed=False, due_date__lte=today + timezone.timedelta(days=7)
    )
    for milestone in pending_milestones:
        if not ContractAlert.objects.filter(
            alert_type=ContractAlert.ALERT_MILESTONE,
            milestone=milestone,
            target_date=milestone.due_date,
        ).exists():
            msg = f"Milestone '{milestone.title}' for contract '{milestone.contract.contract_number}' is due on {milestone.due_date}."
            ContractAlert.objects.create(
                contract=milestone.contract,
                alert_type=ContractAlert.ALERT_MILESTONE,
                message=msg,
                milestone=milestone,
                target_date=milestone.due_date,
            )
            alerts_created += 1

            notify_role_users(
                role_codes=[Role.LEGAL_MGR, Role.PROC_MGR],
                notification_type=Notification.TYPE_CONTRACT_MILESTONE,
                title=f"Milestone Due Warning: {milestone.title}",
                message=msg,
                target_url=f"/contracts/{milestone.contract.id}/",
            )

    # 3. Obligation due scan
    pending_obligations = ContractObligation.objects.select_related("contract").filter(
        is_fulfilled=False, due_date__lte=today + timezone.timedelta(days=7)
    )
    for obligation in pending_obligations:
        if not ContractAlert.objects.filter(
            alert_type=ContractAlert.ALERT_OBLIGATION,
            obligation=obligation,
            target_date=obligation.due_date,
        ).exists():
            msg = f"Legal Obligation '{obligation.title}' ({obligation.responsible_party}) for contract '{obligation.contract.contract_number}' is due on {obligation.due_date}."
            ContractAlert.objects.create(
                contract=obligation.contract,
                alert_type=ContractAlert.ALERT_OBLIGATION,
                message=msg,
                obligation=obligation,
                target_date=obligation.due_date,
            )
            alerts_created += 1

            notify_role_users(
                role_codes=[Role.LEGAL_MGR, Role.PROC_MGR],
                notification_type=Notification.TYPE_CONTRACT_OBLIGATION,
                title=f"Legal Obligation Warning: {obligation.title}",
                message=msg,
                target_url=f"/contracts/{obligation.contract.id}/",
            )

    # 4. Past end_date expiration scan
    expired_contracts = Contract.objects.filter(
        status__in=[Contract.STATUS_ACTIVE, Contract.STATUS_RENEWED, Contract.STATUS_RENEWAL_DUE],
        end_date__lt=today,
    )
    for contract in expired_contracts:
        if not ContractAlert.objects.filter(
            contract=contract,
            alert_type=ContractAlert.ALERT_EXPIRATION,
            target_date=contract.end_date,
        ).exists():
            msg = f"Contract '{contract.contract_number}' has expired as of {contract.end_date}."
            ContractAlert.objects.create(
                contract=contract,
                alert_type=ContractAlert.ALERT_EXPIRATION,
                message=msg,
                target_date=contract.end_date,
            )
            _set_contract_status_by_scheduler(contract, Contract.STATUS_EXPIRED, "end date passed")
            alerts_created += 1

            notify_role_users(
                role_codes=[Role.LEGAL_MGR, Role.PROC_MGR],
                notification_type=Notification.TYPE_CONTRACT_EXPIRATION,
                title=f"Contract Expired: {contract.contract_number}",
                message=msg,
                target_url=f"/contracts/{contract.id}/",
            )

    return alerts_created

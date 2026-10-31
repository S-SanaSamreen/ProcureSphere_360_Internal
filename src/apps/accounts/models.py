import uuid

from django.contrib.auth.models import AbstractUser, BaseUserManager
from django.db import models


class Role(models.Model):
    """

    System RBAC Role (Super Admin, Requester, Dept Approver, Procurement Exec, etc.)

    """

    SUPER_ADMIN = "SUPER_ADMIN"

    REQUESTER = "REQUESTER"

    DEPT_APPROVER = "DEPT_APPROVER"

    PROC_EXEC = "PROC_EXEC"

    PROC_MGR = "PROC_MGR"

    FINANCE_AP = "FINANCE_AP"

    STORES_RECEIVER = "STORES_RECEIVER"

    LEGAL_MGR = "LEGAL_MGR"

    AUDITOR = "AUDITOR"

    VENDOR_USER = "VENDOR_USER"

    ROLE_CHOICES = [
        (SUPER_ADMIN, "Super Admin"),
        (REQUESTER, "Requester"),
        (DEPT_APPROVER, "Department Approver"),
        (PROC_EXEC, "Procurement Executive"),
        (PROC_MGR, "Procurement Manager"),
        (FINANCE_AP, "Finance / AP Specialist"),
        (STORES_RECEIVER, "Stores / Receiver"),
        (LEGAL_MGR, "Legal / Contract Manager"),
        (AUDITOR, "Compliance Auditor"),
        (VENDOR_USER, "Vendor Portal User"),
    ]

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)

    code = models.CharField(max_length=50, choices=ROLE_CHOICES, unique=True)

    name = models.CharField(max_length=100)

    description = models.TextField(blank=True)

    created_at = models.DateTimeField(auto_now_add=True)

    def __str__(self):

        return f"{self.name} ({self.code})"


class UserManager(BaseUserManager):
    """

    Custom user manager for User model where email is the unique identifier.

    """

    def create_user(self, email, password=None, **extra_fields):

        if not email:

            raise ValueError("The Email field must be set")

        email = self.normalize_email(email)

        user = self.model(email=email, username=email, **extra_fields)

        user.set_password(password)

        user.save(using=self._db)

        return user

    def create_superuser(self, email, password=None, **extra_fields):

        extra_fields.setdefault("is_staff", True)

        extra_fields.setdefault("is_superuser", True)

        return self.create_user(email, password, **extra_fields)


class User(AbstractUser):
    """

    Custom User model for ProcureSphere 360.

    Uses UUID primary key and links to primary Role and Department.

    """

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)

    email = models.EmailField(unique=True)

    phone_number = models.CharField(max_length=20, blank=True)

    role = models.ForeignKey(
        Role, on_delete=models.SET_NULL, null=True, blank=True, related_name="users"
    )

    # Scoping relations
    organization = models.ForeignKey(
        "organization.Organization",
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="org_users",
        help_text="Organization to which this user belongs",
    )
    department = models.ForeignKey(
        "organization.Department",
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="users",
    )

    cost_center = models.ForeignKey(
        "organization.CostCenter",
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="requester_users",
        help_text="Designated cost center for Requester accounts",
    )

    approval_limit = models.DecimalField(
        max_digits=14,
        decimal_places=2,
        null=True,
        blank=True,
        help_text=(
            "Largest transaction value this user may approve. Blank means no personal limit "
            "(the approval policy still decides which roles may approve)."
        ),
    )

    vendor = models.ForeignKey(
        "vendors.Vendor",
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="vendor_users",
        help_text="If user belongs to a vendor self-service account",
    )

    must_change_password = models.BooleanField(
        default=False,
        help_text="Designates whether this user must change their password on first login.",
    )
    last_welcome_email_sent_at = models.DateTimeField(
        null=True,
        blank=True,
        help_text="Timestamp of the most recent welcome email delivery.",
    )

    # Second factor (TOTP); see apps/accounts/mfa.py. Recovery codes are stored hashed only.
    mfa_secret = models.CharField(max_length=64, blank=True, default="", editable=False)
    mfa_enabled = models.BooleanField(default=False)
    mfa_recovery_hashes = models.JSONField(default=list, blank=True, editable=False)
    mfa_last_step = models.BigIntegerField(default=0, editable=False)

    objects = UserManager()

    USERNAME_FIELD = "email"

    REQUIRED_FIELDS = []

    def __str__(self):

        return f"{self.email} [{self.role.code if self.role else 'NO_ROLE'}]"

    def clean(self):
        super().clean()
        self._enforce_tenancy_invariants()
        # Every internal user belongs to an organization; only the platform superuser (who
        # administers organizations) and vendors (independent external parties) are exempt.
        if (
            self.role_id
            and not self.is_superuser
            and self.role.code != Role.VENDOR_USER
            and not self.organization_id
            and not (self.department_id and self.department.organization_id)
        ):
            from django.core.exceptions import ValidationError

            raise ValidationError(
                "Internal users must belong to an organization. Vendors are the only exception."
            )

    _TENANCY_FIELDS = {"is_superuser", "role", "organization", "is_active"}

    def save(self, *args, **kwargs):
        update_fields = kwargs.get("update_fields")
        # Routine saves (e.g. last_login, password) must never be blocked by these rules.
        if (
            self._state.adding
            or update_fields is None
            or self._TENANCY_FIELDS & {str(f).removesuffix("_id") for f in update_fields}
        ):
            self._enforce_tenancy_invariants()
        super().save(*args, **kwargs)

    def _enforce_tenancy_invariants(self):
        """
        Platform rules: there is exactly one platform superuser (the Django superuser), and each
        organization has exactly one Super Admin, created by that superuser.
        """
        from django.conf import settings
        from django.core.exceptions import ValidationError

        others = type(self).objects.exclude(pk=self.pk)
        if (
            self.is_superuser
            and getattr(settings, "ENFORCE_SINGLE_PLATFORM_SUPERUSER", True)
            and others.filter(is_superuser=True).exists()
        ):
            raise ValidationError(
                "A platform superuser already exists. There can be only one platform administrator."
            )

        if self.role_id and self.organization_id and self.is_active:
            if (
                self.role.code == Role.SUPER_ADMIN
                and others.filter(
                    role__code=Role.SUPER_ADMIN,
                    organization_id=self.organization_id,
                    is_active=True,
                ).exists()
            ):
                raise ValidationError(
                    "This organization already has a Super Admin. "
                    "Each organization can have only one."
                )

    @property
    def role_code(self):

        return self.role.code if self.role else None

    @property
    def is_vendor(self):
        return self.role_code == Role.VENDOR_USER

    @property
    def effective_organization(self):
        if self.organization_id:
            return self.organization
        if self.department_id and self.department:
            return self.department.organization
        return None

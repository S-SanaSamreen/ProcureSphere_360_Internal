from django import forms
from django.contrib import admin
from django.contrib.auth.admin import UserAdmin as BaseUserAdmin
from django.contrib.auth.forms import UserChangeForm, UserCreationForm

from apps.accounts.models import Role, User


class CustomUserChangeForm(UserChangeForm):
    class Meta(UserChangeForm.Meta):
        model = User

    def clean(self):
        cleaned_data = super().clean()
        role = cleaned_data.get("role")
        organization = cleaned_data.get("organization")
        department = cleaned_data.get("department")
        cost_center = cleaned_data.get("cost_center")
        is_requester = role and getattr(role, "code", "") == Role.REQUESTER

        if is_requester:
            if not department:
                self.add_error("department", "Department is mandatory for Requester accounts.")
            else:
                if not organization:
                    cleaned_data["organization"] = department.organization
                elif department.organization_id != organization.id:
                    self.add_error(
                        "department",
                        f"Department '{department.name}' does not belong to selected Organization '{organization.name}'.",
                    )
            if not cost_center:
                self.add_error("cost_center", "Cost Center is mandatory for Requester accounts.")
            elif department and cost_center.department_id != department.id:
                self.add_error(
                    "cost_center",
                    f"Cost Center '{cost_center.code}' does not belong to the selected Department '{department.name}'.",
                )
        else:
            # Non-requester roles belong to the organization as a whole, not specific department/cost-center
            cleaned_data["department"] = None
            cleaned_data["cost_center"] = None

        return cleaned_data


class CustomUserCreationForm(UserCreationForm):
    class Meta(UserCreationForm.Meta):
        model = User
        fields = ("email", "organization", "role", "department", "cost_center")

    def clean(self):
        cleaned_data = super().clean()
        role = cleaned_data.get("role")
        organization = cleaned_data.get("organization")
        department = cleaned_data.get("department")
        cost_center = cleaned_data.get("cost_center")
        is_requester = role and getattr(role, "code", "") == Role.REQUESTER

        if is_requester:
            if not department:
                self.add_error("department", "Department is mandatory for Requester accounts.")
            else:
                if not organization:
                    cleaned_data["organization"] = department.organization
                elif department.organization_id != organization.id:
                    self.add_error(
                        "department",
                        f"Department '{department.name}' does not belong to selected Organization '{organization.name}'.",
                    )
            if not cost_center:
                self.add_error("cost_center", "Cost Center is mandatory for Requester accounts.")
            elif department and cost_center.department_id != department.id:
                self.add_error(
                    "cost_center",
                    f"Cost Center '{cost_center.code}' does not belong to the selected Department '{department.name}'.",
                )
        else:
            cleaned_data["department"] = None
            cleaned_data["cost_center"] = None

        return cleaned_data


@admin.register(Role)
class RoleAdmin(admin.ModelAdmin):
    list_display = ("name", "code", "created_at")
    search_fields = ("name", "code")


@admin.register(User)
class UserAdmin(BaseUserAdmin):
    form = CustomUserChangeForm
    add_form = CustomUserCreationForm

    list_display = (
        "email",
        "organization",
        "first_name",
        "last_name",
        "role",
        "department",
        "cost_center",
        "is_staff",
        "is_superuser",
        "is_active",
    )
    list_filter = (
        "organization",
        "role",
        "is_staff",
        "is_superuser",
        "is_active",
        "department",
        "cost_center",
    )
    search_fields = ("email", "first_name", "last_name")
    ordering = ("email",)

    fieldsets = (
        (None, {"fields": ("email", "password")}),
        (
            "Personal info",
            {"fields": ("first_name", "last_name", "phone_number")},
        ),
        (
            "Organization & Scoping",
            {"fields": ("organization", "role", "department", "cost_center")},
        ),
        (
            "Permissions",
            {
                "fields": (
                    "is_active",
                    "is_staff",
                    "is_superuser",
                    "groups",
                    "user_permissions",
                )
            },
        ),
        (
            "Important dates",
            {"fields": ("last_login", "date_joined")},
        ),
    )

    add_fieldsets = (
        (
            None,
            {
                "classes": ("wide",),
                "fields": (
                    "email",
                    "organization",
                    "role",
                    "department",
                    "cost_center",
                    "password1",
                    "password2",
                ),
            },
        ),
    )

    class Media:
        js = ("admin/js/user_role_scoping.js",)

    def save_model(self, request, obj, form, change):
        if obj.department and not obj.organization:
            obj.organization = obj.department.organization
        if not obj.role or obj.role.code != Role.REQUESTER:
            obj.department = None
            obj.cost_center = None
        super().save_model(request, obj, form, change)


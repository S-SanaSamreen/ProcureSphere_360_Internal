"""Helpers for fixtures: every internal user belongs to an organisation (tenant isolation)."""

from apps.accounts.models import User
from apps.organization.models import Organization


def default_org():
    org, _ = Organization.objects.get_or_create(
        code="TEST-ORG", defaults={"name": "Test Organization"}
    )
    return org


def create_org_user(**kwargs):
    """``User.objects.create_user`` that attaches internal (non-vendor) users to the test org."""
    if not kwargs.get("vendor") and not kwargs.get("organization") and not kwargs.get("department"):
        kwargs["organization"] = default_org()
    return User.objects.create_user(**kwargs)

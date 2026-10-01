import pytest
from django.test import Client
from apps.accounts.models import Role, User


@pytest.mark.django_db
class TestRoleDashboards:
    """
    Automated regression suite verifying that EVERY ERP role can access
    the home dashboard (/) without TemplateDoesNotExist or 500 errors.
    """

    @pytest.fixture(autouse=True)
    def setup_roles(self):
        self.roles = {}
        for code, name in Role.ROLE_CHOICES:
            role, _ = Role.objects.get_or_create(code=code, defaults={"name": name})
            self.roles[code] = role

    @pytest.mark.parametrize("role_code", [
        Role.SUPER_ADMIN,
        Role.REQUESTER,
        Role.DEPT_APPROVER,
        Role.PROC_EXEC,
        Role.PROC_MGR,
        Role.FINANCE_AP,
        Role.STORES_RECEIVER,
        Role.LEGAL_MGR,
        Role.AUDITOR,
        Role.VENDOR_USER,
    ])
    def test_dashboard_renders_for_every_role(self, role_code):
        user = User.objects.create_user(
            email=f"{role_code.lower()}_test@procuresphere.com",
            password="SecurePassword123!",
            role=self.roles[role_code],
        )

        client = Client()
        client.force_login(user)

        response = client.get("/")
        assert response.status_code == 200, f"Dashboard failed with {response.status_code} for role {role_code}"
        assert b"ProcureSphere 360" in response.content

    def test_dashboard_fallback_for_user_without_role(self):
        user = User.objects.create_user(
            email="norole_test@procuresphere.com",
            password="SecurePassword123!",
        )

        client = Client()
        client.force_login(user)

        response = client.get("/")
        assert response.status_code == 200
        assert b"ProcureSphere 360" in response.content

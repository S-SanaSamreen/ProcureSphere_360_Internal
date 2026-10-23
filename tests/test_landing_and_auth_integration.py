import pytest
from django.urls import reverse

from apps.accounts.models import Role, User
from apps.audit.models import AuditLog
from apps.vendors.models import Vendor, VendorCategory


@pytest.mark.django_db
def test_anonymous_access_serves_landing_page(client):
    """Anonymous visitors accessing root or marketing routes receive server-rendered Django templates with CSRF cookie."""
    # Test root /
    resp_root = client.get("/")
    assert resp_root.status_code == 200
    content = resp_root.content.decode()
    assert "landing-body" in content
    assert "ProcureSphere 360" in content
    assert "ps-login-overlay" in content
    assert "csrftoken" in resp_root.cookies

    # Test marketing subroutes render their respective server-rendered Django templates
    subroutes_and_snippets = [
        ("/about/", "A New Era of Enterprise Procurement"),
        ("/solutions/", "Enterprise Procurement Solutions"),
        ("/benefits/", "Legacy Procurement vs. ProcureSphere 360"),
        ("/process/", "The End-to-End Procurement Process"),
        ("/roles/", "One Platform. Every Stakeholder."),
        ("/faq/", "Frequently Asked Questions"),
    ]
    for route, snippet in subroutes_and_snippets:
        resp = client.get(route)
        assert resp.status_code == 200, f"Route {route} failed with {resp.status_code}"
        assert snippet in resp.content.decode()
        assert "landing-body" in resp.content.decode()


@pytest.mark.django_db
def test_custom_404_page_renders_branded_template(client):
    """Unmatched public route returns HTTP 404 with custom branded landing 404 template."""
    resp = client.get("/non-existent-random-route/")
    assert resp.status_code == 404
    content = resp.content.decode()
    assert "notfound-container" in content
    assert "Oops! Page Not Found" in content
    assert "404-full.webp" in content


@pytest.mark.django_db
def test_session_login_api_authentication_success(client):
    """Modal login via /api/v1/auth/login/ establishes real session and audit trail."""
    role, _ = Role.objects.get_or_create(code=Role.REQUESTER, defaults={"name": "Requester"})
    user = User.objects.create_user(
        email="test_modal_requester@hpe.com",
        password="Password123!",
        role=role,
    )

    # Acquire CSRF cookie
    client.get("/")
    csrf_token = client.cookies.get("csrftoken").value

    # Authenticate via API
    resp = client.post(
        "/api/v1/auth/login/",
        data={"email": "test_modal_requester@hpe.com", "password": "Password123!"},
        content_type="application/json",
        HTTP_X_CSRFTOKEN=csrf_token,
    )
    assert resp.status_code == 200
    data = resp.json()
    assert data["message"] == "Login successful."
    assert data["user"]["email"] == "test_modal_requester@hpe.com"
    assert data["user"]["role_code"] == Role.REQUESTER
    assert "sessionid" in client.cookies

    # Verify AuditLog created
    audit = AuditLog.objects.filter(actor=user, action=AuditLog.ACTION_LOGIN).first()
    assert audit is not None
    assert audit.target_model == "User"
    assert audit.target_object_id == str(user.id)

    # After login, accessing / renders the Requester dashboard
    resp_home = client.get("/")
    assert resp_home.status_code == 200
    assert "requester" in resp_home.content.decode().lower()


@pytest.mark.django_db
def test_session_login_api_invalid_credentials(client):
    """Invalid credentials return HTTP 401 with structured error."""
    client.get("/")
    csrf_token = client.cookies.get("csrftoken").value

    resp = client.post(
        "/api/v1/auth/login/",
        data={"email": "nonexistent@hpe.com", "password": "WrongPassword!"},
        content_type="application/json",
        HTTP_X_CSRFTOKEN=csrf_token,
    )
    assert resp.status_code == 401
    data = resp.json()
    assert data["error"]["code"] == "AUTHENTICATION_FAILED"
    assert "sessionid" not in client.cookies


@pytest.mark.django_db
def test_authenticated_user_accesses_all_role_dashboards(client):
    """When authenticated, / continues dispatching to existing role operational dashboards."""
    roles_to_test = [
        (Role.SUPER_ADMIN, "ProcureSphere 360"),
        (Role.REQUESTER, "Requester"),
        (Role.DEPT_APPROVER, "Approver"),
        (Role.PROC_MGR, "Manager"),
        (Role.PROC_EXEC, "Procurement"),
        (Role.STORES_RECEIVER, "Stores"),
        (Role.FINANCE_AP, "Finance"),
        (Role.LEGAL_MGR, "Legal"),
        (Role.AUDITOR, "Auditor"),
        (Role.VENDOR_USER, "Vendor"),
    ]

    for role_code, expected_snippet in roles_to_test:
        role, _ = Role.objects.get_or_create(code=role_code, defaults={"name": role_code})
        user = User.objects.create_user(
            email=f"verify_{role_code.lower()}@hpe.com",
            password="Password123!",
            role=role,
        )

        if role_code == Role.VENDOR_USER:
            cat = VendorCategory.objects.create(name=f"Cat {role_code}", code=f"CAT-{role_code}")
            vendor = Vendor.objects.create(
                legal_name="Test Modal Vendor",
                tax_identification_number=f"TAX-{role_code}",
                category=cat,
                email=user.email,
                status=Vendor.STATUS_ACTIVE,
            )
            user.vendor = vendor
            user.save()

        client.force_login(user)
        response = client.get("/")
        assert response.status_code == 200, f"Role {role_code} dashboard failed"
        assert expected_snippet.lower() in response.content.decode().lower()


@pytest.mark.django_db
def test_standalone_login_page_remains_functional(client):
    """The traditional standalone /login/ route continues functioning for fallback and tests."""
    resp = client.get("/login/")
    assert resp.status_code == 200
    assert "Sign In" in resp.content.decode()
    assert "loginForm" in resp.content.decode()

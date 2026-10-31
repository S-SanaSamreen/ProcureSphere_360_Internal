"""Platform tier: MFA, admin IP allow-list, backup evidence, maintenance mode, Super Admin setup."""

import pyotp
import pytest
from django.core import mail
from django.core.cache import cache
from django.core.management import call_command
from django.test import override_settings

from apps.accounts import mfa
from apps.accounts.models import Role, User
from apps.core.models import BackupRecord, MaintenanceState
from apps.organization.models import Organization
from apps.organization.services import create_organization_with_admin_service

pytestmark = pytest.mark.django_db

PW = "Password123!"


@pytest.fixture(autouse=True)
def _clear_cache():
    cache.clear()
    yield
    cache.clear()


@pytest.fixture
def org_admin(db_roles):
    org = Organization.objects.create(name="Acme", code="ACME")
    return User.objects.create_user(
        email="sa@acme.test", password=PW, role=db_roles[Role.SUPER_ADMIN], organization=org
    )


def _enable_mfa(user):
    secret = pyotp.random_base32()
    user.mfa_secret = secret
    user.mfa_enabled = True
    plain, hashes = mfa.generate_recovery_codes()
    user.mfa_recovery_hashes = hashes
    user.save()
    return secret, plain


# ---------------------------------------------------------------- MFA


def test_login_without_mfa_is_unchanged(client, org_admin):
    resp = client.post("/login/", {"email": org_admin.email, "password": PW})
    assert resp.status_code == 302 and "/mfa/" not in resp.url
    assert client.get("/superadmin/").headers.get("Location", "") != "/accounts/mfa/verify/"


def test_mfa_login_requires_second_factor(client, org_admin):
    secret, _ = _enable_mfa(org_admin)
    resp = client.post("/login/", {"email": org_admin.email, "password": PW})
    assert resp.status_code == 302 and resp.url == "/accounts/mfa/verify/"
    # protected pages bounce back to verification until the code is entered
    assert client.get("/superadmin/").headers["Location"] == "/accounts/mfa/verify/"
    assert client.get("/api/v1/users/").status_code == 403
    bad = client.post("/accounts/mfa/verify/", {"code": "000000"})
    assert bad.status_code == 200
    good = client.post("/accounts/mfa/verify/", {"code": pyotp.TOTP(secret).now()})
    assert good.status_code == 302
    assert client.get("/superadmin/").headers.get("Location", "") != "/accounts/mfa/verify/"


def test_mfa_totp_cannot_be_replayed(org_admin):
    secret, _ = _enable_mfa(org_admin)
    code = pyotp.TOTP(secret).now()
    assert mfa.verify_second_factor(org_admin, code) is True
    assert mfa.verify_second_factor(org_admin, code) is False


def test_mfa_recovery_code_is_single_use_and_hashed(org_admin):
    _, plain = _enable_mfa(org_admin)
    assert plain[0] not in org_admin.mfa_recovery_hashes
    assert mfa.verify_second_factor(org_admin, plain[0]) is True
    org_admin.refresh_from_db()
    assert len(org_admin.mfa_recovery_hashes) == mfa.RECOVERY_CODE_COUNT - 1
    assert mfa.verify_second_factor(org_admin, plain[0]) is False


def test_mfa_brute_force_lockout(client, org_admin):
    secret, _ = _enable_mfa(org_admin)
    client.post("/login/", {"email": org_admin.email, "password": PW})
    for _i in range(mfa.MAX_FAILURES):
        client.post("/accounts/mfa/verify/", {"code": "000000"})
    # even a correct code is refused while locked out
    resp = client.post("/accounts/mfa/verify/", {"code": pyotp.TOTP(secret).now()})
    assert resp.status_code == 200
    assert client.get("/superadmin/").headers["Location"] == "/accounts/mfa/verify/"


def test_mfa_failures_are_audit_logged(client, org_admin):
    from apps.audit.models import AuditLog

    _enable_mfa(org_admin)
    client.post("/login/", {"email": org_admin.email, "password": PW})
    client.post("/accounts/mfa/verify/", {"code": "000000"})
    assert any(
        (log.new_state or {}).get("event") == "mfa_failed"
        for log in AuditLog.objects.filter(target_model="User")
    )


def test_mfa_enrolment_flow_and_forced_when_required(client, org_admin):
    with override_settings(MFA_REQUIRED_FOR_SUPER_ADMIN=True):
        client.post("/login/", {"email": org_admin.email, "password": PW})
        assert client.get("/superadmin/").headers["Location"] == "/accounts/mfa/setup/"
        page = client.get("/accounts/mfa/setup/")
        assert page.status_code == 200 and b"otpauth://" in page.content
        secret = client.session["mfa_pending_secret"]
        assert client.post("/accounts/mfa/setup/", {"code": "111111"}).status_code == 200
        done = client.post("/accounts/mfa/setup/", {"code": pyotp.TOTP(secret).now()})
        assert done.status_code == 200 and b"recovery" in done.content.lower()
        org_admin.refresh_from_db()
        assert org_admin.mfa_enabled and org_admin.mfa_secret == secret
        assert client.get("/superadmin/").headers.get("Location", "") != "/accounts/mfa/setup/"


def test_mfa_not_required_for_other_roles_even_when_setting_on(client, db_roles):
    org = Organization.objects.create(name="B", code="B1")
    user = User.objects.create_user(
        email="r@b.test", password=PW, role=db_roles[Role.REQUESTER], organization=org
    )
    with override_settings(MFA_REQUIRED_FOR_SUPER_ADMIN=True):
        assert mfa.needs_enrolment(user) is False
        sa = User.objects.create_user(
            email="s@b.test", password=PW, role=db_roles[Role.SUPER_ADMIN], organization=org
        )
        assert mfa.needs_enrolment(sa) is True


def test_api_session_login_blocks_until_verified(client, org_admin):
    _enable_mfa(org_admin)
    resp = client.post(
        "/api/v1/auth/login/",
        {"email": org_admin.email, "password": PW},
        content_type="application/json",
    )
    assert resp.status_code == 200
    assert client.get("/api/v1/users/").status_code == 403


# ---------------------------------------------------------------- admin IP allow-list


def test_admin_ip_allow_list(client, super_admin_user):
    client.force_login(super_admin_user)
    assert client.get("/admin/", REMOTE_ADDR="10.9.9.9").status_code != 403  # default: open
    with override_settings(ADMIN_ALLOWED_IPS=["10.0.0.0/24", "192.168.1.5"]):
        assert client.get("/admin/", REMOTE_ADDR="10.0.0.7").status_code != 403
        assert client.get("/admin/", REMOTE_ADDR="192.168.1.5").status_code != 403
        assert client.get("/admin/", REMOTE_ADDR="8.8.8.8").status_code == 403
        # non-admin paths are unaffected
        assert client.get("/health", REMOTE_ADDR="8.8.8.8").status_code != 403


def test_admin_ip_allow_list_forwarded_header_only_if_trusted(client, super_admin_user):
    client.force_login(super_admin_user)
    kw = {"REMOTE_ADDR": "8.8.8.8", "HTTP_X_FORWARDED_FOR": "10.0.0.7"}
    with override_settings(ADMIN_ALLOWED_IPS=["10.0.0.7"], TRUSTED_PROXY=False):
        assert client.get("/admin/", **kw).status_code == 403  # spoofed header ignored
    with override_settings(ADMIN_ALLOWED_IPS=["10.0.0.7"], TRUSTED_PROXY=True):
        assert client.get("/admin/", **kw).status_code != 403


# ---------------------------------------------------------------- backup evidence


def test_record_backup_evidence_command_and_append_only():
    call_command(
        "record_backup_evidence",
        "--kind=RESTORE_TEST",
        "--reference=s3://bk/2026-10-31.dump",
        "--size-bytes=1024",
        "--checksum=abc",
        "--performed-by=ops",
    )
    rec = BackupRecord.objects.get()
    assert rec.kind == "RESTORE_TEST" and rec.status == "SUCCESS" and rec.size_bytes == 1024
    rec.notes = "edited"
    with pytest.raises(ValueError):
        rec.save()
    with pytest.raises(ValueError):
        rec.delete()


def test_backup_admin_visible_to_platform_superuser_only(client, super_admin_user, org_admin):
    client.force_login(super_admin_user)
    assert client.get("/admin/core/backuprecord/").status_code == 200
    client.force_login(org_admin)
    assert client.get("/admin/core/backuprecord/").status_code in (302, 403)


# ---------------------------------------------------------------- maintenance mode


def test_maintenance_mode_blocks_non_superusers_and_bannerises_superuser(
    client, super_admin_user, org_admin
):
    MaintenanceState.objects.create(enabled=True, message="Upgrading")
    client.force_login(org_admin)
    resp = client.get("/superadmin/")
    assert resp.status_code == 503 and b"Upgrading" in resp.content
    assert client.get("/api/v1/users/").status_code == 503
    assert client.get("/health").status_code != 503
    client.force_login(super_admin_user)
    resp = client.get("/admin/")
    assert resp.status_code == 200 and b"MAINTENANCE MODE is ON" in resp.content


def test_maintenance_mode_off_and_env_switch(client, org_admin):
    client.force_login(org_admin)
    assert client.get("/superadmin/").status_code != 503
    with override_settings(MAINTENANCE_MODE=True):
        assert client.get("/superadmin/").status_code == 503
    state = MaintenanceState.objects.create(enabled=True)
    assert client.get("/superadmin/").status_code == 503
    state.enabled = False
    state.save()  # cache invalidated on save
    assert client.get("/superadmin/").status_code != 503


# ---------------------------------------------------------------- Super Admin creation


def test_platform_org_creation_forces_password_change_and_emails_credentials(db_roles):
    org, admin_user, raw_pwd = create_organization_with_admin_service(
        name="Globex",
        code="GLBX",
        admin_email="boss@globex.test",
        admin_first_name="Gina",
        admin_last_name="Boss",
        send_welcome_email_flag=True,
    )
    admin_user.refresh_from_db()
    assert admin_user.must_change_password is True
    assert admin_user.role.code == Role.SUPER_ADMIN and admin_user.organization_id == org.id
    assert raw_pwd and admin_user.check_password(raw_pwd)
    assert any("boss@globex.test" in m.to for m in mail.outbox)


def test_totp_step_is_time_based():
    secret = pyotp.random_base32()
    assert mfa.check_totp(secret, pyotp.TOTP(secret).now()) is not None
    assert mfa.check_totp(secret, "abcdef") is None

"""PRD groups F (security/RBAC) and G (reports, async job runs)."""

import datetime
import subprocess
import sys
from pathlib import Path

import pytest
from django.contrib import admin as django_admin
from django.core.exceptions import ImproperlyConfigured
from django.test import Client
from django.urls import get_resolver, reverse
from rest_framework.test import APIClient

from apps.accounts.models import Role, User
from apps.core.models import JobRun
from tests.test_group_c_lifecycle import D, world  # noqa: F401  (shared fixture)

pytestmark = pytest.mark.django_db


def _list_urls():
    from django.urls import URLPattern, URLResolver

    out = []

    def walk(patterns, prefix=""):
        for p in patterns:
            if isinstance(p, URLResolver):
                walk(p.url_patterns, prefix + str(p.pattern))
            elif isinstance(p, URLPattern):
                full = prefix + str(p.pattern)
                if full.startswith("api/v1/") and "<" not in full and "(?P" not in full:
                    out.append("/" + full.replace("^", "").replace("$", ""))

    walk(get_resolver().url_patterns)
    return sorted(set(u for u in out if u.endswith("/")))


def _payload_has_rows(resp):
    try:
        data = resp.json()
    except Exception:
        return False
    if isinstance(data, dict) and "results" in data:
        data = data["results"]
    return bool(data) if isinstance(data, list) else False


# ------------------------------------------------------------------------- F1
@pytest.mark.parametrize("kind", ["no_org", "no_role"])
def test_api_fails_closed_for_orgless_and_roleless_users(world, kind):  # noqa: F811
    w = world
    # seed some tenant data so an unscoped queryset would leak it
    assert w["vendor"].pk
    if kind == "no_org":
        user = User.objects.create_user(
            email="noorg@x.test", password="Password123!", role=w["db_roles"][Role.REQUESTER]
        )
    else:
        user = User.objects.create_user(
            email="norole@x.test", password="Password123!", organization=w["org"]
        )
    client = APIClient()
    client.force_login(user)
    leaks = []
    for url in _list_urls():
        if any(
            s in url for s in ("auth/", "/me/", "exports/", "/roles/", "/policies/", "/categories/")
        ):
            continue
        resp = client.get(url)
        if resp.status_code == 200 and _payload_has_rows(resp):
            leaks.append(url)
    assert not leaks, leaks


def test_legal_manager_can_view_vendors_in_own_org(world):  # noqa: F811
    w = world
    c = Client()
    c.force_login(w["legal"])
    assert c.get("/vendors/").status_code == 200
    assert c.get(f"/vendors/{w['vendor'].pk}/").status_code == 200


def test_roleless_user_gets_403_on_web_pages(world):  # noqa: F811
    w = world
    user = User.objects.create_user(
        email="norole2@x.test", password="Password123!", organization=w["org"]
    )
    c = Client()
    c.force_login(user)
    for url in ("/vendors/", "/invoices/", "/purchase-orders/", "/reports/"):
        assert c.get(url).status_code in (302, 403, 404), url


# ------------------------------------------------------------------------- F2
def _user(w, email, code):
    return User.objects.create_user(
        email=email, password="Password123!", role=w["db_roles"][code], organization=w["org"]
    )


@pytest.mark.parametrize(
    "code,expected_ok",
    [
        (Role.FINANCE_AP, True),
        (Role.SUPER_ADMIN, True),
        (Role.AUDITOR, True),
        (Role.PROC_MGR, True),
        (Role.PROC_EXEC, True),
        (Role.STORES_RECEIVER, False),
        (Role.LEGAL_MGR, False),
        (Role.REQUESTER, False),
    ],
)
def test_invoice_reads_restricted_by_role(world, code, expected_ok):  # noqa: F811
    u = world["admin"] if code == Role.SUPER_ADMIN else _user(world, f"inv-{code}@x.test", code)
    c = Client()
    c.force_login(u)
    api = APIClient()
    api.force_login(u)
    assert (c.get("/invoices/").status_code == 200) is expected_ok
    assert (api.get("/api/v1/invoices/").status_code == 200) is expected_ok


def test_stores_receiver_cannot_read_invoice_detail(world):  # noqa: F811
    import uuid

    u = _user(world, "stores-inv@x.test", Role.STORES_RECEIVER)
    c = Client()
    c.force_login(u)
    assert c.get(f"/invoices/{uuid.uuid4()}/").status_code == 403


# ------------------------------------------------------------------------- F4
def test_validate_secret_key_rejects_weak_keys():
    from config.settings.secret_guard import INSECURE_DEFAULT_KEY, validate_secret_key

    for bad in ("", None, INSECURE_DEFAULT_KEY, "short", "django-insecure-" + "a" * 60):
        with pytest.raises(ImproperlyConfigured):
            validate_secret_key(bad)
    good = "k" * 64
    assert validate_secret_key(good) == good


@pytest.mark.parametrize("module", ["prod", "staging"])
def test_prod_and_staging_settings_refuse_default_secret_key(module):
    import os

    root = Path(__file__).resolve().parent.parent
    env = {k: v for k, v in os.environ.items() if k != "PROCURESPHERE_TESTING"}
    env.pop("DJANGO_SECRET_KEY", None)
    env.update(
        DJANGO_SETTINGS_MODULE=f"config.settings.{module}",
        DJANGO_ALLOWED_HOSTS="example.com",
        PYTHONPATH=str(root / "src"),
    )
    bad = subprocess.run(
        [sys.executable, "-c", "import django; django.setup()"],
        env=env,
        capture_output=True,
        text=True,
        cwd=root,
    )
    assert bad.returncode != 0 and "DJANGO_SECRET_KEY" in bad.stderr


# ------------------------------------------------------------------------- F5
def test_audit_trigger_migration_is_noop_on_sqlite_and_reversible():
    import importlib

    from django.db import connection

    mig = importlib.import_module("apps.audit.migrations.0003_pg_append_only_trigger")
    assert connection.vendor != "postgresql"

    class FakeEditor:
        def __init__(self):
            self.connection = connection
            self.executed = []

        def execute(self, sql):
            self.executed.append(sql)

    editor = FakeEditor()
    mig.install(None, editor)
    mig.uninstall(None, editor)
    assert editor.executed == []
    assert "BEFORE UPDATE OR DELETE" in mig.CREATE_SQL
    assert mig.Migration.operations[0].reversible


# ------------------------------------------------------------------------- F6
def test_drf_throttle_configuration():
    from django.conf import settings as s

    assert (
        "rest_framework.throttling.AnonRateThrottle" in s.REST_FRAMEWORK["DEFAULT_THROTTLE_CLASSES"]
    )
    assert {"anon", "user", "login"} <= set(s.REST_FRAMEWORK["DEFAULT_THROTTLE_RATES"])
    from apps.accounts.api_views import SessionLoginView

    assert SessionLoginView.throttle_scope == "login"


def test_login_scope_throttles_when_rate_exceeded(monkeypatch):
    from django.core.cache import cache
    from rest_framework.throttling import ScopedRateThrottle

    cache.clear()
    monkeypatch.setattr(ScopedRateThrottle, "THROTTLE_RATES", {"login": "2/min"})
    client = APIClient()
    codes = [
        client.post("/api/v1/auth/login/", {"email": "a@b.c", "password": "x"}).status_code
        for _ in range(4)
    ]
    cache.clear()
    assert 429 in codes


# ------------------------------------------------------------------------- G1
def test_api_export_queued_as_celery_task_and_ignores_client_status(
    world,  # noqa: F811
    monkeypatch,
):
    from apps.reports import tasks
    from apps.reports.models import ExportJob

    queued = []
    monkeypatch.setattr(tasks.run_export_job_task, "delay", lambda job_id: queued.append(job_id))
    api = APIClient()
    api.force_login(_user(world, "exp@x.test", Role.FINANCE_AP))
    resp = api.post(
        "/api/v1/reports/exports/",
        {
            "report_type": "pr_aging",
            "export_format": "CSV",
            "status": "COMPLETED",
            "file_hash": "f" * 64,
        },
        format="json",
    )
    assert resp.status_code == 201, resp.content
    job = ExportJob.objects.get(pk=resp.json()["id"])
    assert queued == [job.pk]
    assert job.status == ExportJob.STATUS_PENDING and job.file_hash == ""


def test_api_export_completes_when_celery_eager(world):  # noqa: F811
    from apps.reports.models import ExportJob

    api = APIClient()
    api.force_login(_user(world, "exp2@x.test", Role.FINANCE_AP))
    resp = api.post("/api/v1/reports/exports/", {"report_type": "pr_aging", "export_format": "CSV"})
    assert resp.status_code == 201
    job = ExportJob.objects.get(pk=resp.json()["id"])
    assert job.status == ExportJob.STATUS_COMPLETED and job.file_hash


# ------------------------------------------------------------------------- G3
def test_job_run_records_success_and_failure():
    from apps.core.job_runs import record_job_run

    @record_job_run("tests.ok")
    def ok():
        return {"n": 1}

    @record_job_run("tests.boom")
    def boom():
        raise RuntimeError("kaboom")

    assert ok() == {"n": 1}
    with pytest.raises(RuntimeError):
        boom()
    good = JobRun.objects.get(task_name="tests.ok")
    bad = JobRun.objects.get(task_name="tests.boom")
    assert good.status == JobRun.STATUS_SUCCESS and good.finished_at
    assert bad.status == JobRun.STATUS_FAILED and "kaboom" in bad.detail


def test_periodic_tasks_write_job_runs(world):  # noqa: F811
    from apps.contracts.tasks import scan_contract_expirations_and_milestones_task
    from apps.notifications.tasks import scan_overdue_approvals_and_escalate
    from apps.sourcing.tasks import sync_sourcing_bid_windows_task
    from apps.vendors.tasks import scan_vendor_document_expirations_task

    for t in (
        scan_contract_expirations_and_milestones_task,
        scan_overdue_approvals_and_escalate,
        sync_sourcing_bid_windows_task,
        scan_vendor_document_expirations_task,
    ):
        t()
    assert JobRun.objects.values("task_name").distinct().count() == 4


def test_job_run_admin_read_only_platform_superuser_only(world):  # noqa: F811
    from django.test import RequestFactory
    from django.utils import timezone

    JobRun.objects.create(task_name="t", started_at=timezone.now(), status="SUCCESS")
    platform = User.objects.create_superuser(email="plat@x.test", password="Password123!")
    model_admin = django_admin.site._registry[JobRun]

    def req(u):
        r = RequestFactory().get("/")
        r.user = u
        return r

    assert model_admin.has_view_permission(req(platform))
    assert not model_admin.has_add_permission(req(platform))
    assert not model_admin.has_change_permission(req(platform))
    assert not model_admin.has_delete_permission(req(platform))
    assert not model_admin.has_view_permission(req(world["admin"]))
    c = Client()
    c.force_login(platform)
    assert c.get(reverse("admin:core_jobrun_changelist")).status_code == 200

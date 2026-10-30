"""Contract alert cycles and audited scheduler transitions (audit group C)."""

import datetime

import pytest

from apps.audit.models import AuditLog
from apps.contracts.models import Contract, ContractAlert
from apps.contracts.services import (
    activate_contract_service,
    add_contract_milestone_service,
    add_contract_obligation_service,
    approve_legal_review_service,
    create_contract_service,
    renew_contract_service,
    submit_for_legal_review_service,
)
from apps.contracts.services import (
    scan_contract_expirations_and_milestones_service as scan,
)
from tests.test_group_c_lifecycle import D, world  # noqa: F401  (shared fixture)

pytestmark = pytest.mark.django_db


def make_active_contract(w, days_to_end=100, notice=30, title="Alert Contract"):
    today = w["today"]
    contract = create_contract_service(
        title=title,
        vendor=w["vendor"],
        contract_value=D("10000"),
        start_date=today - datetime.timedelta(days=200),
        end_date=today + datetime.timedelta(days=days_to_end),
        renewal_notice_days=notice,
        contract_owner=w["legal"],
    )
    submit_for_legal_review_service(contract=contract, user=w["legal"], notes="n")
    approve_legal_review_service(contract=contract, user=w["legal"], notes="ok")
    return activate_contract_service(contract=contract, user=w["mgr"])


def test_renewal_alert_fires_once_per_cycle_and_again_for_the_next_cycle(world):  # noqa: F811
    w = world
    contract = make_active_contract(w, days_to_end=20, notice=30)

    assert scan() == 1
    contract.refresh_from_db()
    first_end = contract.end_date
    assert contract.status == Contract.STATUS_RENEWAL_DUE
    alert = ContractAlert.objects.get(contract=contract, alert_type=ContractAlert.ALERT_RENEWAL)
    assert alert.target_date == first_end

    assert scan() == 0  # same cycle: no duplicate
    assert ContractAlert.objects.filter(contract=contract, alert_type="RENEWAL").count() == 1

    # Renew for a year: out of the notice window, so no alert...
    renew_contract_service(
        contract=contract, user=w["legal"], new_end_date=first_end + datetime.timedelta(days=365)
    )
    contract.refresh_from_db()
    assert contract.status == Contract.STATUS_RENEWED
    assert scan() == 0

    # ...until time passes and the NEXT cycle's notice window opens.
    next_end = w["today"] + datetime.timedelta(days=10)
    Contract.objects.filter(pk=contract.pk).update(end_date=next_end)
    assert scan() == 1
    contract.refresh_from_db()
    assert contract.status == Contract.STATUS_RENEWAL_DUE
    renewal_alerts = ContractAlert.objects.filter(contract=contract, alert_type="RENEWAL")
    assert renewal_alerts.count() == 2
    assert set(renewal_alerts.values_list("target_date", flat=True)) == {first_end, next_end}


def test_scheduler_driven_status_changes_are_audited_as_system_actions(world):  # noqa: F811
    w = world
    contract = make_active_contract(w, days_to_end=20, notice=30)
    scan()
    entry = AuditLog.objects.get(
        target_model="Contract", target_object_id=str(contract.id), actor__isnull=True
    )
    assert entry.previous_state == {"status": "ACTIVE"}
    assert entry.new_state["status"] == "RENEWAL_DUE"
    assert "scheduled scan" in entry.new_state["source"]
    assert entry.user_agent.startswith("celery-beat/")

    Contract.objects.filter(pk=contract.pk).update(end_date=w["today"] - datetime.timedelta(days=3))
    # RENEWAL_DUE contracts past their end date are expired by the scan
    scan()
    contract.refresh_from_db()
    assert contract.status == Contract.STATUS_EXPIRED
    expired = AuditLog.objects.filter(
        target_model="Contract", target_object_id=str(contract.id), actor__isnull=True
    ).order_by("-timestamp")[0]
    assert expired.previous_state == {"status": "RENEWAL_DUE"}
    assert expired.new_state["status"] == "EXPIRED"
    assert ContractAlert.objects.filter(contract=contract, alert_type="EXPIRATION").count() == 1
    assert scan() == 0  # an expired contract is not alerted again


def test_milestone_alerts_are_per_item_even_with_identical_titles(world):  # noqa: F811
    w = world
    contract = make_active_contract(w, days_to_end=300, notice=10)
    soon = w["today"] + datetime.timedelta(days=3)
    m1 = add_contract_milestone_service(
        contract=contract, title="SLA Review", due_date=soon, user=w["legal"]
    )
    m2 = add_contract_milestone_service(
        contract=contract,
        title="SLA Review",
        due_date=soon + datetime.timedelta(days=1),
        user=w["legal"],
    )
    assert scan() == 2  # the old title-based de-duplication would have produced 1
    assert ContractAlert.objects.filter(milestone=m1).count() == 1
    assert ContractAlert.objects.filter(milestone=m2).count() == 1
    assert scan() == 0

    # Moving a milestone's due date (still inside the window) is something new to be told about.
    m1.due_date = soon + datetime.timedelta(days=2)
    m1.save(update_fields=["due_date"])
    assert scan() == 1
    assert ContractAlert.objects.filter(milestone=m1).count() == 2


def test_obligation_alerts_are_per_item_and_idempotent(world):  # noqa: F811
    w = world
    contract = make_active_contract(w, days_to_end=300, notice=10)
    soon = w["today"] + datetime.timedelta(days=4)
    o1 = add_contract_obligation_service(
        contract=contract, title="Audit", responsible_party="VENDOR", due_date=soon, user=w["legal"]
    )
    o2 = add_contract_obligation_service(
        contract=contract, title="Audit", responsible_party="BUYER", due_date=soon, user=w["legal"]
    )
    assert scan() == 2
    assert scan() == 0
    assert ContractAlert.objects.filter(obligation=o1).count() == 1
    assert ContractAlert.objects.filter(obligation=o2).count() == 1


@pytest.mark.django_db(transaction=True)
def test_alert_backfill_migration_prevents_refiring_legacy_alerts():
    """Runs the real 0004 migration over pre-existing alert rows."""
    from django.db import connection
    from django.db.migrations.executor import MigrationExecutor

    before = [("contracts", "0003_alter_contractalert_alert_type")]
    after = [("contracts", "0004_contract_alert_cycles")]

    executor = MigrationExecutor(connection)
    others = [n for n in executor.loader.graph.leaf_nodes() if n[0] != "contracts"]
    executor.migrate(before)
    # every other app stays at its latest migration; only contracts is rolled back
    old_apps = executor.loader.project_state(before + others).apps
    vendor_model = old_apps.get_model("vendors", "Vendor")
    contract_model = old_apps.get_model("contracts", "Contract")
    milestone_model = old_apps.get_model("contracts", "ContractMilestone")
    alert_model = old_apps.get_model("contracts", "ContractAlert")
    user_model = old_apps.get_model("accounts", "User")
    category_model = old_apps.get_model("vendors", "VendorCategory")

    owner = user_model.objects.create(email="owner@m.test", username="owner@m.test")
    category = category_model.objects.create(name="C", code="C-M")
    vendor = vendor_model.objects.create(
        legal_name="V",
        vendor_number="V-M",
        tax_identification_number="T-M",
        category=category,
        email="v@m.test",
        address="a",
    )
    contract = contract_model.objects.create(
        contract_number="CTR-M",
        title="T",
        vendor=vendor,
        contract_owner=owner,
        contract_value=1,
        start_date="2026-01-01",
        end_date="2027-03-05",
        status="RENEWED",
    )
    milestone = milestone_model.objects.create(
        contract=contract, title="Quarterly Audit", due_date="2026-11-02"
    )
    renewal = alert_model.objects.create(
        contract=contract,
        alert_type="RENEWAL",
        message="Contract 'CTR-M' is reaching renewal notice period (End date: 2026-02-10).",
    )
    ms_alert = alert_model.objects.create(
        contract=contract,
        alert_type="MILESTONE",
        message="Milestone 'Quarterly Audit' for contract 'CTR-M' is due on 2026-11-02.",
    )

    executor = MigrationExecutor(connection)
    executor.migrate(after)
    new_apps = executor.loader.project_state(after + others).apps
    alert_new = new_apps.get_model("contracts", "ContractAlert")

    migrated_renewal = alert_new.objects.get(pk=renewal.pk)
    # the OLD cycle's end date, not the contract's current end date (2027-03-05)
    assert str(migrated_renewal.target_date) == "2026-02-10"
    migrated_ms = alert_new.objects.get(pk=ms_alert.pk)
    assert migrated_ms.milestone_id == milestone.pk
    assert str(migrated_ms.target_date) == "2026-11-02"

    executor = MigrationExecutor(connection)
    executor.migrate(executor.loader.graph.leaf_nodes())  # leave the DB fully migrated

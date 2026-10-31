"""Supplier scorecard: six weighted dimensions, per-org weights, Super Admin config screen."""

from decimal import Decimal

import pytest
from django.core.exceptions import ValidationError

from apps.accounts.models import Role, User
from apps.audit.models import AuditLog
from apps.organization.models import Organization
from apps.scorecards.models import (
    DEFAULT_WEIGHTS,
    ScorecardWeight,
    VendorScorecard,
    compute_composite,
    get_weights,
    rating_band_for,
)
from apps.scorecards.services import (
    calculate_vendor_scorecard_service,
    recalculate_scorecards_for_organization,
    resolve_period,
)
from apps.scorecards.tasks import recalculate_supplier_scorecards_task
from apps.vendors.models import Vendor

pytestmark = [pytest.mark.django_db]

D = Decimal
URL = "/superadmin/scorecard-weights/"
GOOD = {
    "delivery_weight": "30",
    "quality_weight": "30",
    "price_weight": "20",
    "responsiveness_weight": "5",
    "compliance_weight": "10",
    "sla_weight": "5",
}


def _scores(**kw):
    base = {
        "delivery": None,
        "quality": None,
        "price": None,
        "responsiveness": None,
        "compliance": None,
        "sla": None,
    }
    base.update({k: D(str(v)) for k, v in kw.items()})
    return base


@pytest.fixture
def second_org(db):
    return Organization.objects.create(name="Other Org", code="OTH-T")


@pytest.fixture
def admin(procurement_world, db_roles):
    return User.objects.create_user(
        email="sa@test.local",
        password="Password123!",
        role=db_roles[Role.SUPER_ADMIN],
        organization=procurement_world["org"],
    )


class TestWeightsModel:
    def test_defaults_when_no_row(self, procurement_world):
        w = get_weights(procurement_world["org"])
        assert w == DEFAULT_WEIGHTS
        assert sum(w.values()) == 100
        assert w["delivery"] == 25 and w["responsiveness"] == 10 and w["sla"] == 10

    def test_sum_must_be_100(self, procurement_world):
        with pytest.raises(ValidationError):
            ScorecardWeight.objects.create(
                organization=procurement_world["org"],
                delivery_weight=40,
                quality_weight=40,
                price_weight=20,
                responsiveness_weight=10,
                compliance_weight=10,
                sla_weight=10,
            )
        assert not ScorecardWeight.objects.exists()

    def test_valid_custom_weights_used(self, procurement_world):
        ScorecardWeight.objects.create(
            organization=procurement_world["org"],
            **{k: D(v) for k, v in GOOD.items()},
        )
        assert get_weights(procurement_world["org"])["delivery"] == 30


class TestCompositeMaths:
    def test_all_six_weighted(self):
        s = _scores(delivery=100, quality=80, price=60, responsiveness=50, compliance=100, sla=0)
        composite, eff = compute_composite(s, DEFAULT_WEIGHTS)
        # 25*100 + 25*80 + 20*60 + 10*50 + 10*100 + 10*0 = 2500+2000+1200+500+1000 = 7200 / 100
        assert composite == D("72.00")
        assert sum(eff.values()) == 100

    def test_missing_dimension_renormalizes_not_zero(self):
        s = _scores(delivery=80, quality=80, price=80, responsiveness=80, compliance=80)
        composite, eff = compute_composite(s, DEFAULT_WEIGHTS)
        assert composite == D("80.00")  # an SLA of "0" would have dragged this to 72
        assert "sla" not in eff
        assert eff["delivery"] == D("27.78")

    def test_no_data_gives_none(self):
        assert compute_composite(_scores(), DEFAULT_WEIGHTS) == (None, {})

    def test_zero_weight_dimension_ignored(self):
        w = dict(DEFAULT_WEIGHTS, sla=D("0"), delivery=D("35"))
        composite, eff = compute_composite(_scores(delivery=100, sla=0), w)
        assert composite == D("100.00") and "sla" not in eff

    @pytest.mark.parametrize(
        "score,band",
        [
            (100, "A"),
            (90, "A"),
            (89.99, "B"),
            (75, "B"),
            (74, "C"),
            (60, "C"),
            (59.9, "D"),
            (0, "D"),
        ],
    )
    def test_rating_band(self, score, band):
        assert rating_band_for(D(str(score))) == band
        assert rating_band_for(None) == ""

    def test_save_stores_composite_band_and_weights(self, procurement_world):
        w = procurement_world
        card = VendorScorecard.objects.create(
            vendor=w["vendor_a"],
            evaluation_period="Q1-2026",
            evaluated_by=w["exec"],
            delivery_score=D("100"),
            quality_score=D("100"),
            sla_score=D("40"),
        )
        # (25*100 + 25*100 + 10*40) / 60
        assert card.composite_score == D("90.00")
        assert card.rating_band == "A"
        assert D(card.weights_used["delivery"]) == 25
        labels = [r[0] for r in card.dimension_rows]
        assert labels == ["Delivery", "Quality", "Price", "Responsiveness", "Compliance", "SLA"]


class TestServiceAndOrgIsolation:
    def test_service_uses_org_weights_and_period(self, procurement_world):
        w = procurement_world
        ScorecardWeight.objects.create(organization=w["org"], **{k: D(v) for k, v in GOOD.items()})
        Vendor.objects.filter(pk=w["vendor_a"].pk).update(organization=w["org"])
        w["vendor_a"].refresh_from_db()
        card = calculate_vendor_scorecard_service(
            vendor=w["vendor_a"], evaluation_period="Q2-2026", evaluated_by_user=w["exec"]
        )
        assert D(card.weights_used["delivery"]) == 30
        assert card.period_start.isoformat() == "2026-04-01"
        assert card.period_end.isoformat() == "2026-06-30"
        assert card.compliance_score == D("100.00")
        assert card.composite_score == D("100.00") and card.rating_band == "A"
        log = AuditLog.objects.filter(target_model="VendorScorecard").latest("timestamp")
        assert log.new_state["rating_band"] == "A"

    def test_other_org_weights_do_not_leak(self, procurement_world, second_org):
        w = procurement_world
        ScorecardWeight.objects.create(
            organization=second_org, **{k: D(v) for k, v in GOOD.items()}
        )
        assert get_weights(w["org"]) == DEFAULT_WEIGHTS
        assert get_weights(second_org)["delivery"] == 30

    def test_compliance_penalties_bounded(self, procurement_world):
        w = procurement_world
        v = w["vendor_a"]
        Vendor.objects.filter(pk=v.pk).update(is_blacklisted=True)
        v.refresh_from_db()
        card = calculate_vendor_scorecard_service(
            vendor=v, evaluation_period="adhoc", evaluated_by_user=w["exec"]
        )
        assert card.compliance_score == D("0.00")
        assert card.period_start is None  # free-text label -> all history

    def test_resolve_period_formats(self):
        assert resolve_period("2026-Q1")[1].isoformat() == "2026-03-31"
        assert resolve_period("2026-02")[1].isoformat() == "2026-02-28"
        assert resolve_period("2026")[0].isoformat() == "2026-01-01"
        assert resolve_period("Q4") == (None, None)

    def test_recalculate_task_org_scoped_and_idempotent(self, procurement_world, second_org):
        w = procurement_world
        Vendor.objects.filter(pk=w["vendor_a"].pk).update(organization=w["org"])
        Vendor.objects.filter(pk=w["vendor_b"].pk).update(organization=second_org)
        n = recalculate_scorecards_for_organization(w["org"], period="Q3-2026", actor=w["exec"])
        assert n == 1
        assert VendorScorecard.objects.filter(vendor=w["vendor_a"]).count() == 1
        assert not VendorScorecard.objects.filter(vendor=w["vendor_b"]).exists()
        assert (
            recalculate_scorecards_for_organization(w["org"], period="Q3-2026", actor=w["exec"])
            == 0
        )

    def test_celery_task_runs_and_records_job(self, procurement_world):
        from apps.core.models import JobRun

        Vendor.objects.filter(pk=procurement_world["vendor_a"].pk).update(
            organization=procurement_world["org"]
        )
        msg = recalculate_supplier_scorecards_task(period="Q1-2026")
        assert "created" in msg
        assert JobRun.objects.filter(
            task_name="apps.scorecards.tasks.recalculate_supplier_scorecards_task"
        ).exists()


class TestConfigScreen:
    def test_super_admin_can_view_and_save(self, client, admin, procurement_world):
        client.force_login(admin)
        assert client.get(URL).status_code == 200
        r = client.post(URL, GOOD)
        assert r.status_code == 302
        assert get_weights(procurement_world["org"])["delivery"] == 30
        assert AuditLog.objects.filter(target_model="ScorecardWeight").exists()
        sidebar = client.get(URL).content.decode()
        assert "Supplier Scorecard Weights" in sidebar

    def test_sum_not_100_rejected(self, client, admin, procurement_world):
        client.force_login(admin)
        bad = dict(GOOD, delivery_weight="50")
        r = client.post(URL, bad)
        assert r.status_code == 200
        assert "total 100" in r.content.decode()
        assert not ScorecardWeight.objects.exists()

    def test_non_numeric_rejected(self, client, admin):
        client.force_login(admin)
        r = client.post(URL, dict(GOOD, price_weight="abc"))
        assert r.status_code == 200 and not ScorecardWeight.objects.exists()

    @pytest.mark.parametrize("who", ["exec", "mgr", "finance", "auditor", "requester"])
    def test_other_roles_forbidden(self, client, procurement_world, who):
        client.force_login(procurement_world[who])
        assert client.get(URL).status_code == 403
        assert client.post(URL, GOOD).status_code == 403
        assert not ScorecardWeight.objects.exists()

    def test_admin_edits_only_own_org(self, client, admin, procurement_world, second_org):
        other = ScorecardWeight.objects.create(
            organization=second_org, **{k: D(v) for k, v in GOOD.items()}
        )
        client.force_login(admin)
        # a forged org parameter is ignored for an org-scoped admin
        r = client.post(
            URL, dict(GOOD, delivery_weight="35", quality_weight="25", org=second_org.pk)
        )
        assert r.status_code == 302
        other.refresh_from_db()
        assert other.delivery_weight == 30
        mine = ScorecardWeight.objects.get(organization=procurement_world["org"])
        assert mine.delivery_weight == 35

    def test_admin_of_other_org_sees_own_defaults(
        self, client, db_roles, procurement_world, second_org
    ):
        ScorecardWeight.objects.create(
            organization=procurement_world["org"], **{k: D(v) for k, v in GOOD.items()}
        )
        other_admin = User.objects.create_user(
            email="sa2@test.local",
            password="Password123!",
            role=db_roles[Role.SUPER_ADMIN],
            organization=second_org,
        )
        client.force_login(other_admin)
        body = client.get(URL).content.decode()
        assert "Using industry defaults" in body and "Other Org" in body


class TestViewsShowSixDimensions:
    def test_vendor_performance_page_lists_all_dimensions(self, client, procurement_world):
        w = procurement_world
        calculate_vendor_scorecard_service(
            vendor=w["vendor_a"], evaluation_period="Q1-2026", evaluated_by_user=w["exec"]
        )
        client.force_login(w["exec"])
        body = client.get(f"/vendors/{w['vendor_a'].id}/").content.decode()
        for label in ("Responsiveness", "SLA", "Compliance", "(25%)"):
            assert label in body

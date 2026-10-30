"""Sourcing award approval on the approval engine (group D)."""

import pytest
from django.core.exceptions import ValidationError

from apps.accounts.models import Role
from apps.approvals import engine
from apps.approvals.models import ApprovalPolicy, ApprovalRequest
from apps.sourcing import services as svc
from apps.sourcing.models import AwardDecision, SourcingEvent
from tests.test_pe_rfq_sourcing import open_event, submit, to_commercial_review

pytestmark = pytest.mark.django_db

SRC = ApprovalPolicy.MODULE_SOURCING


def ready_event(w, price_a="1000.00", price_b="900.00"):
    """An event in COMMERCIAL_REVIEW with two scored bids (A is dearer than B)."""
    event = open_event(w)
    bid_a = submit(w, event, "vendor_a", "user_a", price=price_a)
    bid_b = submit(w, event, "vendor_b", "user_b", price=price_b)
    to_commercial_review(w, event)
    for bid in (bid_a, bid_b):
        svc.record_commercial_evaluation_service(
            event=event, bid=bid, evaluator=w["exec"], score=70
        )
    return event, bid_a, bid_b


def revise(w, steps, **kw):
    seeded = ApprovalPolicy.objects.get(
        module=SRC, department__isnull=True, cost_center__isnull=True
    )
    return engine.revise_approval_policy(
        policy=seeded,
        actor=w["mgr"],
        steps=[{"approver_role": r} for r in steps],
        **kw,
    )


def role(code):
    return Role.objects.get(code=code)


def test_recommendation_opens_a_request_bound_to_the_award_policy(procurement_world):
    w = procurement_world
    event, bid_a, bid_b = ready_event(w)
    decision = svc.recommend_award_service(
        event=event, winning_bid=bid_b, recommended_by=w["exec"], award_reason="Best value"
    )
    req = ApprovalRequest.objects.get(module=SRC, target_object_id=decision.id)
    assert req.amount == bid_b.total_bid_amount and req.created_by == w["exec"]
    assert [s["role_code"] for s in req.steps] == ["PROC_MGR"] and not req.exception_reason
    assert engine.can_act(req, w["mgr"]) and not engine.can_act(req, w["exec"])

    svc.approve_award_service(event=event, approver=w["mgr"], comments="ok")
    event.refresh_from_db()
    decision.refresh_from_db()
    assert event.status == SourcingEvent.STATUS_AWARDED
    assert decision.status == AwardDecision.STATUS_APPROVED and decision.approved_by == w["mgr"]
    assert ApprovalRequest.objects.get(pk=req.pk).status == "APPROVED"


def test_recommender_cannot_approve_even_when_their_role_is_a_named_approver(procurement_world):
    w = procurement_world
    revise(w, [role(Role.PROC_EXEC)])  # execs may approve awards under this policy
    event, _, bid_b = ready_event(w)
    svc.recommend_award_service(
        event=event, winning_bid=bid_b, recommended_by=w["exec"], award_reason="Best value"
    )
    with pytest.raises(ValidationError, match="Self-approval"):
        svc.approve_award_service(event=event, approver=w["exec"], comments="me")
    svc.approve_award_service(event=event, approver=w["exec2"], comments="independent")
    event.refresh_from_db()
    assert event.status == SourcingEvent.STATUS_AWARDED


def test_multi_step_award_chain_keeps_the_event_in_approval_until_the_last_step(procurement_world):
    w = procurement_world
    revise(w, [role(Role.PROC_MGR), role(Role.FINANCE_AP)])
    event, _, bid_b = ready_event(w)
    decision = svc.recommend_award_service(
        event=event, winning_bid=bid_b, recommended_by=w["exec"], award_reason="Best value"
    )
    svc.approve_award_service(event=event, approver=w["mgr"], comments="step 1")
    event.refresh_from_db()
    decision.refresh_from_db()
    assert event.status == SourcingEvent.STATUS_AWARD_APPROVAL
    assert decision.status == AwardDecision.STATUS_PENDING

    with pytest.raises(ValidationError, match="four-eyes|not authorized"):
        svc.approve_award_service(event=event, approver=w["mgr"], comments="again")
    svc.approve_award_service(event=event, approver=w["finance"], comments="step 2")
    event.refresh_from_db()
    assert event.status == SourcingEvent.STATUS_AWARDED


def test_awarding_to_other_than_the_lowest_bidder_takes_the_exception_route(procurement_world):
    w = procurement_world
    engine.create_policy_with_steps(
        name="Award exception",
        module=SRC,
        is_exception_route=True,
        steps=[{"approver_role": role(Role.FINANCE_AP)}],
    )
    event, bid_a, bid_b = ready_event(w)  # A is more expensive than B
    decision = svc.recommend_award_service(
        event=event, winning_bid=bid_a, recommended_by=w["exec"], award_reason="Better support SLA"
    )
    req = ApprovalRequest.objects.get(module=SRC, target_object_id=decision.id)
    assert "rather than the lowest bid" in req.exception_reason
    assert [s["role_code"] for s in req.steps] == ["PROC_MGR", "FINANCE_AP"]


def test_awarding_to_the_lowest_bidder_is_not_an_exception(procurement_world):
    w = procurement_world
    engine.create_policy_with_steps(
        name="Award exception",
        module=SRC,
        is_exception_route=True,
        steps=[{"approver_role": role(Role.FINANCE_AP)}],
    )
    event, _, bid_b = ready_event(w)
    decision = svc.recommend_award_service(
        event=event, winning_bid=bid_b, recommended_by=w["exec"], award_reason="Lowest price"
    )
    req = ApprovalRequest.objects.get(module=SRC, target_object_id=decision.id)
    assert not req.exception_reason and len(req.steps) == 1


def test_no_award_policy_fails_closed_and_the_event_is_untouched(procurement_world):
    w = procurement_world
    ApprovalPolicy.objects.filter(module=SRC).update(is_active=False)
    event, _, bid_b = ready_event(w)
    with pytest.raises(ValidationError, match="No active approval policy found"):
        svc.recommend_award_service(
            event=event, winning_bid=bid_b, recommended_by=w["exec"], award_reason="Best value"
        )
    event.refresh_from_db()
    assert event.status == SourcingEvent.STATUS_COMMERCIAL_REVIEW
    assert not AwardDecision.objects.filter(event=event).exists()


def test_rejection_requires_an_approver_and_returns_the_event_to_review(procurement_world):
    w = procurement_world
    event, _, bid_b = ready_event(w)
    decision = svc.recommend_award_service(
        event=event, winning_bid=bid_b, recommended_by=w["exec"], award_reason="Best value"
    )
    with pytest.raises(ValidationError, match="not an approver"):
        svc.reject_award_service(event=event, approver=w["requester"], comments="no")
    svc.reject_award_service(event=event, approver=w["mgr"], comments="Re-evaluate bids")
    event.refresh_from_db()
    decision.refresh_from_db()
    assert event.status == SourcingEvent.STATUS_COMMERCIAL_REVIEW
    assert decision.status == AwardDecision.STATUS_REJECTED
    assert (
        ApprovalRequest.objects.get(module=SRC, target_object_id=decision.id).status == "REJECTED"
    )


def test_cancelling_the_event_withdraws_the_pending_award_request(procurement_world):
    w = procurement_world
    event, _, bid_b = ready_event(w)
    decision = svc.recommend_award_service(
        event=event, winning_bid=bid_b, recommended_by=w["exec"], award_reason="Best value"
    )
    svc.cancel_sourcing_event_service(event=event, user=w["exec"], reason="Requirement withdrawn")
    assert (
        ApprovalRequest.objects.get(module=SRC, target_object_id=decision.id).status == "CANCELLED"
    )


def test_direct_award_path_goes_through_the_same_chain(procurement_world):
    w = procurement_world
    revise(w, [role(Role.PROC_MGR), role(Role.FINANCE_AP)])
    event, _, bid_b = ready_event(w)
    decision = svc.evaluate_and_award_sourcing_event_service(
        event=event, winning_bid=bid_b, award_reason="Direct award", approved_by_user=w["mgr"]
    )
    event.refresh_from_db()
    assert decision.status == AwardDecision.STATUS_PENDING  # first of two steps only
    assert event.status == SourcingEvent.STATUS_AWARD_APPROVAL
    svc.approve_award_service(event=event, approver=w["finance"], comments="second")
    event.refresh_from_db()
    assert event.status == SourcingEvent.STATUS_AWARDED


def test_award_approval_entry_gate_admits_roles_named_in_the_policy(procurement_world):
    from apps.sourcing.permissions import can_approve_award

    w = procurement_world
    assert can_approve_award(w["mgr"]) and not can_approve_award(w["finance"])
    revise(w, [role(Role.PROC_MGR), role(Role.FINANCE_AP)])
    assert can_approve_award(w["finance"])
    assert not can_approve_award(w["requester"])

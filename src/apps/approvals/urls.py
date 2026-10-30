from django.urls import path

from .views import (
    approval_request_decide_view,
    approvals_inbox_view,
    delegation_revoke_view,
    delegation_view,
)

urlpatterns = [
    path("approvals/inbox/", approvals_inbox_view, name="approvals_inbox"),
    path("approvals/", approvals_inbox_view, name="approvals_inbox_root"),
    path("inbox/", approvals_inbox_view, name="approvals_inbox_alias"),
    path("approvals/delegation/", delegation_view, name="approvals_delegation"),
    path(
        "approvals/delegation/<uuid:delegation_id>/revoke/",
        delegation_revoke_view,
        name="approvals_delegation_revoke",
    ),
    path(
        "approvals/requests/<uuid:request_id>/decide/",
        approval_request_decide_view,
        name="approval_request_decide",
    ),
]

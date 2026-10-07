from django.urls import path

from .views import approvals_inbox_view, my_approvals_view

urlpatterns = [
    path("approvals/inbox/", approvals_inbox_view, name="approvals_inbox"),
    path("inbox/", approvals_inbox_view, name="approvals_inbox_alias"),
    path("approvals/my/", my_approvals_view, name="my_approvals"),
]

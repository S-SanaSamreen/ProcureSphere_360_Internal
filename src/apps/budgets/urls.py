from django.urls import path

from . import views

urlpatterns = [
    path("", views.list_view, name="budgets_list"),
    path("", views.list_view, name="budget_list"),
    path("ledger/", views.ledger_view, name="budgets_ledger"),
    path("ledger/", views.ledger_view, name="spend_ledger"),
    path("exceptions/", views.budget_exceptions_view, name="budget_exceptions"),
    path("exceptions/", views.budget_exceptions_view, name="budgets_exceptions"),
]

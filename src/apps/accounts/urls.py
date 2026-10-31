from django.urls import path

from .mfa_views import mfa_setup_view, mfa_verify_view
from .views import change_password_view, login_page_view, logout_page_view

app_name = "accounts"

urlpatterns = [
    path("login/", login_page_view, name="login"),
    path("logout/", logout_page_view, name="logout"),
    path("accounts/mfa/verify/", mfa_verify_view, name="mfa_verify"),
    path("accounts/mfa/setup/", mfa_setup_view, name="mfa_setup"),
    path("accounts/change-password/", change_password_view, name="change_password"),
]

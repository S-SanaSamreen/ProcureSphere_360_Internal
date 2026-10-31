from django.http import JsonResponse
from django.shortcuts import redirect

from . import mfa

EXEMPT_PREFIXES = (
    "/static/",
    "/logout/",
    "/password-change/",
    "/accounts/change-password/",
    "/health",
    "/readyz",
    "/api/",
)


class ForcePasswordChangeMiddleware:
    """Sends users holding a temporary password to the change-password page until they set their own."""

    def __init__(self, get_response):
        self.get_response = get_response

    def __call__(self, request):
        user = getattr(request, "user", None)
        if (
            user is not None
            and user.is_authenticated
            and getattr(user, "must_change_password", False)
            and not request.path.startswith(EXEMPT_PREFIXES)
        ):
            return redirect("/accounts/change-password/")
        return self.get_response(request)


MFA_EXEMPT_PREFIXES = (
    "/static/",
    "/logout/",
    "/admin/logout/",
    "/accounts/mfa/",
    "/health",
    "/readyz",
    "/api/v1/auth/",
)


class MFAEnforcementMiddleware:
    """Holds a session at the second-factor step until the TOTP/recovery code is verified, and
    sends Super Admins/the platform superuser to enrolment when MFA is required but not set up."""

    def __init__(self, get_response):
        self.get_response = get_response

    def __call__(self, request):
        user = getattr(request, "user", None)
        if (
            user is not None
            and user.is_authenticated
            and not request.path.startswith(MFA_EXEMPT_PREFIXES)
        ):
            target = None
            if mfa.needs_verification(request):
                target = "/accounts/mfa/verify/"
            elif mfa.needs_enrolment(user):
                target = "/accounts/mfa/setup/"
            if target:
                if request.path.startswith("/api/"):
                    return JsonResponse(
                        {"error": {"code": "MFA_REQUIRED", "message": "Second factor required."}},
                        status=403,
                    )
                return redirect(target)
        return self.get_response(request)


# Provide both alias names for compatibility across branches and settings
PasswordChangeRequiredMiddleware = ForcePasswordChangeMiddleware

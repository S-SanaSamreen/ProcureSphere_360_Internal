from django.contrib import messages
from django.contrib.auth.decorators import login_required
from django.shortcuts import redirect, render
from django.utils.http import url_has_allowed_host_and_scheme
from django.views.decorators.cache import never_cache

from . import mfa


def _home(request):
    nxt = request.GET.get("next") or request.POST.get("next")
    if nxt and url_has_allowed_host_and_scheme(nxt, allowed_hosts={request.get_host()}):
        return redirect(nxt)
    return redirect("/")


@never_cache
@login_required
def mfa_verify_view(request):
    user = request.user
    if not user.mfa_enabled or request.session.get(mfa.SESSION_KEY):
        return _home(request)
    error = None
    if request.method == "POST":
        if mfa.is_locked(user):
            error = "Too many failed attempts. Try again in a few minutes."
            mfa.audit(request, user, "mfa_locked_out")
        elif mfa.verify_second_factor(user, request.POST.get("code", "")):
            request.session[mfa.SESSION_KEY] = True
            mfa.audit(request, user, "mfa_verified")
            return _home(request)
        else:
            error = "Invalid verification code."
            mfa.audit(request, user, "mfa_failed")
    return render(request, "accounts/mfa_verify.html", {"error": error})


@never_cache
@login_required
def mfa_setup_view(request):
    user = request.user
    if not mfa.mfa_applies_to(user):
        return redirect("/")
    if user.mfa_enabled:
        messages.info(request, "Multi-factor authentication is already enabled.")
        return redirect("/")
    secret = request.session.get("mfa_pending_secret")
    if not secret:
        secret = mfa.new_secret()
        request.session["mfa_pending_secret"] = secret
    error = None
    if request.method == "POST":
        if mfa.is_locked(user):
            error = "Too many failed attempts. Try again in a few minutes."
        else:
            step = mfa.check_totp(secret, request.POST.get("code", ""))
            if step is None:
                mfa.register_failure(user)
                error = "Invalid verification code."
            else:
                plain, hashes = mfa.generate_recovery_codes()
                user.mfa_secret = secret
                user.mfa_enabled = True
                user.mfa_recovery_hashes = hashes
                user.mfa_last_step = step
                user.save(
                    update_fields=[
                        "mfa_secret",
                        "mfa_enabled",
                        "mfa_recovery_hashes",
                        "mfa_last_step",
                    ]
                )
                request.session.pop("mfa_pending_secret", None)
                request.session[mfa.SESSION_KEY] = True
                mfa.audit(request, user, "mfa_enabled")
                return render(request, "accounts/mfa_recovery_codes.html", {"codes": plain})
    return render(
        request,
        "accounts/mfa_setup.html",
        {"secret": secret, "otpauth_uri": mfa.provisioning_uri(user, secret), "error": error},
    )

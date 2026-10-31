"""TOTP second factor for Super Admin accounts and the platform superuser."""

import secrets
import time

import pyotp
from django.conf import settings
from django.contrib.auth.hashers import check_password, make_password
from django.core.cache import cache

SESSION_KEY = "mfa_verified"
MAX_FAILURES = 5
LOCKOUT_SECONDS = 300
RECOVERY_CODE_COUNT = 8


def mfa_applies_to(user) -> bool:
    """MFA may be required only for the platform superuser and Super Admins."""
    if not getattr(user, "is_authenticated", False):
        return False
    return bool(user.is_superuser or getattr(user, "role_code", None) == "SUPER_ADMIN")


def mfa_required(user) -> bool:
    return mfa_applies_to(user) and getattr(settings, "MFA_REQUIRED_FOR_SUPER_ADMIN", False)


def needs_verification(request) -> bool:
    user = request.user
    return bool(user.is_authenticated and user.mfa_enabled and not request.session.get(SESSION_KEY))


def needs_enrolment(user) -> bool:
    return mfa_required(user) and not user.mfa_enabled


def new_secret() -> str:
    return pyotp.random_base32()


def provisioning_uri(user, secret: str) -> str:
    return pyotp.TOTP(secret).provisioning_uri(name=user.email, issuer_name="ProcureSphere 360")


def _lock_key(user) -> str:
    return f"mfa-fail:{user.pk}"


def is_locked(user) -> bool:
    return cache.get(_lock_key(user), 0) >= MAX_FAILURES


def register_failure(user) -> None:
    key = _lock_key(user)
    cache.add(key, 0, LOCKOUT_SECONDS)
    try:
        cache.incr(key)
    except ValueError:
        cache.set(key, 1, LOCKOUT_SECONDS)


def check_totp(secret: str, code: str, last_step: int = 0):
    """Return the matched time step (> last_step, so a code cannot be replayed) or None."""
    code = (code or "").strip().replace(" ", "")
    if not (code.isdigit() and len(code) == 6) or not secret:
        return None
    totp = pyotp.TOTP(secret)
    now = int(time.time())
    for offset in (-1, 0, 1):
        step = now // totp.interval + offset
        if step > last_step and secrets.compare_digest(totp.at(step * totp.interval), code):
            return step
    return None


def generate_recovery_codes():
    """Return (plain codes to show once, hashes to store)."""
    plain = [
        f"{secrets.token_hex(2)}-{secrets.token_hex(2)}-{secrets.token_hex(2)}"
        for _ in range(RECOVERY_CODE_COUNT)
    ]
    return plain, [make_password(c) for c in plain]


def verify_second_factor(user, code: str) -> bool:
    """Check a TOTP or recovery code. Locks out after repeated failures (cache based)."""
    if is_locked(user):
        return False
    step = check_totp(user.mfa_secret, code, user.mfa_last_step)
    if step is not None:
        user.mfa_last_step = step
        user.save(update_fields=["mfa_last_step"])
        cache.delete(_lock_key(user))
        return True
    normalized = (code or "").strip().lower()
    for stored in list(user.mfa_recovery_hashes or []):
        if normalized and check_password(normalized, stored):
            user.mfa_recovery_hashes = [h for h in user.mfa_recovery_hashes if h != stored]
            user.save(update_fields=["mfa_recovery_hashes"])
            cache.delete(_lock_key(user))
            return True
    register_failure(user)
    return False


def audit(request, user, event: str) -> None:
    try:
        from apps.audit.models import AuditLog
        from apps.audit.services import create_audit_log_service

        create_audit_log_service(
            actor=user,
            action=AuditLog.ACTION_UPDATE,
            target_model="User",
            target_object_id=str(user.id),
            new_state={"event": event},
            ip_address=request.META.get("REMOTE_ADDR"),
            user_agent=request.META.get("HTTP_USER_AGENT", ""),
        )
    except Exception:  # auditing must never break authentication
        pass

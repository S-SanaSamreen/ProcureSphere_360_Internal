import threading
import uuid

# Thread-local storage for request correlation context
_request_context = threading.local()


def get_current_request_id():
    return getattr(_request_context, "request_id", None)


def get_current_user():
    return getattr(_request_context, "user", None)


def get_client_ip():
    return getattr(_request_context, "client_ip", None)


class RequestIDMiddleware:
    """
    Middleware that assigns a unique UUID correlation ID (X-Request-ID) to every HTTP request.
    """

    def __init__(self, get_response):
        self.get_response = get_response

    def __call__(self, request):
        request_id = request.headers.get("X-Request-ID", str(uuid.uuid4()))
        request.request_id = request_id
        _request_context.request_id = request_id

        # Determine client IP address
        x_forwarded_for = request.META.get("HTTP_X_FORWARDED_FOR")
        if x_forwarded_for:
            ip = x_forwarded_for.split(",")[0].strip()
        else:
            ip = request.META.get("REMOTE_ADDR")
        _request_context.client_ip = ip

        try:
            response = self.get_response(request)
        finally:
            # Never leak request context into later work on the same thread.
            _request_context.request_id = None
            _request_context.client_ip = None
        response["X-Request-ID"] = request_id
        return response


class AuditContextMiddleware:
    """
    Middleware that sets the current user in thread-local storage for audit tracking.
    """

    def __init__(self, get_response):
        self.get_response = get_response

    def __call__(self, request):
        if hasattr(request, "user") and request.user.is_authenticated:
            _request_context.user = request.user
        else:
            _request_context.user = None

        try:
            return self.get_response(request)
        finally:
            _request_context.user = None


class SecurityHeadersMiddleware:
    """
    Middleware that enforces baseline security headers per PRD and AGENTS.md §12:
    Content-Security-Policy, Strict-Transport-Security, Permissions-Policy.
    """

    def __init__(self, get_response):
        self.get_response = get_response

    def __call__(self, request):
        response = self.get_response(request)
        if "Content-Security-Policy" not in response:
            response["Content-Security-Policy"] = (
                "default-src 'self'; "
                "script-src 'self' 'unsafe-inline' 'unsafe-eval' https://cdn.jsdelivr.net https://unpkg.com https://cdnjs.cloudflare.com; "
                "style-src 'self' 'unsafe-inline' https://cdn.jsdelivr.net https://cdnjs.cloudflare.com https://fonts.googleapis.com; "
                "font-src 'self' https://fonts.gstatic.com https://cdnjs.cloudflare.com https://cdn.jsdelivr.net data:; "
                "img-src 'self' data: https:; "
                "media-src 'self'; "
                "connect-src 'self' https://cdn.jsdelivr.net https://unpkg.com;"
            )
        if request.is_secure() and "Strict-Transport-Security" not in response:
            response["Strict-Transport-Security"] = "max-age=31536000; includeSubDomains"
        if "Permissions-Policy" not in response:
            response["Permissions-Policy"] = "geolocation=(), microphone=(), camera=()"
        return response


class FailClosedAccessMiddleware:
    """
    Fail-closed RBAC gate. Authenticated non-superusers with no role receive 403 everywhere
    except session housekeeping paths. (Organisation-less internal users are handled by the
    data selectors/viewsets, which return nothing.)
    """

    EXEMPT_PREFIXES = (
        "/login",
        "/logout",
        "/admin/logout",
        "/password-change",
        "/accounts/change-password",
        "/api/v1/auth/",
        "/health",
        "/readyz",
        "/static",
        "/favicon",
    )

    def __init__(self, get_response):
        self.get_response = get_response

    def __call__(self, request):
        user = getattr(request, "user", None)
        if (
            user
            and user.is_authenticated
            and not user.is_superuser
            and request.path != "/"
            and not request.path.startswith(self.EXEMPT_PREFIXES)
        ):
            if not getattr(user, "role_code", None):
                from apps.core.views import custom_403_view

                return custom_403_view(request)
        return self.get_response(request)


class VendorPortalIsolationMiddleware:
    """
    Strict boundary enforcement: blocks external Vendor Portal users from accessing
    internal staff portals, requisitions, contracts, orders, invoices, and reports (DEF-003).
    """

    BLOCKED_INTERNAL_PREFIXES = (
        "/requisitions",
        "/contracts",
        "/orders",
        "/purchase-orders",
        "/invoices",
        "/receipts",
        "/budgets",
        "/reports",
        "/approvals",
        "/superadmin",
        "/manager",
        "/audit",
        "/admin",
        "/api/v1/requisitions",
        "/api/v1/contracts",
        "/api/v1/budgets",
        "/api/v1/receipts",
        "/api/v1/invoices",
        "/api/v1/reports",
        "/api/v1/audit",
        "/api/v1/manager",
    )

    def __init__(self, get_response):
        self.get_response = get_response

    def __call__(self, request):
        user = getattr(request, "user", None)
        if user and user.is_authenticated and not user.is_superuser:
            from apps.accounts.models import Role

            role_code = getattr(user, "role_code", None) or (
                user.role.code if hasattr(user, "role") and user.role else None
            )
            is_vendor = getattr(user, "is_vendor", False) or (role_code == Role.VENDOR_USER)

            if is_vendor:
                path = request.path.rstrip("/")
                for prefix in self.BLOCKED_INTERNAL_PREFIXES:
                    if path == prefix or path.startswith(prefix + "/"):
                        if request.path.startswith("/api/"):
                            from django.http import JsonResponse

                            return JsonResponse(
                                {
                                    "error": {
                                        "code": "FORBIDDEN",
                                        "message": "External vendors are strictly prohibited from accessing internal buyer endpoints.",
                                    }
                                },
                                status=403,
                            )
                        from django.core.exceptions import PermissionDenied

                        raise PermissionDenied(
                            "Vendors are restricted to the Vendor Portal (/vendor/)."
                        )

        return self.get_response(request)


def _client_ip(request):
    from django.conf import settings

    if getattr(settings, "TRUSTED_PROXY", False):
        forwarded = request.META.get("HTTP_X_FORWARDED_FOR", "")
        if forwarded:
            return forwarded.split(",")[0].strip()
    return request.META.get("REMOTE_ADDR", "")


class AdminIPAllowListMiddleware:
    """Restrict /admin/ to ADMIN_ALLOWED_IPS (IPs or CIDRs). Empty list allows everyone."""

    def __init__(self, get_response):
        self.get_response = get_response

    def __call__(self, request):
        from django.conf import settings
        from django.http import HttpResponseForbidden

        allowed = getattr(settings, "ADMIN_ALLOWED_IPS", None) or []
        if allowed and request.path.startswith("/admin/"):
            import ipaddress

            try:
                ip = ipaddress.ip_address(_client_ip(request))
                ok = any(
                    ip in ipaddress.ip_network(str(entry).strip(), strict=False)
                    for entry in allowed
                    if str(entry).strip()
                )
            except ValueError:
                ok = False
            if not ok:
                return HttpResponseForbidden("Admin access is not permitted from this address.")
        return self.get_response(request)


MAINTENANCE_EXEMPT_PREFIXES = ("/static/", "/health", "/readyz", "/login/", "/logout/", "/admin/")


class MaintenanceModeMiddleware:
    """When maintenance mode is on: non-superusers get a 503 page, superusers see a banner."""

    def __init__(self, get_response):
        self.get_response = get_response

    def __call__(self, request):
        from django.http import HttpResponse, JsonResponse

        from apps.core.models import MaintenanceState

        try:
            enabled, message = MaintenanceState.current()
        except Exception:  # table missing (e.g. mid-migration) must never take the site down
            return self.get_response(request)
        if not enabled:
            return self.get_response(request)
        user = getattr(request, "user", None)
        is_platform = bool(user is not None and user.is_authenticated and user.is_superuser)
        if not is_platform and not request.path.startswith(MAINTENANCE_EXEMPT_PREFIXES):
            if request.path.startswith("/api/"):
                response = JsonResponse(
                    {"error": {"code": "MAINTENANCE", "message": message}}, status=503
                )
            else:
                from django.utils.html import escape

                response = HttpResponse(f"<h1>Maintenance</h1><p>{escape(message)}</p>", status=503)
            response["Retry-After"] = "300"
            return response
        response = self.get_response(request)
        if (
            is_platform
            and "text/html" in response.get("Content-Type", "")
            and hasattr(response, "content")
        ):
            from django.utils.html import escape

            banner = (
                '<div style="background:#b45309;color:#fff;padding:6px 12px;text-align:center;'
                'font:14px sans-serif">MAINTENANCE MODE is ON - only platform administrators '
                f"can use the system. {escape(message)}</div>"
            ).encode()
            body = response.content
            idx = body.find(b"<body")
            end = body.find(b">", idx) + 1 if idx != -1 else 0
            if idx != -1 and end > 0:
                response.content = body[:end] + banner + body[end:]
                if response.has_header("Content-Length"):
                    response["Content-Length"] = str(len(response.content))
        return response

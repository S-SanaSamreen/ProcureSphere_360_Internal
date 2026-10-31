from django.core.exceptions import ImproperlyConfigured

INSECURE_DEFAULT_KEY = "dev-secret-key-change-in-production-procuresphere"
MIN_SECRET_KEY_LENGTH = 50


def validate_secret_key(key):
    """Raise ImproperlyConfigured unless ``key`` looks like a real production secret."""
    if (
        not key
        or key == INSECURE_DEFAULT_KEY
        or key.startswith(("django-insecure", "dev-secret", "test-secret"))
        or len(key) < MIN_SECRET_KEY_LENGTH
    ):
        raise ImproperlyConfigured(
            "DJANGO_SECRET_KEY must be set to a unique random value of at least "
            f"{MIN_SECRET_KEY_LENGTH} characters in production/staging."
        )
    return key

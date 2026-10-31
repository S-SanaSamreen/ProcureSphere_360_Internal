import functools
import logging

from django.utils import timezone

logger = logging.getLogger(__name__)


def record_job_run(task_name=None):
    """Decorator persisting a JobRun for each execution (also on failure, which is re-raised)."""

    def decorator(func):
        name = task_name or f"{func.__module__}.{func.__name__}"

        @functools.wraps(func)
        def wrapper(*args, **kwargs):
            from apps.core.models import JobRun

            started = timezone.now()
            try:
                result = func(*args, **kwargs)
            except Exception as exc:
                _save(JobRun, name, started, JobRun.STATUS_FAILED, f"{type(exc).__name__}: {exc}")
                raise
            _save(JobRun, name, started, JobRun.STATUS_SUCCESS, repr(result)[:2000])
            return result

        return wrapper

    return decorator


def _save(model, name, started, status, detail):
    try:
        model.objects.create(
            task_name=name,
            started_at=started,
            finished_at=timezone.now(),
            status=status,
            detail=detail,
        )
    except Exception:  # never let bookkeeping break the task itself
        logger.exception("Could not persist JobRun for %s", name)

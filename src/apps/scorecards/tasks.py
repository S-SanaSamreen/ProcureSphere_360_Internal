from celery import shared_task

from apps.core.job_runs import record_job_run


@shared_task(name="apps.scorecards.tasks.recalculate_supplier_scorecards_task")
@record_job_run("apps.scorecards.tasks.recalculate_supplier_scorecards_task")
def recalculate_supplier_scorecards_task(period=None):
    """
    Periodic task: computes the current-period six-dimension scorecard for every active vendor
    of every organization, using that organization's configured weights (idempotent per period).
    """
    from apps.organization.models import Organization

    from .services import recalculate_scorecards_for_organization

    created = 0
    orgs = 0
    for org in Organization.objects.all():
        orgs += 1
        created += recalculate_scorecards_for_organization(org, period=period)
    return f"Supplier scorecards recalculated: {created} created across {orgs} organizations."

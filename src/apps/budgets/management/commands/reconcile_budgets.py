from django.core.management.base import BaseCommand, CommandError

from apps.budgets.models import Budget, SpendLedger
from apps.budgets.services import LEDGER_BUCKETS, reconcile_budget


class Command(BaseCommand):
    help = (
        "Compares every budget's stored reserved/committed/actual counters with the totals "
        "derived from its append-only spend ledger and reports any drift. With --fix, drift is "
        "corrected by APPENDING adjustment entries (the ledger is never edited)."
    )

    def add_arguments(self, parser):
        parser.add_argument(
            "--fix",
            action="store_true",
            help="Append RECON adjustment ledger entries so the ledger matches the stored counters.",
        )

    def handle(self, *args, **options):
        drifted = 0
        for budget in Budget.objects.select_related("cost_center", "fiscal_period"):
            result = reconcile_budget(budget)
            if result["in_sync"]:
                continue
            drifted += 1
            self.stdout.write(
                self.style.WARNING(
                    f"DRIFT {budget.cost_center.code} [{budget.fiscal_period.name}] ({budget.pk})"
                )
            )
            for field, row in result["buckets"].items():
                if row["drift"]:
                    self.stdout.write(
                        f"   {field}: stored={row['stored']} ledger={row['ledger']} drift={row['drift']}"
                    )
            if options["fix"]:
                entry_by_field = {v: k for k, v in LEDGER_BUCKETS.items()}
                for field, row in result["buckets"].items():
                    if row["drift"]:
                        SpendLedger.objects.create(
                            budget=budget,
                            entry_type=entry_by_field[field],
                            amount=row["drift"],
                            reference_number="RECONCILIATION",
                            description="Adjustment so the ledger matches stored counters",
                        )
        if drifted and not options["fix"]:
            raise CommandError(
                f"{drifted} budget(s) have ledger drift. Re-run with --fix to append adjustments."
            )
        self.stdout.write(self.style.SUCCESS(f"Budgets reconciled ({drifted} adjusted)."))

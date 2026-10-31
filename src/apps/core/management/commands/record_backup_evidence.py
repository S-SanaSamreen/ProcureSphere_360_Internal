from django.core.management.base import BaseCommand, CommandError

from apps.core.models import BackupRecord


class Command(BaseCommand):
    help = "Append a backup / restore-test evidence record (BackupRecord)."

    def add_arguments(self, parser):
        parser.add_argument("--kind", choices=["BACKUP", "RESTORE_TEST"], default="BACKUP")
        parser.add_argument("--status", choices=["SUCCESS", "FAILED"], default="SUCCESS")
        parser.add_argument("--reference", required=True, help="Backup location / job reference")
        parser.add_argument("--size-bytes", type=int, default=None)
        parser.add_argument("--checksum", default="")
        parser.add_argument("--performed-by", default="")
        parser.add_argument("--notes", default="")

    def handle(self, *args, **opts):
        if opts["size_bytes"] is not None and opts["size_bytes"] < 0:
            raise CommandError("--size-bytes must be non-negative")
        record = BackupRecord.objects.create(
            kind=opts["kind"],
            status=opts["status"],
            reference=opts["reference"],
            size_bytes=opts["size_bytes"],
            checksum=opts["checksum"],
            performed_by=opts["performed_by"],
            notes=opts["notes"],
        )
        self.stdout.write(self.style.SUCCESS(f"Recorded {record}"))

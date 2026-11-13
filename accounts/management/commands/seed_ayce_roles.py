"""Restore the fixed AYCE role definitions through the audited service."""

from __future__ import annotations

from django.contrib.auth.models import User
from django.core.management.base import BaseCommand, CommandError

from accounts import services
from accounts.authorization import ManagementError


class Command(BaseCommand):
    help = (
        "Repair the fixed AYCE groups and permissions. Requires the acting "
        "native superuser and the current administration revision."
    )

    def add_arguments(self, parser) -> None:
        parser.add_argument("--actor", required=True, help="Native superuser username")
        parser.add_argument(
            "--revision",
            required=True,
            type=int,
            help="Current administration revision",
        )

    def handle(self, *args, **options) -> None:
        username = str(options["actor"])
        actor = User.objects.filter(
            username=username, is_active=True, is_superuser=True
        ).first()
        if actor is None:
            raise CommandError("The actor must be an existing active native superuser.")
        revision = services.valid_state_value(options["revision"])
        if revision is None:
            raise CommandError("The revision must be a non-negative whole number.")
        try:
            result = services.seed_fixed_roles(actor_id=actor.pk, revision=revision)
        except ManagementError as exc:
            raise CommandError(exc.code) from None
        repaired = result["repaired_groups"]
        if repaired:
            self.stdout.write(
                self.style.SUCCESS(f"Repaired {repaired} fixed role definition(s).")
            )
        else:
            self.stdout.write("Fixed role definitions already match.")
        self.stdout.write(f"revision={result['revision']} epoch={result['epoch']}")

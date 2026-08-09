"""Register this service's Django Q2 schedules. Idempotent -- safe to re-run.

Every service gets the two backbone sweepers; the rest come from
settings.SERVICE_SCHEDULES so a service declares its own without editing this.

    python manage.py register_schedules
    python manage.py register_schedules --list
"""

from __future__ import annotations

from django.conf import settings
from django.core.management.base import BaseCommand

# (name, func, minutes) -- present in every service.
BACKBONE = [
    # Catches outbox rows whose on_commit task was lost, and rows waiting out a
    # backoff. Without this a crash at the wrong moment strands an event.
    ("sweep_outbox", "platform_common.events.tasks.sweep_outbox", 1),
    # Re-dispatches events accepted but never processed.
    ("sweep_inbox", "platform_common.events.tasks.sweep_inbox", 1),
    # Keeps the outbox/inbox tables bounded.
    ("prune_processed_events", "platform_common.events.tasks.prune_processed", 1440),
]


class Command(BaseCommand):
    help = "Register Django Q2 schedules for this service."

    def add_arguments(self, parser):
        parser.add_argument("--list", action="store_true", help="show, don't write")

    def handle(self, *args, **options):
        from django_q.models import Schedule

        service = settings.SERVICE_NAME
        wanted = BACKBONE + list(getattr(settings, "SERVICE_SCHEDULES", []))

        if options["list"]:
            for name, func, minutes in wanted:
                self.stdout.write(f"  {name:32s} every {minutes:>5} min  {func}")
            return

        created = updated = 0
        for name, func, minutes in wanted:
            _, was_created = Schedule.objects.update_or_create(
                name=f"{service}:{name}",
                defaults={
                    "func": func,
                    "schedule_type": Schedule.MINUTES,
                    "minutes": minutes,
                    "repeats": -1,
                },
            )
            created += was_created
            updated += not was_created

        # Anything we no longer declare is stale; leaving it would keep firing
        # a task nobody owns.
        stale = Schedule.objects.filter(name__startswith=f"{service}:").exclude(
            name__in=[f"{service}:{n}" for n, _, _ in wanted]
        )
        removed = stale.count()
        stale.delete()

        self.stdout.write(self.style.SUCCESS(
            f"{service}: {created} created, {updated} updated, {removed} stale removed"
        ))

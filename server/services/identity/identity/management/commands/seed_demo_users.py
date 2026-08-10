"""Create the four demo sign-ins the SPA's login screen advertises.

Idempotent: re-running resets each password and leaves the user ids alone, so a
customer's accounts and transaction history survive a re-seed.

    uv run python manage.py seed_demo_users

Staff roles cannot be created through the public /api/auth/register endpoint --
that would be a privilege-escalation hole -- so they are seeded here instead.
"""

from __future__ import annotations

from django.core.management.base import BaseCommand

from identity.models import Role, User

PASSWORD = "demo-password-2026"

DEMO_USERS = [
    ("asha@indbank.test", "Asha Menon", Role.CUSTOMER),
    ("ravi@indbank.test", "Ravi Kulkarni", Role.CUSTOMER),
    ("analyst@indbank.test", "Priya Raghavan", Role.FRAUD_ANALYST),
    ("ops@indbank.test", "Kabir Shah", Role.OPS),
    ("admin@indbank.test", "Meera Iyer", Role.ADMIN),
    # A second administrator is not a nicety. Every privileged change is staged
    # for a *different* admin to approve, so with one administrator the console
    # can raise requests and never apply any of them -- and the last-admin guard
    # would refuse to promote anyone to help. Two is the minimum workable estate.
    ("admin2@indbank.test", "Arjun Desai", Role.ADMIN),
]


class Command(BaseCommand):
    help = "Create or reset the four demo users used by the SPA."

    def add_arguments(self, parser):
        parser.add_argument(
            "--password", default=PASSWORD,
            help=f"Password to set for every demo user (default: {PASSWORD})",
        )

    def handle(self, *args, **options):
        password = options["password"]
        created_count = reset_count = 0

        for email, full_name, role in DEMO_USERS:
            user = User.objects.filter(email__iexact=email).first()
            if user is None:
                user = User(email=email, full_name=full_name, role=role,
                            status=User.Status.ACTIVE)
                user.set_password(password)
                user.save()
                created_count += 1
                self.stdout.write(f"  created {email:26s} {role}")
            else:
                # Clear any lockout as well: a demo account that someone
                # fat-fingered five times is otherwise unusable until it expires.
                user.set_password(password)
                user.role = role
                user.full_name = full_name
                user.status = User.Status.ACTIVE
                user.failed_logins = 0
                user.locked_until = None
                user.save()
                reset_count += 1
                self.stdout.write(f"  reset   {email:26s} {role}")

        self.stdout.write(
            self.style.SUCCESS(
                f"{created_count} created, {reset_count} reset. Password: {password}"
            )
        )

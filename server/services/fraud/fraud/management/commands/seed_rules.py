"""Seed the starter ruleset.

Every screening vector and every worked example the requirements name, expressed
as *configuration* rather than code -- which is the point of the DSL. An
administrator can retune any of these without a deploy.

    python manage.py seed_rules
    python manage.py seed_rules --activate    # promote all from SHADOW to ACTIVE

New rules default to SHADOW so they are measured against live traffic before
they can affect a customer.
"""

from __future__ import annotations

from decimal import Decimal

from django.core.management.base import BaseCommand
from django.db import transaction

from fraud.models import Rule, RuleCategory, RuleMode, RuleStat, Threshold
from fraud.rules import validate_condition

# (code, name, condition, weight, hard_block, reason, category)
STARTER_RULES = [
    (
        "R001_AMOUNT_THRESHOLD",
        "Single transfer above the per-rail threshold",
        {"fact": "amount", "op": "gt", "value": "200000"},
        30, False, "R001", RuleCategory.AMOUNT,
    ),
    (
        "R002_VELOCITY_5M",
        "More than 5 transfers in a 5-minute window",
        {"fact": "txn_count_5m", "op": "gt", "value": 5},
        40, False, "R002", RuleCategory.VELOCITY,
    ),
    (
        "R003_VALUE_VELOCITY",
        "Cumulative value in 5 minutes approaching the daily limit",
        {"fact": "txn_sum_5m", "op": "gt", "value": "400000"},
        35, False, "R003", RuleCategory.VELOCITY,
    ),
    (
        "R004_BENEFICIARY_FANOUT",
        "Same account paying many distinct beneficiaries in minutes",
        {"fact": "distinct_benef_5m", "op": "gte", "value": 4},
        50, False, "R004", RuleCategory.VELOCITY,
    ),
    (
        "R005_NEW_BENEF_LARGE",
        "New beneficiary paired with a large amount",
        {"all": [
            {"fact": "beneficiary_is_new", "op": "eq", "value": True},
            {"fact": "amount", "op": "gt", "value": "50000"},
        ]},
        45, False, "R005", RuleCategory.BENEFICIARY,
    ),
    (
        "R006_BLACKLISTED_BENEFICIARY",
        "Beneficiary is on the internal blacklist",
        {"fact": "beneficiary_blacklisted", "op": "eq", "value": True},
        100, True, "R006", RuleCategory.LIST,
    ),
    (
        "R007_HIGH_RISK_CORRIDOR",
        "International transfer to a high-risk country",
        {"all": [
            {"fact": "rail", "op": "eq", "value": "INTERNATIONAL"},
            {"fact": "destination_is_high_risk", "op": "eq", "value": True},
        ]},
        40, False, "R007", RuleCategory.GEO,
    ),
    (
        "R008_IMPOSSIBLE_TRAVEL",
        "Country changed since a transaction within the last hour",
        {"all": [
            {"fact": "country_changed", "op": "eq", "value": True},
            {"fact": "txn_count_1h", "op": "gt", "value": 0},
        ]},
        55, False, "R008", RuleCategory.GEO,
    ),
    (
        "R009_NEW_DEVICE_LARGE",
        "Unrecognised device moving a significant amount",
        {"all": [
            {"fact": "device_is_new", "op": "eq", "value": True},
            {"fact": "amount", "op": "gt", "value": "25000"},
        ]},
        35, False, "R009", RuleCategory.DEVICE,
    ),
    (
        "R010_BLOCKED_DEVICE",
        "Device is on the blocked list",
        {"fact": "device_blocked", "op": "eq", "value": True},
        100, True, "R010", RuleCategory.LIST,
    ),
    (
        "R011_AMOUNT_ANOMALY",
        "Amount far outside this account's own normal behaviour",
        {"fact": "amount_zscore", "op": "gt", "value": 4.0},
        40, False, "R011", RuleCategory.BEHAVIOUR,
    ),
    (
        "R012_ODD_HOURS_LARGE",
        "Large transfer in the small hours",
        {"all": [
            {"fact": "hour_of_day", "op": "between", "value": [1, 4]},
            {"fact": "amount", "op": "gt", "value": "100000"},
        ]},
        25, False, "R012", RuleCategory.BEHAVIOUR,
    ),
    (
        "R013_FIRST_TXN_LARGE",
        "First ever transaction on the account is large",
        {"all": [
            {"fact": "is_first_txn", "op": "eq", "value": True},
            {"fact": "amount", "op": "gt", "value": "100000"},
        ]},
        45, False, "R013", RuleCategory.BEHAVIOUR,
    ),
    (
        "R014_INTL_NEW_BENEF_HIGH",
        "International transfer to a brand-new beneficiary, large amount",
        {"all": [
            {"fact": "rail", "op": "eq", "value": "INTERNATIONAL"},
            {"fact": "beneficiary_is_new", "op": "eq", "value": True},
            {"fact": "amount", "op": "gt", "value": "100000"},
        ]},
        45, False, "R014", RuleCategory.BENEFICIARY,
    ),
    (
        "R015_COOLING_OFF",
        "Beneficiary still within its cooling-off period",
        {"all": [
            {"fact": "beneficiary_in_cooling_off", "op": "eq", "value": True},
            {"fact": "amount", "op": "gt", "value": "10000"},
        ]},
        50, False, "R015", RuleCategory.BENEFICIARY,
    ),
]

HIGH_RISK_COUNTRIES = ["KP", "IR", "SY", "AF", "YE", "SS", "MM"]


class Command(BaseCommand):
    help = "Seed the starter fraud ruleset, thresholds and lists."

    def add_arguments(self, parser):
        parser.add_argument(
            "--activate", action="store_true",
            help="promote the seeded rules from SHADOW to ACTIVE",
        )

    @transaction.atomic
    def handle(self, *args, **options):
        mode = RuleMode.ACTIVE if options["activate"] else RuleMode.SHADOW

        created = updated = 0
        for code, name, condition, weight, hard, reason, category in STARTER_RULES:
            # Validate through the same path the admin API uses; a seed that
            # cannot be evaluated is worse than no seed.
            validate_condition(condition)

            rule, was_created = Rule.objects.update_or_create(
                code=code,
                defaults={
                    "name": name,
                    "condition": condition,
                    "weight": weight,
                    "hard_block": hard,
                    "reason_code": reason,
                    "category": category,
                    "mode": mode,
                    "created_by": "seed",
                    "updated_by": "seed",
                },
            )
            RuleStat.objects.get_or_create(rule=rule)
            created += was_created
            updated += not was_created

        if not Threshold.objects.filter(is_active=True).exists():
            Threshold.objects.create(
                allow_below=40,
                block_at_or_above=75,
                safe_harbour_amount=Decimal("0"),  # ADR-005: never fail open
                version=1,
                is_active=True,
                updated_by="seed",
            )

        from fraud.models import ListEntry

        for country in HIGH_RISK_COUNTRIES:
            ListEntry.objects.get_or_create(
                list_type=ListEntry.ListType.HIGH_RISK_COUNTRY,
                value=country,
                defaults={"reason": "FATF high-risk jurisdiction (sample)", "added_by": "seed"},
            )

        from fraud.services import refresh_caches

        refresh_caches()

        self.stdout.write(self.style.SUCCESS(
            f"{created} rule(s) created, {updated} updated, all in {mode} mode."
        ))
        self.stdout.write(
            "thresholds: ALLOW < 40, REVIEW 40-74, BLOCK >= 75; "
            f"{len(HIGH_RISK_COUNTRIES)} high-risk countries."
        )
        if mode == RuleMode.SHADOW:
            self.stdout.write(self.style.WARNING(
                "Rules are in SHADOW: they are evaluated and recorded but do not "
                "affect decisions. Re-run with --activate to promote them."
            ))

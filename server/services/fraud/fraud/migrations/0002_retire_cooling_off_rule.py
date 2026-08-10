"""Retire any rule that reads the removed `beneficiary_in_cooling_off` fact.

Payees are usable the moment they are added, so the fact is gone from the
feature vector. A rule still referencing it would raise on every screening --
caught and skipped by services.screen(), but logging an exception per transfer
forever and quietly contributing nothing. Disabling it is part of removing the
fact, not an afterthought.

Rules are disabled rather than deleted: a decision recorded last month cites the
rules that fired, and deleting one would make that decision unexplainable.
"""

from __future__ import annotations

import json

from django.db import migrations

REMOVED_FACT = "beneficiary_in_cooling_off"


def retire(apps, schema_editor):
    Rule = apps.get_model("fraud", "Rule")
    stale = [
        rule.pk
        for rule in Rule.objects.exclude(mode="DISABLED")
        if REMOVED_FACT in json.dumps(rule.condition)
    ]
    if stale:
        Rule.objects.filter(pk__in=stale).update(mode="DISABLED", updated_by="migration")


def unretire(apps, schema_editor):
    """Deliberately not reversible in effect.

    Re-enabling a rule that reads a fact this codebase no longer produces would
    reintroduce the exception-per-transfer. Rolling the migration back leaves
    the rules disabled, which is the safe state.
    """


class Migration(migrations.Migration):

    dependencies = [("fraud", "0001_initial")]

    operations = [migrations.RunPython(retire, unretire)]

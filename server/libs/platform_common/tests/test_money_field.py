"""MoneyField: exact monetary storage.

The reason this field exists is measurable -- Django's DecimalField on SQLite
gets REAL (float) affinity and loses precision. These tests pin the behaviour we
switched to, and would fail loudly if anyone reverted to DecimalField.
"""

from __future__ import annotations

from decimal import Decimal

import pytest
from django.core.exceptions import ValidationError
from django.db import connection, models
from django.db.models import Sum

from platform_common.db import MAX_AMOUNT, MoneyField, from_minor, to_minor

pytestmark = pytest.mark.django_db


class MoneyRow(models.Model):
    amount = MoneyField()
    label = models.CharField(max_length=20, default="")

    class Meta:
        app_label = "platform_common"
        managed = False
        db_table = "test_money_row"


@pytest.fixture(scope="session", autouse=True)
def money_table(django_db_setup, django_db_blocker):
    """Create the scratch table once, at session scope.

    It has to happen outside any transaction: SQLite cannot toggle
    PRAGMA foreign_keys mid-transaction, so the schema editor refuses to run
    inside pytest-django's per-test atomic block.
    """
    with django_db_blocker.unblock():
        with connection.schema_editor(atomic=False) as editor:
            editor.create_model(MoneyRow)
    yield
    with django_db_blocker.unblock():
        with connection.schema_editor(atomic=False) as editor:
            editor.delete_model(MoneyRow)


class TestConversion:
    @pytest.mark.parametrize(
        "value,minor",
        [
            ("0", 0),
            ("1", 10_000),
            ("0.0001", 1),
            ("150000.1234", 1_500_001_234),
            ("-42.5000", -425_000),
        ],
    )
    def test_round_trip(self, value, minor):
        assert to_minor(Decimal(value)) == minor
        assert from_minor(minor) == Decimal(value)

    def test_rejects_float(self):
        with pytest.raises(ValidationError, match="float is not accepted"):
            to_minor(1.5)

    def test_rejects_non_finite(self):
        for bad in ("NaN", "Infinity"):
            with pytest.raises(ValidationError, match="finite"):
                to_minor(Decimal(bad))

    def test_rejects_overflow(self):
        with pytest.raises(ValidationError, match="representable range"):
            to_minor(MAX_AMOUNT + Decimal("1"))

    def test_uses_bankers_rounding_beyond_four_places(self):
        # Consistent with platform_common.money.Money.
        assert to_minor(Decimal("1.00005")) == 10_000
        assert to_minor(Decimal("1.00015")) == 10_002

    def test_none_passes_through(self):
        assert to_minor(None) is None
        assert from_minor(None) is None


class TestStorage:
    def test_stored_as_integer_not_real(self):
        """The whole point. If this ever reads 'real', money is on floats."""
        MoneyRow.objects.create(amount=Decimal("150000.1234"))
        with connection.cursor() as cursor:
            cursor.execute("SELECT typeof(amount) FROM test_money_row LIMIT 1")
            storage_class = cursor.fetchone()[0]
        assert storage_class == "integer"

    def test_reads_back_as_decimal(self):
        MoneyRow.objects.create(amount=Decimal("150000.1234"))
        row = MoneyRow.objects.get()
        assert isinstance(row.amount, Decimal)
        assert row.amount == Decimal("150000.1234")

    def test_large_amount_survives_exactly(self):
        """DecimalField on SQLite mangles this one: 99999999999.9999 came back
        as 100000000000.0000."""
        big = Decimal("99999999999.9999")
        MoneyRow.objects.create(amount=big)
        assert MoneyRow.objects.get().amount == big

    def test_sub_minor_precision_survives(self):
        MoneyRow.objects.create(amount=Decimal("0.0001"))
        assert MoneyRow.objects.get().amount == Decimal("0.0001")

    def test_negative_amounts(self):
        MoneyRow.objects.create(amount=Decimal("-2500.7500"))
        assert MoneyRow.objects.get().amount == Decimal("-2500.7500")


class TestAggregationAndQuerying:
    def test_sum_is_exact(self):
        values = ["0.1000", "0.2000", "150000.1234", "99999999999.9999"]
        for value in values:
            MoneyRow.objects.create(amount=Decimal(value))

        total = MoneyRow.objects.aggregate(t=Sum("amount"))["t"]
        assert total == sum(Decimal(v) for v in values)
        assert isinstance(total, Decimal)

    def test_classic_float_trap_sums_exactly(self):
        # 0.1 + 0.2 == 0.3 exactly. On a float column it would not.
        MoneyRow.objects.create(amount=Decimal("0.1000"))
        MoneyRow.objects.create(amount=Decimal("0.2000"))
        assert MoneyRow.objects.aggregate(t=Sum("amount"))["t"] == Decimal("0.3000")

    def test_comparison_filters_use_decimal(self):
        MoneyRow.objects.create(amount=Decimal("100.0000"), label="small")
        MoneyRow.objects.create(amount=Decimal("100.0001"), label="big")

        matched = MoneyRow.objects.filter(amount__gt=Decimal("100.0000"))
        assert [r.label for r in matched] == ["big"]

    def test_ordering_is_numeric(self):
        for value in ["2.0000", "10.0000", "1.0000"]:
            MoneyRow.objects.create(amount=Decimal(value), label=value)
        ordered = [r.amount for r in MoneyRow.objects.order_by("amount")]
        assert ordered == [Decimal("1.0000"), Decimal("2.0000"), Decimal("10.0000")]

    def test_sum_of_many_small_amounts_does_not_drift(self):
        for _ in range(1000):
            MoneyRow.objects.create(amount=Decimal("0.0001"))
        assert MoneyRow.objects.aggregate(t=Sum("amount"))["t"] == Decimal("0.1000")

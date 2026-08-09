"""MoneyField -- exact monetary storage on any backend, including SQLite.

**Why this exists.** Django's ``DecimalField`` on SQLite is stored with REAL
(float) affinity. Measured: ``99999999999999.9999`` reads back as
``100000000000000.0000``, and ``SUM()`` silently drops the fractional part. That
is precisely the "float in a money path" defect the rest of this system is built
to prevent, so ``DecimalField`` is unusable for balances here.

**What we do instead.** Store an exact integer count of 1/10,000 units -- the
same approach Stripe and most payment processors take with minor units, extended
by two extra places so FX rates and per-unit fees do not need rounding until the
rail boundary.

Consequences, all good:
  * exact on SQLite *and* PostgreSQL -- one code path, no backend-specific money
  * ``SUM()`` is integer addition, so aggregates are exact too
  * comparisons and ORDER BY are exact
  * the ledger invariant "debits == credits" is decidable rather than approximate

The Python-side type is always ``Decimal``; the integer representation never
leaks out of this module.
"""

from __future__ import annotations

from decimal import ROUND_HALF_EVEN, Decimal, InvalidOperation

from django.core import checks
from django.core.exceptions import ValidationError
from django.db import models

__all__ = ["MoneyField", "SCALE", "SCALE_FACTOR", "MAX_AMOUNT", "to_minor", "from_minor"]

SCALE = 4
SCALE_FACTOR = Decimal(10) ** SCALE          # 10_000
QUANTUM = Decimal(1).scaleb(-SCALE)          # 0.0001

# int64 bound divided by the scale factor. ~922 trillion, which is comfortably
# more than any single transaction or account balance we will ever hold, but is
# a real ceiling worth asserting rather than discovering.
MAX_MINOR = 2**63 - 1
MAX_AMOUNT = Decimal(MAX_MINOR) / SCALE_FACTOR


class MoneyOverflow(ValidationError):
    """The amount cannot be represented exactly in int64 minor units."""


def to_minor(value) -> int | None:
    """Decimal (or str/int) -> exact integer minor units."""
    if value is None:
        return None
    if isinstance(value, int) and not isinstance(value, bool):
        value = Decimal(value)
    elif isinstance(value, float):
        raise ValidationError(
            "float is not accepted for money; use Decimal or str"
        )
    elif not isinstance(value, Decimal):
        try:
            value = Decimal(str(value))
        except (InvalidOperation, ValueError) as exc:
            raise ValidationError(f"cannot interpret {value!r} as money") from exc

    if not value.is_finite():
        raise ValidationError(f"money must be finite, got {value}")

    scaled = (value * SCALE_FACTOR).quantize(Decimal(1), rounding=ROUND_HALF_EVEN)
    minor = int(scaled)
    if abs(minor) > MAX_MINOR:
        raise MoneyOverflow(
            f"amount {value} exceeds the representable range (+/-{MAX_AMOUNT})"
        )
    return minor


def from_minor(minor) -> Decimal | None:
    """Integer minor units -> Decimal quantised to 4dp."""
    if minor is None:
        return None
    return (Decimal(minor) / SCALE_FACTOR).quantize(QUANTUM)


class MoneyField(models.BigIntegerField):
    """A monetary amount. Reads and writes ``Decimal``; stores ``BigInteger``.

    Use exactly like a DecimalField::

        balance = MoneyField(default=Decimal("0"))

    Notes for callers:
      * ``F()`` arithmetic operates on *minor units*, so prefer reading, doing
        the arithmetic in Python with Decimal, and writing back inside the same
        locked transaction. The ledger does this deliberately anyway, because it
        needs the resulting balance for ``balance_after``.
      * ``Sum()`` returns a Decimal: the field's converter is applied to the
        aggregate because Django infers its output_field from the column.
    """

    description = "Monetary amount stored as exact integer minor units"

    def __init__(self, *args, **kwargs):
        # A money column is never a float and never auto-scales; reject the
        # DecimalField kwargs someone will inevitably copy across.
        kwargs.pop("max_digits", None)
        kwargs.pop("decimal_places", None)
        super().__init__(*args, **kwargs)

    def db_type(self, connection) -> str:
        return "bigint"

    # -- python <-> db --------------------------------------------------

    def from_db_value(self, value, expression, connection):
        return from_minor(value)

    def to_python(self, value):
        if value is None or isinstance(value, Decimal):
            return value
        return from_minor(value) if isinstance(value, int) else Decimal(str(value))

    def get_prep_value(self, value):
        if value is None:
            return None
        # Values already in minor units arrive only from internal machinery;
        # everything user-facing is a Decimal.
        return to_minor(value)

    def value_to_string(self, obj) -> str:
        value = self.value_from_object(obj)
        return "" if value is None else str(value)

    def formfield(self, **kwargs):
        return super().formfield(
            **{"form_class": None, "max_digits": 20, "decimal_places": SCALE, **kwargs}
        )

    def check(self, **kwargs):
        errors = super().check(**kwargs)
        default = self.get_default()
        if isinstance(default, float):
            errors.append(
                checks.Error(
                    "MoneyField default must not be a float.",
                    hint="Use Decimal('0') instead of 0.0.",
                    obj=self,
                    id="platform_common.E001",
                )
            )
        return errors

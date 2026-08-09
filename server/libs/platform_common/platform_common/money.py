"""Money value object.

Rules enforced here so they can't be got wrong elsewhere:
  * amounts are ``Decimal``, never ``float`` -- binary floats cannot represent
    0.1 exactly, and a payment system that rounds silently is a defect;
  * amounts are quantised to 4 decimal places with ROUND_HALF_EVEN (banker's
    rounding), which is what avoids a systematic bias when rounding half-way
    values over millions of transactions;
  * arithmetic between different currencies raises rather than coercing;
  * JSON representation is ``{"amount": "150000.0000", "currency": "INR"}`` with
    the amount as a *string*, so it survives any JSON parser without becoming a
    float.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from decimal import Decimal, InvalidOperation, ROUND_HALF_EVEN

__all__ = ["Money", "CurrencyMismatch", "InvalidMoney", "PRECISION", "quantize"]

PRECISION = Decimal("0.0001")
_CURRENCY_RE = re.compile(r"^[A-Z]{3}$")

# Minor-unit exponent per currency, for display and for rail formatting.
# Money is always *stored* at 4dp; this only governs presentation and the
# rounding applied when handing an amount to an external rail.
MINOR_UNITS = {
    "INR": 2, "USD": 2, "EUR": 2, "GBP": 2, "AED": 2, "SGD": 2,
    "AUD": 2, "CAD": 2, "CHF": 2, "JPY": 0, "KWD": 3, "BHD": 3, "OMR": 3,
}


class CurrencyMismatch(Exception):
    """Raised when arithmetic is attempted across two different currencies."""


class InvalidMoney(Exception):
    """Raised when an amount or currency code is not usable as money."""


def quantize(value: Decimal) -> Decimal:
    """Round to the storage precision using banker's rounding."""
    return value.quantize(PRECISION, rounding=ROUND_HALF_EVEN)


@dataclass(frozen=True, order=False)
class Money:
    amount: Decimal
    currency: str

    def __post_init__(self) -> None:
        currency = self.currency
        if not isinstance(currency, str) or not _CURRENCY_RE.match(currency):
            raise InvalidMoney(f"currency must be a 3-letter ISO code, got {currency!r}")

        amount = self.amount
        # bool is a subclass of int; accepting it would silently turn True into 1.
        if isinstance(amount, bool) or isinstance(amount, float):
            raise InvalidMoney(
                f"amount must be Decimal, int or str -- got {type(amount).__name__}. "
                "float is banned in money paths."
            )
        if not isinstance(amount, Decimal):
            try:
                amount = Decimal(str(amount))
            except (InvalidOperation, ValueError) as exc:
                raise InvalidMoney(f"cannot interpret {amount!r} as money") from exc
        if not amount.is_finite():
            raise InvalidMoney(f"amount must be finite, got {amount}")

        object.__setattr__(self, "amount", quantize(amount))
        object.__setattr__(self, "currency", currency)

    # ---- construction -------------------------------------------------

    @classmethod
    def zero(cls, currency: str) -> "Money":
        return cls(Decimal("0"), currency)

    @classmethod
    def parse(cls, data: dict | None) -> "Money | None":
        """Build from the wire representation. ``None`` passes through."""
        if data is None:
            return None
        if not isinstance(data, dict) or "amount" not in data or "currency" not in data:
            raise InvalidMoney(f"expected {{'amount','currency'}}, got {data!r}")
        return cls(Decimal(str(data["amount"])), str(data["currency"]))

    def to_dict(self) -> dict:
        return {"amount": str(self.amount), "currency": self.currency}

    # ---- arithmetic ---------------------------------------------------

    def _check(self, other: "Money") -> None:
        if not isinstance(other, Money):
            raise TypeError(f"cannot combine Money with {type(other).__name__}")
        if other.currency != self.currency:
            raise CurrencyMismatch(f"{self.currency} vs {other.currency}")

    def __add__(self, other: "Money") -> "Money":
        self._check(other)
        return Money(self.amount + other.amount, self.currency)

    def __sub__(self, other: "Money") -> "Money":
        self._check(other)
        return Money(self.amount - other.amount, self.currency)

    def __neg__(self) -> "Money":
        return Money(-self.amount, self.currency)

    def __abs__(self) -> "Money":
        return Money(abs(self.amount), self.currency)

    def __mul__(self, factor: Decimal | int) -> "Money":
        if isinstance(factor, float):
            raise InvalidMoney("refusing to multiply money by a float; use Decimal")
        return Money(self.amount * Decimal(str(factor)), self.currency)

    __rmul__ = __mul__

    # ---- comparison ---------------------------------------------------

    def __lt__(self, other: "Money") -> bool:
        self._check(other)
        return self.amount < other.amount

    def __le__(self, other: "Money") -> bool:
        self._check(other)
        return self.amount <= other.amount

    def __gt__(self, other: "Money") -> bool:
        self._check(other)
        return self.amount > other.amount

    def __ge__(self, other: "Money") -> bool:
        self._check(other)
        return self.amount >= other.amount

    # ---- predicates ---------------------------------------------------

    @property
    def is_zero(self) -> bool:
        return self.amount == 0

    @property
    def is_positive(self) -> bool:
        return self.amount > 0

    @property
    def is_negative(self) -> bool:
        return self.amount < 0

    # ---- presentation -------------------------------------------------

    def for_rail(self) -> Decimal:
        """Amount rounded to the currency's minor unit, for handing to a rail.

        A rail will not accept 4dp on an INR instruction. Rounding happens here,
        explicitly and once, rather than accidentally at a serialisation boundary.
        """
        exponent = MINOR_UNITS.get(self.currency, 2)
        return self.amount.quantize(Decimal(1).scaleb(-exponent), rounding=ROUND_HALF_EVEN)

    def __str__(self) -> str:
        return f"{self.for_rail()} {self.currency}"

    def __repr__(self) -> str:
        return f"Money('{self.amount}', '{self.currency}')"

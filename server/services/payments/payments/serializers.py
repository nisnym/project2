"""Request validation and response representation.

ALL input validation lives here, never in views.
"""

from __future__ import annotations

from decimal import Decimal

from rest_framework import serializers

from .models import Rail, TransferSchedule

# Which rails belong to which direction of money. Posting a transfer over
# BANK_DEBIT (a funding rail) would build a transaction the saga cannot route,
# so it is rejected at the edge rather than half-way through.
TRANSFER_RAILS = (Rail.INTERNAL, Rail.DOMESTIC, Rail.INTERNATIONAL)
FUNDING_RAILS = (Rail.BANK_DEBIT, Rail.CARD, Rail.WALLET)

MAX_AMOUNT = Decimal("100000000")


class MoneyAmountField(serializers.DecimalField):
    """Amounts arrive as strings and stay exact.

    ``DecimalField`` with a string input is the whole point: JSON numbers are
    IEEE-754 doubles, and 0.1 + 0.2 is not 0.3 in anyone's currency.
    """

    def __init__(self, **kwargs):
        kwargs.setdefault("max_digits", 18)
        kwargs.setdefault("decimal_places", 4)
        kwargs.setdefault("min_value", Decimal("0.0001"))
        kwargs.setdefault("max_value", MAX_AMOUNT)
        super().__init__(**kwargs)


class CreateTransferSerializer(serializers.Serializer):
    account_id = serializers.UUIDField()
    beneficiary_id = serializers.UUIDField()
    amount = MoneyAmountField()
    currency = serializers.CharField(max_length=3, default="INR")
    rail = serializers.ChoiceField(choices=[(r, r.label) for r in TRANSFER_RAILS])
    remarks = serializers.CharField(max_length=140, required=False, allow_blank=True,
                                    default="")
    purpose_code = serializers.CharField(max_length=40, required=False, allow_blank=True,
                                         default="")

    def validate_currency(self, value: str) -> str:
        return value.upper()


class CreateFundingSerializer(serializers.Serializer):
    account_id = serializers.UUIDField()
    funding_source_id = serializers.UUIDField()
    amount = MoneyAmountField()
    currency = serializers.CharField(max_length=3, default="INR")
    rail = serializers.ChoiceField(choices=[(r, r.label) for r in FUNDING_RAILS])

    def validate_currency(self, value: str) -> str:
        return value.upper()


class CreateScheduleSerializer(serializers.Serializer):
    account_id = serializers.UUIDField()
    beneficiary_id = serializers.UUIDField()
    amount = MoneyAmountField()
    currency = serializers.CharField(max_length=3, default="INR")
    rail = serializers.ChoiceField(choices=[(r, r.label) for r in TRANSFER_RAILS])
    frequency = serializers.ChoiceField(choices=TransferSchedule.Frequency.choices)
    start_at = serializers.DateTimeField()
    end_date = serializers.DateField(required=False, allow_null=True, default=None)
    max_runs = serializers.IntegerField(required=False, allow_null=True, default=None,
                                        min_value=1, max_value=1200)
    remarks = serializers.CharField(max_length=140, required=False, allow_blank=True,
                                    default="")

    def validate_start_at(self, value):
        from django.utils import timezone

        # A start date in the past would fire immediately and repeatedly as the
        # sweeper caught up -- surprising, and with real money.
        if value <= timezone.now():
            raise serializers.ValidationError("The first run must be in the future.")
        return value

    def validate(self, attrs: dict) -> dict:
        if attrs.get("end_date") is None and attrs.get("max_runs") is None:
            raise serializers.ValidationError(
                "Give the schedule an end date or a maximum number of runs; a "
                "recurring transfer with no stopping condition runs forever."
            )
        if attrs.get("end_date") and attrs["end_date"] < attrs["start_at"].date():
            raise serializers.ValidationError(
                {"end_date": "The end date is before the first run."}
            )
        return attrs


class AddFundingSourceSerializer(serializers.Serializer):
    from .models import FundingSource

    source_type = serializers.ChoiceField(choices=FundingSource.SourceType.choices)
    display_name = serializers.CharField(max_length=60)
    token = serializers.CharField(max_length=200)
    currency = serializers.CharField(max_length=3, required=False, default="INR")

    def validate_token(self, value: str) -> str:
        """The token is a PSP/vault reference, never a card number.

        A 13-19 digit all-numeric string is almost certainly a real PAN that a
        careless client pasted in. Storing it would drag this service into PCI
        scope, so refuse it outright.
        """
        digits = value.replace(" ", "").replace("-", "")
        if digits.isdigit() and 13 <= len(digits) <= 19:
            raise serializers.ValidationError(
                "This looks like a card number. Send the vault token instead -- "
                "raw PANs are never accepted here."
            )
        return value

    def validate_currency(self, value: str) -> str:
        return value.upper()

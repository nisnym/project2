"""Request validation and response representation.

ALL input validation lives here, never in views.
"""

from __future__ import annotations

import re

from rest_framework import serializers

from .models import Beneficiary

IFSC = re.compile(r"^[A-Z]{4}0[A-Z0-9]{6}$")
SWIFT = re.compile(r"^[A-Z]{6}[A-Z0-9]{2}([A-Z0-9]{3})?$")
ACCOUNT_NUMBER = re.compile(r"^[A-Z0-9]{6,34}$")


class AddBeneficiarySerializer(serializers.Serializer):
    nickname = serializers.CharField(max_length=60)
    beneficiary_type = serializers.ChoiceField(choices=Beneficiary.Type.choices)
    account_number = serializers.CharField(max_length=40)
    bank_code = serializers.CharField(max_length=20, required=False, allow_blank=True,
                                      default="")
    swift_bic = serializers.CharField(max_length=11, required=False, allow_blank=True,
                                      default="")
    country = serializers.CharField(max_length=2, required=False, default="IN")
    currency = serializers.CharField(max_length=3, required=False, default="INR")

    def validate_account_number(self, value: str) -> str:
        cleaned = value.replace(" ", "").upper()
        if not ACCOUNT_NUMBER.match(cleaned):
            raise serializers.ValidationError(
                "Account number must be 6-34 letters or digits."
            )
        return cleaned

    def validate_country(self, value: str) -> str:
        return value.upper()

    def validate_currency(self, value: str) -> str:
        return value.upper()

    def validate(self, attrs: dict) -> dict:
        """Rail-specific routing requirements.

        A domestic transfer without an IFSC, or an international one without a
        SWIFT/BIC, is unroutable. Catching it here turns a payment that would
        fail hours later at the rail into a form error the customer can fix.
        """
        kind = attrs["beneficiary_type"]
        bank_code = (attrs.get("bank_code") or "").upper()
        swift = (attrs.get("swift_bic") or "").upper()

        if kind == Beneficiary.Type.DOMESTIC:
            if not IFSC.match(bank_code):
                raise serializers.ValidationError(
                    {"bank_code": "A domestic payee needs a valid IFSC, e.g. HDFC0001234."}
                )
        elif kind == Beneficiary.Type.INTERNATIONAL:
            if not SWIFT.match(swift):
                raise serializers.ValidationError(
                    {"swift_bic": "An international payee needs a valid SWIFT/BIC."}
                )
            if attrs.get("country", "IN") == "IN":
                raise serializers.ValidationError(
                    {"country": "An international payee cannot be in India."}
                )

        attrs["bank_code"] = bank_code
        attrs["swift_bic"] = swift
        return attrs

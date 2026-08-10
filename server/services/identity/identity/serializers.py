"""Request validation and response representation.

ALL input validation lives here, never in views.
"""

from __future__ import annotations

from rest_framework import serializers

from .models import STAFF_ROLES, AdminChangeRequest, Role

MIN_PASSWORD_LENGTH = 8

# Actions a caller may name on /api/admin/users/<id>/actions. LOCK and
# REVOKE_SESSIONS apply immediately; the rest are staged for a second admin.
USER_ACTIONS = sorted(
    {"LOCK", "REVOKE_SESSIONS"}
    | {
        AdminChangeRequest.Action.ROLE_CHANGE,
        AdminChangeRequest.Action.UNLOCK,
        AdminChangeRequest.Action.ACTIVATE,
        AdminChangeRequest.Action.CLOSE,
        AdminChangeRequest.Action.PASSWORD_RESET,
    }
)


class CreateStaffSerializer(serializers.Serializer):
    """A staff account. No password field on purpose.

    The requesting admin never chooses the credential: one is generated at
    approval time and shown once to the *approver*. An admin who could set the
    password of an account they also requested would have defeated the
    four-eyes control without ever needing it approved on their own terms.
    """

    email = serializers.EmailField(max_length=254)
    full_name = serializers.CharField(max_length=120, required=False, allow_blank=True,
                                      default="")
    phone = serializers.CharField(max_length=20, required=False, allow_blank=True,
                                  default="")
    role = serializers.ChoiceField(choices=sorted(STAFF_ROLES))
    reason = serializers.CharField(max_length=200, required=False, allow_blank=True,
                                   default="")

    def validate_email(self, value: str) -> str:
        return value.strip().lower()


class RoleChangeSerializer(serializers.Serializer):
    role = serializers.ChoiceField(choices=Role.choices)


class UserActionSerializer(serializers.Serializer):
    action = serializers.ChoiceField(choices=[(a, a) for a in USER_ACTIONS])
    reason = serializers.CharField(max_length=200, required=False, allow_blank=True,
                                   default="")


class ProfileUpdateSerializer(serializers.Serializer):
    full_name = serializers.CharField(max_length=120, required=False, allow_blank=True)
    phone = serializers.CharField(max_length=20, required=False, allow_blank=True)

    def validate(self, attrs: dict) -> dict:
        if not attrs:
            raise serializers.ValidationError(
                "Supply at least one of full_name or phone."
            )
        return attrs


class ChangePasswordSerializer(serializers.Serializer):
    current_password = serializers.CharField(max_length=200)
    new_password = serializers.CharField(max_length=200)

    def validate_new_password(self, value: str) -> str:
        if len(value) < MIN_PASSWORD_LENGTH:
            raise serializers.ValidationError(
                f"Password must be at least {MIN_PASSWORD_LENGTH} characters."
            )
        return value

    def validate(self, attrs: dict) -> dict:
        if attrs["current_password"] == attrs["new_password"]:
            raise serializers.ValidationError(
                {"new_password": "The new password must differ from the current one."}
            )
        return attrs

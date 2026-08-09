"""The authenticated caller.

Deliberately not a Django ``User`` model: nine of the ten services have no user
table and must not grow one. A service knows *who* is calling from the token
alone -- identity-svc owns the actual records.
"""

from __future__ import annotations

from dataclasses import dataclass, field

__all__ = ["Principal", "ServicePrincipal", "Role"]


class Role:
    CUSTOMER = "CUSTOMER"
    FRAUD_ANALYST = "FRAUD_ANALYST"
    OPS = "OPS"
    ADMIN = "ADMIN"

    ALL = (CUSTOMER, FRAUD_ANALYST, OPS, ADMIN)
    STAFF = (FRAUD_ANALYST, OPS, ADMIN)


@dataclass
class Principal:
    """A human caller, established from a user access token."""

    id: str
    role: str
    email: str = ""
    device_id: str | None = None
    token_id: str | None = None
    claims: dict = field(default_factory=dict)

    # -- DRF/Django duck-typing ----------------------------------------
    is_authenticated = True
    is_anonymous = False
    is_service = False

    def __str__(self) -> str:
        return f"{self.role}:{self.id}"

    def has_role(self, *roles: str) -> bool:
        return self.role in roles

    @property
    def is_staff_role(self) -> bool:
        return self.role in Role.STAFF

    def owns(self, user_id) -> bool:
        return str(user_id) == str(self.id)


@dataclass
class ServicePrincipal:
    """Another service calling an ``/internal/*`` endpoint."""

    service: str
    scopes: tuple[str, ...] = ()
    claims: dict = field(default_factory=dict)

    is_authenticated = True
    is_anonymous = False
    is_service = True
    id = None
    role = "SERVICE"

    def __str__(self) -> str:
        return f"service:{self.service}"

    def has_scope(self, scope: str) -> bool:
        return scope in self.scopes

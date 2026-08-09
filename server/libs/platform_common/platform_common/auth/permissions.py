"""Permission classes.

The rule this module exists to enforce: **role and ownership are separate
checks, and customer-scoped endpoints need both.** A valid CUSTOMER token says
what kind of thing you may do; it says nothing about whose account you may do it
to. Conflating them is how horizontal-privilege bugs happen.
"""

from __future__ import annotations

from rest_framework import permissions

from .principal import Role

__all__ = [
    "IsAuthenticatedPrincipal",
    "HasRole",
    "IsCustomer",
    "IsFraudAnalyst",
    "IsOps",
    "IsAdmin",
    "IsStaff",
    "IsOwnerOrStaffReadOnly",
    "IsService",
    "HasScope",
]


class IsAuthenticatedPrincipal(permissions.BasePermission):
    message = "Authentication credentials were not provided."

    def has_permission(self, request, view) -> bool:
        return getattr(request.user, "is_authenticated", False)


class HasRole(permissions.BasePermission):
    """Subclass and set ``roles``, or use ``HasRole.of("ADMIN", "OPS")``."""

    roles: tuple[str, ...] = ()
    message = "Your role does not permit this action."

    @classmethod
    def of(cls, *roles: str):
        return type(f"HasRole_{'_'.join(roles)}", (cls,), {"roles": roles})

    def has_permission(self, request, view) -> bool:
        user = request.user
        if not getattr(user, "is_authenticated", False):
            return False
        if getattr(user, "is_service", False):
            return False
        return user.role in self.roles


IsCustomer = HasRole.of(Role.CUSTOMER)
IsFraudAnalyst = HasRole.of(Role.FRAUD_ANALYST, Role.ADMIN)
IsOps = HasRole.of(Role.OPS, Role.ADMIN)
IsAdmin = HasRole.of(Role.ADMIN)
IsStaff = HasRole.of(*Role.STAFF)


class IsOwnerOrStaffReadOnly(permissions.BasePermission):
    """Object-level: the owning customer has full access; staff may read.

    The object must expose ``user_id`` (or ``owner_user_id``). Staff writes to
    customer data go through explicit, audited admin endpoints -- never through
    a customer endpoint with an elevated token.
    """

    message = "You do not have access to this resource."
    owner_fields = ("user_id", "owner_user_id")

    def has_object_permission(self, request, view, obj) -> bool:
        user = request.user
        if not getattr(user, "is_authenticated", False):
            return False
        if getattr(user, "is_service", False):
            return True

        if user.role in Role.STAFF:
            return request.method in permissions.SAFE_METHODS

        for attr in self.owner_fields:
            if hasattr(obj, attr):
                return str(getattr(obj, attr)) == str(user.id)
        return False


class IsService(permissions.BasePermission):
    """``/internal/*`` endpoints. Never satisfied by a user token."""

    message = "This endpoint is only callable by another service."

    def has_permission(self, request, view) -> bool:
        return getattr(request.user, "is_service", False)


class HasScope(permissions.BasePermission):
    """Scope check for service callers: ``HasScope.of("ledger:write")``."""

    scopes: tuple[str, ...] = ()
    message = "The calling service lacks the required scope."

    @classmethod
    def of(cls, *scopes: str):
        return type(f"HasScope_{'_'.join(s.replace(':', '_') for s in scopes)}",
                    (cls,), {"scopes": scopes})

    def has_permission(self, request, view) -> bool:
        user = request.user
        if not getattr(user, "is_service", False):
            return False
        return any(user.has_scope(scope) for scope in self.scopes)
